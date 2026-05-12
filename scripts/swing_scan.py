"""短期波段掃描 CLI — 兩階段執行。

用法：
    python3 scripts/swing_scan.py --mode preselect    # 盤後 13:35 預選
    python3 scripts/swing_scan.py --mode confirm      # 開盤 09:35 確認
    python3 scripts/swing_scan.py --mode check        # 持倉檢查（任意時間）

模式說明：
  preselect (13:35)
    1. 用 FinMind 批次抓 universe 日線資料
    2. 三重確認掃描
    3. Top 5 寫入 swing_positions.json（status=WAITING）
    4. Discord 推預選清單 + 持倉檢查

  confirm (09:35)
    1. 讀取 WAITING 清單
    2. 用 Fugle 即時 quote 驗證進場條件仍成立
    3. 通過 → 改 status=HOLDING + 推進場確認
    4. 不通過 → 改 status=EXITED（取消） + 推取消通知

  check
    1. 讀取所有 HOLDING
    2. 推持倉檢查 embed
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

# 確保能 import 上層
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from config.settings import settings
from src.data.finmind_client import FinMindClient
from src.notifier.discord_bot import DiscordNotifier
from src.notifier.swing_embed import (
    build_confirm_embed,
    build_exit_embed,
    build_holding_check_embed,
    build_preselect_embed,
)
from src.risk.swing_position import SwingPosition, SwingPositionManager, SwingStatus
from src.strategies.swing_breakout import SwingCandidate, evaluate

# 設定
_LOOKBACK_DAYS = 30        # 抓 30 天日線（夠算 20EMA + 10 日突破）
_TOP_N         = 5          # 預選 Top 5
_MAX_RISK_NTD  = 100_000    # 單筆風險上限


def _load_universe_with_names() -> list[tuple[str, str]]:
    """從 universe.yaml 載入監控池 + 中文名（重用 scheduler 的解析邏輯）。"""
    from src.scheduler import _parse_universe_with_names
    path = Path(__file__).resolve().parent.parent / "config" / "universe.yaml"
    symbols, name_map = _parse_universe_with_names(path)
    return [(s, name_map.get(s, s)) for s in symbols]


def _scan_market_change(client: FinMindClient) -> float:
    """取大盤（TAIEX）當日漲跌幅。"""
    try:
        end = date.today()
        start = end - timedelta(days=10)
        df = client.daily_price("TAIEX", start, end)
        if df.empty or len(df) < 2:
            return 0.0
        close_col = "close" if "close" in df.columns else "Close"
        prev = float(df[close_col].iloc[-2])
        curr = float(df[close_col].iloc[-1])
        return (curr - prev) / prev if prev > 0 else 0.0
    except Exception as e:
        logger.warning(f"取大盤資料失敗：{e}")
        return 0.0


def cmd_preselect() -> None:
    """13:35 盤後預選。"""
    logger.info("=== 波段預選掃描開始 ===")

    notifier = DiscordNotifier(webhook_url=settings.discord_webhook_url)
    manager = SwingPositionManager()
    manager.cleanup_old(days=30)

    # 先推持倉檢查（如果有）
    holding = manager.get_holding()
    today = date.today()
    if holding:
        embed = build_holding_check_embed(holding, today)
        notifier._send(embed)
        logger.info(f"已推播持倉檢查 ({len(holding)} 檔)")

    # 載入 universe
    targets = _load_universe_with_names()
    logger.info(f"掃描 {len(targets)} 檔 universe")

    # 抓日線資料（批次）
    client = FinMindClient(token=settings.finmind_token)
    stock_ids = [sid for sid, _ in targets]
    daily_data = client.daily_prices_batch(stock_ids, _LOOKBACK_DAYS)
    logger.info(f"取得 {len(daily_data)} 檔日線資料")

    # 大盤資料
    market_change = _scan_market_change(client)
    logger.info(f"大盤漲跌幅：{market_change*100:+.2f}%")

    # 逐檔評估
    candidates: list[SwingCandidate] = []
    for sid, name in targets:
        df = daily_data.get(sid)
        if df is None or df.empty:
            continue
        c = evaluate(
            df, sid, name,
            max_risk_ntd=_MAX_RISK_NTD,
            market_change_pct=market_change,
            inst_net_buy_lots=None,   # 法人資料 FinMind v4 付費，先 None
        )
        if c:
            candidates.append(c)

    candidates.sort(key=lambda x: x.score, reverse=True)
    top = candidates[:_TOP_N]

    logger.info(f"通過 {len(candidates)} 檔，取 Top {len(top)}")

    if not top:
        logger.info("無符合條件標的，跳過推播")
        return

    # 推預選清單
    embed = build_preselect_embed(top)
    notifier._send(embed)

    # 寫入 swing_positions.json（status=WAITING）
    next_trading_day = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    for c in top:
        pos = SwingPosition(
            symbol=c.symbol,
            name=c.name,
            status=SwingStatus.WAITING,
            entry_date=next_trading_day,
            entry_limit=c.entry_limit,
            stop_loss=c.stop_loss,
            take_profit_1=c.take_profit_1,
            take_profit_2=c.take_profit_2,
            lots=c.suggested_lots,
            score=c.score,
            breakdown=c.breakdown,
        )
        manager.add(pos)

    logger.info("✅ 預選清單已寫入 + Discord 推播")


def cmd_confirm() -> None:
    """09:35 開盤確認。"""
    logger.info("=== 波段進場確認開始 ===")

    notifier = DiscordNotifier(webhook_url=settings.discord_webhook_url)
    manager = SwingPositionManager()
    waiting = manager.get_waiting()

    if not waiting:
        logger.info("無 WAITING 待確認")
        return

    today = date.today().strftime("%Y-%m-%d")
    today_targets = [p for p in waiting if p.entry_date == today]
    if not today_targets:
        logger.info(f"WAITING {len(waiting)} 檔但無今日進場目標")
        return

    # 用 Fugle 抓即時 quote 驗證
    from fugle_marketdata import RestClient
    rest = RestClient(api_key=settings.fugle_api_key)

    for pos in today_targets:
        try:
            raw = rest.stock.intraday.quote(symbol=pos.symbol)
            current_price = float(raw.get("lastPrice") or raw.get("closePrice") or 0)
        except Exception as e:
            logger.warning(f"[{pos.symbol}] Fugle quote 失敗：{e}")
            continue

        # 驗證條件：現價在進場區間內（不能漲太多）
        max_chase_pct = 0.015  # 最多追 1.5%
        ok = current_price <= pos.entry_limit * (1 + max_chase_pct) and current_price > 0

        if ok:
            manager.confirm_entry(pos.symbol, pos.entry_date, current_price, pos.lots)
            pos.entry_actual = current_price
            pos.status = SwingStatus.HOLDING
            embed = build_confirm_embed(pos, current_price, ok=True)
        else:
            reason = f"現價 {current_price} 超過追價上限 ({pos.entry_limit * (1+max_chase_pct):.2f})"
            manager.cancel_waiting(pos.symbol, pos.entry_date, reason)
            embed = build_confirm_embed(pos, current_price, ok=False)

        notifier._send(embed)
        logger.info(f"[{pos.symbol}] {'✅ 進場' if ok else '⛔ 取消'} @{current_price}")


def cmd_check() -> None:
    """持倉檢查（任意時間執行）。"""
    notifier = DiscordNotifier(webhook_url=settings.discord_webhook_url)
    manager = SwingPositionManager()
    holding = manager.get_holding()

    if not holding:
        logger.info("無 HOLDING 持倉")
        return

    embed = build_holding_check_embed(holding, date.today())
    notifier._send(embed)
    logger.info(f"已推播持倉檢查 ({len(holding)} 檔)")


def main() -> None:
    parser = argparse.ArgumentParser(description="短期波段掃描 CLI")
    parser.add_argument("--mode", choices=["preselect", "confirm", "check"], required=True)
    args = parser.parse_args()

    if args.mode == "preselect":
        cmd_preselect()
    elif args.mode == "confirm":
        cmd_confirm()
    elif args.mode == "check":
        cmd_check()


if __name__ == "__main__":
    main()
