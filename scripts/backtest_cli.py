"""回測 CLI — 下載歷史 1分K 並跑 ORB 回測。

用法：
    python scripts/backtest_cli.py --days 20 --symbols 2303 2330 2317
    python scripts/backtest_cli.py --days 30 --top 20   # 用均量 Top 20 股

FinMind API 限制：免費方案每分鐘約 30 次請求，--days 大時會自動限速。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from loguru import logger

from config.settings import settings
from src.backtest.engine import ORBBacktestEngine
from src.backtest.reports import build_report, print_report
from src.data.finmind_client import FinMindClient

FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"


def fetch_intraday_1min(
    symbol: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """用 Fugle Historical Candles API 抓歷史 1分K。"""
    from fugle_marketdata import RestClient
    rest = RestClient(api_key=settings.fugle_api_key)
    try:
        data = rest.stock.historical.candles(**{
            "symbol":    symbol,
            "timeframe": "1",
            "from":      start_date.isoformat(),
            "to":        end_date.isoformat(),
        })
        candles = data.get("data", [])
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame(candles)
        df["time"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.sort_values("time").reset_index(drop=True)
    except Exception as e:
        logger.warning(f"[{symbol}] 1分K 下載失敗：{e}")
        return pd.DataFrame()


def get_top_symbols(client: FinMindClient, top_n: int, days: int) -> list[str]:
    """用近 20 日均量排序取 Top N 股票。"""
    from config.settings import PROJECT_ROOT
    import yaml

    with open(PROJECT_ROOT / "config" / "universe.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    seeds = data.get("seed_universe", [])
    etfs  = data.get("etf_universe", [])
    candidates = list(dict.fromkeys(seeds + etfs))

    end = date.today()
    start = end - timedelta(days=days + 10)
    vol_map = {}

    for sym in candidates:
        try:
            df = client.daily_price(sym, start, end)
            if df.empty:
                continue
            df["Trading_Volume"] = pd.to_numeric(df["Trading_Volume"], errors="coerce")
            avg_vol = df["Trading_Volume"].tail(days).mean() / 1000
            vol_map[sym] = avg_vol
        except Exception:
            pass
        time.sleep(0.3)

    sorted_syms = sorted(vol_map, key=vol_map.get, reverse=True)
    result = sorted_syms[:top_n]
    logger.info(f"Top {top_n} 均量股票：{result}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="ORB-15 回測系統")
    parser.add_argument("--days",    type=int,   default=20,  help="回測天數（交易日）")
    parser.add_argument("--symbols", nargs="+",  default=[],  help="指定股票代號")
    parser.add_argument("--top",     type=int,   default=0,   help="使用均量 Top N 股")
    parser.add_argument("--no-trail",action="store_true",     help="停用 Trailing Stop（用固定停利）")
    parser.add_argument("--capital", type=float, default=10_000_000, help="初始資金（元）")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")

    client = FinMindClient(token=settings.finmind_token)
    engine = ORBBacktestEngine(use_trailing=not args.no_trail)

    # 決定股票清單
    if args.symbols:
        symbols = args.symbols
    elif args.top > 0:
        symbols = get_top_symbols(client, args.top, args.days)
    else:
        from config.settings import PROJECT_ROOT
        import yaml
        with open(PROJECT_ROOT / "config" / "universe.yaml") as f:
            data = yaml.safe_load(f)
        symbols = list(dict.fromkeys(
            data.get("seed_universe", [])[:20] + data.get("etf_universe", [])
        ))

    # 計算回測日期範圍
    end_date   = date.today() - timedelta(days=1)   # 昨天
    start_date = end_date - timedelta(days=int(args.days * 1.5))  # 多抓一些日曆日

    logger.info(f"回測範圍：{start_date} ~ {end_date}，共 {len(symbols)} 檔")

    all_trades = []
    etf_prefixes = ("00", "0050", "0056")

    for i, sym in enumerate(symbols):
        is_etf = sym.startswith(etf_prefixes) or not sym.isdigit()
        logger.info(f"[{i+1}/{len(symbols)}] 下載 {sym} 1分K…")

        df = fetch_intraday_1min(sym, start_date, end_date)
        if df.empty:
            logger.warning(f"  {sym} 無資料，跳過")
            time.sleep(1)
            continue

        # 依日期分組
        df["trade_date"] = df["time"].dt.date
        for trade_date, day_df in df.groupby("trade_date"):
            records = engine.run_day(
                symbol=sym,
                trade_date=trade_date,
                bars=day_df.copy(),
                is_etf=is_etf,
            )
            all_trades.extend(records)

        logger.info(f"  {sym} 完成，累計 {len(all_trades)} 筆交易")
        time.sleep(1.5)   # FinMind rate limit

    if not all_trades:
        logger.error("無任何交易紀錄，請檢查資料")
        return

    report = build_report(all_trades, initial_capital=args.capital)
    print_report(report, all_trades)

    # 存 CSV
    out_path = Path("logs") / f"backtest_{date.today()}.csv"
    out_path.parent.mkdir(exist_ok=True)
    pd.DataFrame([t.__dict__ for t in all_trades]).to_csv(out_path, index=False, encoding="utf-8-sig")
    logger.info(f"交易明細已存至 {out_path}")


if __name__ == "__main__":
    main()
