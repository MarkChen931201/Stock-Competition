"""量能指標與 ATR 計算的單元測試."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.indicators.volume import (
    atr_pct,
    average_true_range,
    avg_volume_lots,
    turnover_rate,
)


def make_ohlcv(n: int = 30, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0.5, 2, n)
    low = close - rng.uniform(0.5, 2, n)
    vol = rng.integers(5_000_000, 30_000_000, n)  # 股
    return pd.DataFrame(
        {
            "max": high,
            "min": low,
            "close": close,
            "Trading_Volume": vol,
        }
    )


def test_atr_positive():
    df = make_ohlcv()
    atr = average_true_range(df, 14)
    assert atr.iloc[-1] > 0


def test_atr_pct_in_reasonable_range():
    df = make_ohlcv()
    val = atr_pct(df, 14)
    assert 0 < val < 0.5  # 隨機資料的 ATR% 不該超過 50%


def test_avg_volume_lots_units():
    """Trading_Volume 單位是股, 平均量(張) 應是平均股 / 1000."""
    df = make_ohlcv()
    expected = float(df["Trading_Volume"].tail(20).mean() / 1_000)
    assert avg_volume_lots(df, 20) == pytest.approx(expected)


def test_turnover_rate_returns_nan_when_no_shares():
    df = make_ohlcv()
    assert np.isnan(turnover_rate(df, None))
    assert np.isnan(turnover_rate(df, 0))


def test_turnover_rate_basic():
    df = make_ohlcv()
    avg_shares = float(df["Trading_Volume"].mean())
    shares = 1_000_000_000  # 10 億股
    expected = avg_shares / shares
    assert turnover_rate(df, shares) == expected
