"""策略 C：委託簿失衡偵測（OBI Burst）— 發 WATCH 預警 + 完整交易計畫。

邏輯：
- 連續 N 個快照的 weighted_obi > 門檻 → 買盤異常壓倒
- 同時當日成交量位於前 top_vol_pct → 異常大量
- 發出 WATCH 訊號，附帶「交易執行卡」：
    進場區間、停損、停利 T1/T2、建議張數、有效時間（突破觸發價）

注意：OBI 訊號雜訊高，故 signal_type = WATCH，但給予明確指示，
人工只需「確認 + 下單」，不必再計算。
"""
from __future__ import annotations

from collections import defaultdict, deque

from src.data.cache import IntraDayCache
from src.data.twse_client import OrderBook
from src.strategies.base import BaseStrategy, Direction, Signal, SignalType

# 交易計畫常數
_MAX_RISK_NTD       = 100000    # 單筆最大風險（NT$ 10 萬，配合競賽 1000 萬資金）
_MAX_LOTS_PER_TRADE = 50        # 單筆最大張數（避免低價股暴量）
_LOT_SIZE           = 1000      # 1 張 = 1000 股
_COST_RATE          = 0.005     # 來回成本約 0.5%（手續費 + 證交稅）
_STOP_BUFFER        = 0.003     # 停損緩衝 0.3%
_ENTRY_BAND         = 0.005     # 進場區間 ±0.5%
_TRIGGER_BREAK_PCT  = 0.001     # 觸發價突破 0.1%
_VALID_MINUTES      = 5         # 有效時間 5 分鐘
_SWING_LOOKBACK     = 5         # 看最近 5 根 1分K 找 swing


def _build_trade_plan(
    direction: Direction,
    last_bar,
    book,
    recent_bars: list,
) -> dict:
    """依方向計算完整交易計畫。回傳 dict 給 signal.extra 使用。

    Args:
        direction:   做多/做空
        last_bar:    最近 1 根 1分K（用其 close/high/low）
        book:        最新 OrderBook（用 best_bid/best_ask）
        recent_bars: 最近 N 根 1分K（用來找 swing high/low）

    Returns:
        dict 含 entry_low/high、stop_loss、tp1/tp2、lots、trigger_break
    """
    best_bid = book.best_bid
    best_ask = book.best_ask
    close = last_bar.close

    if direction == Direction.LONG:
        # 進場區間：買一 ~ 賣一 +0.5%（控制不追太高）
        entry_low  = best_bid
        entry_high = round(best_ask * (1 + _ENTRY_BAND), 2)
        entry_mid  = (entry_low + entry_high) / 2

        # 停損：最近 N 根 bar 最低點 -0.3%
        swing_low = min(b.low for b in recent_bars)
        stop_loss = round(swing_low * (1 - _STOP_BUFFER), 2)

        # 觸發價：突破最近高點 +0.1%（避免假突破）
        swing_high = max(b.high for b in recent_bars)
        trigger_break = round(swing_high * (1 + _TRIGGER_BREAK_PCT), 2)

        # R = 進場 - 停損
        risk_per_share = entry_mid - stop_loss
        if risk_per_share <= 0:
            # 異常：停損高於進場，退而求其次用 close * 1%
            risk_per_share = close * 0.01
            stop_loss = round(entry_mid - risk_per_share, 2)

        tp1 = round(entry_mid + risk_per_share, 2)
        tp2 = round(entry_mid + risk_per_share * 2, 2)
    else:
        # 做空：進場區間 = 賣一 ~ 買一 -0.5%
        entry_high = best_ask
        entry_low  = round(best_bid * (1 - _ENTRY_BAND), 2)
        entry_mid  = (entry_low + entry_high) / 2

        swing_high = max(b.high for b in recent_bars)
        stop_loss = round(swing_high * (1 + _STOP_BUFFER), 2)

        swing_low = min(b.low for b in recent_bars)
        trigger_break = round(swing_low * (1 - _TRIGGER_BREAK_PCT), 2)

        risk_per_share = stop_loss - entry_mid
        if risk_per_share <= 0:
            risk_per_share = close * 0.01
            stop_loss = round(entry_mid + risk_per_share, 2)

        tp1 = round(entry_mid - risk_per_share, 2)
        tp2 = round(entry_mid - risk_per_share * 2, 2)

    # 建議張數：單筆風險 NT$5,000 / 每張風險金額
    # 每張風險 = 價差風險 × 1000 股 + 交易成本（買進金額 × 0.5%）
    price_risk_per_lot = risk_per_share * _LOT_SIZE
    cost_per_lot       = entry_mid * _LOT_SIZE * _COST_RATE
    total_risk_per_lot = price_risk_per_lot + cost_per_lot
    suggested_lots = max(1, int(_MAX_RISK_NTD / total_risk_per_lot)) if total_risk_per_lot > 0 else 1
    # 上限張數（避免低價股暴量超過資金部位）
    suggested_lots = min(suggested_lots, _MAX_LOTS_PER_TRADE)

    return {
        "entry_low":      entry_low,
        "entry_high":     entry_high,
        "entry_mid":      round(entry_mid, 2),
        "stop_loss":      stop_loss,
        "take_profit_1":  tp1,
        "take_profit_2":  tp2,
        "suggested_lots": suggested_lots,
        "trigger_break":  trigger_break,
        "valid_minutes":  _VALID_MINUTES,
        "risk_per_share": round(risk_per_share, 2),
        "risk_per_lot":   round(price_risk_per_lot + cost_per_lot, 0),
    }


