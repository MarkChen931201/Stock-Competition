"""FastAPI 入口 — 健康檢查 + 手動觸發盤前篩選。

端點：
  GET  /health          系統健康狀態（給 Docker healthcheck 用）
  GET  /stats           今日訊號推播統計
  POST /screener/run    手動觸發盤前篩選並推播至 Discord
  POST /signal/test     手動發測試訊號（驗證 Discord 推播是否正常）
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import BackgroundTasks, FastAPI, HTTPException
from loguru import logger
from pydantic import BaseModel

from config.settings import settings
from src.notifier.discord_bot import DiscordNotifier
from src.scheduler import IntraDayScheduler

# 全域排程器（lifespan 啟動時建立）
_scheduler: IntraDayScheduler | None = None
_scheduler_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan：啟動時初始化排程器，關閉時清理。"""
    global _scheduler, _scheduler_task

    now = datetime.now()
    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_close = now.replace(hour=13, minute=25, second=0, microsecond=0)

    if market_open <= now <= market_close:
        # 盤中時間 → 自動啟動排程器
        logger.info("盤中時間，自動啟動 IntraDayScheduler…")
        _scheduler = IntraDayScheduler()
        _scheduler_task = asyncio.create_task(_scheduler.run())
    else:
        logger.info(f"現在 {now.strftime('%H:%M')} 非盤中，排程器待命（可手動觸發）")
        _scheduler = IntraDayScheduler()

    yield

    # 關閉
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    logger.info("FastAPI 關閉，排程器已停止")


app = FastAPI(
    title="Stock-Competition 當沖訊號系統",
    description="台股即時當沖訊號推播 API",
    version="0.1.0",
    lifespan=lifespan,
)


# --- 健康檢查 ---

@app.get("/health", tags=["系統"])
def health():
    return {
        "status": "ok",
        "time": datetime.now().isoformat(),
        "scheduler_running": _scheduler_task is not None and not _scheduler_task.done(),
    }


# --- 今日統計 ---

@app.get("/stats", tags=["系統"])
def stats():
    if _scheduler is None:
        return {"sent": 0, "rejected_cost": 0, "rejected_dedup": 0}
    return _scheduler._dispatcher.stats


# --- 手動啟動排程器（非盤中時測試用）---

@app.post("/scheduler/start", tags=["系統"])
async def start_scheduler(background_tasks: BackgroundTasks):
    global _scheduler, _scheduler_task

    if _scheduler_task and not _scheduler_task.done():
        raise HTTPException(status_code=409, detail="排程器已在執行中")

    if _scheduler is None:
        _scheduler = IntraDayScheduler()

    _scheduler_task = asyncio.create_task(_scheduler.run())
    return {"status": "started"}


@app.post("/scheduler/stop", tags=["系統"])
async def stop_scheduler():
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        return {"status": "stopped"}
    return {"status": "not_running"}


# --- 手動觸發盤前篩選 ---

class ScreenerRequest(BaseModel):
    top_n: int = 30
    notify: bool = True  # 是否推播至 Discord


@app.post("/screener/run", tags=["盤前"])
async def run_screener(req: ScreenerRequest):
    """手動跑盤前篩選，回傳候選股清單並（可選）推播 Discord。"""
    try:
        from scripts.prefetch_universe import run_screener as _run
        candidates = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _run(top_n=req.top_n, notify=req.notify),
        )
        return {
            "count": len(candidates),
            "candidates": candidates,
        }
    except Exception as e:
        logger.exception(f"/screener/run 失敗：{e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- 手動測試訊號推播 ---

class TestSignalRequest(BaseModel):
    symbol: str = "2330"
    name: str = "台積電"
    direction: str = "LONG"   # "LONG" or "SHORT"
    price: float = 900.0


@app.post("/signal/test", tags=["測試"])
def test_signal(req: TestSignalRequest):
    """發一筆假訊號，驗證 Discord Embed 格式是否正確。"""
    from src.notifier.embed_builder import build_signal_embed
    from src.risk.cost_calculator import AssetType, calc_round_trip_cost
    from src.strategies.base import Direction, Signal, SignalType

    try:
        direction = Direction(req.direction.upper())
    except ValueError:
        raise HTTPException(status_code=400, detail="direction 必須是 LONG 或 SHORT")

    signal = Signal(
        symbol=req.symbol,
        name=req.name,
        direction=direction,
        signal_type=SignalType.ENTRY,
        trigger_price=req.price,
        strategy_name="測試訊號",
        stop_loss=round(req.price * 0.992, 2),
        take_profit=round(req.price * 1.012, 2),
        reason="手動觸發的測試訊號，請忽略",
    )

    cost = calc_round_trip_cost(
        buy_price=req.price,
        sell_price=signal.take_profit,
        lots=1,
        asset=AssetType.STOCK,
    )

    embed = build_signal_embed(signal, cost)
    notifier = DiscordNotifier(webhook_url=settings.discord_webhook_url)
    ok = notifier._send(embed, content="🧪 測試訊號")

    return {
        "success": ok,
        "symbol": req.symbol,
        "trigger_price": req.price,
        "breakeven_sell_price": cost.breakeven_sell_price,
        "total_cost": cost.total,
    }
