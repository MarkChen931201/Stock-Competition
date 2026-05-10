"""台股當沖交易成本計算。

規則:
- 手續費: 買賣雙邊 0.1425%, 單邊未滿 NT$20 以 NT$20 收
- 證交稅 (僅賣方): 股票當沖 0.15%, ETF 當沖/現股 0.1%
- 1 張 = 1000 股
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import ceil

LOT_SIZE = 1_000
BROKER_FEE_RATE = 0.001425
MIN_BROKER_FEE = 20.0
TAX_RATE_STOCK_DAYTRADE = 0.0015
TAX_RATE_ETF = 0.001


class AssetType(str, Enum):
    STOCK = "stock"
    ETF = "etf"


@dataclass(frozen=True)
class TradeCost:
    buy_fee: float
    sell_fee: float
    tax: float
    total: float
    breakeven_pct: float        # 賣價需要 / 買價 - 1 的最小幅度
    breakeven_sell_price: float # 含成本的損益兩平賣價


def _broker_fee(notional: float) -> float:
    return max(notional * BROKER_FEE_RATE, MIN_BROKER_FEE)


def _tax_rate(asset: AssetType) -> float:
    return TAX_RATE_ETF if asset == AssetType.ETF else TAX_RATE_STOCK_DAYTRADE


def calc_round_trip_cost(
    buy_price: float,
    sell_price: float,
    lots: int = 1,
    asset: AssetType = AssetType.STOCK,
) -> TradeCost:
    """計算一次完整當沖（先買後賣）的全部成本與損益兩平門檻。"""
    if buy_price <= 0 or sell_price <= 0 or lots <= 0:
        raise ValueError("price and lots must be positive")

    shares = lots * LOT_SIZE
    buy_notional = buy_price * shares
    sell_notional = sell_price * shares

    buy_fee = _broker_fee(buy_notional)
    sell_fee = _broker_fee(sell_notional)
    tax = sell_notional * _tax_rate(asset)

    total = buy_fee + sell_fee + tax
    breakeven_sell_price = solve_breakeven_sell_price(buy_price, lots, asset)
    breakeven_pct = breakeven_sell_price / buy_price - 1.0

    return TradeCost(
        buy_fee=buy_fee,
        sell_fee=sell_fee,
        tax=tax,
        total=total,
        breakeven_pct=breakeven_pct,
        breakeven_sell_price=breakeven_sell_price,
    )


def solve_breakeven_sell_price(
    buy_price: float,
    lots: int = 1,
    asset: AssetType = AssetType.STOCK,
) -> float:
    """解出損益兩平的賣出價。

    P_sell * S - (P_sell * S * fee_rate) - (P_sell * S * tax_rate)
        = P_buy * S + buy_fee
    => P_sell = (P_buy * S + buy_fee) / (S * (1 - fee_rate - tax_rate))

    若買方手續費觸及 NT$20 低消, 結果仍正確 (代入 max 後計算).
    """
    shares = lots * LOT_SIZE
    buy_notional = buy_price * shares
    buy_fee = _broker_fee(buy_notional)
    tax_rate = _tax_rate(asset)

    denom = shares * (1.0 - BROKER_FEE_RATE - tax_rate)
    raw_sell = (buy_notional + buy_fee) / denom

    # 賣方若也觸及 20 元低消（小資金或低價股）, 用迭代修正
    sell_notional = raw_sell * shares
    if sell_notional * BROKER_FEE_RATE < MIN_BROKER_FEE:
        # 賣方手續費為固定 20, 重新解析解
        # P_sell * S - 20 - P_sell * S * tax_rate = buy_notional + buy_fee
        # P_sell = (buy_notional + buy_fee + 20) / (S * (1 - tax_rate))
        raw_sell = (buy_notional + buy_fee + MIN_BROKER_FEE) / (
            shares * (1.0 - tax_rate)
        )

    return round_to_tick(raw_sell, ceil_mode=True)


# --- Tick 跳動規則（台股 2020/3/23 後的最小升降單位） ---
def tick_size(price: float) -> float:
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def round_to_tick(price: float, ceil_mode: bool = False) -> float:
    t = tick_size(price)
    n = price / t
    rounded_n = ceil(n) if ceil_mode else round(n)
    return round(rounded_n * t, 2)


def min_profitable_sell_price(
    buy_price: float,
    safety_ticks: int = 1,
    lots: int = 1,
    asset: AssetType = AssetType.STOCK,
) -> float:
    """損益兩平 + 額外 N 個 tick 安全邊際, 作為訊號最小目標價."""
    be = solve_breakeven_sell_price(buy_price, lots, asset)
    return round_to_tick(be + safety_ticks * tick_size(be), ceil_mode=True)
