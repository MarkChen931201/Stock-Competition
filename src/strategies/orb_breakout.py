"""策略 A：早盤爆量突破（Opening Range Breakout, ORB-15）。

邏輯：
1. 09:00–09:14 持續更新開盤區間（OR）的最高/最低點
2. 09:15 後 OR 鎖定，等待突破
3. 多單條件（同時滿足）：
   - 1分K 收盤價 > OR 高點
   - 突破當根成交量 ≥ 開盤區間平均量 × volume_ratio（預設 1.5）
   - RSI(6) 介於 rsi_low ~ rsi_high（預設 50–80，避免過熱追高）
   - 個股漲幅 > 大盤漲幅（相對強度為正）
4. 空單對稱反向（收盤價 < OR 低點）
5. 停損：OR 中點 or -stop_loss_pct 取較近者
6. 停利：trigger_price + 1.5R（R = 進場價 - 停損）
"""
from __future__ import annotations

from src.data.cache import IntraDayCache
from src.data.fugle_client import Bar
from src.strategies.base import BaseStrategy, Direction, Signal, SignalType


def _calc_rsi(closes: list[float], period: int = 6) -> float | None:
    """計算 RSI(period)。closes 長度必須 > period。"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-period - 1 + i] - closes[-period - 1 + i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


class ORBBreakoutStrategy(BaseStrategy):
    """ORB-15 開盤區間突破策略（主力策略）。

    預設參數（可透過 params dict 覆蓋）：
        volume_ratio    float  突破量 / 開盤區間均量門檻  預設 1.5
        rsi_period      int    RSI 週期                   預設 6
        rsi_low         float  RSI 下限（避免過冷）        預設 50
        rsi_high        float  RSI 上限（避免追頂）        預設 80
        stop_loss_pct   float  最大停損比例               預設 0.008 (0.8%)
        profit_ratio    float  停利 R 倍數                預設 1.5
        market_symbol   str    大盤代號（相對強度比較用）  預設 "TAIEX"
    """

    name = "ORB-15 開盤區間突破"

    def __init__(self, cache: IntraDayCache, params: dict | None = None):
        super().__init__(cache, params)
        # 記錄已觸發過的方向，避免同支股票同方向重複發訊號
        self._fired: dict[str, set[Direction]] = {}

    def reset(self) -> None:
        """每日開盤前清除已觸發紀錄。"""
        self._fired.clear()

    def generate_signal(self, symbol: str, stock_name: str) -> Signal | None:
        orb = self.cache.get_orb(symbol)

        # ORB 尚未鎖定（還在 09:00–09:14）→ 不產訊號
        if not orb.is_valid:
            return None

        bar = self.cache.get_last_bar(symbol)
        if bar is None:
            return None

        # 取參數
        volume_ratio: float = self._param("volume_ratio", 0.8)   # 降低：低量日仍可捕捉相對爆量
        rsi_period: int = self._param("rsi_period", 6)
        rsi_low: float = self._param("rsi_low", 40.0)            # 放寬：RSI 過濾條件鬆開
        rsi_high: float = self._param("rsi_high", 85.0)
        stop_loss_pct: float = self._param("stop_loss_pct", 0.008)
        profit_ratio: float = self._param("profit_ratio", 1.5)

        close = bar.close
        current_vol = bar.volume

        # --- 計算開盤區間平均量 ---
        all_bars = self.cache.get_bars(symbol)
        orb_bars = [b for b in all_bars if b.timestamp < orb.formed_at] if orb.formed_at else []
        if not orb_bars:
            return None
        avg_orb_vol = sum(b.volume for b in orb_bars) / len(orb_bars)

        # --- 計算 RSI ---
        closes = [b.close for b in all_bars[-(rsi_period + 5):]]
        rsi = _calc_rsi(closes, rsi_period)

        # --- 相對強度：個股漲幅 vs 大盤 ---
        first_bar = all_bars[0] if all_bars else None
        stock_rs = (close / first_bar.open - 1) if first_bar and first_bar.open else 0.0

        # 大盤相對強度（取 TAIEX，若無資料則跳過此條件）
        market_symbol = self._param("market_symbol", "TAIEX")
        market_bar = self.cache.get_last_bar(market_symbol)
        market_first = self.cache.get_bars(market_symbol, n=1)
        if market_bar and market_first:
            market_rs = (market_bar.close / market_first[0].open - 1) if market_first[0].open else 0.0
        else:
            market_rs = 0.0  # 無大盤資料時放寬此條件

        fired_directions = self._fired.setdefault(symbol, set())

        # ===== 多單條件 =====
        long_cond = (
            close > orb.high                          # 突破 OR 高點
            and current_vol >= avg_orb_vol * volume_ratio  # 爆量
            and rsi is not None and rsi_low <= rsi <= rsi_high  # RSI 合理
            and stock_rs >= market_rs                 # 相對強度為正
            and Direction.LONG not in fired_directions
        )

        if long_cond:
            stop_loss = max(orb.midpoint, close * (1 - stop_loss_pct))
            r = close - stop_loss
            take_profit = round(close + profit_ratio * r, 2)
            self._fired[symbol].add(Direction.LONG)

            return Signal(
                symbol=symbol,
                name=stock_name,
                direction=Direction.LONG,
                signal_type=SignalType.ENTRY,
                trigger_price=close,
                strategy_name=self.name,
                stop_loss=round(stop_loss, 2),
                take_profit=take_profit,
                reason=(
                    f"突破 OR 高點 {orb.high}｜"
                    f"量比 {current_vol / avg_orb_vol:.1f}x｜"
                    f"RSI({rsi_period})={rsi}"
                ),
                extra={
                    "orb_high": orb.high,
                    "orb_low": orb.low,
                    "volume_ratio": round(current_vol / avg_orb_vol, 2),
                    "rsi": rsi,
                    "stock_rs_pct": round(stock_rs * 100, 2),
                },
            )

        # ===== 空單條件（對稱反向）=====
        short_cond = (
            close < orb.low
            and current_vol >= avg_orb_vol * volume_ratio
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
                symbol=symbol,
                name=stock_name,
                direction=Direction.SHORT,
                signal_type=SignalType.ENTRY,
                trigger_price=close,
                strategy_name=self.name,
                stop_loss=round(stop_loss, 2),
                take_profit=take_profit,
                reason=(
                    f"跌破 OR 低點 {orb.low}｜"
                    f"量比 {current_vol / avg_orb_vol:.1f}x｜"
                    f"RSI({rsi_period})={rsi}"
                ),
                extra={
                    "orb_high": orb.high,
                    "orb_low": orb.low,
                    "volume_ratio": round(current_vol / avg_orb_vol, 2),
                    "rsi": rsi,
                    "stock_rs_pct": round(stock_rs * 100, 2),
                },
            )

        return None
