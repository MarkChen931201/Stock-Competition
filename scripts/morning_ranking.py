"""早盤排行榜 — 09:30 推播全市場成交量 / 成交額 / 振幅 Top 30。

執行方式：
  等待 09:30 自動執行：  python scripts/morning_ranking.py
  立即查詢（盤中）：     python scripts/morning_ranking.py --now
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from config.settings import settings
from src.data.finmind_client import FinMindClient
from src.data.twse_ranking import StockSnapshot, fetch_ranking
from src.notifier.discord_bot import DiscordNotifier

TOP_N = 30
CHUNK = 10   # 每個 Discord field 最多幾筆（field 上限 1024 字元）


def _vol_row(rank: int, s: StockSnapshot) -> str:
    return "`{r:>2} {sym:<6} {name:<7} {price:>7.2f} {vol:>10,.0f}`".format(
        r=rank, sym=s.symbol, name=s.name[:5], price=s.last_price, vol=s.volume_lots
    )


def _turn_row(rank: int, s: StockSnapshot) -> str:
    return "`{r:>2} {sym:<6} {name:<7} {price:>7.2f} {turn:>9,.1f}`".format(
        r=rank, sym=s.symbol, name=s.name[:5], price=s.last_price, turn=s.turnover_m
    )


def _amp_row(rank: int, s: StockSnapshot) -> str:
    return "`{r:>2} {sym:<6} {name:<7} {price:>7.2f} {amp:>7.2%}`".format(
        r=rank, sym=s.symbol, name=s.name[:5], price=s.last_price, amp=s.amplitude_pct
    )


def _add_ranking_fields(embed, title_emoji: str, title: str, header: str,
                        items: list[StockSnapshot], row_fn) -> None:
    """把一個排行拆成每 CHUNK 筆一個 field 加入 embed。"""
    for chunk_start in range(0, len(items), CHUNK):
        chunk = items[chunk_start: chunk_start + CHUNK]
        start_rank = chunk_start + 1
        end_rank   = chunk_start + len(chunk)
        rows = [header] + [row_fn(chunk_start + j + 1, s) for j, s in enumerate(chunk)]
        field_name = (
            f"{title_emoji} {title} {start_rank}–{end_rank}"
            if chunk_start > 0
            else f"{title_emoji} {title}"
        )
        embed.add_embed_field(name=field_name, value="\n".join(rows), inline=False)


async def run(top_n: int = TOP_N) -> None:
    from discord_webhook import DiscordEmbed
    logger.info("開始抓取早盤排行榜…")

    # 取全市場股票清單
    try:
        client = FinMindClient(token=settings.finmind_token)
        info_df = client.stock_info()
        tse_df = info_df[info_df["type"].str.lower().isin(["twse", "上市"])]
        all_symbols = [
            s for s in tse_df["stock_id"].astype(str).tolist()
            if s.isdigit() and 4 <= len(s) <= 5
        ]
        logger.info(f"取得 {len(all_symbols)} 檔上市標的")
    except Exception as e:
        logger.warning(f"FinMind 失敗，改用 universe.yaml：{e}")
        import yaml
        from config.settings import PROJECT_ROOT
        with open(PROJECT_ROOT / "config" / "universe.yaml", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        all_symbols = data.get("seed_universe", []) + data.get("etf_universe", [])

    vol_rank, turn_rank, amp_rank = await fetch_ranking(all_symbols, top_n=top_n)

    if not vol_rank:
        logger.warning("未取得任何資料（可能在非交易時段）")
        return

    logger.info(f"成交量#1：{vol_rank[0].symbol} {vol_rank[0].name} {vol_rank[0].volume_lots:,.0f} 張")
    logger.info(f"成交額#1：{turn_rank[0].symbol} {turn_rank[0].name} {turn_rank[0].turnover_m:,.1f} 百萬")
    logger.info(f"振幅  #1：{amp_rank[0].symbol} {amp_rank[0].name} {amp_rank[0].amplitude_pct:.2%}")

    # 建立三則分開的 Embed（避免單一 embed 超過 Discord 6000 字元上限）
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    notifier = DiscordNotifier(webhook_url=settings.discord_webhook_url)

    # ── Embed 1：成交量 ──
    e1 = DiscordEmbed(
        title=f"🏆 早盤排行榜 | {now_str}",
        description=f"09:00–09:30 全市場統計，共掃描 {len(all_symbols)} 檔",
        color="f4a700",
    )
    e1.set_timestamp()
    _add_ranking_fields(
        e1, "📊", f"成交量 Top {top_n}（張）",
        "`  # 代號   名稱    最新價    成交量(張)`",
        vol_rank, _vol_row,
    )
    e1.set_footer(text="Stock-Competition · morning_ranking")
    notifier._send(e1, content="📢 早盤排行榜出爐！")

    # ── Embed 2：成交額 ──
    e2 = DiscordEmbed(color="2ecc71")
    _add_ranking_fields(
        e2, "💰", f"成交額 Top {top_n}（百萬元）",
        "`  # 代號   名稱    最新價   成交額(百萬)`",
        turn_rank, _turn_row,
    )
    e2.set_footer(text="Stock-Competition · morning_ranking")
    notifier._send(e2)

    # ── Embed 3：振幅 ──
    e3 = DiscordEmbed(color="e74c3c")
    _add_ranking_fields(
        e3, "📈", f"振幅 Top {top_n}",
        "`  # 代號   名稱    最新價     振幅`",
        amp_rank, _amp_row,
    )
    e3.set_footer(text="Stock-Competition · morning_ranking")
    ok = notifier._send(e3)

    logger.info(f"Discord 推播：{'✅ 成功' if ok else '❌ 失敗'}")


async def _wait_and_run() -> None:
    now = datetime.now()
    target = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now < target:
        wait_sec = (target - now).total_seconds()
        logger.info(f"等待至 09:30，剩 {wait_sec:.0f} 秒…")
        await asyncio.sleep(wait_sec)
    await run()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", action="store_true", help="立即查詢（需在盤中執行）")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO", colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")

    now = datetime.now()
    is_trading = now.replace(hour=9, minute=0, second=0) <= now <= now.replace(hour=13, minute=30, second=0)

    if not is_trading and args.now:
        logger.warning("⚠️  現在非交易時段（09:00–13:30），TWSE API 不提供即時資料")
        sys.exit(0)

    asyncio.run(run() if args.now else _wait_and_run())
