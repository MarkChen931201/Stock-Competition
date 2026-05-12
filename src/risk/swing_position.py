"""短期波段持倉管理 — 跨日狀態持久化到 JSON。

設計：
  - 持倉資料寫入 data/swing_positions.json（每日盤後自動更新）
  - 每日 13:35 推播 ：
      1. 新建議買進（昨日突破，今日符合條件）
      2. 持倉檢查（接近停損？停利？最長持有 5 天）
      3. 已出場（觸發停損/停利）

狀態流轉：
  WAITING   → 候選中（已掛單，等待成交）
  HOLDING   → 持倉中
  EXITED    → 已出場（停損/停利/時間止損）
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path


class SwingStatus(str, Enum):
    WAITING = "WAITING"   # 已推薦，等隔日進場確認
    HOLDING = "HOLDING"   # 持倉中
    EXITED  = "EXITED"    # 已出場


@dataclass
class SwingPosition:
    """單筆波段持倉。"""
    symbol: str
    name: str
    status: SwingStatus

    # 進場資料
    entry_date: str           # YYYY-MM-DD（預計或實際進場日）
    entry_limit: float        # 預定進場限價
    entry_actual: float = 0.0 # 實際成交價（成交後填入）
    lots: int = 0

    # 停損停利
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0

    # 評分資訊（顯示用）
    score: float = 0.0
    breakdown: dict = field(default_factory=dict)
    strategy: str = "swing_breakout_v1"

    # 出場資料
    exit_date: str | None = None
    exit_price: float = 0.0
    exit_reason: str | None = None
    pnl_pct: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SwingPosition":
        d = dict(d)
        d["status"] = SwingStatus(d["status"])
        return cls(**d)

    def days_held(self, today: date) -> int:
        """已持有天數（從進場日算起）。"""
        try:
            entry_dt = datetime.strptime(self.entry_date, "%Y-%m-%d").date()
            return (today - entry_dt).days
        except Exception:
            return 0


class SwingPositionManager:
    """波段持倉管理員。

    用 JSON 檔持久化，跨日仍可讀回。
    """

    def __init__(self, storage_path: Path | str | None = None):
        if storage_path is None:
            storage_path = Path(__file__).parent.parent.parent / "data" / "swing_positions.json"
        self.path = Path(storage_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.positions: list[SwingPosition] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.positions = []
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            self.positions = [SwingPosition.from_dict(d) for d in raw]
        except Exception:
            self.positions = []

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([p.to_dict() for p in self.positions], f, ensure_ascii=False, indent=2)

    def add(self, position: SwingPosition) -> None:
        """新增持倉（候選或已成交）。"""
        # 同一檔同一日同一狀態避免重複
        for p in self.positions:
            if (p.symbol == position.symbol and
                p.entry_date == position.entry_date and
                p.status == position.status):
                return  # 已存在
        self.positions.append(position)
        self.save()

    def get_active(self) -> list[SwingPosition]:
        """取得目前未出場的持倉（WAITING + HOLDING）。"""
        return [p for p in self.positions if p.status != SwingStatus.EXITED]

    def get_waiting(self) -> list[SwingPosition]:
        return [p for p in self.positions if p.status == SwingStatus.WAITING]

    def get_holding(self) -> list[SwingPosition]:
        return [p for p in self.positions if p.status == SwingStatus.HOLDING]

    def confirm_entry(self, symbol: str, entry_date: str, actual_price: float, lots: int) -> bool:
        """把 WAITING 轉為 HOLDING（成交確認）。"""
        for p in self.positions:
            if (p.symbol == symbol and p.entry_date == entry_date
                and p.status == SwingStatus.WAITING):
                p.status = SwingStatus.HOLDING
                p.entry_actual = actual_price
                p.lots = lots
                self.save()
                return True
        return False

    def exit_position(
        self, symbol: str, exit_price: float, exit_reason: str, today: date | None = None
    ) -> bool:
        """關閉一個持倉。"""
        for p in self.positions:
            if p.symbol == symbol and p.status == SwingStatus.HOLDING:
                p.status = SwingStatus.EXITED
                p.exit_price = exit_price
                p.exit_reason = exit_reason
                p.exit_date = (today or date.today()).strftime("%Y-%m-%d")
                if p.entry_actual > 0:
                    p.pnl_pct = round((exit_price - p.entry_actual) / p.entry_actual * 100, 2)
                self.save()
                return True
        return False

    def cancel_waiting(self, symbol: str, entry_date: str, reason: str) -> bool:
        """取消未成交的 WAITING（隔日確認失敗）。"""
        for p in self.positions:
            if (p.symbol == symbol and p.entry_date == entry_date
                and p.status == SwingStatus.WAITING):
                p.status = SwingStatus.EXITED
                p.exit_reason = f"取消：{reason}"
                p.exit_date = entry_date
                self.save()
                return True
        return False

    def cleanup_old(self, days: int = 30) -> int:
        """清理 N 天前的 EXITED 紀錄（節省檔案大小）。"""
        today = date.today()
        before = len(self.positions)
        kept = []
        for p in self.positions:
            if p.status == SwingStatus.EXITED and p.exit_date:
                try:
                    ex_dt = datetime.strptime(p.exit_date, "%Y-%m-%d").date()
                    if (today - ex_dt).days <= days:
                        kept.append(p)
                except Exception:
                    kept.append(p)
            else:
                kept.append(p)
        self.positions = kept
        self.save()
        return before - len(kept)
