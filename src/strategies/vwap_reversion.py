"""策略 B：VWAP 偏離回歸（震盪日輔助策略）。

邏輯：
- 計算盤中 VWAP 與 ±sigma σ 通道（滾動 20 根 bar 標準差）
- 多單：價格跌至 VWAP - sigma*σ + 下影線 K + 量縮 → 做多回歸 VWAP
- 空單：價格漲至 VWAP + sigma*σ + 上影線 K + 量縮 → 做空回歸 VWAP
- 限制：僅在大盤指數漲跌幅 ≤ market_flat_pct（預設 ±0.5%）時啟用
  （趨勢盤關閉，避免逆勢被夾殺）
- 停損：突破 ±2σ 通道（更極端的偏離）
- 停利：回到 VWAP（或 VWAP ± 0.3σ 留安全邊際）
"""
from __future__ import annotations

from src.data.cache import IntraDayCache
from src.strategies.base import BaseStrategy, Direction, Signal, SignalType


def _is_lower_shadow(bar) -> bool:
    """判斷是否有明顯下影線（下影線長度 > 實體的 1.5 倍）。"""
    body = abs(bar.close - bar.open)
    lower_shadow = min(bar.open, bar.close) - bar.low
    if body == 0:
        return lower_shadow > 0
    return lower_shadow > body * 1.5


def _is_upper_shadow(bar) -> bool:
    """判斷是否有明顯上影線（上影線長度 > 實體的 1.5 倍）。"""
    body = abs(bar.close - bar.open)
    upper_shadow = bar.high - max(bar.open, bar.close)
    if body == 0:
        return upper_shadow > 0
    return upper_shadow > body * 1.5


class VWAPReversionStrategy(BaseStrategy):
    """VWAP 偏離回歸策略（補 ORB 沒訊號的震盪盤）。

    預設參數（可透過 params dict 覆蓋）：
        sigma           float  觸發偏離的標準差倍數      預設 1.5
        stop_sigma      float  停損的標準差倍數          預設 2.0
        vol_shrink_ratio float  量縮判斷：當根量 / 近N均量  預設 0.7
        vol_ref_bars    int    量縮參考 bar 數           預設 10
        market_flat_pct float  大盤視為震盪的漲跌幅上限  預設 0.005 (0.5%)
        market_symbol   str    大盤代號                  預設 "TAIEX"
        min_bars        int    至少需要幾根 bar 才啟動    預設 30
    """

    name = "VWAP 偏離回歸"

    def __init__(self, cache: IntraDayCache, params: dict | None = None):
        super().__init__(cache, params)
        self._fired: dict[str, set[Direction]] = {}

    def reset(self) -> None:
        self._fired.clear()

    def _market_is_flat(self) -> bool:
        """判斷大盤是否為震盪盤（漲跌幅在 ±market_flat_pct 內）。
        VWAP 回歸策略只在震盪盤啟動，趨勢盤逆勢回歸容易被夾殺。
        """
        market_symbol: str = self._param("market_symbol", "TAIEX")
        market_flat_pct: float = self._param("market_flat_pct", 0.008)  # 放寬至 ±0.8%

        market_bars = self.cache.get_bars(market_symbol)
        if not market_bars or market_bars[0].open == 0:
            return True  # 無大盤資料時不限制

        first_open = market_bars[0].open
        last_close = market_bars[-1].close
        change_pct = abs(last_close / first_open - 1)
        return change_pct <= market_flat_pct

    def generate_signal(self, symbol: str, stock_name: str) -> Signal | None:
        # --- 大盤趨勢過濾 ---
        if not self._market_is_flat():
            return None

        # --- 基本資料檢查 ---
        min_bars: int = self._param("min_bars", 30)
        all_bars = self.cache.get_bars(symbol)
        if len(all_bars) < min_bars:
            return None

        bar = self.cache.get_last_bar(symbol)
        if bar is None:
            return None

        vwap_state = self.cache.get_vwap(symbol)
        vwap = vwap_state.vwap
        std = vwap_state.std
        if vwap == 0 or std == 0:
            return None

        # --- 讀取參數 ---
        sigma: float = self._param("sigma", 1.0)            # 降低：軌道更窄，更容易觸及
        stop_sigma: float = self._param("stop_sigma", 1.5)
        vol_shrink_ratio: float = self._param("vol_shrink_ratio", 0.8)  # 稍微放寬量縮判斷
        vol_ref_bars: int = self._param("vol_ref_bars", 10)

        lower_band = vwap_state.lower_band(sigma)
        upper_band = vwap_state.upper_band(sigma)
        stop_lower = vwap_state.lower_band(stop_sigma)
        stop_upper = vwap_state.upper_band(stop_sigma)

        # 量縮判斷：當根量 < 近 N 根均量 × vol_shrink_ratio
        avg_vol = self.cache.get_recent_volume(symbol, vol_ref_bars)
        is_vol_shrink = avg_vol > 0 and bar.volume < avg_vol * vol_shrink_ratio

        fired_directions = self._fired.setdefault(symbol, set())

        # ===== 多單：跌至下軌 + 下影線 + 量縮 =====
        long_cond = (
            bar.low <= lower_band           # 觸及下軌
            and bar.close > lower_band      # 收盤拉回下軌以上（未收破）
            and _is_lower_shadow(bar)       # 有明顯下影線
            and is_vol_shrink               # 量縮（賣壓衰竭）
            and Direction.LONG not in fired_directions
        )

        if long_cond:
            self._fired[symbol].add(Direction.LONG)
            return Signal(
                symbol=symbol,
                name=stock_name,
                direction=Direction.LONG,
                signal_type=SignalType.ENTRY,
                trigger_price=bar.close,
                strategy_name=self.name,
                stop_loss=round(stop_lower, 2),
                take_profit=round(vwap, 2),
                reason=(
                    f"觸及 VWAP-{sigma}σ={lower_band:.2f}｜"
                    f"下影線｜量縮 {bar.volume/avg_vol:.1%}｜"
                    f"目標 VWAP={vwap:.2f}"
                ),
                extra={
                    "vwap": vwap,
                    "lower_band": lower_band,
                    "upper_band": upper_band,
                    "std": round(std, 3),
                    "vol_ratio": round(bar.volume / avg_vol, 2) if avg_vol else None,
                },
            )

        # ===== 空單：漲至上軌 + 上影線 + 量縮 =====
        short_cond = (
            bar.high >= upper_band
            and bar.close < upper_band
            and _is_upper_shadow(bar)
            and is_vol_shrink
            and Direction.SHORT not in fired_directions
        )

        if short_cond:
            self._fired[symbol].add(Direction.SHORT)
            return Signal(
                symbol=symbol,
                name=stock_name,
                direction=Direction.SHORT,
                signal_type=SignalType.ENTRY,
                trigger_price=bar.close,
                strategy_name=self.name,
                stop_loss=round(stop_upper, 2),
                take_profit=round(vwap, 2),
                reason=(
                    f"觸及 VWAP+{sigma}σ={upper_band:.2f}｜"
                    f"上影線｜量縮 {bar.volume/avg_vol:.1%}｜"
                    f"目標 VWAP={vwap:.2f}"
                ),
                extra={
                    "vwap": vwap,
                    "lower_band": lower_band,
                    "upper_band": upper_band,
                    "std": round(std, 3),
                    "vol_ratio": round(bar.volume / avg_vol, 2) if avg_vol else None,
                },
            )

        return None
