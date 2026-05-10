"""早盤排行榜腳本 — 09:30 推播全市場成交量 / 成交額 Top 20。

執行方式（兩種）：
  1. 手動立即查詢：
       python scripts/morning_ranking.py --now

  2. 等待 09:30 自動推播：
       python scripts/morning_ranking.py

整合至排程器：
  IntraDayScheduler 在 09:30 會自動呼叫本腳本的 run() 函式。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from discord_webhook import DiscordEmbed
from loguru import logger

from config.settings import settings
from src.data.finmind_client import FinMindClient
from src.data.twse_ranking import StockSnapshot, fetch_ranking
from src.notifier.discord_bot import DiscordNotifier


def _build_embed(
    vol_rank: list[StockSnapshot],
    turn_rank: list[StockSnapshot],
    as_of: datetime,
) -> DiscordEmbed:
    """組成雙欄排行榜 Embed。"""
    embed = DiscordEmbed(
        title=f"🏆 早盤排行榜 | {as_of:%Y-%m-%d %H:%M}",
        description="09:00–09:30 累計成交統計",
        color="f4a700",
    )
    embed.set_timestamp()

    # 成交量 Top 20
    vol_lines = ["`  # 代號   名稱       價格  成交量(張)`"]
    for i, s in enumerate(vol_rank[:20], 1):
        vol_lines.append(
            "`{rank:>2} {sym:<6} {name:<8} {price:>6.2f} {vol:>9,.0f}`".format(
                rank=i,
                sym=s.symbol,
                name=s.name[:6],
                price=s.last_price,
                vol=s.volume_lots,
            )
        )
    embed.add_embed_field(
        name="📊 成交量 Top 20（張）",
        value="\n".join(vol_lines[:11]),   # header + 10 筆（field 上限 1024）
        inline=False,
    )
    if len(vol_rank) > 10:
        embed.add_embed_field(
            name="📊 成交量 Top 11–20",
            value="\n".join(["`  # 代號   名稱       價格  成交量(張)`"] + vol_lines[11:]),
            inline=False,
        )

    # 成交額 Top 20
    turn_lines = ["`  # 代號   名稱       價格  成交額(百萬)`"]
    for i, s in enumerate(turn_rank[:20], 1):
        turn_lines.append(
            "`{rank:>2} {sym:<6} {name:<8} {price:>6.2f} {turn:>10,.1f}`".format(
                rank=i,
                sym=s.symbol,
                name=s.name[:6],
                price=s.last_price,
                turn=s.turnover_m,
            )
        )
    embed.add_embed_field(
        name="💰 成交額 Top 20（百萬元）",
        value="\n".join(turn_lines[:11]),
        inline=False,
    )
    if len(turn_rank) > 10:
        embed.add_embed_field(
            name="💰 成交額 Top 11–20",
            value="\n".join(["`  # 代號   名稱       價格  成交額(百萬)`"] + turn_lines[11:]),
            inline=False,
        )

    embed.set_footer(text="Stock-Competition · morning_ranking")
    return embed


async def run(top_n: int = 20) -> None:
    """抓取全市場（上市）標的並推播排行榜。"""
    logger.info("開始抓取早盤排行榜…")

    # 從 FinMind 取全市場上市股票清單
    try:
        client = FinMindClient(token=settings.finmind_token)
        info_df = client.stock_info()
        # 只取上市（type = 'twse'）、純股票（排除 ETF 避免混入）
        tse_df = info_df[
            info_df["type"].str.lower().isin(["twse", "上市"])
        ]
        # 只保留 4–5 碼純數字代號（排除債券 01xxxx、認購權證 0xxxxx 等）
        all_symbols = [
            s for s in tse_df["stock_id"].astype(str).tolist()
            if s.isdigit() and 4 <= len(s) <= 5
        ]
        logger.info(f"取得 {len(all_symbols)} 檔上市標的")
    except Exception as e:
        logger.warning(f"FinMind 取股票清單失敗，改用 universe.yaml：{e}")
        # fallback：用 universe.yaml 的種子股池
        import yaml
        from config.settings import PROJECT_ROOT
        with open(PROJECT_ROOT / "config" / "universe.yaml", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        all_symbols = data.get("seed_universe", []) + data.get("etf_universe", [])

    # 查詢 TWSE 即時資料
    vol_rank, turn_rank = await fetch_ranking(all_symbols, top_n=top_n)

    if not vol_rank:
        logger.warning("未取得任何排行資料（可能在非交易時段）")
        return

    logger.info(f"成交量第一：{vol_rank[0].symbol} {vol_rank[0].name} {vol_rank[0].volume_lots:,.0f} 張")
    logger.info(f"成交額第一：{turn_rank[0].symbol} {turn_rank[0].name} {turn_rank[0].turnover_m:,.1f} 百萬")

    # 推播 Discord
    embed = _build_embed(vol_rank, turn_rank, as_of=datetime.now())
    notifier = DiscordNotifier(webhook_url=settings.discord_webhook_url)
    ok = notifier._send(embed, content="📢 早盤排行榜出爐！")
    logger.info(f"Discord 推播：{'✅ 成功' if ok else '❌ 失敗'}")


async def _wait_and_run() -> None:
    """等到 09:30 再執行。"""
    now = datetime.now()
    target = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now >= target:
        # 已過 09:30，立即執行
        await run()
        return
    wait_sec = (target - now).total_seconds()
    logger.info(f"等待至 09:30，剩 {wait_sec:.0f} 秒…")
    await asyncio.sleep(wait_sec)
    await run()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", action="store_true", help="立即查詢，不等 09:30")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO", colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")

    now = datetime.now()
    is_trading = now.replace(hour=9, minute=0) <= now <= now.replace(hour=13, minute=30)

    if not is_trading and args.now:
        logger.warning("⚠️  現在非交易時段（09:00–13:30），TWSE API 不提供即時資料")
        logger.warning("    競賽當天盤中執行才會有真實排行榜。")
        sys.exit(0)

    if args.now:
        asyncio.run(run())
    else:
        asyncio.run(_wait_and_run())
