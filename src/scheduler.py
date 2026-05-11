"""盤中排程器 — 管理所有資料流與策略的生命週期。

時間軸：
  09:00        開市，啟動 Fugle WebSocket（前5檔 tick）+ Fugle Quote 輪詢（全30檔）
  09:00–09:14  更新 ORB 區間
  09:15        ORB 鎖定，策略開始發訊號
  09:00–13:20  每根 1分K（REST 輪詢）更新後呼叫三個策略
  13:20        收盤，停止發訊號
  13:25        關閉所有連線，推播今日統計摘要

排行榜（09:30）請另開終端機執行：
  python3 scripts/morning_ranking.py --now
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import yaml
from loguru import logger

from config.settings import settings
from src.data.cache import IntraDayCache, cache as global_cache
from src.data.fugle_client import Bar, FugleWebSocketClient, Tick
from src.data.fugle_quote_client import FugleQuote, FugleQuoteClient
from src.data.twse_client import OrderBook
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
        self._quote = FugleQuoteClient(api_key=settings.fugle_api_key, poll_interval=3.0)
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

        # Fugle WebSocket 免費方案訂閱上限約 5 個，只取前 5 核心標的（tick 資料）
        fugle_symbols = self.symbols[:5]
        self._fugle.add_symbols(fugle_symbols)
        logger.info(f"Fugle WebSocket 訂閱（tick）：{fugle_symbols}")

        # Fugle Quote 輪詢全部 30 檔（五檔委買委賣 + OBI，無訂閱上限）
        self._quote.add_symbols(self.symbols)

        # 掛上 callback
        self._fugle.on_tick(self._on_tick)
        self._fugle.on_bar(self._on_bar)
        self._quote.on_quote(self._on_quote)

        # 並行跑 Fugle WS + Fugle Quote 輪詢 + 收盤監控 + 每分鐘拉K
        try:
            await asyncio.gather(
                self._fugle.run(),
                self._quote.run(),
                self._closing_monitor(),
                self._bar_polling_loop(),
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

    async def _on_quote(self, quote: FugleQuote) -> None:
        """Fugle Quote 更新：轉成 OrderBook 格式注入 cache，供 OBI 策略使用。"""
        book = OrderBook(
            symbol=quote.symbol,
            timestamp=quote.timestamp,
            bid_prices=quote.bid_prices,
            bid_volumes=quote.bid_sizes,
            ask_prices=quote.ask_prices,
            ask_volumes=quote.ask_sizes,
            last_price=quote.last_price,
            total_volume_lots=quote.trade_volume,
        )
        self.cache.update_orderbook(book)
        self._obi.on_orderbook(book)

    # --- 收盤監控 ---

    async def _bar_polling_loop(self) -> None:
        """每 60 秒用 Fugle REST API 拉一次所有標的的最新 1分K，注入 cache。
        補足 WebSocket candles 訂閱被上限擋掉的缺口。
        """
        from fugle_marketdata import RestClient as FugleRestClient
        rest = FugleRestClient(api_key=settings.fugle_api_key)

        while True:
            await asyncio.sleep(60)
            now = datetime.now()
            if (now.hour, now.minute) >= (_SIGNAL_CUTOFF_HOUR, _SIGNAL_CUTOFF_MINUTE):
                break
            for symbol in self.symbols:
                try:
                    data = rest.stock.intraday.candles(
                        symbol=symbol, timeframe="1"
                    )
                    candles = data.get("data", [])
                    if not candles:
                        continue
                    latest = candles[-1]
                    ts_str = latest.get("date", "")
                    ts = datetime.fromisoformat(ts_str) if ts_str else now
                    from src.data.fugle_client import Bar
                    bar = Bar(
                        symbol=symbol,
                        open=float(latest.get("open", 0)),
                        high=float(latest.get("high", 0)),
                        low=float(latest.get("low", 0)),
                        close=float(latest.get("close", 0)),
                        volume=int(latest.get("volume", 0)),
                        timestamp=ts,
                    )
                    self.cache.update_bar(bar)
                except Exception as e:
                    logger.debug(f"[{symbol}] REST candle 查詢失敗：{e}")
            logger.debug("1分K REST 輪詢完成")

    async def _morning_ranking_task(self) -> None:
        """等到 09:30 推播全市場早盤排行榜（啟動後至少等 30 秒讓 TWSE 穩定）。"""
        now = datetime.now()
        target = now.replace(hour=9, minute=30, second=0, microsecond=0)
        wait_sec = max((target - now).total_seconds(), 30)
        if wait_sec > 0:
            logger.info(f"早盤排行榜將於 {wait_sec:.0f} 秒後推播")
            await asyncio.sleep(wait_sec)

        try:
            from scripts.morning_ranking import run as ranking_run
            await ranking_run(top_n=20)
        except Exception as e:
            logger.exception(f"早盤排行榜失敗：{e}")

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
        await self._quote.stop()

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
