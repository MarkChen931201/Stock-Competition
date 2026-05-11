"""TWSE 早盤排行榜 — 查詢全市場累計成交量、成交額、振幅。

資料來源：TWSE getStockInfo API
欄位說明：
  c  = 代號  n  = 名稱  z  = 最新成交價
  v  = 累計成交量（股）  tlong = 累計成交額（元）
  h  = 當日最高  l  = 當日最低  y  = 昨收
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
_BATCH_SIZE = 50


@dataclass
class StockSnapshot:
    symbol: str
    name: str
    last_price: float
    prev_close: float     # 昨收價
    high: float           # 今日最高
    low: float            # 今日最低
    volume_lots: float    # 累計成交量（張）
    turnover_k: float     # 累計成交額（千元）

    @property
    def turnover_m(self) -> float:
        return self.turnover_k / 1000

    @property
    def amplitude_pct(self) -> float:
        """振幅 = (今日最高 - 今日最低) / 昨收，昨收為 0 時回傳 0。"""
        if self.prev_close <= 0:
            return 0.0
        return (self.high - self.low) / self.prev_close


async def fetch_ranking(
    symbols: list[str],
    top_n: int = 30,
    timeout: float = 8.0,
) -> tuple[list[StockSnapshot], list[StockSnapshot], list[StockSnapshot]]:
    """查詢全市場即時資料，回傳三種排行榜。

    Returns:
        (volume_ranking, turnover_ranking, amplitude_ranking)
    """
    # 用 dict 去重，同一代號只保留最新一筆
    seen: dict[str, StockSnapshot] = {}

    async with httpx.AsyncClient(headers=_HEADERS, timeout=timeout) as http:
        for i in range(0, len(symbols), _BATCH_SIZE):
            batch = symbols[i: i + _BATCH_SIZE]
            ex_ch = "|".join(f"tse_{s}.tw" for s in batch)

            for attempt in range(2):
                try:
                    resp = await http.get(
                        _TWSE_API,
                        params={"ex_ch": ex_ch, "json": "1", "delay": "0"},
                    )
                    resp.raise_for_status()
                    for item in resp.json().get("msgArray", []):
                        snap = _parse(item)
                        if snap:
                            # 同代號取成交量較大者（避免重複時用舊資料蓋掉新資料）
                            existing = seen.get(snap.symbol)
                            if existing is None or snap.volume_lots >= existing.volume_lots:
                                seen[snap.symbol] = snap
                    break
                except Exception as e:
                    if attempt == 0:
                        logger.debug(f"TWSE batch {i} 重試：{e}")
                        await asyncio.sleep(1.0)
                    else:
                        logger.warning(f"TWSE batch {i}–{i+_BATCH_SIZE} 失敗：{e}")

            await asyncio.sleep(0.4)

    snapshots = list(seen.values())
    if not snapshots:
        return [], [], []

    vol_rank   = sorted(snapshots, key=lambda s: s.volume_lots,    reverse=True)[:top_n]
    turn_rank  = sorted(snapshots, key=lambda s: s.turnover_k,     reverse=True)[:top_n]
    amp_rank   = sorted(snapshots, key=lambda s: s.amplitude_pct,  reverse=True)[:top_n]
    return vol_rank, turn_rank, amp_rank


def _parse(item: dict) -> StockSnapshot | None:
    try:
        symbol = item.get("c", "").strip()
        name   = item.get("n", "").strip()
        if not symbol:
            return None

        def _f(key: str) -> float:
            v = item.get(key, "-")
            return float(v) if v and v not in ("-", "") else 0.0

        # TWSE getStockInfo 欄位說明（實測確認）：
        # z = 最新成交價（撮合中可能為 "-"，fallback 用 o 開盤價）
        # y = 昨收價
        # h = 今日最高, l = 今日最低
        # v = 累計成交量，單位「張」（= 千股），不需再除 1000
        # tlong = 最後更新的 Unix 時間戳（毫秒），非成交額，不可使用
        # 成交額需自行估算：volume_lots × last_price（元/張）

        raw_z = item.get("z", "-")
        raw_o = item.get("o", "-")
        last_price = _f("z") if raw_z not in ("-", "") else _f("o")

        prev_close  = _f("y")
        high        = _f("h")
        low         = _f("l")
        volume_lots = _f("v")          # 已是張，不需除以 1000

        if volume_lots <= 0:
            return None

        # 成交額估算（元）= 張數 × 每張股數 × 參考價
        ref_price   = last_price if last_price > 0 else prev_close
        turnover_k  = volume_lots * ref_price * 1000 / 1000  # → 千元
        # 即 volume_lots * ref_price（因為 1張=1000股，千元/1000=元，消掉）

        return StockSnapshot(
            symbol=symbol, name=name,
            last_price=last_price, prev_close=prev_close,
            high=high, low=low,
            volume_lots=volume_lots, turnover_k=turnover_k,
        )
    except Exception as e:
        logger.debug(f"parse 失敗：{e}")
        return None
