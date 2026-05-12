"""趨勢分數計算引擎 — 多維度評估個股的當前趨勢強度。

評分維度（總分 10 分）：
  1. VWAP 位置          0~2 分    價格相對 VWAP 的偏離程度
  2. EMA 排列           0~2 分    EMA(5/10/20) 多頭/空頭排列
  3. ROC 速度           0~2 分    最近 5 根 bar 的漲跌速度
  4. 動量加速度         0~2 分    速度的二階導數（加速 vs 減速）
  5. 連續同向 K 棒      0~2 分    最近連續紅K/黑K 數量

用途：
  - 各策略都可呼叫 calc_trend_score(symbol, direction) 過濾低分訊號
  - SignalScorer 用此分數作為「趨勢」維度（新增第 8 個維度）

設計原則：
  - 純 stateless：每次呼叫都重新計算，不維護狀態
  - 對稱：多單看正向、空單看負向
  - 分數越高 = 趨勢越強且方向越明確
"""
from __future__ import annotations

from dataclasses import dataclass

from src.data.cache import IntraDayCache
from src.strategies.base import Direction


@dataclass
class TrendResult:
    """單檔股票的趨勢評估結果。"""
    score: float                     # 總分 0~10
    breakdown: dict                  # 各維度明細
    vwap_position_pct: float         # 價格相對 VWAP 偏離百分比
    ema5: float
    ema10: float
    ema20: float
    roc_5bar_pct: float              # 5 根 bar 的 ROC（%）
    acceleration: float              # ROC 變化率（>0 = 加速）
    consec_same_dir: int             # 連續同向 K 棒數
    direction_clear: bool            # 方向是否明確（>= 5 分）


