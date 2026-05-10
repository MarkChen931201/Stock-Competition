"""TWSE 早盤排行榜 — 查詢全市場（或指定清單）累計成交量與成交額。

資料來源：TWSE getStockInfo API（支援一次批次查多檔，用 | 分隔）
查詢時機：09:30，取當日累計成交量（張）與成交額（千元）。

欄位說明（TWSE API）：
  c  = 股票代號        n  = 股票名稱
  v  = 累計成交量（股）z  = 最新成交價
  tv = 最新單筆成交量  tlong = 累計成交額（元）
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from loguru import logger

_TWSE_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://mis.twse.com.tw/",
    "User-Agent": "Mozilla/5.0",
}
_BATCH_SIZE = 50   # 每次查幾檔（太大容易被擋）


@dataclass
class StockSnapshot:
    symbol: str
    name: str
    last_price: float
    volume_lots: float    # 累計成交量（張）
    turnover_k: float     # 累計成交額（千元）

    @property
    def turnover_m(self) -> float:
        """成交額（百萬元）。"""
        return self.turnover_k / 1000


async def fetch_ranking(
    symbols: list[str],
    top_n: int = 20,
    timeout: float = 8.0,
) -> tuple[list[StockSnapshot], list[StockSnapshot]]:
    """查詢所有標的的當日累計成交量與成交額，回傳排行榜。

    Args:
        symbols:  股票代號清單（純數字，不含 .tw）
        top_n:    各排行取前幾名
        timeout:  單次 HTTP 請求逾時秒數

    Returns:
        (volume_ranking, turnover_ranking)
        volume_ranking   依成交量由大到小
        turnover_ranking 依成交額由大到小
    """
    snapshots: list[StockSnapshot] = []

    async with httpx.AsyncClient(headers=_HEADERS, timeout=timeout) as http:
        # 分批查詢
        for i in range(0, len(symbols), _BATCH_SIZE):
            batch = symbols[i: i + _BATCH_SIZE]
            ex_ch = "|".join(f"tse_{s}.tw" for s in batch)
            for attempt in range(2):   # 最多重試 1 次
                try:
                    resp = await http.get(
                        _TWSE_API,
                        params={"ex_ch": ex_ch, "json": "1", "delay": "0"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    for item in data.get("msgArray", []):
                        snap = _parse(item)
                        if snap:
                            snapshots.append(snap)
                    break   # 成功就跳出重試
                except Exception as e:
                    if attempt == 0:
                        logger.debug(f"TWSE batch {i} 第一次失敗，重試：{e}")
                        await asyncio.sleep(1.0)
                    else:
                        logger.warning(f"TWSE batch {i}–{i+_BATCH_SIZE} 查詢失敗：{e}")
            # 避免過快打 TWSE，每批間隔 0.4 秒
            await asyncio.sleep(0.4)

    if not snapshots:
        return [], []

    vol_rank = sorted(snapshots, key=lambda s: s.volume_lots, reverse=True)[:top_n]
    turn_rank = sorted(snapshots, key=lambda s: s.turnover_k, reverse=True)[:top_n]
    return vol_rank, turn_rank


def _parse(item: dict) -> StockSnapshot | None:
    try:
        symbol = item.get("c", "").strip()
        name = item.get("n", "").strip()
        if not symbol:
            return None

        z = item.get("z", "-")
        last_price = float(z) if z and z != "-" else 0.0

        # 累計成交量（股）→ 張
        v = item.get("v", "0")
        volume_lots = float(v) / 1000 if v and v != "-" else 0.0

        # 累計成交額（元）→ 千元
        tlong = item.get("tlong", "0")
        turnover_k = float(tlong) / 1000 if tlong and tlong != "-" else 0.0

        # 過濾無效資料（開盤前成交量為 0）
        if volume_lots <= 0:
            return None

        return StockSnapshot(
            symbol=symbol,
            name=name,
            last_price=last_price,
            volume_lots=volume_lots,
            turnover_k=turnover_k,
        )
    except Exception as e:
        logger.debug(f"parse 失敗：{e}，item={item}")
        return None
