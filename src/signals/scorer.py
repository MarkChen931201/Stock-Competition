"""訊號品質評分器 — 多維度評估每個訊號，過濾低分訊號。

評分維度（滿分 10 分）：
  1. 籌碼面（外資+投信方向）：0~2 分
  2. ORB 寬度                ：0~2 分（越寬越好）
  3. 量比                    ：0~2 分（越大越好）
  4. 大盤順向                ：0~2 分（同向加分）
  5. 時段                    ：0~2 分（早盤最高分）

  評分 ≥ min_score（預設 4.0）才推播，低分直接 REJECT。

這樣即使個別條件剛好通過，但綜合分數不夠就不推播，
大幅提升訊號品質。
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.data.institutional import InstitutionalLoader
    from src.strategies.base import Signal


class SignalScorer:
    """計算訊號品質分數。

    Args:
        inst_loader:  InstitutionalLoader 實例（無法取得時可傳 None）
        min_score:    最低推播門檻（滿分 10，預設 4.0）
    """

    def __init__(
        self,
        inst_loader: "InstitutionalLoader | None" = None,
        min_score: float = 4.0,
    ):
        self._inst = inst_loader
        self._min_score = min_score

    def score(self, signal: "Signal") -> tuple[float, dict]:
        """計算訊號分數，回傳 (總分, 各維度明細)。"""
        from src.strategies.base import Direction, SignalType

        extra = signal.extra or {}
        breakdown = {}

        # ── 1. 籌碼面（0~2 分）──
        if self._inst is not None:
            chip_score_raw = self._inst.get_score(signal.symbol)
            direction_match = (
                (signal.direction == Direction.LONG  and chip_score_raw >= 0) or
                (signal.direction == Direction.SHORT and chip_score_raw <= 0)
            )
            chip_score = 2.0 if abs(chip_score_raw) > 0.3 and direction_match \
                         else (1.0 if direction_match else 0.0)
        else:
            chip_score = 1.0   # 無資料給中性分
        breakdown["籌碼"] = chip_score

        # ── 2. ORB 寬度（0~2 分）──
        orb_w = extra.get("orb_width_pct", 0.0)   # 單位 %
        if orb_w >= 3.0:
            orb_score = 2.0
        elif orb_w >= 1.5:
            orb_score = 1.5
        elif orb_w >= 0.8:
            orb_score = 1.0
        else:
            orb_score = 0.0   # 太窄（但已被 min_orb_pct 過濾，基本不會到這裡）
        breakdown["ORB寬度"] = orb_score

        # ── 3. 量比（0~2 分）──
        vol_r = extra.get("volume_ratio", 0.0)
        if vol_r >= 3.0:
            vol_score = 2.0
        elif vol_r >= 2.0:
            vol_score = 1.5
        elif vol_r >= 1.5:
            vol_score = 1.0
        elif vol_r >= 1.0:
            vol_score = 0.5
        else:
            vol_score = 0.0
        breakdown["量比"] = vol_score

        # ── 4. 大盤順向（0~2 分）──
        mkt_chg = extra.get("market_change_pct", 0.0)   # 單位 %
        mkt_same_dir = (
            (signal.direction == Direction.LONG  and mkt_chg >= 0) or
            (signal.direction == Direction.SHORT and mkt_chg <= 0)
        )
        if abs(mkt_chg) >= 1.0 and mkt_same_dir:
            mkt_score = 2.0
        elif mkt_same_dir:
            mkt_score = 1.0
        else:
            mkt_score = 0.0
        breakdown["大盤順向"] = mkt_score

        # ── 5. 時段（0~2 分）──
        now = signal.generated_at
        hour = now.hour
        minute = now.minute
        open_min = (hour - 9) * 60 + minute
        if 15 <= open_min <= 45:       # 09:15–09:45 黃金時段
            time_score = 2.0
        elif 45 < open_min <= 90:      # 09:45–10:30
            time_score = 1.5
        elif 90 < open_min <= 120:     # 10:30–11:00
            time_score = 1.0
        else:
            time_score = 0.5
        breakdown["時段"] = time_score

        total = sum(breakdown.values())
        return total, breakdown

    def is_qualified(self, signal: "Signal") -> tuple[bool, float, dict]:
        """判斷訊號是否通過品質門檻。

        Returns:
            (通過, 分數, 各維度明細)
        """
        total, breakdown = self.score(signal)
        return total >= self._min_score, total, breakdown