def _ema(prices: list[float], period: int) -> float:
    """簡單 EMA 計算（用 SMA 當作 seed）。"""
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    multiplier = 2 / (period + 1)
    # 用前 period 個的 SMA 當 seed
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calc_trend_score(
    cache: IntraDayCache,
    symbol: str,
    direction: Direction,
) -> TrendResult:
    """計算指定方向的趨勢分數。

    Args:
        cache:     盤中記憶體快取
        symbol:    股票代號
        direction: 評估方向（多單看上漲動能，空單看下跌動能）

    Returns:
        TrendResult，含總分 + 各維度明細
    """
    bars = cache.get_bars(symbol)
    if len(bars) < 5:
        # 資料不足，給中性分數
        return TrendResult(
            score=0.0, breakdown={"資料不足": 0.0},
            vwap_position_pct=0.0, ema5=0.0, ema10=0.0, ema20=0.0,
            roc_5bar_pct=0.0, acceleration=0.0, consec_same_dir=0,
            direction_clear=False,
        )

    closes = [b.close for b in bars]
    last_close = closes[-1]
    is_long = direction == Direction.LONG

    # ── 維度 1：VWAP 位置（0~2 分）──
    vwap_state = cache.get_vwap(symbol)
    vwap = vwap_state.vwap if vwap_state.vwap > 0 else last_close
    vwap_dev = (last_close - vwap) / vwap if vwap > 0 else 0.0
    # 多單：價格在 VWAP 上方 0.5% 以上 = 強勢
    # 空單：價格在 VWAP 下方 0.5% 以上 = 弱勢
    if is_long:
        if vwap_dev >= 0.01:    vwap_score = 2.0    # >1% 在 VWAP 上方
        elif vwap_dev >= 0.003: vwap_score = 1.5
        elif vwap_dev >= 0:     vwap_score = 1.0
        elif vwap_dev >= -0.003: vwap_score = 0.5
        else:                   vwap_score = 0.0
    else:
        if vwap_dev <= -0.01:    vwap_score = 2.0
        elif vwap_dev <= -0.003: vwap_score = 1.5
        elif vwap_dev <= 0:      vwap_score = 1.0
        elif vwap_dev <= 0.003:  vwap_score = 0.5
        else:                    vwap_score = 0.0

    # ── 維度 2：EMA 排列（0~2 分）──
    ema5  = _ema(closes, 5)
    ema10 = _ema(closes, 10) if len(closes) >= 10 else ema5
    ema20 = _ema(closes, 20) if len(closes) >= 20 else ema10

    if is_long:
        # 多頭排列：價格 > EMA5 > EMA10 > EMA20
        if last_close > ema5 > ema10 > ema20:         ema_score = 2.0
        elif last_close > ema5 > ema10:               ema_score = 1.5
        elif last_close > ema5:                       ema_score = 1.0
        elif last_close > ema10:                      ema_score = 0.5
        else:                                         ema_score = 0.0
    else:
        # 空頭排列：價格 < EMA5 < EMA10 < EMA20
        if last_close < ema5 < ema10 < ema20:         ema_score = 2.0
        elif last_close < ema5 < ema10:               ema_score = 1.5
        elif last_close < ema5:                       ema_score = 1.0
        elif last_close < ema10:                      ema_score = 0.5
        else:                                         ema_score = 0.0

    # ── 維度 3：ROC 速度（0~2 分）──
    # 最近 5 根 bar 的 ROC（漲跌速度）
    if len(closes) >= 6:
        roc_5 = (closes[-1] - closes[-6]) / closes[-6] if closes[-6] > 0 else 0.0
    else:
        roc_5 = 0.0

    roc_pct = roc_5 * 100  # 轉成百分比
    if is_long:
        if roc_pct >= 1.5:    roc_score = 2.0    # 5 根漲 >1.5%
        elif roc_pct >= 0.8:  roc_score = 1.5
        elif roc_pct >= 0.3:  roc_score = 1.0
        elif roc_pct >= 0:    roc_score = 0.5
        else:                 roc_score = 0.0
    else:
        if roc_pct <= -1.5:   roc_score = 2.0
        elif roc_pct <= -0.8: roc_score = 1.5
        elif roc_pct <= -0.3: roc_score = 1.0
        elif roc_pct <= 0:    roc_score = 0.5
        else:                 roc_score = 0.0

    # ── 維度 4：動量加速度（0~2 分）──
    # 比較「前 5 根 ROC」與「最新 5 根 ROC」，加速 = 正、減速 = 負
    if len(closes) >= 11:
        prev_roc_5 = (closes[-6] - closes[-11]) / closes[-11] if closes[-11] > 0 else 0.0
        acceleration = roc_5 - prev_roc_5
    else:
        acceleration = 0.0

    accel_pct = acceleration * 100
    if is_long:
        # 多單：加速度為正（越漲越快）= 最佳
        if accel_pct >= 0.5:    accel_score = 2.0    # 強加速
        elif accel_pct >= 0.1:  accel_score = 1.5
        elif accel_pct >= 0:    accel_score = 1.0    # 至少沒減速
        elif accel_pct >= -0.3: accel_score = 0.5
        else:                   accel_score = 0.0    # 大幅減速
    else:
        if accel_pct <= -0.5:   accel_score = 2.0
        elif accel_pct <= -0.1: accel_score = 1.5
        elif accel_pct <= 0:    accel_score = 1.0
        elif accel_pct <= 0.3:  accel_score = 0.5
        else:                   accel_score = 0.0

    # ── 維度 5：連續同向 K 棒（0~2 分）──
    consec = 0
    for b in reversed(bars):
        if is_long and b.close >= b.open:
            consec += 1
        elif not is_long and b.close <= b.open:
            consec += 1
        else:
            break

    if consec >= 5:    consec_score = 2.0
    elif consec >= 3:  consec_score = 1.5
    elif consec >= 2:  consec_score = 1.0
    elif consec >= 1:  consec_score = 0.5
    else:              consec_score = 0.0

    # ── 加總 ──
    breakdown = {
        "VWAP位置":   round(vwap_score, 1),
        "EMA排列":    round(ema_score, 1),
        "ROC速度":    round(roc_score, 1),
        "加速度":     round(accel_score, 1),
        "連續K":      round(consec_score, 1),
    }
    total = sum(breakdown.values())

    return TrendResult(
        score=total,
        breakdown=breakdown,
        vwap_position_pct=round(vwap_dev * 100, 2),
        ema5=round(ema5, 2),
        ema10=round(ema10, 2),
        ema20=round(ema20, 2),
        roc_5bar_pct=round(roc_pct, 2),
        acceleration=round(accel_pct, 3),
        consec_same_dir=consec,
        direction_clear=total >= 5.0,
    )
