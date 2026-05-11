"""策略 D：早盤動能追蹤（Morning Momentum）。

邏輯：
  9:30 後觀察：若個股已上漲 > momentum_pct（預設 2%），
  且成交量創今日新高，則追趨勢方向進場。

  這個策略補足 ORB 的盲點：
  - ORB 只捕捉「突破開盤區間」的行情
  - 有些股票開盤就一路飆，早就脫離 ORB 太遠
  - 動能策略可以在確認趨勢後加入

進場條件（多單，空單對稱）：
  1. 09:30 之後（等趨勢確認）
  2. 開盤至今漲幅 > momentum_pct（預設 2%）
  3. 近 3 根 bar 連續收紅（確認動能持續）
  4. 當根成交量 > 今日均量 × vol_ratio（爆量確認）
  5. 大盤同向（順勢）

停損：進場價 × (1 - atr_stop_mult × 當日 ATR%)
停利：進場價 + 2R（動能行情給更大空間）
"""
from __future__ import annotations

from src.data.cache import IntraDayCache
from src.strategies.base import BaseStrategy, Direction, Signal, SignalType


class MorningMomentumStrategy(BaseStrategy):
    """早盤動能追蹤策略。

    預設參數：
        momentum_pct    float  開盤至今漲幅門檻          預設 0.02 (2%)
        vol_ratio       float  當根量 / 今日均量          預設 1.5
        consec_bars     int    連續同向 bar 數量          預設 3
        stop_loss_pct   float  停損比例                  預設 0.01 (1%)
        profit_ratio    float  停利倍數                  預設 2.0
        start_minute    int    最早幾分鐘後才進場         預設 30（09:30）
        end_minute      int    最晚幾分鐘後停止進場       預設 90（10:30）
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

        # ── 時段限制 ──
        t = bar.timestamp
        open_minutes = (t.hour - 9) * 60 + t.minute  # 距開盤分鐘數
        start_min = self._param("start_minute", 30)
        end_min   = self._param("end_minute", 90)
        if not (start_min <= open_minutes <= end_min):
            return None

        all_bars = self.cache.get_bars(symbol)
        if len(all_bars) < 5:
            return None

        # ── 讀取參數 ──
        momentum_pct:  float = self._param("momentum_pct", 0.02)
        vol_ratio:     float = self._param("vol_ratio", 1.5)
        consec_bars:   int   = self._param("consec_bars", 3)
        stop_loss_pct: float = self._param("stop_loss_pct", 0.01)
        profit_ratio:  float = self._param("profit_ratio", 2.0)
        market_symbol: str   = self._param("market_symbol", "TAIEX")

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

        # ===== 多單：今日漲幅 > 2%，連續紅 K，爆量，大盤正向 =====
        long_cond = (
            change_pct >= momentum_pct
            and all_up
            and bar.volume >= avg_vol * vol_ratio
            and mkt_change >= 0
            and Direction.LONG not in fired_directions
        )

        if long_cond:
            stop_loss  = round(close * (1 - stop_loss_pct), 2)
            R          = close - stop_loss
            take_profit = round(close + profit_ratio * R, 2)
            self._fired[symbol].add(Direction.LONG)
            return Signal(
                symbol=symbol, name=stock_name,
                direction=Direction.LONG, signal_type=SignalType.ENTRY,
                trigger_price=close, strategy_name=self.name,
                stop_loss=stop_loss, take_profit=take_profit,
                reason=(
                    f"早盤漲幅 {change_pct:+.2%}｜"
                    f"連續 {consec_bars} 根紅K｜"
                    f"量比 {bar.volume/avg_vol:.1f}x｜"
                    f"大盤 {mkt_change:+.2%}"
                ),
                extra={
                    "change_pct": round(change_pct * 100, 2),
                    "vol_ratio":  round(bar.volume / avg_vol, 2),
                    "mkt_change": round(mkt_change * 100, 2),
                },
            )

        # ===== 空單：今日跌幅 > 2%，連續黑 K，爆量，大盤負向 =====
        short_cond = (
            change_pct <= -momentum_pct
            and all_down
            and bar.volume >= avg_vol * vol_ratio
            and mkt_change <= 0
            and Direction.SHORT not in fired_directions
        )

        if short_cond:
            stop_loss  = round(close * (1 + stop_loss_pct), 2)
            R          = stop_loss - close
            take_profit = round(close - profit_ratio * R, 2)
            self._fired[symbol].add(Direction.SHORT)
            return Signal(
                symbol=symbol, name=stock_name,
                direction=Direction.SHORT, signal_type=SignalType.ENTRY,
                trigger_price=close, strategy_name=self.name,
                stop_loss=stop_loss, take_profit=take_profit,
                reason=(
                    f"早盤跌幅 {change_pct:+.2%}｜"
                    f"連續 {consec_bars} 根黑K｜"
                    f"量比 {bar.volume/avg_vol:.1f}x｜"
                    f"大盤 {mkt_change:+.2%}"
                ),
                extra={
                    "change_pct": round(change_pct * 100, 2),
                    "vol_ratio":  round(bar.volume / avg_vol, 2),
                    "mkt_change": round(mkt_change * 100, 2),
                },
            )

        return None
