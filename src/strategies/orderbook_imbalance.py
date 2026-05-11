"""策略 C：委託簿失衡偵測（OBI Burst）— 僅發 WATCH 預警訊號。

邏輯：
- 連續 N 個快照的 OBI > obi_threshold（預設 0.6）→ 買盤異常壓倒
- 同時當日成交量位於前 top_vol_pct（預設 5%）→ 異常大量
- 發出 WATCH 訊號，由人工 confirm 後再手動下單
- 用途：抓「拉抬前 30 秒~1 分鐘的委單堆積」

注意：OBI 訊號雜訊高，故 signal_type = WATCH，不直接發 ENTRY。
"""
from __future__ import annotations

from collections import defaultdict, deque

from src.data.cache import IntraDayCache
from src.data.twse_client import OrderBook
from src.strategies.base import BaseStrategy, Direction, Signal, SignalType


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
        w_obi_threshold: float = self._param("w_obi_threshold", 0.4)   # 加權 OBI 門檻
        blr_threshold:   float = self._param("blr_threshold", 0.55)    # 買一壓力比門檻
        consecutive:     int   = self._param("consecutive", 2)
        top_vol_pct:     float = self._param("top_vol_pct", 0.15)
        cooldown_bars:   int   = self._param("cooldown_bars", 5)

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

        # ===== 買盤失衡（WATCH 多）=====
        # 條件：連續 N 次 weighted_obi > 門檻 AND 買一壓力比 > 門檻
        all_obi_high = all(obi >= w_obi_threshold for obi in history)
        if all_obi_high and blr >= blr_threshold and is_high_volume:
            self._cooldown[symbol] = current_bar_count
            avg_obi = sum(history) / len(history)
            return Signal(
                symbol=symbol,
                name=stock_name,
                direction=Direction.LONG,
                signal_type=SignalType.WATCH,
                trigger_price=bar.close,
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
                    "best_bid": book.best_bid,
                    "best_ask": book.best_ask,
                    "spread": book.spread,
                    "bar_volume": bar.volume,
                },
            )

        # ===== 賣盤失衡（WATCH 空）=====
        all_obi_low = all(obi <= -w_obi_threshold for obi in history)
        if all_obi_low and blr <= (1 - blr_threshold) and is_high_volume:
            self._cooldown[symbol] = current_bar_count
            avg_obi = sum(history) / len(history)
            return Signal(
                symbol=symbol,
                name=stock_name,
                direction=Direction.SHORT,
                signal_type=SignalType.WATCH,
                trigger_price=bar.close,
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
                },
            )

        return None
