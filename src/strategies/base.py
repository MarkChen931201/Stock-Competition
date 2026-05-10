"""Strategy 抽象基底類別。

所有策略都繼承 BaseStrategy，實作 generate_signal()。
訊號生成後由 dispatcher 負責風險過濾與推播，策略本身不直接推播。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.data.cache import IntraDayCache


class Direction(str, Enum):
    LONG = "LONG"    # 做多（先買後賣）
    SHORT = "SHORT"  # 做空（先賣後買）


class SignalType(str, Enum):
    ENTRY = "ENTRY"   # 進場訊號
    WATCH = "WATCH"   # 觀察預警（不直接下單，僅提醒）


@dataclass
class Signal:
    """策略產出的訊號資料包。

    dispatcher 收到後會：
    1. 計算交易成本 / 損益兩平價
    2. 驗證最小獲利空間（≥ 0.8%）
    3. 去重（同股 N 分鐘內不重發）
    4. 發送 Discord Embed
    """
    symbol: str
    name: str                      # 股票名稱（顯示用）
    direction: Direction
    signal_type: SignalType
    trigger_price: float           # 觸發訊號的成交價
    strategy_name: str             # 來源策略名稱

    # 策略建議的停損/停利（dispatcher 可再覆蓋）
    stop_loss: float = 0.0
    take_profit: float = 0.0

    # 附加資訊（填入 Discord embed 的詳細欄位）
    reason: str = ""               # 訊號觸發原因說明
    extra: dict = field(default_factory=dict)

    generated_at: datetime = field(default_factory=datetime.now)


class BaseStrategy(ABC):
    """所有策略的抽象基底。

    子類別需實作：
        name: str             策略名稱（顯示用）
        generate_signal()     每根 1分K 更新後呼叫，回傳 Signal 或 None
    """

    name: str = "BaseStrategy"

    def __init__(self, cache: "IntraDayCache", params: dict | None = None):
        self.cache = cache
        self.params = params or {}

    @abstractmethod
    def generate_signal(self, symbol: str, stock_name: str) -> Signal | None:
        """根據 cache 中最新狀態判斷是否產生訊號。

        Args:
            symbol:     股票代號，例如 "2330"
            stock_name: 股票名稱，例如 "台積電"

        Returns:
            Signal 物件，或 None（無訊號）
        """
        ...

    def _param(self, key: str, default):
        """安全讀取策略參數，不存在時使用預設值。"""
        return self.params.get(key, default)
