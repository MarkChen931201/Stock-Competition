"""Fugle MarketData WebSocket client — 即時 tick & 1分K 訂閱。

架構說明：
- FugleWebSocketClient 管理一條 WebSocket 連線，可同時訂閱多支股票。
- 每收到一筆 trade tick，會呼叫所有已註冊的 on_tick callback。
- 每收到一根 1分K 更新，會呼叫所有已註冊的 on_bar callback。
- 使用 asyncio 原生 websockets 庫，避免 SDK 版本鎖定。

Fugle WebSocket 文件參考：
  https://developer.fugle.tw/docs/marketdata/websocket-api/intraday/trade
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

import websockets
from loguru import logger

# Fugle WebSocket endpoint（v1.0，不含 intraday）
_WS_BASE = "wss://api.fugle.tw/marketdata/v1.0/stock/streaming"

# 心跳間隔（秒）
_HEARTBEAT_INTERVAL = 30


@dataclass
class Tick:
    """單筆成交 tick 資料。"""
    symbol: str          # 股票代號，例如 "2330"
    price: float         # 成交價
    volume: int          # 成交量（股）
    side: str            # "Buy" / "Sell" / ""
    timestamp: datetime  # 成交時間（台北時區）


@dataclass
class Bar:
    """1 分鐘 K 棒（即時更新，每分鐘收盤後最終確認）。"""
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int          # 該分鐘成交量（股）
    timestamp: datetime  # K 棒起始時間


# callback 型別別名
TickCallback = Callable[[Tick], Awaitable[None]]
BarCallback = Callable[[Bar], Awaitable[None]]


class FugleWebSocketClient:
    """非同步 Fugle WebSocket 訂閱管理器。

    用法：
        client = FugleWebSocketClient(api_key="xxx")
        client.add_symbols(["2330", "2317"])
        client.on_tick(my_tick_handler)
        client.on_bar(my_bar_handler)
        await client.run()   # 阻塞直到被取消
    """

    def __init__(self, api_key: str, reconnect_delay: float = 5.0):
        if not api_key:
            raise ValueError("FUGLE_API_KEY is required")
        self._api_key = api_key
        self._reconnect_delay = reconnect_delay

        # 要訂閱的股票代號集合
        self._symbols: set[str] = set()

        # 已登記的 callback 清單
        self._tick_callbacks: list[TickCallback] = []
        self._bar_callbacks: list[BarCallback] = []

        # 各標的最新一根 1分K（用於組合 bar 資料）
        self._current_bars: dict[str, dict] = defaultdict(dict)

        self._running = False
        self._ws: websockets.WebSocketClientProtocol | None = None

    # --- 設定介面 ---

    def add_symbols(self, symbols: list[str]) -> None:
        """新增要訂閱的股票代號（可在 run() 前後呼叫）。"""
        self._symbols.update(symbols)

    def remove_symbols(self, symbols: list[str]) -> None:
        self._symbols.difference_update(symbols)

    def on_tick(self, cb: TickCallback) -> None:
        """註冊 tick 處理函式（async def handler(tick: Tick)）。"""
        self._tick_callbacks.append(cb)

    def on_bar(self, cb: BarCallback) -> None:
        """註冊 1分K 處理函式（async def handler(bar: Bar)）。"""
        self._bar_callbacks.append(cb)

    # --- 主要執行迴圈 ---

    async def run(self) -> None:
        """啟動 WebSocket 連線，斷線後自動重連，直到外部取消。"""
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                logger.info("FugleWebSocketClient cancelled, shutting down.")
                self._running = False
                break
            except Exception as e:
                logger.warning(f"WebSocket 連線中斷：{e}，{self._reconnect_delay}s 後重連…")
                await asyncio.sleep(self._reconnect_delay)

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    # --- 私有方法 ---

    async def _connect_and_listen(self) -> None:
        logger.info(f"連線至 Fugle WebSocket：{_WS_BASE}")

        async with websockets.connect(_WS_BASE, ping_interval=None) as ws:
            self._ws = ws

            # Step 1: 連線後先送 auth event（Fugle v2 SDK 的認證方式）
            await ws.send(json.dumps({
                "event": "auth",
                "data": {"apikey": self._api_key}
            }))

            # 等待 authenticated 回應（最多 5 秒）
            auth_ok = False
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                msg = json.loads(raw)
                if msg.get("event") == "authenticated":
                    auth_ok = True
                    logger.info("Fugle WebSocket 認證成功，開始訂閱…")
                else:
                    logger.warning(f"Fugle 認證回應異常：{msg}")
            except asyncio.TimeoutError:
                logger.warning("Fugle 認證逾時")

            if not auth_ok:
                return

            # Step 2: 訂閱所有股票
            await self._subscribe_all(ws)

            # 並行執行：收訊息 + 定期心跳
            receive_task = asyncio.create_task(self._receive_loop(ws))
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))

            try:
                await asyncio.gather(receive_task, heartbeat_task)
            finally:
                receive_task.cancel()
                heartbeat_task.cancel()
                self._ws = None

    async def _subscribe_all(self, ws: websockets.WebSocketClientProtocol) -> None:
        """對每支股票訂閱 trades 頻道。
        注意：免費方案上限 10 個訂閱，只訂 trades 不訂 candles。
        1分K 由 scheduler 每分鐘透過 REST API 補抓。
        """
        for symbol in self._symbols:
            await ws.send(json.dumps({
                "event": "subscribe",
                "data": {"channel": "trades", "symbol": symbol}
            }))
            logger.debug(f"已訂閱 trades: {symbol}")

    async def _receive_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """持續接收並分派 WebSocket 訊息。"""
        async for raw in ws:
            try:
                msg = json.loads(raw)
                await self._dispatch(msg)
            except json.JSONDecodeError:
                logger.warning(f"收到非 JSON 訊息：{raw!r}")
            except Exception as e:
                logger.exception(f"處理訊息時發生錯誤：{e}")

    async def _heartbeat_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """定期送 ping 保持連線（Fugle v2 ping 格式）。"""
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            try:
                await ws.send(json.dumps({"event": "ping", "data": {"state": "ping"}}))
                logger.debug("ping sent")
            except Exception:
                break

    async def _dispatch(self, msg: dict) -> None:
        """根據訊息 event/channel 路由到對應的處理器。
        Fugle v2 訊息格式：{"event": "...", "data": {...}}
        """
        event = msg.get("event", "")
        data = msg.get("data", {})

        if event in ("pong", "subscribed", "authenticated", "unsubscribed"):
            return
        if event == "error":
            logger.error(f"Fugle error：{msg}")
            return

        channel = data.get("channel", "") if isinstance(data, dict) else ""

        if channel == "trades":
            tick = self._parse_tick(data)
            if tick:
                await self._fire_tick(tick)
        elif channel == "candles":
            bar = self._parse_bar(data)
            if bar:
                await self._fire_bar(bar)

    def _parse_tick(self, data: dict) -> Tick | None:
        """把 Fugle trades channel payload 轉成 Tick dataclass。"""
        try:
            symbol = data.get("symbol", "")
            # Fugle 時間格式：ISO 8601，例如 "2024-05-11T09:00:01.000+08:00"
            ts_str = data.get("time") or data.get("at", "")
            ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now()

            return Tick(
                symbol=symbol,
                price=float(data.get("price", 0)),
                volume=int(data.get("volume", 0)),
                side=data.get("side", ""),
                timestamp=ts,
            )
        except Exception as e:
            logger.warning(f"parse_tick 失敗：{e}，原始資料：{data}")
            return None

    def _parse_bar(self, data: dict) -> Bar | None:
        """把 Fugle candles channel payload 轉成 Bar dataclass。"""
        try:
            symbol = data.get("symbol", "")
            ts_str = data.get("time") or data.get("at", "")
            ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now()

            return Bar(
                symbol=symbol,
                open=float(data.get("open", 0)),
                high=float(data.get("high", 0)),
                low=float(data.get("low", 0)),
                close=float(data.get("close", 0)),
                volume=int(data.get("volume", 0)),
                timestamp=ts,
            )
        except Exception as e:
            logger.warning(f"parse_bar 失敗：{e}，原始資料：{data}")
            return None

    async def _fire_tick(self, tick: Tick) -> None:
        for cb in self._tick_callbacks:
            try:
                await cb(tick)
            except Exception as e:
                logger.exception(f"tick callback 錯誤：{e}")

    async def _fire_bar(self, bar: Bar) -> None:
        for cb in self._bar_callbacks:
            try:
                await cb(bar)
            except Exception as e:
                logger.exception(f"bar callback 錯誤：{e}")
