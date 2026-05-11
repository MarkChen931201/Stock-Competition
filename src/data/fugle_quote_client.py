"""Fugle REST Quote 輪詢 — 替代 TWSE getStockInfo，提供五檔 + OBI + 成交量。

每 3 秒輪詢一次，用 Fugle intraday/quote API。
資料比 TWSE 更豐富（含 bids/asks 五檔、成交額、振幅），且不會被 rate limit。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

from fugle_marketdata import RestClient
from loguru import logger


@dataclass
class FugleQuote:
    """Fugle quote API 回傳的完整快照。"""
    symbol: str
    name: str
    timestamp: datetime

    last_price: float
    prev_close: float
    open_price: float
    high: float
    low: float

    # 五檔委買（index 0 = 最佳）
    bid_prices: list[float] = field(default_factory=list)
    bid_sizes: list[int]    = field(default_factory=list)   # 單位：張
    ask_prices: list[float] = field(default_factory=list)
    ask_sizes: list[int]    = field(default_factory=list)

    # 累計成交
    trade_volume: int        = 0      # 張（總）
    trade_value: float       = 0.0   # 元
    trade_vol_at_bid: int    = 0      # 內盤量（主動賣，成交在買價）
    trade_vol_at_ask: int    = 0      # 外盤量（主動買，成交在賣價）

    @property
    def obi(self) -> float:
        """等權重 OBI（五檔總和）。"""
        total_bid = sum(self.bid_sizes)
        total_ask = sum(self.ask_sizes)
        total = total_bid + total_ask
        return (total_bid - total_ask) / total if total else 0.0

    @property
    def weighted_obi(self) -> float:
        """加權 OBI：買一權重 5，買二 4，...，買五 1。"""
        weights = [5, 4, 3, 2, 1]
        w_bid = sum(v * w for v, w in zip(self.bid_sizes, weights))
        w_ask = sum(v * w for v, w in zip(self.ask_sizes, weights))
        total = w_bid + w_ask
        return (w_bid - w_ask) / total if total else 0.0

    @property
    def best_level_ratio(self) -> float:
        """買一壓力比 = 買一量 / (買一量 + 賣一量)。"""
        b1 = self.bid_sizes[0] if self.bid_sizes else 0
        a1 = self.ask_sizes[0] if self.ask_sizes else 0
        total = b1 + a1
        return b1 / total if total else 0.5

    @property
    def uptick_ratio(self) -> float:
        """內外盤比（外盤比）= 外盤量 / (外盤量 + 內盤量)。
        基於實際成交方向，反映市場「現實」買壓。
        > 0.55 = 主動買方強勢（外盤多），適合做多
        < 0.45 = 主動賣方強勢（內盤多），適合做空
        0.45~0.55 = 多空均衡
        """
        total = self.trade_vol_at_bid + self.trade_vol_at_ask
        return self.trade_vol_at_ask / total if total else 0.5

    @property
    def buy_sell_pressure(self) -> str:
        """買賣壓判斷（人類可讀）。"""
        r = self.uptick_ratio
        if r >= 0.6:   return "強買壓 🔥"
        if r >= 0.55:  return "偏買壓 📈"
        if r <= 0.4:   return "強賣壓 ❄️"
        if r <= 0.45:  return "偏賣壓 📉"
        return "均衡 ↔️"

    @property
    def best_bid(self) -> float:
        return self.bid_prices[0] if self.bid_prices else 0.0

    @property
    def best_ask(self) -> float:
        return self.ask_prices[0] if self.ask_prices else 0.0

    @property
    def amplitude_pct(self) -> float:
        if self.prev_close <= 0:
            return 0.0
        return (self.high - self.low) / self.prev_close


QuoteCallback = Callable[[FugleQuote], Awaitable[None]]


class FugleQuoteClient:
    """每隔 poll_interval 秒輪詢 Fugle quote API，推送 FugleQuote 給 callback。

    用法：
        client = FugleQuoteClient(api_key='...')
        client.add_symbols(['2330', '2303'])
        client.on_quote(my_handler)
        await client.run()
    """

    def __init__(self, api_key: str, poll_interval: float = 3.0):
        self._rest = RestClient(api_key=api_key)
        self._poll_interval = poll_interval
        self._symbols: list[str] = []
        self._callbacks: list[QuoteCallback] = []
        self._running = False

    def add_symbols(self, symbols: list[str]) -> None:
        for s in symbols:
            if s not in self._symbols:
                self._symbols.append(s)

    def on_quote(self, cb: QuoteCallback) -> None:
        self._callbacks.append(cb)

    async def run(self) -> None:
        self._running = True
        logger.info(f"Fugle Quote 輪詢啟動，間隔 {self._poll_interval}s，共 {len(self._symbols)} 檔，每檔間隔 1s")
        while self._running:
            try:
                for symbol in self._symbols:
                    if not self._running:
                        break
                    try:
                        raw = await asyncio.get_event_loop().run_in_executor(
                            None, lambda s=symbol: self._rest.stock.intraday.quote(symbol=s)
                        )
                        q = _parse(raw)
                        if q:
                            for cb in self._callbacks:
                                await cb(q)
                    except Exception as e:
                        logger.debug(f"[{symbol}] quote 查詢失敗：{e}")
                    await asyncio.sleep(1.0)   # 每檔間隔 1 秒，避免 rate limit
            except asyncio.CancelledError:
                logger.info("FugleQuoteClient cancelled.")
                break
            except Exception as e:
                logger.warning(f"Quote 輪詢錯誤：{e}")
            await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        self._running = False


def _parse(raw: dict) -> FugleQuote | None:
    try:
        symbol      = raw.get("symbol", "")
        name        = raw.get("name", "")
        last_price  = float(raw.get("lastPrice") or raw.get("closePrice") or 0)
        prev_close  = float(raw.get("previousClose") or raw.get("referencePrice") or 0)
        open_price  = float(raw.get("openPrice") or 0)
        high        = float(raw.get("highPrice") or 0)
        low         = float(raw.get("lowPrice") or 0)

        bids = raw.get("bids", [])
        asks = raw.get("asks", [])
        bid_prices = [float(b["price"]) for b in bids]
        bid_sizes  = [int(b["size"])   for b in bids]
        ask_prices = [float(a["price"]) for a in asks]
        ask_sizes  = [int(a["size"])   for a in asks]

        total = raw.get("total", {})
        trade_volume     = int(total.get("tradeVolume")      or 0)
        trade_value      = float(total.get("tradeValue")     or 0)
        trade_vol_at_bid = int(total.get("tradeVolumeAtBid") or 0)  # 內盤量
        trade_vol_at_ask = int(total.get("tradeVolumeAtAsk") or 0)  # 外盤量

        return FugleQuote(
            symbol=symbol, name=name,
            timestamp=datetime.now(),
            last_price=last_price, prev_close=prev_close,
            open_price=open_price, high=high, low=low,
            bid_prices=bid_prices, bid_sizes=bid_sizes,
            ask_prices=ask_prices, ask_sizes=ask_sizes,
            trade_volume=trade_volume, trade_value=trade_value,
            trade_vol_at_bid=trade_vol_at_bid,
            trade_vol_at_ask=trade_vol_at_ask,
        )
    except Exception as e:
        logger.debug(f"FugleQuote parse 失敗：{e}")
        return None
