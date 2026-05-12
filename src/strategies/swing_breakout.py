"""短期波段策略 — 三重確認型（突破 + 法人 + 均線）。

設計用途：
  - 抓 3-5 日的中短線行情
  - 跟主程式（盤中當沖）分開：用日線資料、盤後執行
  - 進場時點：隔日開盤限價買進

三重確認進場條件（全部滿足，做多 only）：
  A. 突破：收盤價突破近 10 日最高（不含當日）
  B. 均線：5/10/20 EMA 多頭排列 + 收盤 > 5 EMA
  C. 量能：當日量 > 20 日均量 × 1.5
  D. 過濾：RSI(14) 50~80（避免買在頂部）
  E. 過濾：股價 ≥ 10 元、20日均量 ≥ 1000 張（流動性）

加分項（不影響進場，但會提升分數排序）：
  - 法人（外資+投信）當日合計買超
  - 大盤 TAIEX 同步上漲

評分（總分 10）：
  1. 突破強度       0~2  收盤離 10 日高 >1% 加分
  2. 均線多頭排列   0~2  5>10>20 完整排列
  3. 量能放大       0~2  量比 1.5/2.0/3.0
  4. RSI 健康度     0~2  60-70 最佳
  5. 法人 + 大盤    0~2  bonus

每日 13:35 掃完後，回傳分數最高的前 N 檔，附完整建議：
  - 隔日限價買進價（收盤 ~ +0.5%）
  - 停損（進場 -3% 或 10 日線取較近）
  - 停利 T1（+6%）/ T2（+12%）
  - 預期持有 3-5 天
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.risk.tick_utils import floor_price, snap_price, stop_loss_price, take_profit_price


@dataclass
class SwingCandidate:
    """波段選股候選。"""
    symbol: str
    name: str
    score: float
    breakdown: dict

    # 最新狀態
    close: float
    high_10d: float       # 近 10 日最高（不含當日）
    ema5: float
    ema10: float
    ema20: float
    rsi14: float
    vol_ratio: float      # 當日量 / 20 日均量
    avg_vol_lots: float   # 20 日平均量（張）

    # 進場建議
    entry_limit: float    # 建議隔日限價（收盤 +0.3%，floor 到 tick）
    stop_loss: float      # 停損價
    take_profit_1: float  # 第一目標 +6%
    take_profit_2: float  # 第二目標 +12%
    risk_per_share: float
    suggested_lots: int

    # 加分項
    market_change_pct: float
    inst_net_buy_lots: int | None  # 法人合計買超張（None 表示無資料）


def _ema(series: pd.Series, period: int) -> float:
    """指數移動平均（用 pandas）。"""
    if len(series) < period:
        return float(series.iloc[-1]) if len(series) > 0 else 0.0
    return float(series.ewm(span=period, adjust=False).mean().iloc[-1])


def _rsi(closes: pd.Series, period: int = 14) -> float:
    """RSI 計算。"""
    if len(closes) < period + 1:
        return 50.0
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean().iloc[-1]
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean().iloc[-1]
    if loss == 0 or pd.isna(loss):
        return 100.0
    rs = gain / loss
    return float(100 - 100 / (1 + rs))


def evaluate(
    df: pd.DataFrame,
    symbol: str,
    name: str,
    max_risk_ntd: float = 100000,
    market_change_pct: float = 0.0,
    inst_net_buy_lots: int | None = None,
) -> SwingCandidate | None:
    """評估單一股票是否符合波段進場條件。

    Args:
        df:             日線資料（DataFrame，須含 open/high/low/close/Trading_Volume）
        symbol:         股票代號
        name:           股票名稱
        max_risk_ntd:   單筆風險上限
        market_change_pct: 當日大盤漲跌幅（加分用）
        inst_net_buy_lots: 法人當日合計買超張（加分用）

    Returns:
        SwingCandidate 或 None（不合格）
    """
    if len(df) < 21:  # 至少需 21 天資料
        return None

    # 標準化欄位（FinMind 不同版本可能用 close 或 Close）
    close_col = "close" if "close" in df.columns else "Close"
    high_col  = "max" if "max" in df.columns else ("high" if "high" in df.columns else "High")
    low_col   = "min" if "min" in df.columns else ("low"  if "low"  in df.columns else "Low")
    vol_col   = "Trading_Volume" if "Trading_Volume" in df.columns else (
                "trading_volume" if "trading_volume" in df.columns else "volume")

    closes  = df[close_col].astype(float)
    highs   = df[high_col].astype(float)
    volumes = df[vol_col].astype(float)

    last_close = float(closes.iloc[-1])

    # ── 流動性過濾 ──
    if last_close < 10:    # 股價太低
        return None
    avg_vol_lots = float(volumes.tail(20).mean()) / 1000  # FinMind volume 是股，除 1000 轉張
    if avg_vol_lots < 1000:  # 20 日均量 < 1000 張
        return None

    # ── A. 突破條件：收盤 > 近 10 日最高（不含當日）──
    high_10d = float(highs.iloc[-11:-1].max())   # 前 10 日（不含當日）
    if last_close <= high_10d:
        return None  # 沒突破

    # ── B. 均線多頭排列 ──
    ema5  = _ema(closes, 5)
    ema10 = _ema(closes, 10)
    ema20 = _ema(closes, 20)
    if not (ema5 > ema10 > ema20):
        return None  # 均線非多頭排列
    if last_close < ema5:
        return None  # 收盤跌破 5 EMA

    # ── C. 量能放大 ──
    last_vol_lots = float(volumes.iloc[-1]) / 1000
    vol_ratio = last_vol_lots / avg_vol_lots if avg_vol_lots > 0 else 0
    if vol_ratio < 1.5:
        return None  # 量不夠

    # ── D. RSI 健康度（不要超買）──
    rsi14 = _rsi(closes, 14)
    if not (50 <= rsi14 <= 80):
        return None

    # ── 通過所有硬條件，開始評分 ──
    breakdown = {}

    # 1. 突破強度（0~2）
    break_pct = (last_close - high_10d) / high_10d
    if break_pct >= 0.03:    breakdown["突破"] = 2.0
    elif break_pct >= 0.015: breakdown["突破"] = 1.5
    elif break_pct >= 0.005: breakdown["突破"] = 1.0
    else:                    breakdown["突破"] = 0.5

    # 2. 均線多頭強度（0~2）
    ema_spread = (ema5 - ema20) / ema20
    if ema_spread >= 0.05:   breakdown["均線"] = 2.0
    elif ema_spread >= 0.03: breakdown["均線"] = 1.5
    elif ema_spread >= 0.01: breakdown["均線"] = 1.0
    else:                    breakdown["均線"] = 0.5

    # 3. 量能（0~2）
    if vol_ratio >= 3.0:     breakdown["量能"] = 2.0
    elif vol_ratio >= 2.0:   breakdown["量能"] = 1.5
    else:                    breakdown["量能"] = 1.0

    # 4. RSI 健康度（0~2，60-70 最佳）
    if 60 <= rsi14 <= 70:    breakdown["RSI"] = 2.0
    elif 55 <= rsi14 <= 75:  breakdown["RSI"] = 1.5
    else:                    breakdown["RSI"] = 1.0

    # 5. 法人 + 大盤（0~2）
    bonus = 0.0
    if market_change_pct > 0:
        bonus += 0.5
    if inst_net_buy_lots is not None and inst_net_buy_lots > 0:
        if inst_net_buy_lots >= 5000:   bonus += 1.5
        elif inst_net_buy_lots >= 1000: bonus += 1.0
        else:                           bonus += 0.5
    breakdown["法人/大盤"] = round(min(bonus, 2.0), 1)

    score = sum(breakdown.values())

    # ── 計算進場 / 停損 / 停利（合法 tick）──
    entry_limit = snap_price(last_close * 1.003)            # 收盤 +0.3% 限價
    # 停損：進場 -3% 或 10 日線取較近
    sl_pct  = entry_limit * 0.97
    sl_ema  = ema10 * 0.99
    stop_loss = stop_loss_price(max(sl_pct, sl_ema), is_long=True)
    risk_per_share = entry_limit - stop_loss
    if risk_per_share <= 0:
        return None

    take_profit_1 = take_profit_price(entry_limit * 1.06, is_long=True)
    take_profit_2 = take_profit_price(entry_limit * 1.12, is_long=True)

    # 建議張數（限制 50 張 + 風險 NT$100,000）
    cost_per_lot = entry_limit * 1000 * 0.005   # 來回成本約 0.5%
    total_risk_per_lot = risk_per_share * 1000 + cost_per_lot
    suggested_lots = max(1, min(50, int(max_risk_ntd / total_risk_per_lot)))

    return SwingCandidate(
        symbol=symbol,
        name=name,
        score=round(score, 1),
        breakdown={k: round(v, 1) for k, v in breakdown.items()},
        close=last_close,
        high_10d=high_10d,
        ema5=round(ema5, 2),
        ema10=round(ema10, 2),
        ema20=round(ema20, 2),
        rsi14=round(rsi14, 1),
        vol_ratio=round(vol_ratio, 2),
        avg_vol_lots=round(avg_vol_lots, 0),
        entry_limit=entry_limit,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        risk_per_share=round(risk_per_share, 2),
        suggested_lots=suggested_lots,
        market_change_pct=round(market_change_pct * 100, 2),
        inst_net_buy_lots=inst_net_buy_lots,
    )
