"""策略 E：趨勢回踩進場（Trend Pullback）。

設計用途：
  捕捉「強勢股拉回 VWAP/EMA20 不破，繼續上攻」的二次進場機會。
  解決現有策略「錯過第一波就放棄」的痛點。

進場邏輯（多單，空單對稱）：
  1. 趨勢分數 >= min_trend_score（已經是強勢股）
  2. 之前已經漲過 ≥ pullback_from_high_pct（例如漲 2% 以上）
  3. 最近一根 K 棒回踩到 VWAP 或 EMA20 附近（差距 ≤ touch_pct）
  4. 回踩當下不破（low 沒跌破，且 close > VWAP/EMA20）
  5. 量縮（回踩量 < 均量 × 0.8）→ 確認是「弱勢回踩」非真跌
  6. 當下出現「反彈訊號」：close > open（紅K）
  7. RSI 仍 > 50（多頭格局未破）

停損：回踩低點 -0.3%
停利：前波高點（1R 預估）+ 突破前高後的 2R

優勢：
  - 比追突破更安全（已有支撐確認）
  - 比逆勢逢低買更穩（趨勢分數已篩過）
  - 適合錯過第一波後的二次機會
"""
from __future__ import annotations

from src.data.cache import IntraDayCache
from src.signals.trend_score import calc_trend_score
from src.strategies.base import BaseStrategy, Direction, Signal, SignalType


