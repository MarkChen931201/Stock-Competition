"""盤中記憶體快取 — ORB 區間、VWAP 累計、最新 tick/bar。

所有策略共享同一個 IntraDayCache 實例（singleton pattern）。
資料結構全部住在記憶體，不持久化；每日開盤前 reset()。
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque

from src.data.fugle_client import Bar, Tick
from src.data.twse_client import OrderBook


@dataclass
class ORBState:
    """開盤區間（Opening Range Breakout）狀態。

    在 09:00–09:15 的 15 分鐘內持續更新；09:15 後鎖定。
    """
    high: float = 0.0
    low: float = float("inf")
    locked: bool = False           # True 代表 09:15 已過，不再更新
    formed_at: datetime | None = None  # 鎖定時間

    def update(self, bar: Bar) -> None:
        if self.locked:
            return
        if bar.high > self.high:
            self.high = bar.high
        if bar.low < self.low:
            self.low = bar.low

    def lock(self, at: datetime) -> None:
        self.locked = True
        self.formed_at = at

    @property
    def midpoint(self) -> float:
        return round((self.high + self.low) / 2, 2)

    @property
    def is_valid(self) -> bool:
        return self.locked and self.high > 0 and self.low < float("inf")


@dataclass
class VWAPState:
    """VWAP 累計計算狀態（以 1分K 更新）。

    VWAP = Σ(typical_price × volume) / Σ(volume)
    typical_price = (high + low + close) / 3
    """
    cum_tp_vol: float = 0.0   # Σ(typical_price × volume)
    cum_vol: int = 0          # Σ(volume)（股）

    # 滾動標準差（用於 ±1.5σ 通道）：儲存最近 N 根 bar 的 typical_price
    _tp_window: Deque[float] = field(default_factory=lambda: deque(maxlen=20))

    def update(self, bar: Bar) -> None:
        typical = (bar.high + bar.low + bar.close) / 3
        self.cum_tp_vol += typical * bar.volume
        self.cum_vol += bar.volume
        self._tp_window.append(typical)

    @property
    def vwap(self) -> float:
        if self.cum_vol == 0:
            return 0.0
        return round(self.cum_tp_vol / self.cum_vol, 2)

    @property
    def std(self) -> float:
        """最近 N 根 bar 的 typical_price 標準差，用於 VWAP ±σ 通道。"""
        n = len(self._tp_window)
        if n < 2:
            return 0.0
        mean = sum(self._tp_window) / n
        variance = sum((x - mean) ** 2 for x in self._tp_window) / (n - 1)
        return variance ** 0.5

    def upper_band(self, sigma: float = 1.5) -> float:
        return round(self.vwap + sigma * self.std, 2)

    def lower_band(self, sigma: float = 1.5) -> float:
        return round(self.vwap - sigma * self.std, 2)


class IntraDayCache:
    """盤中所有標的的共享狀態快取。

    每個策略只需要注入這個物件即可讀取最新的市場狀態，
    不需要各自維護獨立的資料結構。
    """

    def __init__(self, orb_lock_time_str: str = "09:15"):
        # "09:15" → (9, 15)
        h, m = (int(x) for x in orb_lock_time_str.split(":"))
        self._orb_lock_hour = h
        self._orb_lock_minute = m

        # 各標的狀態
        self._orb: dict[str, ORBState] = defaultdict(ORBState)
        self._vwap: dict[str, VWAPState] = defaultdict(VWAPState)

        # 最新 tick / bar / orderbook（每次更新覆蓋）
        self._last_tick: dict[str, Tick] = {}
        self._last_bar: dict[str, Bar] = {}
        self._last_book: dict[str, OrderBook] = {}

        # 1分K 歷史（deque 保留盤中所有棒，最多 400 根 ≈ 6.5小時）
        self._bars: dict[str, Deque[Bar]] = defaultdict(lambda: deque(maxlen=400))

    def reset(self) -> None:
        """每日開盤前呼叫，清除所有盤中狀態。"""
        self._orb.clear()
        self._vwap.clear()
        self._last_tick.clear()
        self._last_bar.clear()
        self._last_book.clear()
        self._bars.clear()

    # --- 更新介面（由 client callback 呼叫）---

    def update_tick(self, tick: Tick) -> None:
        self._last_tick[tick.symbol] = tick

    def update_bar(self, bar: Bar) -> None:
        symbol = bar.symbol
        self._last_bar[symbol] = bar
        self._bars[symbol].append(bar)

        # 更新 VWAP
        self._vwap[symbol].update(bar)

        # 更新 ORB（若尚未鎖定）
        orb = self._orb[symbol]
        if not orb.locked:
            # 判斷是否已過 orb_lock_time
            t = bar.timestamp
            if (t.hour, t.minute) >= (self._orb_lock_hour, self._orb_lock_minute):
                orb.lock(at=t)
            else:
                orb.update(bar)

    def update_orderbook(self, book: OrderBook) -> None:
        self._last_book[book.symbol] = book

    # --- 讀取介面 ---

    def get_orb(self, symbol: str) -> ORBState:
        return self._orb[symbol]

    def get_vwap(self, symbol: str) -> VWAPState:
        return self._vwap[symbol]

    def get_last_tick(self, symbol: str) -> Tick | None:
        return self._last_tick.get(symbol)

    def get_last_bar(self, symbol: str) -> Bar | None:
        return self._last_bar.get(symbol)

    def get_last_book(self, symbol: str) -> OrderBook | None:
        return self._last_book.get(symbol)

    def get_bars(self, symbol: str, n: int | None = None) -> list[Bar]:
        """取得最近 n 根 1分K（n=None 代表全部）。"""
        bars = self._bars[symbol]
        if n is None:
            return list(bars)
        return list(bars)[-n:]

    def get_recent_volume(self, symbol: str, n_bars: int = 20) -> float:
        """最近 n 根 bar 的平均成交量（股）。"""
        bars = self.get_bars(symbol, n_bars)
        if not bars:
            return 0.0
        return sum(b.volume for b in bars) / len(bars)


# 全域單例，各模組直接 import 使用
cache = IntraDayCache()
