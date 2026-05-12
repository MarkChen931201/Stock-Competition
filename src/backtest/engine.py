"""回測引擎 — 用歷史 1分K 模擬 ORB-15 + Trailing Stop。

資料來源：FinMind TaiwanStockPriceMinute
策略邏輯：與盤中系統完全相同（ORB-15、保本、K線追蹤停利）
輸出：每筆交易明細 + 整體績效統計

執行方式：
    python scripts/backtest_cli.py --days 20 --top 20
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
import pandas as pd
from loguru import logger

from src.data.blacklist import is_blacklisted


# ── 交易成本常數 ──
BROKER_FEE_RATE = 0.001425
MIN_BROKER_FEE  = 20.0
TAX_RATE_STOCK  = 0.0015
TAX_RATE_ETF    = 0.001
LOT_SIZE        = 1000


def _broker_fee(notional: float) -> float:
    return max(notional * BROKER_FEE_RATE, MIN_BROKER_FEE)


def _calc_pnl(entry: float, exit_: float, direction: str,
              lots: int = 1, is_etf: bool = False) -> dict:
    """計算一筆完整當沖的損益（含成本）。"""
    shares = lots * LOT_SIZE
    buy_price  = entry  if direction == "LONG" else exit_
    sell_price = exit_  if direction == "LONG" else entry

    buy_fee  = _broker_fee(buy_price  * shares)
    sell_fee = _broker_fee(sell_price * shares)
    tax      = sell_price * shares * (TAX_RATE_ETF if is_etf else TAX_RATE_STOCK)
    total_cost = buy_fee + sell_fee + tax

    gross_pnl = (sell_price - buy_price) * shares
    net_pnl   = gross_pnl - total_cost

    return {
        "gross_pnl": gross_pnl,
        "net_pnl":   net_pnl,
        "cost":      total_cost,
        "return_pct": net_pnl / (buy_price * shares),
    }


@dataclass
class TradeRecord:
    """一筆回測交易紀錄。"""
    date:        date
    symbol:      str
    direction:   str        # LONG / SHORT
    entry_time:  datetime
    entry_price: float
    exit_time:   datetime
    exit_price:  float
    exit_reason: str        # trailing_stop / time_stop / eod
    orb_high:    float
    orb_low:     float
    orb_width_pct: float
    lots:        int = 1

    gross_pnl:   float = 0.0
    net_pnl:     float = 0.0
    cost:        float = 0.0
    return_pct:  float = 0.0
    r_multiple:  float = 0.0   # 損益 / 初始 R
    initial_stop: float = 0.0
    initial_r:   float = 0.0


class ORBBacktestEngine:
    """ORB-15 策略回測引擎。

    Args:
        volume_ratio:    突破量比門檻
        min_orb_pct:     ORB 最小寬度
        stop_loss_pct:   初始停損 %
        profit_ratio:    固定停利倍數（trailing stop 模式下為上限參考）
        use_trailing:    是否啟用 K 線追蹤停利
        max_hold_bars:   最多持倉幾根 bar（時間停損）
    """

    def __init__(
        self,
        # v2: 同步最新 strategies/orb_breakout.py 參數
        volume_ratio:     float = 1.2,         # 1.5 → 1.2（同步放寬版）
        min_orb_pct:      float = 0.010,       # 0.8% → 1.0%
        stop_loss_pct:    float = 0.008,
        profit_ratio:     float = 1.5,
        use_trailing:     bool  = True,
        max_hold_bars:    int   = 90,
        early_exit_bars:  int   = 15,
        early_exit_r:     float = 0.3,
        # v2 新增：黑名單 + 流動性過濾
        use_blacklist:    bool  = True,
        min_total_volume_lots:   int   = 500,    # 累積量門檻
        min_daily_amplitude_pct: float = 0.010,  # 振幅門檻
    ):
        self.volume_ratio    = volume_ratio
        self.min_orb_pct     = min_orb_pct
        self.stop_loss_pct   = stop_loss_pct
        self.profit_ratio    = profit_ratio
        self.use_trailing    = use_trailing
        self.max_hold_bars   = max_hold_bars
        self.early_exit_bars = early_exit_bars
        self.early_exit_r    = early_exit_r
        self.use_blacklist   = use_blacklist
        self.min_total_volume_lots   = min_total_volume_lots
        self.min_daily_amplitude_pct = min_daily_amplitude_pct

    def run_day(
        self,
        symbol: str,
        trade_date: date,
        bars: pd.DataFrame,   # columns: time, open, high, low, close, volume
        is_etf: bool = False,
    ) -> list[TradeRecord]:
        """對單一股票單一交易日執行回測，回傳所有交易紀錄。"""
        if bars.empty or len(bars) < 20:
            return []

        # v2: 黑名單過濾
        if self.use_blacklist and is_blacklisted(symbol):
            return []

        # v2: 流動性過濾（累積量 + 振幅）
        total_vol_lots = float(bars["volume"].sum())   # 1分K的 volume 單位是張
        if total_vol_lots < self.min_total_volume_lots:
            return []
        day_high = float(bars["high"].max())
        day_low  = float(bars["low"].min())
        day_open = float(bars["open"].iloc[0])
        if day_open > 0:
            amplitude = (day_high - day_low) / day_open
            if amplitude < self.min_daily_amplitude_pct:
                return []

        bars = bars.copy().sort_values("time").reset_index(drop=True)

        # ── 建立 ORB 區間（09:00–09:14）──
        orb_mask = bars["time"].dt.hour == 9
        orb_mask &= bars["time"].dt.minute < 15
        orb_bars = bars[orb_mask]
        if orb_bars.empty:
            return []

        orb_high = orb_bars["high"].max()
        orb_low  = orb_bars["low"].min()
        prev_close = float(bars["open"].iloc[0])    # 用開盤第一根 open 近似昨收
        orb_width_pct = (orb_high - orb_low) / prev_close if prev_close > 0 else 0

        if orb_width_pct < self.min_orb_pct:
            return []   # ORB 太窄，跳過

        avg_orb_vol = orb_bars["volume"].mean()
        if avg_orb_vol == 0:
            return []

        # ── 掃描突破（09:15 後每根 bar）──
        signal_mask = (bars["time"].dt.hour * 60 + bars["time"].dt.minute) >= 9 * 60 + 15
        trade_bars = bars[signal_mask].reset_index(drop=True)

        records: list[TradeRecord] = []
        fired_long  = False
        fired_short = False

        for i, row in trade_bars.iterrows():
            close = row["close"]
            vol   = row["volume"]

            # 多單觸發
            if (not fired_long
                    and close > orb_high
                    and vol >= avg_orb_vol * self.volume_ratio):
                fired_long = True
                rec = self._simulate_trade(
                    symbol=symbol, trade_date=trade_date,
                    bars=trade_bars, entry_idx=i,
                    direction="LONG", entry_price=close,
                    orb_high=orb_high, orb_low=orb_low,
                    orb_width_pct=orb_width_pct, is_etf=is_etf,
                )
                if rec:
                    records.append(rec)

            # 空單觸發
            if (not fired_short
                    and close < orb_low
                    and vol >= avg_orb_vol * self.volume_ratio):
                fired_short = True
                rec = self._simulate_trade(
                    symbol=symbol, trade_date=trade_date,
                    bars=trade_bars, entry_idx=i,
                    direction="SHORT", entry_price=close,
                    orb_high=orb_high, orb_low=orb_low,
                    orb_width_pct=orb_width_pct, is_etf=is_etf,
                )
                if rec:
                    records.append(rec)

        return records

    def _simulate_trade(
        self,
        symbol: str, trade_date: date,
        bars: pd.DataFrame, entry_idx: int,
        direction: str, entry_price: float,
        orb_high: float, orb_low: float, orb_width_pct: float,
        is_etf: bool,
    ) -> Optional[TradeRecord]:
        orb_mid = (orb_high + orb_low) / 2

        if direction == "LONG":
            initial_stop = max(orb_mid, entry_price * (1 - self.stop_loss_pct))
        else:
            initial_stop = min(orb_mid, entry_price * (1 + self.stop_loss_pct))

        R = abs(entry_price - initial_stop)
        if R == 0:
            return None

        cost_pct = TAX_RATE_ETF + BROKER_FEE_RATE if is_etf else TAX_RATE_STOCK + BROKER_FEE_RATE
        breakeven_price = (
            entry_price * (1 + cost_pct) if direction == "LONG"
            else entry_price * (1 - cost_pct)
        )

        current_stop = initial_stop
        phase = "initial"
        exit_price = entry_price
        exit_reason = "eod"
        exit_time = bars.iloc[-1]["time"]
        recent_lows = []

        post_entry = bars.iloc[entry_idx + 1:entry_idx + 1 + self.max_hold_bars]

        for bars_held, (j, bar) in enumerate(post_entry.iterrows()):
            h, l, c = bar["high"], bar["low"], bar["close"]
            recent_lows.append(l if direction == "LONG" else h)
            if len(recent_lows) > 2:
                recent_lows.pop(0)

            if direction == "LONG":
                peak = h
                profit_r = (c - entry_price) / R

                # ── 早期時間停損：持倉 N 根後若未達 early_exit_r，直接出場 ──
                if (self.early_exit_bars > 0
                        and bars_held == self.early_exit_bars - 1
                        and profit_r < self.early_exit_r
                        and phase == "initial"):
                    exit_price  = c
                    exit_reason = "early_time_stop"
                    exit_time   = bar["time"]
                    break

                # 保本
                if phase == "initial" and profit_r >= 1.0:
                    phase = "breakeven_trailing"
                    current_stop = max(current_stop, breakeven_price)

                # K 線追蹤
                if phase == "breakeven_trailing" and self.use_trailing and len(recent_lows) == 2:
                    trail = min(recent_lows)
                    current_stop = max(current_stop, trail)

                # 固定停利上限（避免拉太遠）
                fixed_tp = entry_price + self.profit_ratio * R
                if c >= fixed_tp and not self.use_trailing:
                    exit_price  = fixed_tp
                    exit_reason = "take_profit"
                    exit_time   = bar["time"]
                    break

                # 停損觸發
                if l <= current_stop:
                    exit_price  = current_stop
                    exit_reason = "trailing_stop" if phase != "initial" else "stop_loss"
                    exit_time   = bar["time"]
                    break

            else:  # SHORT
                profit_r = (entry_price - c) / R

                # ── 早期時間停損 ──
                if (self.early_exit_bars > 0
                        and bars_held == self.early_exit_bars - 1
                        and profit_r < self.early_exit_r
                        and phase == "initial"):
                    exit_price  = c
                    exit_reason = "early_time_stop"
                    exit_time   = bar["time"]
                    break

                if phase == "initial" and profit_r >= 1.0:
                    phase = "breakeven_trailing"
                    current_stop = min(current_stop, breakeven_price)

                if phase == "breakeven_trailing" and self.use_trailing and len(recent_lows) == 2:
                    trail = max(recent_lows)
                    current_stop = min(current_stop, trail)

                fixed_tp = entry_price - self.profit_ratio * R
                if c <= fixed_tp and not self.use_trailing:
                    exit_price  = fixed_tp
                    exit_reason = "take_profit"
                    exit_time   = bar["time"]
                    break

                if h >= current_stop:
                    exit_price  = current_stop
                    exit_reason = "trailing_stop" if phase != "initial" else "stop_loss"
                    exit_time   = bar["time"]
                    break
        else:
            # 時間到收盤強制出場
            exit_price  = bars.iloc[min(entry_idx + self.max_hold_bars, len(bars) - 1)]["close"]
            exit_reason = "time_stop"
            exit_time   = bars.iloc[min(entry_idx + self.max_hold_bars, len(bars) - 1)]["time"]

        pnl = _calc_pnl(entry_price, exit_price, direction, lots=1, is_etf=is_etf)

        rec = TradeRecord(
            date=trade_date,
            symbol=symbol,
            direction=direction,
            entry_time=bars.iloc[entry_idx]["time"],
            entry_price=entry_price,
            exit_time=exit_time,
            exit_price=exit_price,
            exit_reason=exit_reason,
            orb_high=orb_high,
            orb_low=orb_low,
            orb_width_pct=orb_width_pct,
            initial_stop=initial_stop,
            initial_r=R,
        )
        rec.gross_pnl  = pnl["gross_pnl"]
        rec.net_pnl    = pnl["net_pnl"]
        rec.cost       = pnl["cost"]
        rec.return_pct = pnl["return_pct"]
        rec.r_multiple = (exit_price - entry_price) / R * (1 if direction == "LONG" else -1)
        return rec