def _calc_rsi(closes: list[float], period: int = 6) -> float | None:
    """簡單 RSI 計算（同 orb_breakout.py 的版本）。"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-period - 1 + i] - closes[-period - 1 + i - 1]
        if diff >= 0:
            gains.append(diff); losses.append(0.0)
        else:
            gains.append(0.0); losses.append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 2)


class TrendPullbackStrategy(BaseStrategy):
    """趨勢回踩進場策略 — 撿二次進場機會。

    預設參數：
        min_trend_score        float  趨勢分數門檻       預設 6.0
        pullback_from_high_pct float  從前波高點回檔幅度  預設 0.005 (0.5%)
        touch_pct              float  接觸 VWAP/EMA 範圍 預設 0.005 (0.5%)
        max_pullback_pct       float  最大回檔幅度       預設 0.015 (1.5%)
        vol_shrink_ratio       float  回踩量縮比         預設 0.8
        end_minute             int    最晚進場時點       預設 180 (12:00)
        market_symbol          str    大盤代號           預設 "TAIEX"
    """

    name = "趨勢回踩進場"

    def __init__(self, cache: IntraDayCache, params: dict | None = None):
        super().__init__(cache, params)
        # 每檔股票每方向只發一次（避免重複進場）
        self._fired: dict[str, set[Direction]] = {}

    def reset(self) -> None:
        self._fired.clear()

    def generate_signal(self, symbol: str, stock_name: str) -> Signal | None:
        bar = self.cache.get_last_bar(symbol)
        if bar is None:
            return None

        bars = self.cache.get_bars(symbol)
        if len(bars) < 10:
            return None  # 至少要 10 根 K 棒才有意義

        # ── 時段限制 ──
        t = bar.timestamp
        open_minutes = (t.hour - 9) * 60 + t.minute
        end_min = self._param("end_minute", 180)
        if open_minutes < 30 or open_minutes > end_min:
            return None

        # ── 讀取參數 ──
        min_trend_score:        float = self._param("min_trend_score", 6.0)
        pullback_from_high_pct: float = self._param("pullback_from_high_pct", 0.005)
        touch_pct:              float = self._param("touch_pct", 0.005)
        max_pullback_pct:       float = self._param("max_pullback_pct", 0.015)
        vol_shrink_ratio:       float = self._param("vol_shrink_ratio", 0.8)
        market_symbol:          str   = self._param("market_symbol", "TAIEX")

        close = bar.close

        # ── 計算當日 high/low（自開盤）──
        today_high = max(b.high for b in bars)
        today_low  = min(b.low for b in bars)

        # ── 大盤方向 ──
        mkt_bars = self.cache.get_bars(market_symbol)
        mkt_change = 0.0
        if mkt_bars and mkt_bars[0].open > 0:
            mkt_change = (mkt_bars[-1].close / mkt_bars[0].open) - 1.0

        # ── VWAP / EMA 參考點 ──
        vwap_state = self.cache.get_vwap(symbol)
        vwap = vwap_state.vwap if vwap_state.vwap > 0 else close

        # ── 平均量（過濾量縮）──
        avg_vol = self.cache.get_recent_volume(symbol, n_bars=10)
        if avg_vol == 0:
            return None

        # ── RSI ──
        closes_for_rsi = [b.close for b in bars[-12:]]
        rsi = _calc_rsi(closes_for_rsi, 6)
        if rsi is None:
            return None

        fired = self._fired.setdefault(symbol, set())

        # ── 計算趨勢分數 ──
        trend_long  = calc_trend_score(self.cache, symbol, Direction.LONG)
        trend_short = calc_trend_score(self.cache, symbol, Direction.SHORT)

        # ===== 多單：強勢股回踩 VWAP 不破 =====
        if (
            Direction.LONG not in fired
            and trend_long.score >= min_trend_score
            and mkt_change >= -0.003                      # 大盤沒大跌
            and rsi >= 50                                  # 仍在多頭格局
        ):
            # 條件 A：已從低點漲過 pullback_from_high_pct（之前是強勢）
            range_from_low = (today_high - today_low) / today_low if today_low > 0 else 0.0
            had_run = range_from_low >= pullback_from_high_pct

            # 條件 B：當下價格接近 VWAP（差距 ≤ touch_pct）
            near_vwap = abs(close - vwap) / vwap <= touch_pct if vwap > 0 else False

            # 條件 C：回檔幅度合理（不能跌太深）
            pullback_pct = (today_high - close) / today_high if today_high > 0 else 0.0
            shallow_pullback = pullback_pct <= max_pullback_pct

            # 條件 D：當下不破 VWAP（價格 >= VWAP 或 low > VWAP × 0.997）
            not_broken = close >= vwap or bar.low >= vwap * 0.997

            # 條件 E：量縮回檔（量 < 均量 × 0.8）→ 賣壓不大
            vol_shrunk = bar.volume <= avg_vol * vol_shrink_ratio

            # 條件 F：當下是紅K（反彈訊號）
            bullish_bar = bar.close > bar.open

            if had_run and near_vwap and shallow_pullback and not_broken and vol_shrunk and bullish_bar:
                # 停損：回踩低點 -0.3%
                stop_loss = round(bar.low * 0.997, 2)
                R = close - stop_loss
                if R <= 0:
                    return None
                # 停利：前波高點（保守）
                take_profit = round(min(today_high * 1.005, close + 2 * R), 2)

                fired.add(Direction.LONG)
                return Signal(
                    symbol=symbol, name=stock_name,
                    direction=Direction.LONG, signal_type=SignalType.ENTRY,
                    trigger_price=close, strategy_name=self.name,
                    stop_loss=stop_loss, take_profit=take_profit,
                    reason=(
                        f"回踩 VWAP {vwap:.2f}（差 {(close-vwap)/vwap:+.2%}）｜"
                        f"前波回檔 {pullback_pct:.2%}（淺）｜"
                        f"量縮 {bar.volume/avg_vol:.1f}x｜"
                        f"RSI={rsi}｜趨勢分 {trend_long.score:.1f}/10"
                    ),
                    extra={
                        "vwap": round(vwap, 2),
                        "today_high": round(today_high, 2),
                        "pullback_pct": round(pullback_pct * 100, 2),
                        "vol_ratio": round(bar.volume / avg_vol, 2),
                        "rsi": rsi,
                        "trend_score": round(trend_long.score, 1),
                        "trend_breakdown": trend_long.breakdown,
                        "market_change_pct": round(mkt_change * 100, 2),
                    },
                )

        # ===== 空單：弱勢股反彈到 VWAP 後失敗 =====
        if (
            Direction.SHORT not in fired
            and trend_short.score >= min_trend_score
            and mkt_change <= 0.003
            and rsi <= 50
        ):
            range_from_high = (today_high - today_low) / today_high if today_high > 0 else 0.0
            had_run = range_from_high >= pullback_from_high_pct

            near_vwap = abs(close - vwap) / vwap <= touch_pct if vwap > 0 else False
            rebound_pct = (close - today_low) / today_low if today_low > 0 else 0.0
            shallow_rebound = rebound_pct <= max_pullback_pct

            not_broken = close <= vwap or bar.high <= vwap * 1.003
            vol_shrunk = bar.volume <= avg_vol * vol_shrink_ratio
            bearish_bar = bar.close < bar.open

            if had_run and near_vwap and shallow_rebound and not_broken and vol_shrunk and bearish_bar:
                stop_loss = round(bar.high * 1.003, 2)
                R = stop_loss - close
                if R <= 0:
                    return None
                take_profit = round(max(today_low * 0.995, close - 2 * R), 2)

                fired.add(Direction.SHORT)
                return Signal(
                    symbol=symbol, name=stock_name,
                    direction=Direction.SHORT, signal_type=SignalType.ENTRY,
                    trigger_price=close, strategy_name=self.name,
                    stop_loss=stop_loss, take_profit=take_profit,
                    reason=(
                        f"反彈 VWAP {vwap:.2f} 失敗（差 {(close-vwap)/vwap:+.2%}）｜"
                        f"前波反彈 {rebound_pct:.2%}（淺）｜"
                        f"量縮 {bar.volume/avg_vol:.1f}x｜"
                        f"RSI={rsi}｜趨勢分 {trend_short.score:.1f}/10"
                    ),
                    extra={
                        "vwap": round(vwap, 2),
                        "today_low": round(today_low, 2),
                        "rebound_pct": round(rebound_pct * 100, 2),
                        "vol_ratio": round(bar.volume / avg_vol, 2),
                        "rsi": rsi,
                        "trend_score": round(trend_short.score, 1),
                        "trend_breakdown": trend_short.breakdown,
                        "market_change_pct": round(mkt_change * 100, 2),
                    },
                )

        return None
