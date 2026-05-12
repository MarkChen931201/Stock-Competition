"""訊號品質評分器 v3 — 多維度評估每個訊號，過濾低分訊號。

評分維度（滿分 16 分）：
  1. 籌碼面（外資+投信方向）  ：0~2 分
  2. ORB 寬度                ：0~2 分（越寬越好）
  3. 量比                    ：0~2 分（越大越好）
  4. 大盤順向                ：0~2 分（同向加分）
  5. 時段                    ：0~2 分（早盤最高分）
  6. 內外盤比（uptick ratio）：0~2 分
  7. 加權 OBI（買賣壓力）    ：0~2 分
  8. 趨勢分數（v3 新增）     ：0~2 分（壓縮 trend_score 10 分至 2 分）

  評分 ≥ min_score（預設 7.0 / 16）才推播，低分直接 REJECT。

v3 新增：趨勢分數維度
  - 來源：trend_score.calc_trend_score (滿分 10)
  - 壓縮：trend_score / 10 × 2 = 0~2 分
  - 趨勢分數涵蓋 VWAP/EMA/ROC/加速度/連續K
  - 訊號評分 + 趨勢分數雙重把關，避免逆勢進場

OrderBook 資料由 dispatcher 在 submit 時注入（透過 cache）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.data.institutional import InstitutionalLoader
    from src.strategies.base import Signal


class SignalScorer:
    """計算訊號品質分數。

    Args:
        inst_loader:  InstitutionalLoader 實例（無法取得時可傳 None）
        min_score:    最低推播門檻（滿分 16，預設 7.0）
        cache:        IntraDayCache 實例（用於讀取 OrderBook + 趨勢分數）
    """

    def __init__(
        self,
        inst_loader: "InstitutionalLoader | None" = None,
        min_score: float = 7.0,
        cache=None,
    ):
        self._inst = inst_loader
        self._min_score = min_score
        self._cache = cache

    def score(self, signal: "Signal") -> tuple[float, dict]:
        """計算訊號分數，回傳 (總分, 各維度明細)。"""
        from src.strategies.base import Direction

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
            chip_score = 1.0
        breakdown["籌碼"] = chip_score

        # ── 2. ORB 寬度（0~2 分）──
        orb_w = extra.get("orb_width_pct", 0.0)
        if orb_w >= 3.0:
            orb_score = 2.0
        elif orb_w >= 1.5:
            orb_score = 1.5
        elif orb_w >= 0.8:
            orb_score = 1.0
        else:
            orb_score = 0.0
        breakdown["ORB"] = orb_score

        # ── 3. 量比（0~2 分）──
        vol_r = extra.get("volume_ratio", 0.0)
        if vol_r >= 3.0:    vol_score = 2.0
        elif vol_r >= 2.0:  vol_score = 1.5
        elif vol_r >= 1.5:  vol_score = 1.0
        elif vol_r >= 1.0:  vol_score = 0.5
        else:               vol_score = 0.0
        breakdown["量比"] = vol_score

        # ── 4. 大盤順向（0~2 分）──
        mkt_chg = extra.get("market_change_pct", 0.0)
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
        breakdown["大盤"] = mkt_score

        # ── 5. 時段（0~2 分）──
        open_min = (signal.generated_at.hour - 9) * 60 + signal.generated_at.minute
        if 15 <= open_min <= 45:       time_score = 2.0
        elif 45 < open_min <= 90:      time_score = 1.5
        elif 90 < open_min <= 120:     time_score = 1.0
        else:                          time_score = 0.5
        breakdown["時段"] = time_score

        # ── 6. 內外盤比（0~2 分）── ✨新增
        uptick = extra.get("uptick_ratio", 50.0)  # 預設 50（中性）
        if signal.direction == Direction.LONG:
            # 做多：外盤比越高越好
            if uptick >= 60:     uptick_score = 2.0   # 🔥 強買壓
            elif uptick >= 55:   uptick_score = 1.5
            elif uptick >= 50:   uptick_score = 1.0
            elif uptick >= 47:   uptick_score = 0.5
            else:                uptick_score = 0.0
        else:
            # 做空：外盤比越低越好
            if uptick <= 40:     uptick_score = 2.0   # ❄️ 強賣壓
            elif uptick <= 45:   uptick_score = 1.5
            elif uptick <= 50:   uptick_score = 1.0
            elif uptick <= 53:   uptick_score = 0.5
            else:                uptick_score = 0.0
        breakdown["內外盤"] = uptick_score

        # ── 7. 加權 OBI（0~2 分）── ✨新增
        # 從 cache 讀取最新 OrderBook（如有）
        w_obi = 0.0
        blr = 0.5
        if self._cache is not None:
            book = self._cache.get_last_book(signal.symbol)
            if book is not None:
                w_obi = book.weighted_obi
                blr = book.best_level_ratio

        if signal.direction == Direction.LONG:
            # 做多：加權 OBI 越正越好，買一壓力比越高越好
            obi_score = 0.0
            if w_obi >= 0.3 and blr >= 0.6:     obi_score = 2.0   # 強雙重買壓
            elif w_obi >= 0.15 or blr >= 0.55:  obi_score = 1.0
            elif w_obi >= 0:                    obi_score = 0.5
        else:
            obi_score = 0.0
            if w_obi <= -0.3 and blr <= 0.4:    obi_score = 2.0
            elif w_obi <= -0.15 or blr <= 0.45: obi_score = 1.0
            elif w_obi <= 0:                    obi_score = 0.5
        breakdown["加權OBI"] = obi_score

        # ── 8. 趨勢分數（0~2 分）── ✨v3 新增
        # 用 trend_score 引擎計算（滿分 10），再壓縮成 0~2 分
        trend_score_10 = 0.0
        if self._cache is not None:
            try:
                from src.signals.trend_score import calc_trend_score
                trend = calc_trend_score(self._cache, signal.symbol, signal.direction)
                trend_score_10 = trend.score
                # 把趨勢明細存入 signal.extra，供 embed 顯示
                if signal.extra is None:
                    signal.extra = {}
                signal.extra.setdefault("trend_score", round(trend.score, 1))
                signal.extra.setdefault("trend_breakdown", trend.breakdown)
                signal.extra.setdefault("vwap_position_pct", trend.vwap_position_pct)
                signal.extra.setdefault("acceleration_pct", trend.acceleration)
            except Exception:
                trend_score_10 = 0.0

        # 壓縮 0~10 → 0~2（線性映射）
        trend_score_2 = round(trend_score_10 / 5.0, 1)   # 10/5=2, 5/5=1
        trend_score_2 = min(2.0, max(0.0, trend_score_2))
        breakdown["趨勢"] = trend_score_2

        total = sum(breakdown.values())
        return total, breakdown

    def is_qualified(self, signal: "Signal") -> tuple[bool, float, dict]:
        """判斷訊號是否通過品質門檻。

        Returns:
            (通過, 分數, 各維度明細)
        """
        total, breakdown = self.score(signal)
        return total >= self._min_score, total, breakdown
