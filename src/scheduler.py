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
from src.data.broad_scanner import BroadScanner, load_all_twse_symbols
from src.data.institutional import InstitutionalLoader
from src.risk.trailing_stop import TrailingStopManager
from src.signals.scorer import SignalScorer
from src.notifier.discord_bot import DiscordNotifier
from src.signals.dispatcher import SignalDispatcher
from src.strategies.base import Signal
from src.strategies.orderbook_imbalance import OBIBurstStrategy
from src.strategies.orb_breakout import ORBBreakoutStrategy
from src.strategies.vwap_reversion import VWAPReversionStrategy
from src.strategies.morning_momentum import MorningMomentumStrategy
from src.strategies.trend_pullback import TrendPullbackStrategy

# 盤中訊號接收截止時間（分鐘），13:20 後不再發訊號
_SIGNAL_CUTOFF_HOUR = 13
_SIGNAL_CUTOFF_MINUTE = 20


import re

# 解析 universe.yaml 中註解格式的中文名：- "3481"   # 群創
_NAME_PATTERN = re.compile(r'^\s*-\s*"(\d{4,5})"\s*#\s*(\S+)')


def _parse_universe_with_names(path) -> tuple[list[str], dict[str, str]]:
    """逐行讀取 universe.yaml，從註解抓中文名。

    格式範例：- "3481"   # 群創
    → 代號 3481、名稱「群創」
    """
    symbols: list[str] = []
    name_map: dict[str, str] = {}

    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _NAME_PATTERN.match(line)
            if m:
                sid, name = m.group(1), m.group(2)
                if sid not in name_map:  # 避免重複
                    symbols.append(sid)
                    name_map[sid] = name
    return symbols, name_map


