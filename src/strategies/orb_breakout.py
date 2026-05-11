"""策略 A：早盤爆量突破（Opening Range Breakout, ORB-15）。

邏輯：
1. 09:00–09:14 持續更新開盤區間（OR）的最高/最低點
2. 09:15 後 OR 鎖定，等待突破
3. 多單條件（同時滿足）：
   - ORB 區間寬度 ≥ min_orb_pct（過濾太窄的假突破區間）
   - 大盤漲幅 ≥ market_long_threshold（方向鎖：盤漲才做多）
   - 1分K 收盤價 > OR 高點
   - 突破當根成交量 ≥ 開盤區間平均量 × volume_ratio
   - RSI(6) 介於 rsi_low ~ rsi_high
   - 個股漲幅 ≥ 大盤漲幅（相對強度為正）
4. 空單對稱反向（盤跌才做空）
5. 停損：OR 中點 or -stop_loss_pct 取較近者
6. 停利：trigger_price + profit_ratio × R
"""
from __future__ import annotations

from src.data.cache import IntraDayCache
from src.strategies.base import BaseStrategy, Direction, Signal, SignalType


def _calc_rsi(closes: list[float], period: int = 6) -> float | None:
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


def _market_change(cache: IntraDayCache, market_symbol: str) -> float:
    """取大盤今日漲跌幅（無資料時回傳 0）。"""
    bars = cache.get_bars(market_symbol)
    if not bars or bars[0].open == 0:
        return 0.0
    return (bars[-1].close / bars[0].open) - 1.0


