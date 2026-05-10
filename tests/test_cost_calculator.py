"""損益兩平與成本計算的單元測試."""
from __future__ import annotations

import pytest

from src.risk.cost_calculator import (
    AssetType,
    LOT_SIZE,
    MIN_BROKER_FEE,
    calc_round_trip_cost,
    min_profitable_sell_price,
    round_to_tick,
    solve_breakeven_sell_price,
    tick_size,
)


def test_tick_size_table():
    assert tick_size(9.99) == 0.01
    assert tick_size(10) == 0.05
    assert tick_size(49.9) == 0.05
    assert tick_size(50) == 0.1
    assert tick_size(99.9) == 0.1
    assert tick_size(100) == 0.5
    assert tick_size(499.9) == 0.5
    assert tick_size(500) == 1.0
    assert tick_size(1500) == 5.0


def test_round_to_tick_ceil():
    assert round_to_tick(100.21, ceil_mode=True) == 100.5
    assert round_to_tick(100.5, ceil_mode=True) == 100.5
    assert round_to_tick(50.04, ceil_mode=True) == 50.1


def test_breakeven_stock_100():
    """買 100 元 1 張 (10 萬), 損益兩平賣價約需漲 ~0.43%."""
    be = solve_breakeven_sell_price(100.0, lots=1, asset=AssetType.STOCK)
    # tick=0.5 故會被向上取整到 100.5
    assert be == pytest.approx(100.5, abs=0.0001)
    # 真實損益兩平 (數學解) 約 100.435
    cost = calc_round_trip_cost(100.0, be, lots=1)
    assert cost.breakeven_pct >= 0.0042
    assert cost.breakeven_pct <= 0.0055


def test_breakeven_etf_lower_tax():
    """ETF 稅率 0.1%, 損益兩平門檻較低."""
    be_stock = solve_breakeven_sell_price(50.0, asset=AssetType.STOCK)
    be_etf = solve_breakeven_sell_price(50.0, asset=AssetType.ETF)
    assert be_etf <= be_stock


def test_round_trip_cost_components():
    """100 元 * 1000 股 = 10 萬, 買賣手續費應為 142.5 + sell_fee, tax = sell_notional * 0.0015."""
    cost = calc_round_trip_cost(100.0, 102.0, lots=1, asset=AssetType.STOCK)
    assert cost.buy_fee == pytest.approx(100_000 * 0.001425)
    assert cost.sell_fee == pytest.approx(102_000 * 0.001425)
    assert cost.tax == pytest.approx(102_000 * 0.0015)
    assert cost.total == pytest.approx(
        cost.buy_fee + cost.sell_fee + cost.tax
    )


def test_min_broker_fee_kicks_in_for_low_price():
    """買 5 元 1 張 = 5000 元, 0.1425% = 7.125 元 < 20, 應觸發 20 元低消."""
    cost = calc_round_trip_cost(5.0, 5.5, lots=1)
    assert cost.buy_fee == MIN_BROKER_FEE
    # 賣方 5500 * 0.001425 = 7.84 也 < 20, 也會觸發
    assert cost.sell_fee == MIN_BROKER_FEE


def test_min_profitable_sell_has_safety_margin():
    """安全邊際應比 breakeven 高至少 1 個 tick."""
    buy = 100.0
    be = solve_breakeven_sell_price(buy)
    target = min_profitable_sell_price(buy, safety_ticks=1)
    assert target >= be + tick_size(be) - 1e-9


def test_lot_size_constant():
    assert LOT_SIZE == 1000
