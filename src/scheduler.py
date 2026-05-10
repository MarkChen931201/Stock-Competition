"""盤中排程器 — 管理所有資料流與策略的生命週期。

時間軸：
  09:00        開市，啟動 Fugle WebSocket + TWSE 輪詢
  09:00–09:14  更新 ORB 區間（FugleClient 的 bar callback 會自動更新 cache）
  09:15        ORB 鎖定，ORBBreakoutStrategy 開始發訊號
  09:00–13:20  每根 1分K 結束後呼叫三個策略
  13:20        收盤，停止接收新訊號
  13:25        關閉所有連線，推播今日統計摘要
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import yaml
from loguru import logger

from config.settings import settings
from src.data.cache import IntraDayCache, cache as global_cache
from src.data.fugle_client import Bar, FugleWebSocketClient, Tick
from src.data.twse_client import OrderBook, TWSEClient
from src.notifier.discord_bot import DiscordNotifier
from src.signals.dispatcher import SignalDispatcher
from src.strategies.base import Signal
from src.strategies.orderbook_imbalance import OBIBurstStrategy
from src.strategies.orb_breakout import ORBBreakoutStrategy
from src.strategies.vwap_reversion import VWAPReversionStrategy

# 盤中訊號接收截止時間（分鐘），13:20 後不再發訊號
_SIGNAL_CUTOFF_HOUR = 13
_SIGNAL_CUTOFF_MINUTE = 20


def _load_universe() -> tuple[list[str], dict[str, str]]:
    """從 universe.yaml 讀取股票池，回傳 (代號列表, {代號: 名稱} 對照表)。

    名稱對照表先用代號本身填充（盤前 screener 執行後可更新為真實名稱）。
    """
    universe_path = settings.PROJECT_ROOT / "config" / "universe.yaml" if hasattr(settings, "PROJECT_ROOT") else None

    # 嘗試讀取設定檔
    try:
        from config.settings import PROJECT_ROOT
        path = PROJECT_ROOT / "config" / "universe.yaml"
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        seeds = data.get("seed_universe", [])
        etfs = data.get("etf_universe", [])
        all_symbols = seeds + etfs
    except Exception as e:
        logger.warning(f"無法讀取 universe.yaml：{e}，使用空清單")
        all_symbols = []

    # 名稱對照（預設用代號，盤前篩選後可注入真實名稱）
    name_map = {s: s for s in all_symbols}
    return all_symbols, name_map


class IntraDayScheduler:
    """盤中主排程器，管理 Fugle WS、TWSE 輪詢、策略執行、推播。

    Args:
        cache:      共享記憶體快取（預設使用全域 singleton）
        symbols:    要監控的股票代號清單（None 時從 universe.yaml 讀取）
        name_map:   {代號: 名稱} 對照表
    """

    def __init__(
        self,
        cache: IntraDayCache | None = None,
        symbols: list[str] | None = None,
        name_map: dict[str, str] | None = None,
    ):
        self.cache = cache or global_cache

        if symbols is None:
            symbols, name_map = _load_universe()
        self.symbols = symbols
        self.name_map = name_map or {s: s for s in symbols}

        # --- 初始化各元件 ---
        self._fugle = FugleWebSocketClient(api_key=settings.fugle_api_key)
        self._twse = TWSEClient(poll_interval=3.0)
        self._notifier = DiscordNotifier(webhook_url=settings.discord_webhook_url)
        self._dispatcher = SignalDispatcher(
            notifier=self._notifier,
            min_profit_pct=0.008,
            cooldown_minutes=5,
        )

        # --- 三個策略（共享同一個 cache）---
        self._orb = ORBBreakoutStrategy(self.cache)
        self._vwap = VWAPReversionStrategy(self.cache)
        self._obi = OBIBurstStrategy(self.cache)

        # 防止收盤後繼續發訊號
        self._signal_stopped = False

    # --- 啟動入口 ---

    async def run(self) -> None:
        """啟動整個盤中系統，直到 13:25 後自動結束。"""
        logger.info(f"IntraDayScheduler 啟動，監控 {len(self.symbols)} 檔標的")
        self.cache.reset()
        self._orb.reset()
        self._vwap.reset()
        self._obi.reset()
        self._dispatcher.reset()
        self._signal_stopped = False

        # 訂閱所有標的
        self._fugle.add_symbols(self.symbols)
        self._twse.add_symbols(self.symbols)

        # 掛上 callback
        self._fugle.on_tick(self._on_tick)
        self._fugle.on_bar(self._on_bar)
        self._twse.on_orderbook(self._on_orderbook)

        # 並行跑 Fugle WS + TWSE 輪詢 + 收盤監控
        try:
            await asyncio.gather(
                self._fugle.run(),
                self._twse.run(),
                self._closing_monitor(),
            )
        except asyncio.CancelledError:
            logger.info("Scheduler 收到取消訊號，開始關閉…")
        finally:
            await self._shutdown()

    # --- Callback 函式 ---

    async def _on_tick(self, tick: Tick) -> None:
        self.cache.update_tick(tick)

    async def _on_bar(self, bar: Bar) -> None:
        self.cache.update_bar(bar)

        if self._signal_stopped:
            return

        # 13:20 後停止發訊號
        t = bar.timestamp
        if (t.hour, t.minute) >= (_SIGNAL_CUTOFF_HOUR, _SIGNAL_CUTOFF_MINUTE):
            if not self._signal_stopped:
                self._signal_stopped = True
                logger.info("13:20 已到，停止發送新訊號")
            return

        symbol = bar.symbol
        name = self.name_map.get(symbol, symbol)

        # 依序呼叫三個策略
        for strategy in (self._orb, self._vwap, self._obi):
            try:
                signal = strategy.generate_signal(symbol, name)
                if signal:
                    self._dispatcher.submit(signal)
            except Exception as e:
                logger.exception(f"[{symbol}] {strategy.name} 發生錯誤：{e}")

    async def _on_orderbook(self, book: OrderBook) -> None:
        self.cache.update_orderbook(book)
        # OBI 策略需要每次 orderbook 更新時記錄 OBI 值
        self._obi.on_orderbook(book)

    # --- 收盤監控 ---

    async def _closing_monitor(self) -> None:
        """每分鐘檢查是否到 13:25，到了就關閉所有連線。"""
        while True:
            await asyncio.sleep(60)
            now = datetime.now()
            if (now.hour, now.minute) >= (13, 25):
                logger.info("13:25 收盤，開始關閉系統…")
                await self._shutdown()
                break

    async def _shutdown(self) -> None:
        """優雅關閉所有連線，並推播今日統計摘要。"""
        await self._fugle.stop()
        await self._twse.stop()

        stats = self._dispatcher.stats
        logger.info(
            f"今日統計 — 推播:{stats['sent']} 筆 | "
            f"成本過濾:{stats['rejected_cost']} 筆 | "
            f"去重過濾:{stats['rejected_dedup']} 筆"
        )

        # 推播收盤摘要到 Discord
        try:
            from discord_webhook import DiscordEmbed
            embed = DiscordEmbed(title="📊 今日收盤摘要", color="5865f2")
            embed.set_timestamp()
            embed.add_embed_field(
                name="訊號統計",
                value=(
                    f"✅ 推播：**{stats['sent']}** 筆\n"
                    f"❌ 成本過濾：**{stats['rejected_cost']}** 筆\n"
                    f"🔄 去重過濾：**{stats['rejected_dedup']}** 筆"
                ),
                inline=False,
            )
            embed.set_footer(text="Stock-Competition · 競賽模擬系統")
            self._notifier._send(embed)
        except Exception as e:
            logger.warning(f"收盤摘要推播失敗：{e}")
