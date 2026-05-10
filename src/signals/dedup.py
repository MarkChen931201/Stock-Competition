"""訊號去重模組 — 同股同方向在冷卻期內不重複推播。"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.strategies.base import Direction


class SignalDedup:
    """記錄每個 (symbol, direction) 的最後推播時間，冷卻期內擋掉重複訊號。

    Args:
        cooldown_minutes: 同股同方向的冷卻分鐘數，預設 5 分鐘。
    """

    def __init__(self, cooldown_minutes: int = 5):
        self._cooldown = timedelta(minutes=cooldown_minutes)
        # key: (symbol, direction)  value: 上次推播時間
        self._last_sent: dict[tuple[str, Direction], datetime] = {}

    def is_duplicate(self, symbol: str, direction: Direction) -> bool:
        """回傳 True 代表冷卻中，應擋掉此訊號。"""
        key = (symbol, direction)
        last = self._last_sent.get(key)
        if last is None:
            return False
        return datetime.now() - last < self._cooldown

    def mark_sent(self, symbol: str, direction: Direction) -> None:
        """訊號推播成功後呼叫，記錄時間。"""
        self._last_sent[(symbol, direction)] = datetime.now()

    def reset(self) -> None:
        """每日開盤前清除所有紀錄。"""
        self._last_sent.clear()
