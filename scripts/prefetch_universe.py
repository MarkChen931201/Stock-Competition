"""盤前自動篩選腳本.

流程:
1. 讀 config/universe.yaml 的 seed_universe + etf_universe.
2. 用 FinMind 抓近 N 日日 K.
3. 計算: 平均成交量(張) / 週轉率 / ATR% / 收盤價過濾.
4. 算綜合分數 (Z-score 加權), 由高到低排序.
5. 輸出到 logs/screener_YYYYMMDD.csv 並推播 Discord.

執行: python -m scripts.prefetch_universe
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml
from loguru import logger

# 讓 `python -m scripts.prefetch_universe` 與 `python scripts/prefetch_universe.py` 都可跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import PROJECT_ROOT, settings  # noqa: E402
from src.data.finmind_client import FinMindClient  # noqa: E402
from src.indicators.volume import atr_pct, avg_volume_lots, turnover_rate  # noqa: E402
from src.notifier.discord_bot import DiscordNotifier  # noqa: E402


def load_universe() -> tuple[list[str], list[str]]:
    cfg_path = PROJECT_ROOT / "config" / "universe.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    seeds = [str(s) for s in (cfg.get("seed_universe") or [])]
    etfs = [str(s) for s in (cfg.get("etf_universe") or [])]
    return seeds, etfs


def zscore(series: pd.Series) -> pd.Series:
    std = series.std(ddof=0)
    if std == 0 or pd.isna(std):
        return pd.Series([0.0] * len(series), index=series.index)
    return (series - series.mean()) / std


def screen(
    stock_ids: list[str],
    asset_type: str,
    client: FinMindClient,
    stock_info: pd.DataFrame,
) -> list[dict]:
    """對單一資產類型 (stock / etf) 跑篩選."""
    name_lookup = (
        dict(zip(stock_info["stock_id"].astype(str), stock_info["stock_name"]))
        if not stock_info.empty
        else {}
    )

    daily = client.daily_prices_batch(stock_ids, settings.screener_lookback_days)
    rows: list[dict] = []
    for sid in stock_ids:
        df = daily.get(sid)
        if df is None or len(df) < 14:
            logger.info(f"[{sid}] skip: insufficient data")
            continue
        last_close = float(df["close"].iloc[-1])
        if not (
            settings.screener_price_min <= last_close <= settings.screener_price_max
        ):
            logger.info(f"[{sid}] skip: price {last_close} out of range")
            continue

        vol_lots = avg_volume_lots(df, settings.screener_lookback_days)
        atrp = atr_pct(df, period=14)

        # ETF 通常無流通股數 dataset, 用 fallback: 用日均成交額 / 市值近似 (省略也可)
        try:
            shares = client.shares_outstanding(sid) if asset_type == "stock" else None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{sid}] shares_outstanding failed: {e}")
            shares = None
        tr = turnover_rate(df, shares) if shares else float("nan")

        rows.append(
            {
                "stock_id": sid,
                "name": name_lookup.get(sid, ""),
                "asset_type": asset_type,
                "close": last_close,
                "avg_volume_lots": vol_lots,
                "turnover_rate": tr,
                "atr_pct": atrp,
            }
        )

    if not rows:
        return []

    df = pd.DataFrame(rows)

    # 過濾條件 (週轉率對 ETF 多半 NaN, 改以量代替)
    cond_volume = df["avg_volume_lots"] >= settings.screener_min_avg_volume_lots
    cond_atr = df["atr_pct"] >= settings.screener_min_atr_pct
    if asset_type == "stock":
        # 有週轉率資料才套門檻，取不到流通股數時不因此擋掉標的
        has_turnover = df["turnover_rate"].notna()
        cond_turnover = (
            (~has_turnover) |
            (df["turnover_rate"] >= settings.screener_min_turnover_rate)
        )
        passed = df[cond_volume & cond_atr & cond_turnover].copy()
    else:
        passed = df[cond_volume & cond_atr].copy()

    if passed.empty:
        return []

    # 綜合分數: 量(0.4) + ATR%(0.4) + 週轉率(0.2)
    passed["z_vol"] = zscore(passed["avg_volume_lots"])
    passed["z_atr"] = zscore(passed["atr_pct"])
    passed["z_turn"] = zscore(passed["turnover_rate"].fillna(passed["turnover_rate"].mean()))
    passed["score"] = (
        0.4 * passed["z_vol"] + 0.4 * passed["z_atr"] + 0.2 * passed["z_turn"]
    )
    passed = passed.sort_values("score", ascending=False)
    return passed.to_dict(orient="records")


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)

    seeds, etfs = load_universe()
    if not seeds and not etfs:
        logger.error("universe.yaml 為空, 請至少設定 seed_universe 或 etf_universe")
        return 1

    with FinMindClient(settings.finmind_token) as client:
        try:
            stock_info = client.stock_info()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"stock_info fetch failed: {e}")
            stock_info = pd.DataFrame()

        stock_results = screen(seeds, "stock", client, stock_info) if seeds else []
        etf_results = screen(etfs, "etf", client, stock_info) if etfs else []

    all_results = (stock_results + etf_results)
    all_results = sorted(all_results, key=lambda r: r["score"], reverse=True)
    top_n = all_results[: settings.screener_top_n]

    # 輸出 CSV
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    csv_path = log_dir / f"screener_{today}.csv"
    pd.DataFrame(top_n).to_csv(csv_path, index=False, encoding="utf-8-sig")
    logger.info(f"Wrote {len(top_n)} candidates to {csv_path}")

    # 推播 Discord
    if settings.discord_webhook_url:
        notifier = DiscordNotifier(settings.discord_webhook_url)
        notifier.send_screener_result(
            candidates=top_n,
            as_of=datetime.now(),
            criteria={
                "min_avg_volume_lots": settings.screener_min_avg_volume_lots,
                "min_turnover_rate": settings.screener_min_turnover_rate,
                "min_atr_pct": settings.screener_min_atr_pct,
                "price_min": settings.screener_price_min,
                "price_max": settings.screener_price_max,
            },
        )
    else:
        logger.warning("DISCORD_WEBHOOK_URL 未設定, 略過推播")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
