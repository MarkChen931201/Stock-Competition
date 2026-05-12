"""廣域市場掃描器 — 每 5 分鐘輕量掃全市場，篩出異動股加入第二層監控。

兩層架構：
  第一層（本模組）：每 5 分鐘，用 TWSE 批次 API 掃全市場 ~1700 支
    → 找出「漲跌幅 > threshold 或成交量異常」的前 N 名
    → 寫入 hot_symbols 共享集合

  第二層（scheduler）：讀取 hot_symbols，對這些股票額外跑完整策略

TWSE 批次 API 限制：每批 50 支，間隔 1 秒
  ~1700 / 50 = 34 批 × 1s = 34s 查詢 + 270s 等待 = 304s ≈ 12 req/min → 安全
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import yaml
from loguru import logger

_TWSE_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://mis.twse.com.tw/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}
_SCAN_INTERVAL = 120    # 2 分鐘掃一次（從 5 分鐘加快）
_BATCH_SIZE    = 50     # 每批 50 檔
_BATCH_DELAY   = 1.2    # 批次間隔秒數
_TOP_N         = 50     # 每次篩出前 N 個異動股（從 30 擴大）


class BroadScanner:
    """全市場輕量掃描器，動態更新 hot_symbols。

    用法：
        scanner = BroadScanner(all_symbols, hot_symbols_set)
        await scanner.run()    # 持續跑直到取消
    """

    def __init__(
        self,
        all_symbols: list[str],
        hot_symbols: set[str],
        change_threshold: float = 0.02,   # 漲跌幅 > 2% 視為異動
        vol_ratio_threshold: float = 2.0, # 成交量 > 前20日均量 2 倍
        top_n: int = _TOP_N,
    ):
        self.all_symbols   = all_symbols
        self.hot_symbols   = hot_symbols   # 共享給 scheduler 讀取
        self._change_th    = change_threshold
        self._vol_ratio_th = vol_ratio_threshold
        self._top_n        = top_n
        self._running      = False

    async def run(self) -> None:
        self._running = True
        logger.info(f"廣域掃描器啟動，每 {_SCAN_INTERVAL}s 掃 {len(self.all_symbols)} 檔")
        while self._running:
            now = datetime.now()
            hour, minute = now.hour, now.minute

            # 非交易時段（09:00 前或 13:30 後）不查詢 TWSE
            in_market = (9, 0) <= (hour, minute) <= (13, 30)
            if not in_market:
                # 收盤後清空熱門池，避免昨日資料影響明日
                if (hour, minute) > (13, 30) and self.hot_symbols:
                    self.hot_symbols.clear()
                    logger.debug("收盤後清空熱門股池")
                await asyncio.sleep(60)   # 非交易時段每分鐘確認一次
                continue

            try:
                await self._scan_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"廣域掃描錯誤：{e}")
            await asyncio.sleep(_SCAN_INTERVAL)

    async def stop(self) -> None:
        self._running = False

    async def _scan_once(self) -> None:
        candidates: list[dict] = []

        async with httpx.AsyncClient(headers=_HEADERS, timeout=8.0) as http:
            for i in range(0, len(self.all_symbols), _BATCH_SIZE):
                batch = self.all_symbols[i: i + _BATCH_SIZE]
                ex_ch = "|".join(f"tse_{s}.tw" for s in batch)
                for attempt in range(2):
                    try:
                        resp = await http.get(
                            _TWSE_API,
                            params={"ex_ch": ex_ch, "json": "1", "delay": "0"},
                        )
                        resp.raise_for_status()
                        for item in resp.json().get("msgArray", []):
                            parsed = self._parse_item(item)
                            if parsed:
                                candidates.append(parsed)
                        break
                    except Exception as e:
                        if attempt == 0:
                            await asyncio.sleep(1.0)
                        else:
                            logger.debug(f"廣域掃描 batch {i} 失敗：{e}")
                await asyncio.sleep(_BATCH_DELAY)

        if not candidates:
            logger.warning("廣域掃描：TWSE 無回應（可能被限速）")
            return

        # 篩選：漲跌幅 > threshold 或成交量異常
        movers = [
            c for c in candidates
            if abs(c["change_pct"]) >= self._change_th
            or c.get("vol_ratio", 0) >= self._vol_ratio_th
        ]

        # 依「漲跌幅絕對值 + 成交量比」綜合分數排序
        movers.sort(
            key=lambda x: abs(x["change_pct"]) * 0.6 + min(x.get("vol_ratio", 0), 5) * 0.4,
            reverse=True,
        )

        new_hot = {m["symbol"] for m in movers[: self._top_n]}
        added   = new_hot - self.hot_symbols
        removed = self.hot_symbols - new_hot

        self.hot_symbols.clear()
        self.hot_symbols.update(new_hot)

        logger.info(
            f"廣域掃描完成：{len(candidates)} 檔 → {len(movers)} 異動 → "
            f"熱門池 {len(self.hot_symbols)} 檔 "
            f"(+{len(added)} -{len(removed)})"
        )
        if added:
            logger.info(f"  新增熱門：{sorted(added)}")

    @staticmethod
    def _parse_item(item: dict) -> dict | None:
        try:
            symbol = item.get("c", "").strip()
            if not symbol or not symbol.isdigit():
                return None

            def _f(key: str) -> float:
                v = item.get(key, "-")
                return float(v) if v and v not in ("-", "") else 0.0

            z = item.get("z", "-")
            last = float(z) if z not in ("-", "") else _f("o")
            prev = _f("y")
            vol  = _f("v")   # 張（TWSE v 欄位）

            if last <= 0 or prev <= 0:
                return None

            change_pct = (last - prev) / prev

            return {
                "symbol":     symbol,
                "last":       last,
                "prev_close": prev,
                "change_pct": change_pct,
                "volume":     vol,
                "vol_ratio":  0.0,  # 無歷史均量時先設 0
            }
        except Exception:
            return None


def load_all_twse_symbols(universe_path: str) -> list[str]:
    """從 universe.yaml 之外，額外載入所有常見上市股代號供廣域掃描。
    目前用硬編碼的 1xxx~9xxx 全掃，實際上線後可換成 FinMind 清單。
    """
    # 先用 universe.yaml 的標的
    with open(universe_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    base = data.get("seed_universe", []) + data.get("etf_universe", [])

    # 補充常見大型股（避免 FinMind 查詢太慢）
    extra = [
        # 額外補充未在 universe 的高流動性股
        "2886", "2887", "2883", "2888", "2890", "2885",
        "2610", "2618", "2634", "2606", "2637",
        "1402", "1434", "1440", "1605", "1504",
        "2912", "2915", "2903", "9917", "1802",
        "3481", "2409", "2344", "2408", "6770", "2337",
        "2356", "2353", "3706", "2313", "3037",
    ]
    all_syms = list(dict.fromkeys(base + extra))
    # 只保留純數字 4-5 碼
    return [s for s in all_syms if s.isdigit() and 4 <= len(s) <= 5]
