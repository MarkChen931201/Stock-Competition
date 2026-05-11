"""籌碼面資料 — 每日盤前從 FinMind 抓昨日外資/投信淨買超。

快取在記憶體，每日更新一次（收盤後或隔日盤前）。
用法：
    loader = InstitutionalLoader(finmind_token)
    loader.load(date.today() - timedelta(days=1))
    direction = loader.get_direction("2303")   # "buy" / "sell" / "neutral"
"""
from __future__ import annotations

from datetime import date, timedelta

import httpx
from loguru import logger

_FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"


class InstitutionalLoader:
    def __init__(self, token: str):
        self._token = token
        self._data: dict[str, dict] = {}   # symbol → {fii_net, sit_net}
        self._loaded_date: date | None = None

    def load(self, target_date: date | None = None) -> None:
        """下載指定日期的三大法人買賣超（預設昨日）。"""
        if target_date is None:
            target_date = date.today() - timedelta(days=1)

        if self._loaded_date == target_date:
            return   # 已是最新，不重複下載

        logger.info(f"下載 {target_date} 三大法人資料…")
        params = {
            "dataset":    "TaiwanStockInstitutionalInvestors",
            "start_date": target_date.isoformat(),
            "end_date":   target_date.isoformat(),
            "token":      self._token,
        }
        try:
            r = httpx.get(_FINMIND_BASE, params=params, timeout=20)
            r.raise_for_status()
            body = r.json()
            if body.get("status") != 200:
                logger.warning(f"FinMind 籌碼面查詢失敗：{body}")
                return
            rows = body.get("data", [])
        except Exception as e:
            logger.warning(f"籌碼面下載失敗：{e}")
            return

        self._data.clear()
        for row in rows:
            sym  = str(row.get("stock_id", ""))
            name = row.get("name", "")
            # 外資淨買超（Foreign_Investor_net）
            fii_net = float(row.get("Foreign_Investor_net", 0) or 0)
            # 投信淨買超（Investment_Trust_net）
            sit_net = float(row.get("Investment_Trust_net", 0) or 0)
            if sym:
                self._data[sym] = {
                    "fii_net": fii_net,
                    "sit_net": sit_net,
                    "combined": fii_net + sit_net,
                }

        self._loaded_date = target_date
        logger.info(f"籌碼面載入完成：{len(self._data)} 檔")

    def get_direction(self, symbol: str) -> str:
        """回傳 'buy'、'sell' 或 'neutral'。"""
        d = self._data.get(symbol)
        if d is None:
            return "neutral"
        combined = d["combined"]
        if combined > 0:
            return "buy"
        elif combined < 0:
            return "sell"
        return "neutral"

    def get_score(self, symbol: str) -> float:
        """回傳籌碼分數（正 = 買超，負 = 賣超，0 = 無資料），用於訊號評分。"""
        d = self._data.get(symbol)
        if d is None:
            return 0.0
        combined = d["combined"]
        # 正規化：以 10 億為單位 clamp 到 [-1, 1]
        return max(-1.0, min(1.0, combined / 1_000_000_000))

    def summary(self, symbol: str) -> str:
        d = self._data.get(symbol)
        if d is None:
            return "無籌碼資料"
        fii_dir = "買" if d["fii_net"] > 0 else "賣"
        sit_dir = "買" if d["sit_net"] > 0 else "賣"
        fii_amt = abs(d["fii_net"] / 1e6)
        sit_amt = abs(d["sit_net"] / 1e6)
        return f"外資{fii_dir}{fii_amt:.1f}億 投信{sit_dir}{sit_amt:.1f}億"
