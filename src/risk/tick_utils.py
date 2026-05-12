"""台股升降單位工具函數。

台股每檔股票的最小報價單位（tick size）依股價分級：
  < 10 元        0.01
  10 ~ 50 元    0.05
  50 ~ 100 元   0.10
  100 ~ 500 元  0.50
  500 ~ 1000 元 1.00
  >= 1000 元    5.00

用法：
    from src.risk.tick_utils import floor_price, ceil_price, round_price

    stop_loss   = floor_price(98.73)   # → 98.50（往下取整到最近 tick）
    take_profit = ceil_price(102.31)   # → 102.50（往上取整到最近 tick）
"""
from __future__ import annotations

import math


def tick_size(price: float) -> float:
    """回傳指定股價的最小升降單位。"""
    if price < 10:
        return 0.01
    elif price < 50:
        return 0.05
    elif price < 100:
        return 0.10
    elif price < 500:
        return 0.50
    elif price < 1000:
        return 1.00
    else:
        return 5.00


def floor_price(price: float) -> float:
    """向下取整到最近合法 tick（例如多單停損、多單進場上限）。"""
    tick = tick_size(price)
    # 避免浮點誤差：乘以 100 取整再除回
    result = math.floor(round(price / tick, 8)) * tick
    return round(result, 10)


def ceil_price(price: float) -> float:
    """向上取整到最近合法 tick（例如多單停利、空單進場下限）。"""
    tick = tick_size(price)
    result = math.ceil(round(price / tick, 8)) * tick
    return round(result, 10)


def snap_price(price: float) -> float:
    """四捨五入到最近合法 tick（一般顯示用）。"""
    tick = tick_size(price)
    result = round(round(price / tick, 8)) * tick
    return round(result, 10)


def stop_loss_price(price: float, is_long: bool) -> float:
    """計算合法停損價格。
    多單停損：向下 tick（確保設在支撐下方）
    空單停損：向上 tick（確保設在壓力上方）
    """
    return floor_price(price) if is_long else ceil_price(price)


def take_profit_price(price: float, is_long: bool) -> float:
    """計算合法停利價格。
    多單停利：向上 tick（確保過得了壓力）
    空單停利：向下 tick（確保突破支撐）
    """
    return ceil_price(price) if is_long else floor_price(price)


def entry_price(price: float, is_long: bool) -> float:
    """計算合法進場限價。
    多單進場：向下 tick（掛低一點比較容易成交）
    空單進場：向上 tick
    """
    return floor_price(price) if is_long else ceil_price(price)
