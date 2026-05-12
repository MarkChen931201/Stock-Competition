"""策略 D：早盤動能追蹤（Morning Momentum v2）。

v2 改動：
  - 進場時點：09:30 → 09:15（OR 鎖定就動，搶開盤動量）
  - 漲幅門檻：2% → 1.2%（不再等到漲完才追）
  - 連續 K 棒：3 → 2（早一根進場，搭配加速度確認）
  - 新增「動量加速度」過濾：必須加速中（避免追在動能尾聲）
  - 新增「趨勢分數」過濾：trend_score >= 5 才進
  - 停利：1R → 1.5R 主目標 + 3R 追蹤目標（動能行情拉得更遠）

進場條件（多單，空單對稱）：
  1. 09:15 之後（OR 鎖定）+ 11:00 前（早盤動能視窗）
  2. 開盤至今漲幅 ≥ momentum_pct（預設 1.2%）
  3. 近 2 根 bar 連續收紅
  4. 加速度 > 0（漲速沒減慢）
  5. 當根成交量 > 今日均量 × vol_ratio（爆量確認）
  6. 大盤同向（順勢）
  7. TrendScore >= 5（趨勢已成形）
"""
from __future__ import annotations

from src.data.cache import IntraDayCache
from src.risk.tick_utils import stop_loss_price, take_profit_price
from src.signals.trend_score import calc_trend_score
from src.strategies.base import BaseStrategy, Direction, Signal, SignalType