def _load_universe() -> tuple[list[str], dict[str, str]]:
    """從 universe.yaml 讀取股票池，回傳 (代號列表, {代號: 中文名} 對照表)。

    優先策略：
      1. 先用 yaml 註解解析中文名（136 檔監控股一定有）
      2. 廣域掃描新增的股票會在 IntraDayScheduler 啟動時透過 FinMind 補全
    """
    try:
        from config.settings import PROJECT_ROOT
        path = PROJECT_ROOT / "config" / "universe.yaml"

        # 從註解解析中文名（最完整）
        symbols, name_map = _parse_universe_with_names(path)

        # 安全網：若註解解析失敗，退回標準 yaml 讀法（只有代號）
        if not symbols:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            seeds = data.get("seed_universe", [])
            etfs = data.get("etf_universe", [])
            symbols = seeds + etfs
            name_map = {s: s for s in symbols}

        logger.info(f"載入監控池：{len(symbols)} 檔（中文名 {sum(1 for s in symbols if name_map.get(s) != s)} 檔）")
        return symbols, name_map
    except Exception as e:
        logger.warning(f"無法讀取 universe.yaml：{e}，使用空清單")
        return [], {}


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
        self._quote = FugleQuoteClient(api_key=settings.fugle_api_key, poll_interval=30.0)
        self._notifier = DiscordNotifier(webhook_url=settings.discord_webhook_url)
        self._trailing = TrailingStopManager(notifier=self._notifier)
        # 籌碼面資料（盤前載入昨日）
        self._inst = InstitutionalLoader(token=settings.finmind_token)
        # 評分系統 v3：8 維度滿分 16，門檻 5.5（約 34%）— 放寬版
        # 新增第 8 維「趨勢分數」，需要 cache 才能計算
        self._scorer = SignalScorer(inst_loader=self._inst, min_score=5.5, cache=self.cache)
        self._dispatcher = SignalDispatcher(
            notifier=self._notifier,
            min_profit_pct=0.006,         # 放寬：0.8% → 0.6%
            cooldown_minutes=3,           # 放寬：5 → 3 分鐘
            trailing_stop_manager=self._trailing,
            signal_scorer=self._scorer,
            cache=self.cache,
        )
        # 快速 OBI 輪詢（前 20 核心股，每 8 秒）— 平衡型擴大
        # 20 檔 × 1s/檔 + 8s 等待 = 28s/輪 → ~43 req/min（仍安全）
        self._fast_quote = FugleQuoteClient(
            api_key=settings.fugle_api_key, poll_interval=8.0
        )

        # --- 五個策略（共享同一個 cache）---
        self._orb      = ORBBreakoutStrategy(self.cache)
        self._vwap     = VWAPReversionStrategy(self.cache)
        self._obi      = OBIBurstStrategy(self.cache)
        self._mom      = MorningMomentumStrategy(self.cache)
        self._pullback = TrendPullbackStrategy(self.cache)   # v3 新增：趨勢回踩

        # 防止收盤後繼續發訊號
        self._signal_stopped = False

        # 廣域掃描：熱門股共享集合（TWSE 發現異動後加入）
        self._hot_symbols: set[str] = set()
        _all_scan_syms = load_all_twse_symbols(
            str(__import__("pathlib").Path(__file__).parent.parent / "config" / "universe.yaml")
        )
        self._broad_scanner = BroadScanner(
            all_symbols=_all_scan_syms,
            hot_symbols=self._hot_symbols,
        )

        # 從 FinMind 補全全市場中文名（廣域掃描新增的熱門股也能有名字）
        self._enrich_names_from_finmind()

    def _enrich_names_from_finmind(self) -> None:
        """從 FinMind TaiwanStockInfo 載入全市場中文名，補進 name_map。

        失敗（API 限額、網路錯誤）不影響運行，保留 yaml 註解解析的結果。
        """
        try:
            from src.data.finmind_client import FinMindClient
            client = FinMindClient(token=settings.finmind_token)
            df = client.stock_info()
            if df.empty:
                logger.warning("FinMind stock_info 回傳空，跳過中文名補全")
                return

            added = 0
            for _, row in df.iterrows():
                sid = str(row["stock_id"])
                nm = str(row["stock_name"]).strip()
                # 只補：原本沒有 / 原本是代號 = 代號（fallback）
                if nm and (sid not in self.name_map or self.name_map[sid] == sid):
                    self.name_map[sid] = nm
                    added += 1
            logger.info(f"FinMind 補全中文名 {added} 檔（總 {len(self.name_map)} 檔）")
        except Exception as e:
            logger.warning(f"FinMind 中文名補全失敗（不影響系統）：{e}")

    # --- 啟動入口 ---

    async def run(self) -> None:
        """啟動整個盤中系統，直到 13:25 後自動結束。"""
        logger.info(f"IntraDayScheduler 啟動，監控 {len(self.symbols)} 檔標的")
        # 盤前載入昨日籌碼資料
        from datetime import timedelta
        self._inst.load(datetime.now().date() - timedelta(days=1))
        self.cache.reset()
        self._orb.reset()
        self._vwap.reset()
        self._obi.reset()
        self._mom.reset()
        self._pullback.reset()                                # v3 新增
        self._dispatcher.reset()
        self._signal_stopped = False

        # Fugle WebSocket 免費方案訂閱上限約 5 個，只取前 5 核心標的（tick 資料）
        fugle_symbols = self.symbols[:5]
        self._fugle.add_symbols(fugle_symbols)
        logger.info(f"Fugle WebSocket 訂閱（tick）：{fugle_symbols}")

        # Fugle Quote 輪詢：一般頻率（前 20 以外的股票，30s）
        self._quote.add_symbols(self.symbols[20:])

        # 快速 OBI 輪詢：前 20 核心股，8s 輪詢（從 10 檔擴大到 20 檔）
        # 20 檔 × 1s/檔 + 8s 等待 = 28s/輪 ≈ 43 req/min → 安全
        self._fast_quote.add_symbols(self.symbols[:20])
        self._fast_quote.on_quote(self._on_quote)  # 共用同一個 callback

        # 掛上 callback
        self._fugle.on_tick(self._on_tick)
        self._fugle.on_bar(self._on_bar)
        self._quote.on_quote(self._on_quote)

        # 並行跑：一般Quote + 快速OBI + K棒輪詢 + 廣域掃描 + 收盤監控 + 排行榜 + 熱門股同步 + 波段策略
        try:
            await asyncio.gather(
                self._quote.run(),
                self._fast_quote.run(),
                self._closing_monitor(),
                self._bar_polling_loop(),
                self._broad_scanner.run(),
                self._morning_ranking_task(),
                self._sync_hot_symbols_task(),
                self._swing_confirm_task(),     # 09:35 波段進場確認
                self._swing_preselect_task(),   # 13:35 波段盤後預選
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

        # Trailing Stop 更新（無論是否收盤都要檢查，確保出場提醒不漏）
        self._trailing.update(bar)

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

        # 依序呼叫五個策略
        for strategy in (self._orb, self._vwap, self._obi, self._mom, self._pullback):
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
            uptick_ratio=quote.uptick_ratio,   # 內外盤比
        )
        self.cache.update_orderbook(book)
        self._obi.on_orderbook(book)

    # --- 收盤監控 ---

    async def _bar_polling_loop(self) -> None:
        """啟動時補抓今日全部歷史 K 棒（重建 ORB/VWAP），之後每 90 秒更新最新一根。
        30 檔 × 1s/檔 + 60s 等待 = 90s 週期 ≈ 20 req/min → 安全範圍。
        """
        from fugle_marketdata import RestClient as FugleRestClient
        from src.data.fugle_client import Bar
        rest = FugleRestClient(api_key=settings.fugle_api_key)

        # ── 第一步：補抓今日所有歷史 K 棒（重建 ORB 區間和 VWAP 狀態）──
        logger.info("補抓今日歷史 1分K，重建 ORB/VWAP 狀態…")
        for symbol in self.symbols:
            try:
                data = rest.stock.intraday.candles(symbol=symbol, timeframe="1")
                candles = data.get("data", [])
                for c in candles:
                    ts = datetime.fromisoformat(c["date"])
                    bar = Bar(
                        symbol=symbol,
                        open=float(c.get("open", 0)),
                        high=float(c.get("high", 0)),
                        low=float(c.get("low", 0)),
                        close=float(c.get("close", 0)),
                        volume=int(c.get("volume", 0)),
                        timestamp=ts,
                    )
                    self.cache.update_bar(bar)
            except Exception as e:
                logger.warning(f"[{symbol}] 歷史 K 棒補抓失敗：{e}")
            await asyncio.sleep(1.0)

        logger.info(f"歷史 K 棒補抓完成，ORB 狀態範例：" +
                    f"2303 high={self.cache.get_orb('2303').high} " +
                    f"low={self.cache.get_orb('2303').low} " +
                    f"locked={self.cache.get_orb('2303').locked}")

        # 初始化完成後立即跑一次策略
        for symbol in self.symbols:
            bar = self.cache.get_last_bar(symbol)
            if bar:
                await self._on_bar(bar)

        while True:
            now = datetime.now()
            if (now.hour, now.minute) >= (_SIGNAL_CUTOFF_HOUR, _SIGNAL_CUTOFF_MINUTE):
                break

            # 合併固定監控池 + 廣域掃描發現的熱門股（去重）
            scan_targets = list(dict.fromkeys(self.symbols + list(self._hot_symbols)))

            updated = 0
            for symbol in scan_targets:
                try:
                    data = rest.stock.intraday.candles(symbol=symbol, timeframe="1")
                    candles = data.get("data", [])
                    if not candles:
                        await asyncio.sleep(0.5)
                        continue
                    latest = candles[-1]
                    ts_str = latest.get("date", "")
                    ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now()
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
                    updated += 1
                except Exception as e:
                    logger.debug(f"[{symbol}] candle 查詢失敗：{e}")
                await asyncio.sleep(0.5)   # 每檔間隔 0.5 秒（136檔×0.5s=68s）

            logger.info(f"1分K 更新完成：{updated}/{len(scan_targets)} 檔（含熱門股 {len(self._hot_symbols)} 檔）")

            # 觸發一次策略判斷
            for symbol in scan_targets:
                bar = self.cache.get_last_bar(symbol)
                if bar:
                    await self._on_bar(bar)

            # 等到下一輪（60 秒後）
            await asyncio.sleep(60)

    async def _sync_hot_symbols_task(self) -> None:
        """定期把廣域掃描的熱門股加入慢輪詢（每 2 分鐘檢查一次）。

        效果：熱門股可享有完整的 OBI / 五檔 / 內外盤比資料，
        進而能在 OBI 策略中觸發訊號（之前只有 K 線會觸發，OBI 不會）。
        """
        # 啟動後等 30 秒再開始（讓系統穩定）
        await asyncio.sleep(30)
        universe_set = set(self.symbols)

        while True:
            await asyncio.sleep(120)  # 每 2 分鐘
            try:
                new_hot = self._hot_symbols - universe_set
                if not new_hot:
                    continue

                before = len(self._quote._symbols)
                self._quote.add_symbols(list(new_hot))
                added = len(self._quote._symbols) - before
                if added > 0:
                    total = len(self._quote._symbols)
                    logger.info(
                        f"熱門股動態加入慢輪詢：+{added} 檔（總 {total} 檔，含 universe 外熱門 {len(new_hot)} 檔）"
                    )
            except Exception as e:
                logger.warning(f"熱門股同步失敗：{e}")

    async def _swing_preselect_task(self) -> None:
        """每日 13:35 執行盤後波段預選 + 持倉檢查。"""
        await self._wait_until_and_run(
            target_hour=13, target_minute=35,
            label="波段盤後預選",
            cli_arg="preselect",
        )

    async def _swing_confirm_task(self) -> None:
        """每日 09:35 執行波段進場確認。"""
        await self._wait_until_and_run(
            target_hour=9, target_minute=35,
            label="波段開盤確認",
            cli_arg="confirm",
        )

    async def _wait_until_and_run(
        self, target_hour: int, target_minute: int, label: str, cli_arg: str
    ) -> None:
        """等到指定時間後，呼叫 swing_scan.py 對應指令。

        - 若現在 < 目標時間 → 等到目標
        - 若已過目標 < 30 分鐘 → 30 秒後補執行
        - 若已過 ≥ 30 分鐘 → 跳過今日
        """
        now = datetime.now()
        target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        delta_sec = (target - now).total_seconds()

        if delta_sec > 0:
            wait_sec = delta_sec
            logger.info(f"{label} 將於 {wait_sec:.0f} 秒後執行（{target_hour:02d}:{target_minute:02d}）")
        elif -1800 <= delta_sec <= 0:
            wait_sec = 30
            logger.info(f"{label} 已過 {-delta_sec:.0f} 秒，30 秒後補執行")
        else:
            logger.info(f"{label} 已過 {-delta_sec/60:.0f} 分鐘，跳過今日")
            return

        await asyncio.sleep(wait_sec)

        try:
            from pathlib import Path
            script = Path(__file__).resolve().parent.parent / "scripts" / "swing_scan.py"
            proc = await asyncio.create_subprocess_exec(
                "python3", str(script), "--mode", cli_arg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                logger.info(f"✅ {label} 執行完成")
            else:
                logger.warning(f"{label} 失敗（exit {proc.returncode}）：{stdout.decode()[:500]}")
        except Exception as e:
            logger.exception(f"{label} 例外：{e}")

    async def _morning_ranking_task(self) -> None:
        """等到 09:30 推播全市場早盤排行榜。

        策略：
          - 若現在 < 09:30 → 等到 09:30 推播
          - 若 09:30 ≤ 現在 ≤ 10:00 → 立刻補推（剛過不久，資料仍有效）
          - 若 > 10:00 → 跳過今天（資料已過時，明天系統重啟會自動處理）
        """
        now = datetime.now()
        target = now.replace(hour=9, minute=30, second=0, microsecond=0)
        delta_sec = (target - now).total_seconds()

        # 計算等待秒數
        if delta_sec > 0:
            # 未到 09:30 → 等到目標時間
            wait_sec = delta_sec
            logger.info(f"早盤排行榜將於 {wait_sec:.0f} 秒後（09:30）推播")
        elif -1800 <= delta_sec <= 0:
            # 09:30 ~ 10:00 → 立刻補推（給系統 30 秒讓 TWSE 穩定）
            wait_sec = 30
            logger.info(f"已過 09:30 共 {-delta_sec:.0f} 秒，30 秒後補推早盤排行榜")
        else:
            # 已過 10:00 → 跳過今天
            logger.info(f"已過 10:00（{-delta_sec/60:.0f} 分鐘），跳過今日早盤排行榜")
            return

        await asyncio.sleep(wait_sec)

        try:
            from scripts.morning_ranking import run as ranking_run
            await ranking_run(top_n=20)
            logger.info("✅ 早盤排行榜推播完成")
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
        await self._fast_quote.stop()

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
