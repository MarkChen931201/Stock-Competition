"""正式啟動腳本 — 直接跑排程器，不透過 FastAPI（適合純 CLI 啟動）。

用法：
    python scripts/run_live.py

會在 09:00–13:25 持續執行，收盤後自動結束。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 讓 Python 找得到 src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from config.settings import settings
from src.scheduler import IntraDayScheduler


def _setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        colorize=True,
    )
    logger.add(
        "logs/live_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="1 day",
        retention="7 days",
        encoding="utf-8",
    )


async def main() -> None:
    _setup_logging()
    logger.info("🚀 Stock-Competition 當沖訊號系統啟動")
    logger.info(f"Fugle API Key: {'已設定' if settings.fugle_api_key else '❌ 未設定'}")
    logger.info(f"Discord Webhook: {'已設定' if settings.discord_webhook_url else '❌ 未設定'}")

    scheduler = IntraDayScheduler()
    await scheduler.run()
    logger.info("✅ 系統正常結束")


if __name__ == "__main__":
    asyncio.run(main())