class MorningMomentumStrategy(BaseStrategy):
    """早盤動能追蹤策略。

    預設參數（v2 強化）：
        momentum_pct    float  開盤至今漲幅門檻          預設 0.012 (1.2%) ✨
        vol_ratio       float  當根量 / 今日均量          預設 1.5
        consec_bars     int    連續同向 bar 數量          預設 2 ✨
        stop_loss_pct   float  停損比例                  預設 0.01 (1%)
        profit_ratio    float  主停利倍數                預設 1.5 ✨
        trail_profit_r  float  追蹤停利倍數              預設 3.0 ✨
        start_minute    int    最早幾分鐘後才進場         預設 15（09:15）✨
        end_minute      int    最晚幾分鐘後停止進場       預設 120（11:00）✨
        min_trend_score float  趨勢分數門檻              預設 5.0 ✨
        market_symbol   str    大盤代號                  預設 "TAIEX"
    """

    name = "早盤動能追蹤"

    def __init__(self, cache: IntraDayCache, params: dict | None = None):
        super().__init__(cache, params)
        self._fired: dict[str, set[Direction]] = {}

    def reset(self) -> None:
        self._fired.clear()

    def generate_signal(self, symbol: str, stock_name: str) -> Signal | None:
        bar = self.cache.get_last_bar(symbol)
        if bar is None:
            return None

        # ── 時段限制（v2：09:15 ~ 11:00）──
        t = bar.timestamp
        open_minutes = (t.hour - 9) * 60 + t.minute
        start_min = self._param("start_minute", 15)   # v2: 30 → 15
        end_min   = self._param("end_minute", 120)    # v2: 90 → 120
        if not (start_min <= open_minutes <= end_min):
            return None

        all_bars = self.cache.get_bars(symbol)
        if len(all_bars) < 5:
            return None

        # ── 讀取參數（放寬版：更早發訊號）──
        momentum_pct:    float = self._param("momentum_pct", 0.008)    # 放寬：1.2% → 0.8%
        vol_ratio:       float = self._param("vol_ratio", 1.2)         # 放寬：1.5 → 1.2
        consec_bars:     int   = self._param("consec_bars", 2)
        stop_loss_pct:   float = self._param("stop_loss_pct", 0.01)
        profit_ratio:    float = self._param("profit_ratio", 1.5)
        trail_profit_r:  float = self._param("trail_profit_r", 3.0)
        min_trend_score: float = self._param("min_trend_score", 3.5)   # 放寬：5.0 → 3.5
        market_symbol:   str   = self._param("market_symbol", "TAIEX")

        close      = bar.close
        first_open = all_bars[0].open if all_bars[0].open else close
        change_pct = (close / first_open - 1) if first_open > 0 else 0.0

        # ── 今日均量 ──
        avg_vol = self.cache.get_recent_volume(symbol, n_bars=20)
        if avg_vol == 0:
            return None

        # ── 連續 K 棒方向 ──
        recent = all_bars[-consec_bars:] if len(all_bars) >= consec_bars else all_bars
        all_up   = all(b.close >= b.open for b in recent)
        all_down = all(b.close <= b.open for b in recent)

        # ── 大盤方向 ──
        mkt_bars = self.cache.get_bars(market_symbol)
        mkt_change = 0.0
        if mkt_bars and mkt_bars[0].open > 0:
            mkt_change = (mkt_bars[-1].close / mkt_bars[0].open) - 1.0

        fired_directions = self._fired.setdefault(symbol, set())

        # ── 趨勢分數計算（多空各算一次）──
        trend_long  = calc_trend_score(self.cache, symbol, Direction.LONG)
        trend_short = calc_trend_score(self.cache, symbol, Direction.SHORT)

        # ===== 多單（v2 強化條件）=====
        long_cond = (
            change_pct >= momentum_pct
            and all_up
            and bar.volume >= avg_vol * vol_ratio
            and mkt_change >= 0
            and trend_long.score >= min_trend_score       # v2: 趨勢分數過濾
            and trend_long.acceleration >= 0              # v2: 加速度確認（沒減速）
            and Direction.LONG not in fired_directions
        )

        if long_cond:
            stop_loss   = stop_loss_price(close * (1 - stop_loss_pct), is_long=True)
            R           = close - stop_loss
            take_profit = take_profit_price(close + profit_ratio * R, is_long=True)
            trail_tp    = take_profit_price(close + trail_profit_r * R, is_long=True)
            self._fired[symbol].add(Direction.LONG)
            return Signal(
                symbol=symbol, name=stock_name,
                direction=Direction.LONG, signal_type=SignalType.ENTRY,
                trigger_price=close, strategy_name=self.name,
                stop_loss=stop_loss, take_profit=take_profit,
                reason=(
                    f"早盤漲幅 {change_pct:+.2%}｜"
                    f"連 {consec_bars} 根紅K｜"
                    f"量比 {bar.volume/avg_vol:.1f}x｜"
                    f"加速度 {trend_long.acceleration:+.2f}%｜"
                    f"趨勢分 {trend_long.score:.1f}/10｜"
                    f"大盤 {mkt_change:+.2%}"
                ),
                extra={
                    "change_pct": round(change_pct * 100, 2),
                    "vol_ratio":  round(bar.volume / avg_vol, 2),
                    "mkt_change": round(mkt_change * 100, 2),
                    "trend_score": round(trend_long.score, 1),
                    "trend_breakdown": trend_long.breakdown,
                    "acceleration": trend_long.acceleration,
                    "vwap_position_pct": trend_long.vwap_position_pct,
                    "trail_take_profit": trail_tp,
                },
            )

        # ===== 空單（v2 強化條件）=====
        short_cond = (
            change_pct <= -momentum_pct
            and all_down
            and bar.volume >= avg_vol * vol_ratio
            and mkt_change <= 0
            and trend_short.score >= min_trend_score
            and trend_short.acceleration <= 0             # 空單：加速度為負（跌得越來越快）
            and Direction.SHORT not in fired_directions
        )

        if short_cond:
            stop_loss   = stop_loss_price(close * (1 + stop_loss_pct), is_long=False)
            R           = stop_loss - close
            take_profit = take_profit_price(close - profit_ratio * R, is_long=False)
            trail_tp    = take_profit_price(close - trail_profit_r * R, is_long=False)
            self._fired[symbol].add(Direction.SHORT)
            return Signal(
                symbol=symbol, name=stock_name,
                direction=Direction.SHORT, signal_type=SignalType.ENTRY,
                trigger_price=close, strategy_name=self.name,
                stop_loss=stop_loss, take_profit=take_profit,
                reason=(
                    f"早盤跌幅 {change_pct:+.2%}｜"
                    f"連 {consec_bars} 根黑K｜"
                    f"量比 {bar.volume/avg_vol:.1f}x｜"
                    f"加速度 {trend_short.acceleration:+.2f}%｜"
                    f"趨勢分 {trend_short.score:.1f}/10｜"
                    f"大盤 {mkt_change:+.2%}"
                ),
                extra={
                    "change_pct": round(change_pct * 100, 2),
                    "vol_ratio":  round(bar.volume / avg_vol, 2),
                    "mkt_change": round(mkt_change * 100, 2),
                    "trend_score": round(trend_short.score, 1),
                    "trend_breakdown": trend_short.breakdown,
                    "acceleration": trend_short.acceleration,
                    "vwap_position_pct": trend_short.vwap_position_pct,
                    "trail_take_profit": trail_tp,
                },
            )

        return None