class ORBBreakoutStrategy(BaseStrategy):
    """ORB-15 開盤區間突破策略（主力策略）。

    新增優化參數：
        min_orb_pct     float  ORB 最小寬度（相對昨收）  預設 0.008 (0.8%)
                               → 太窄的 ORB 假突破機率高，直接跳過
        market_long_th  float  大盤漲幅門檻才做多        預設 -0.003 (-0.3%)
                               → 大盤跌超過 -0.3% 時不做多
        market_short_th float  大盤跌幅門檻才做空        預設 0.003 (0.3%)
                               → 大盤漲超過 0.3% 時不做空
    """

    name = "ORB-15 開盤區間突破"

    def __init__(self, cache: IntraDayCache, params: dict | None = None):
        super().__init__(cache, params)
        self._fired: dict[str, set[Direction]] = {}

    def reset(self) -> None:
        self._fired.clear()

    def generate_signal(self, symbol: str, stock_name: str) -> Signal | None:
        orb = self.cache.get_orb(symbol)
        if not orb.is_valid:
            return None

        bar = self.cache.get_last_bar(symbol)
        if bar is None:
            return None

        # ── 讀取參數 ──
        volume_ratio: float  = self._param("volume_ratio", 1.5)
        rsi_period: int      = self._param("rsi_period", 6)
        rsi_low: float       = self._param("rsi_low", 40.0)
        rsi_high: float      = self._param("rsi_high", 85.0)
        stop_loss_pct: float = self._param("stop_loss_pct", 0.008)
        profit_ratio: float  = self._param("profit_ratio", 1.5)
        market_symbol: str   = self._param("market_symbol", "TAIEX")
        min_orb_pct: float   = self._param("min_orb_pct", 0.015)  # 回測最佳：1.5%
        market_long_th: float  = self._param("market_long_th", -0.003)
        market_short_th: float = self._param("market_short_th", 0.003)
        time_cutoff_hour: int  = self._param("time_cutoff_hour", 11)   # 11:00 後不開新倉
        time_cutoff_min: int   = self._param("time_cutoff_min", 0)

        close = bar.close
        current_vol = bar.volume
        all_bars = self.cache.get_bars(symbol)

        # ── 時段過濾：11:00 後不開新倉 ──
        t = bar.timestamp
        if (t.hour, t.minute) >= (time_cutoff_hour, time_cutoff_min):
            return None

        # ── 優化 1：ORB 最小寬度過濾 ──
        # 太窄的區間（< 0.8% 昨收）代表今天開盤很平，突破容易是假突破
        prev_close = all_bars[0].open if all_bars else 0.0
        orb_width_pct = (orb.high - orb.low) / prev_close if prev_close > 0 else 0.0
        if orb_width_pct < min_orb_pct:
            return None   # 區間太窄，跳過

        # ── 優化 2：大盤方向鎖 ──
        mkt_change = _market_change(self.cache, market_symbol)
        # 大盤方向不明確（震盪）時，兩邊都可做；有明確方向時只做順向
        allow_long  = mkt_change >= market_long_th    # 大盤沒跌太多 → 可做多
        allow_short = mkt_change <= market_short_th   # 大盤沒漲太多 → 可做空

        # ── ORB 均量 & RSI ──
        orb_bars = [b for b in all_bars if b.timestamp < orb.formed_at] if orb.formed_at else []
        if not orb_bars:
            return None
        avg_orb_vol = sum(b.volume for b in orb_bars) / len(orb_bars)

        closes = [b.close for b in all_bars[-(rsi_period + 5):]]
        rsi = _calc_rsi(closes, rsi_period)

        # ── 相對強度 ──
        first_bar = all_bars[0] if all_bars else None
        stock_rs = (close / first_bar.open - 1) if first_bar and first_bar.open else 0.0
        market_bar = self.cache.get_last_bar(market_symbol)
        market_first = self.cache.get_bars(market_symbol, n=1)
        if market_bar and market_first:
            market_rs = (market_bar.close / market_first[0].open - 1) if market_first[0].open else 0.0
        else:
            market_rs = 0.0

        fired_directions = self._fired.setdefault(symbol, set())

        # ── 今日累計量動能：開盤至今的總量應超過 ORB 均量 × bars數量（代表量能持續）
        total_vol_today = sum(b.volume for b in all_bars)
        avg_bar_vol = self.cache.get_recent_volume(symbol, n_bars=20)
        # 今日累計量 > 同期均量 × 1.2 → 今天是活躍日
        n_bars_so_far = len(all_bars)
        is_active_day = (avg_bar_vol == 0) or (total_vol_today >= avg_bar_vol * n_bars_so_far * 0.8)

        # ===== 多單條件 =====
        long_cond = (
            allow_long
            and close > orb.high
            and current_vol >= avg_orb_vol * volume_ratio
            and is_active_day                             # 今天量能不能太差
            and rsi is not None and rsi_low <= rsi <= rsi_high
            and stock_rs >= market_rs
            and Direction.LONG not in fired_directions
        )

        if long_cond:
            stop_loss = max(orb.midpoint, close * (1 - stop_loss_pct))
            r = close - stop_loss
            take_profit = round(close + profit_ratio * r, 2)
            self._fired[symbol].add(Direction.LONG)
            return Signal(
                symbol=symbol, name=stock_name,
                direction=Direction.LONG, signal_type=SignalType.ENTRY,
                trigger_price=close, strategy_name=self.name,
                stop_loss=round(stop_loss, 2), take_profit=take_profit,
                reason=(
                    f"突破 OR {orb.high}｜寬度 {orb_width_pct:.1%}｜"
                    f"量比 {current_vol/avg_orb_vol:.1f}x｜RSI={rsi}｜"
                    f"大盤 {mkt_change:+.2%}"
                ),
                extra={
                    "orb_high": orb.high, "orb_low": orb.low,
                    "orb_width_pct": round(orb_width_pct * 100, 2),
                    "volume_ratio": round(current_vol / avg_orb_vol, 2),
                    "rsi": rsi,
                    "market_change_pct": round(mkt_change * 100, 2),
                    "stock_rs_pct": round(stock_rs * 100, 2),
                },
            )

        # ===== 空單條件（對稱反向）=====
        short_cond = (
            allow_short
            and close < orb.low
            and current_vol >= avg_orb_vol * volume_ratio
            and is_active_day
            and rsi is not None and (100 - rsi_high) <= rsi <= (100 - rsi_low)
            and stock_rs <= market_rs
            and Direction.SHORT not in fired_directions
        )

        if short_cond:
            stop_loss = min(orb.midpoint, close * (1 + stop_loss_pct))
            r = stop_loss - close
            take_profit = round(close - profit_ratio * r, 2)
            self._fired[symbol].add(Direction.SHORT)
            return Signal(
                symbol=symbol, name=stock_name,
                direction=Direction.SHORT, signal_type=SignalType.ENTRY,
                trigger_price=close, strategy_name=self.name,
                stop_loss=round(stop_loss, 2), take_profit=take_profit,
                reason=(
                    f"跌破 OR {orb.low}｜寬度 {orb_width_pct:.1%}｜"
                    f"量比 {current_vol/avg_orb_vol:.1f}x｜RSI={rsi}｜"
                    f"大盤 {mkt_change:+.2%}"
                ),
                extra={
                    "orb_high": orb.high, "orb_low": orb.low,
                    "orb_width_pct": round(orb_width_pct * 100, 2),
                    "volume_ratio": round(current_vol / avg_orb_vol, 2),
                    "rsi": rsi,
                    "market_change_pct": round(mkt_change * 100, 2),
                    "stock_rs_pct": round(stock_rs * 100, 2),
                },
            )

        return None