class OBIBurstStrategy(BaseStrategy):
    """委託簿失衡預警策略。

    generate_signal() 由外部在每次 orderbook 更新後呼叫
    （與其他策略在 bar 更新後呼叫不同）。

    預設參數：
        obi_threshold    float  OBI 觸發門檻（買盤）    預設 0.6
        obi_neg_threshold float OBI 觸發門檻（賣盤）   預設 -0.6
        consecutive      int   連續幾次超過門檻才觸發   預設 3
        top_vol_pct      float 成交量百分位門檻         預設 0.05 (前5%)
        cooldown_bars    int   同股觸發後冷卻幾根 bar   預設 5
    """

    name = "OBI 委託簿失衡"

    def __init__(self, cache: IntraDayCache, params: dict | None = None):
        super().__init__(cache, params)
        # 各標的最近 N 次 OBI 值（滾動 deque）
        consecutive: int = params.get("consecutive", 3) if params else 3
        self._obi_history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=consecutive)
        )
        # 冷卻計時：上次觸發時的 bar 數量
        self._cooldown: dict[str, int] = {}

    def reset(self) -> None:
        self._obi_history.clear()
        self._cooldown.clear()

    def on_orderbook(self, book: OrderBook) -> None:
        """每次 orderbook 更新後記錄 OBI，由外部呼叫。"""
        self._obi_history[book.symbol].append(book.obi)

    def generate_signal(self, symbol: str, stock_name: str) -> Signal | None:
        """在每根 bar 更新後呼叫，結合 weighted_obi + best_level_ratio 判斷。"""
        w_obi_threshold: float = self._param("w_obi_threshold", 0.3)   # 放寬：0.4 → 0.3
        blr_threshold:   float = self._param("blr_threshold", 0.52)    # 放寬：0.55 → 0.52
        consecutive:     int   = self._param("consecutive", 2)
        top_vol_pct:     float = self._param("top_vol_pct", 0.25)      # 放寬：前 15% → 前 25%
        cooldown_bars:   int   = self._param("cooldown_bars", 3)       # 放寬：5 → 3 根

        history = self._obi_history.get(symbol)
        if not history or len(history) < consecutive:
            return None

        all_bars = self.cache.get_bars(symbol)
        current_bar_count = len(all_bars)
        last_fired = self._cooldown.get(symbol, 0)
        if current_bar_count - last_fired < cooldown_bars:
            return None

        bar = self.cache.get_last_bar(symbol)
        book = self.cache.get_last_book(symbol)
        if bar is None or book is None:
            return None

        # 用加權 OBI + 買一壓力比 取代原本的等權重 OBI
        w_obi = book.weighted_obi
        blr   = book.best_level_ratio

        all_volumes = [b.volume for b in all_bars]
        if not all_volumes:
            return None
        threshold_idx = int(len(sorted(all_volumes)) * (1 - top_vol_pct))
        vol_threshold = sorted(all_volumes)[threshold_idx]
        is_high_volume = bar.volume >= vol_threshold

        # 取最近 N 根 bar 用於計算 swing high/low
        recent_bars = all_bars[-_SWING_LOOKBACK:] if len(all_bars) >= _SWING_LOOKBACK else all_bars
        if not recent_bars:
            return None

        # ===== 買盤失衡（WATCH 多）=====
        # 條件：連續 N 次 weighted_obi > 門檻 AND 買一壓力比 > 門檻
        all_obi_high = all(obi >= w_obi_threshold for obi in history)
        if all_obi_high and blr >= blr_threshold and is_high_volume:
            self._cooldown[symbol] = current_bar_count
            avg_obi = sum(history) / len(history)

            # 計算完整交易計畫
            plan = _build_trade_plan(Direction.LONG, bar, book, recent_bars)

            return Signal(
                symbol=symbol,
                name=stock_name,
                direction=Direction.LONG,
                signal_type=SignalType.WATCH,
                trigger_price=bar.close,
                stop_loss=plan["stop_loss"],
                take_profit=plan["take_profit_1"],
                strategy_name=self.name,
                reason=(
                    f"加權OBI={w_obi:.2f}（連{consecutive}次≥{w_obi_threshold}）｜"
                    f"買一壓力比={blr:.1%}｜"
                    f"大量 {bar.volume:,} 股｜"
                    f"買一={book.best_bid} / 賣一={book.best_ask}"
                ),
                extra={
                    "obi_history": list(history),
                    "avg_obi": round(avg_obi, 3),
                    "weighted_obi": round(w_obi, 3),
                    "best_level_ratio": round(blr, 3),
                    "best_bid": book.best_bid,
                    "best_ask": book.best_ask,
                    "spread": book.spread,
                    "bar_volume": bar.volume,
                    # 交易計畫
                    **plan,
                },
            )

        # ===== 賣盤失衡（WATCH 空）=====
        all_obi_low = all(obi <= -w_obi_threshold for obi in history)
        if all_obi_low and blr <= (1 - blr_threshold) and is_high_volume:
            self._cooldown[symbol] = current_bar_count
            avg_obi = sum(history) / len(history)

            plan = _build_trade_plan(Direction.SHORT, bar, book, recent_bars)

            return Signal(
                symbol=symbol,
                name=stock_name,
                direction=Direction.SHORT,
                signal_type=SignalType.WATCH,
                trigger_price=bar.close,
                stop_loss=plan["stop_loss"],
                take_profit=plan["take_profit_1"],
                strategy_name=self.name,
                reason=(
                    f"加權OBI={w_obi:.2f}（連{consecutive}次≤{-w_obi_threshold}）｜"
                    f"買一壓力比={blr:.1%}｜"
                    f"大量 {bar.volume:,} 股｜"
                    f"買一={book.best_bid} / 賣一={book.best_ask}"
                ),
                extra={
                    "obi_history": list(history),
                    "avg_obi": round(avg_obi, 3),
                    "weighted_obi": round(w_obi, 3),
                    "best_level_ratio": round(blr, 3),
                    "best_bid": book.best_bid,
                    "best_ask": book.best_ask,
                    "spread": book.spread,
                    "bar_volume": bar.volume,
                    # 交易計畫
                    **plan,
                },
            )

        return None
