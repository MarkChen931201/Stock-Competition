"""FinMind v4 REST API client (歷史日K + 個股基本資訊).

用 httpx 直接打 REST, 不依賴 FinMind SDK 以降低相依風險.
若你已安裝 FinMind 套件, 兩者可並存.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import httpx
import pandas as pd
from loguru import logger

FINMIND_BASE_URL = "https://api.finmindtrade.com/api/v4/data"


class FinMindClient:
    def __init__(self, token: str, timeout: float = 20.0):
        if not token:
            logger.warning("FINMIND_TOKEN is empty - rate limits will be tight.")
        self.token = token
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FinMindClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # --- 內部 helper ---
    def _get(self, params: dict) -> pd.DataFrame:
        params = {**params, "token": self.token}
        r = self._client.get(FINMIND_BASE_URL, params=params)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != 200:
            raise RuntimeError(f"FinMind error: {body}")
        return pd.DataFrame(body.get("data", []))

    # --- public ---
    def stock_info(self) -> pd.DataFrame:
        """全市場個股清單 (含 industry_category, type, etc)."""
        return self._get({"dataset": "TaiwanStockInfo"})

    def daily_price(
        self,
        stock_id: str,
        start_date: date,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """日 K (含開高低收, 成交量, 成交金額)."""
        params = {
            "dataset": "TaiwanStockPrice",
            "data_id": stock_id,
            "start_date": start_date.isoformat(),
        }
        if end_date is not None:
            params["end_date"] = end_date.isoformat()
        df = self._get(params)
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)

    def shares_outstanding(self, stock_id: str) -> int | None:
        """流通股數 (用於計算週轉率). 若無資料回傳 None."""
        df = self._get(
            {"dataset": "TaiwanStockShareholding", "data_id": stock_id}
        )
        if df.empty:
            return None
        # 取最新一筆
        latest = df.sort_values("date").iloc[-1]
        # 欄位名為 NumberOfShareholders / TotalNumberOfShares 視 FinMind 版本
        for col in ("TotalNumberOfShares", "total_number_of_shares"):
            if col in latest:
                return int(latest[col])
        return None

    def daily_prices_batch(
        self,
        stock_ids: Iterable[str],
        lookback_days: int,
    ) -> dict[str, pd.DataFrame]:
        """批次抓取多檔的日 K (時間範圍以 lookback_days 反推)."""
        end = date.today()
        # 抓 lookback * 1.6 天以蓋掉週末/假日
        start = end - timedelta(days=int(lookback_days * 1.6) + 5)
        out: dict[str, pd.DataFrame] = {}
        for sid in stock_ids:
            try:
                df = self.daily_price(sid, start, end)
                if not df.empty:
                    out[sid] = df.tail(lookback_days).reset_index(drop=True)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{sid}] daily_price failed: {e}")
        return out
