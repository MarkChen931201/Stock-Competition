"""量能與波動度指標 (給盤前 screener 與盤中策略共用)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def average_true_range(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR (Wilder). 需要欄位 max(high), min(low), close."""
    high = df["max"] if "max" in df else df["high"]
    low = df["min"] if "min" in df else df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Wilder smoothing
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def avg_volume_lots(df: pd.DataFrame, period: int = 20) -> float:
    """平均成交張數 (FinMind 的 Trading_Volume 單位為股, 需 / 1000)."""
    if "Trading_Volume" in df:
        vol_shares = df["Trading_Volume"].tail(period).astype(float)
    elif "volume" in df:
        vol_shares = df["volume"].tail(period).astype(float)
    else:
        return float("nan")
    return float((vol_shares / 1_000).mean())


def turnover_rate(df: pd.DataFrame, shares_outstanding: int | None) -> float:
    """週轉率 = 期間平均成交股數 / 流通股數. 若沒有 shares_outstanding 用近似法."""
    if shares_outstanding is None or shares_outstanding <= 0:
        return float("nan")
    if "Trading_Volume" in df:
        avg_vol_shares = float(df["Trading_Volume"].mean())
    elif "volume" in df:
        avg_vol_shares = float(df["volume"].mean()) * 1_000  # 若是張轉股
    else:
        return float("nan")
    return avg_vol_shares / shares_outstanding


def atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    """ATR / Close 的百分比 (近一期). 用來衡量日內波動度."""
    if len(df) < period + 1:
        return float("nan")
    atr = average_true_range(df, period).iloc[-1]
    last_close = float(df["close"].iloc[-1])
    if last_close <= 0 or np.isnan(atr):
        return float("nan")
    return float(atr / last_close)
