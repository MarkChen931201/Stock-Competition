"""每日盤後自動更新 universe.yaml — 用今日成交量 Top N 替換監控池。

執行時機：13:30 收盤後（或隔日 08:30 盤前）
執行方式：
    python scripts/update_universe.py

功能：
  1. 讀取今日 logs/screener_*.csv（盤前篩選結果）
  2. 加上今日廣域掃描的熱門股
  3. 合併固定持倉池（避免把正在監控的股票移掉）
  4. 更新 universe.yaml
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from loguru import logger

from config.settings import PROJECT_ROOT, settings
from src.data.finmind_client import FinMindClient

# 固定保留的核心標的（流動性最高，不管今日表現都監控）
CORE_SYMBOLS = [
    "3481", "2409", "2303", "2344", "2408",   # 半導體/面板
    "2330", "2454", "3711",
    "2317", "2356", "2353", "2382", "3231",   # 電子
    "2888", "2885", "2883", "2887", "2886",   # 金融
    "2890", "2891", "2882", "5880",
    "2610", "2618", "2609", "2603",           # 航運
    "1303", "2002",                           # 傳產
    "0050", "0056", "00878", "00929",         # ETF
]

# ETF 白名單（永遠保留）
ETF_WHITELIST = ["0050", "0056", "00878", "00919", "00929", "00940", "006208", "00631L"]


def load_screener_candidates(today: date) -> list[str]:
    """讀取今日盤前篩選結果。"""
    csv_path = PROJECT_ROOT / "logs" / f"screener_{today.strftime('%Y%m%d')}.csv"
    if not csv_path.exists():
        logger.warning(f"找不到今日篩選結果：{csv_path}")
        return []
    import pandas as pd
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    return df["stock_id"].astype(str).tolist()


def get_top_by_volume(client: FinMindClient, n: int = 50) -> list[str]:
    """用 FinMind 取昨日成交量 Top N。"""
    import pandas as pd
    from datetime import timedelta

    yesterday = date.today() - timedelta(days=1)
    try:
        info_df = client.stock_info()
        tse = info_df[info_df["type"].str.lower().isin(["twse", "上市"])]
        symbols = [s for s in tse["stock_id"].astype(str).tolist()
                   if s.isdigit() and 4 <= len(s) <= 5]

        records = []
        import time
        for i, sym in enumerate(symbols[:300]):   # 只查前 300 檔避免太慢
            try:
                df = client.daily_price(sym, yesterday, yesterday)
                if df.empty:
                    continue
                vol = float(df["Trading_Volume"].iloc[-1]) / 1000
                close = float(df["close"].iloc[-1])
                if 10 <= close <= 5000 and vol > 0:
                    records.append({"symbol": sym, "vol": vol})
            except Exception:
                pass
            if i % 50 == 49:
                time.sleep(0.5)

        records.sort(key=lambda x: x["vol"], reverse=True)
        return [r["symbol"] for r in records[:n]]
    except Exception as e:
        logger.warning(f"FinMind Top N 查詢失敗：{e}")
        return []


def update_universe(new_symbols: list[str]) -> None:
    """更新 universe.yaml。"""
    universe_path = PROJECT_ROOT / "config" / "universe.yaml"

    # 讀取現有設定
    with open(universe_path, encoding="utf-8") as f:
        existing = yaml.safe_load(f)

    # 合併：核心 + 新增 + ETF（去重）
    combined = list(dict.fromkeys(CORE_SYMBOLS + new_symbols))
    # 過濾掉 ETF（ETF 單獨管理）
    seeds = [s for s in combined if s not in ETF_WHITELIST]

    new_data = {
        "seed_universe": seeds[:150],   # 最多 150 檔
        "etf_universe":  ETF_WHITELIST,
    }

    with open(universe_path, "w", encoding="utf-8") as f:
        f.write(f"# 監控標的池：自動更新 {date.today()}\n\n")
        yaml.dump(new_data, f, allow_unicode=True, default_flow_style=False)

    logger.info(f"universe.yaml 更新完成：{len(seeds)} 檔股票 + {len(ETF_WHITELIST)} ETF")


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO", colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")

    today = date.today()
    client = FinMindClient(token=settings.finmind_token)

    # 1. 今日盤前篩選結果
    screener = load_screener_candidates(today)
    logger.info(f"盤前篩選結果：{len(screener)} 檔")

    # 2. 昨日成交量 Top 50（資料比較穩定）
    logger.info("查詢昨日成交量 Top 50…")
    top_vol = get_top_by_volume(client, n=50)
    logger.info(f"成交量 Top 50：{top_vol[:10]}…")

    # 3. 合併更新
    all_new = list(dict.fromkeys(screener + top_vol))
    update_universe(all_new)
    logger.info("✅ universe.yaml 更新完成，明日生效")


if __name__ == "__main__":
    main()
