"""TWSE 官方 API client — 即時五檔委買委賣 + 大盤指數。

資料來源：
  - 個股即時揭示：https://mis.twse.com.tw/stock/api/getStockInfo.jsp
  - 大盤（加權）：stock_id = "tse_t00.tw"

輪詢策略：
  - 盤中每 N 秒（預設 3 秒）輪詢一次，TWSE 更新頻率約 5 秒。
  - 使用 httpx.AsyncClient 非同步請求，避免阻塞 asyncio event loop。

注意：TWSE 這支 API 非官方文件公開，欄位名稱為縮寫，映射如下：
  z = 成交價, a = 委賣價（五檔，_分隔）, b = 委買價
  f = 委賣量, g = 委買量, v = 成交量(張), c = 股票代號
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

import httpx
from loguru import logger

_TWSE_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://mis.twse.com.tw/",
    "User-Agent": "Mozilla/5.0",
}


@dataclass
class OrderBook:
    """五檔委買委賣快照。"""
    symbol: str
    timestamp: datetime

    # 委買：price[0] 為最佳買一，price[4] 為買五
    bid_prices: list[float] = field(default_factory=list)
    bid_volumes: list[int] = field(default_factory=list)   # 單位：張

    # 委賣：price[0] 為最佳賣一
    ask_prices: list[float] = field(default_factory=list)
    ask_volumes: list[int] = field(default_factory=list)

    last_price: float = 0.0
    total_volume_lots: int = 0        # 今日累計成交量（張）
    uptick_ratio: float = 0.5         # 內外盤比（外盤量/總量），0.5 = 無資料

    @property
    def obi(self) -> float:
        """Order Book Imbalance = (買量 - 賣量) / (買量 + 賣量)，範圍 [-1, 1]。
        > 0 代表買盤壓過賣盤，< 0 代表賣盤壓過買盤。
        """
        total_bid = sum(self.bid_volumes)
        total_ask = sum(self.ask_volumes)
        total = total_bid + total_ask
        if total == 0:
            return 0.0
        return (total_bid - total_ask) / total

    @property
    def best_bid(self) -> float:
        return self.bid_prices[0] if self.bid_prices else 0.0

    @property
    def best_ask(self) -> float:
        return self.ask_prices[0] if self.ask_prices else 0.0

    @property
    def spread(self) -> float:
        """買賣價差（元）。"""
        if self.best_ask and self.best_bid:
            return round(self.best_ask - self.best_bid, 2)
        return 0.0


OrderBookCallback = Callable[[OrderBook], Awaitable[None]]


class TWSEClient:
    """輪詢 TWSE 五檔資料，並透過 callback 推送更新。

    用法：
        client = TWSEClient(poll_interval=3.0)
        client.add_symbols(["2330", "2317"])
        client.on_orderbook(my_handler)
        await client.run()
    """

    def __init__(self, poll_interval: float = 3.0, timeout: float = 5.0):
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._symbols: list[str] = []
        self._callbacks: list[OrderBookCallback] = []
        self._running = False

    def add_symbols(self, symbols: list[str]) -> None:
        """新增要輪詢的股票代號（使用 tse_XXXX.tw 格式）。"""
        for s in symbols:
            key = s if s.endswith(".tw") else f"tse_{s}.tw"
            if key not in self._symbols:
                self._symbols.append(key)

    def on_orderbook(self, cb: OrderBookCallback) -> None:
        """註冊 OrderBook 更新 callback。"""
        self._callbacks.append(cb)

    async def run(self) -> None:
        """啟動輪詢迴圈，直到外部取消。"""
        self._running = True
        async with httpx.AsyncClient(headers=_HEADERS, timeout=self._timeout) as http:
            logger.info(f"TWSE 輪詢啟動，間隔 {self._poll_interval}s，共 {len(self._symbols)} 檔")
            while self._running:
                try:
                    books = await self._fetch_all(http)
                    for book in books:
                        await self._fire(book)
                except asyncio.CancelledError:
                    logger.info("TWSEClient cancelled.")
                    break
                except Exception as e:
                    logger.warning(f"TWSE 輪詢錯誤：{e}")
                await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        self._running = False

    # --- 私有 ---

    async def _fetch_all(self, http: httpx.AsyncClient) -> list[OrderBook]:
        if not self._symbols:
            return []
        # TWSE API 支援一次查多檔，用 "|" 分隔
        ex_ch = "|".join(self._symbols)
        resp = await http.get(_TWSE_API, params={"ex_ch": ex_ch, "json": "1", "delay": "0"})
        resp.raise_for_status()
        data = resp.json()
        msgArray = data.get("msgArray", [])
        return [ob for item in msgArray if (ob := self._parse(item)) is not None]

    @staticmethod
    def _parse_price_list(raw: str) -> list[float]:
        """'100.5_101.0_101.5_102.0_102.5' → [100.5, 101.0, 101.5, 102.0, 102.5]"""
        if not raw or raw == "-":
            return []
        return [float(x) for x in raw.split("_") if x and x != "-"]

    @staticmethod
    def _parse_vol_list(raw: str) -> list[int]:
        if not raw or raw == "-":
            return []
        return [int(x) for x in raw.split("_") if x and x != "-"]

    def _parse(self, item: dict) -> OrderBook | None:
        try:
            # 從 "tse_2330.tw" 取出 "2330"
            raw_code = item.get("c", "")
            symbol = raw_code  # 可依需要去掉前綴

            # 時間：TWSE 格式 "09:01:23"，日期用今天
            time_str = item.get("t", "")
            now = datetime.now()
            if time_str:
                h, m, s = (int(x) for x in time_str.split(":"))
                ts = now.replace(hour=h, minute=m, second=s, microsecond=0)
            else:
                ts = now

            # 委買/委賣價量（五檔）
            # TWSE: a=賣價(賣一在前), b=買價(買一在前), f=賣量, g=買量
            ask_prices = TWSEClient._parse_price_list(item.get("a", ""))
            bid_prices = TWSEClient._parse_price_list(item.get("b", ""))
            ask_volumes = TWSEClient._parse_vol_list(item.get("f", ""))
            bid_volumes = TWSEClient._parse_vol_list(item.get("g", ""))

            # 最新成交價
            z = item.get("z", "-")
            last_price = float(z) if z and z != "-" else 0.0

            # 今日成交量（張）—— TWSE 欄位 v 單位為「股」，除以 1000 轉張
            v = item.get("v", "0")
            total_vol = int(float(v)) if v and v != "-" else 0

            return OrderBook(
                symbol=symbol,
                timestamp=ts,
                bid_prices=bid_prices,
                bid_volumes=bid_volumes,
                ask_prices=ask_prices,
                ask_volumes=ask_volumes,
                last_price=last_price,
                total_volume_lots=total_vol,
            )
        except Exception as e:
            logger.warning(f"TWSE parse 失敗：{e}，item={item}")
            return None

    async def _fire(self, book: OrderBook) -> None:
        for cb in self._callbacks:
            try:
                await cb(book)
            except Exception as e:
                logger.exception(f"orderbook callback 錯誤：{e}")
