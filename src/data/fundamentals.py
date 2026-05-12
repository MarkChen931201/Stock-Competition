"""基本面資料抓取 — FinMind 月營收、EPS、PER。

加快取機制：同一交易日內只抓一次。
若 FinMind 失敗（限額、付費限制），回傳 None 不影響系統運行。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from loguru import logger


@dataclass
class FundamentalData:
    """單檔股票的基本面快照。"""
    symbol: str
    eps_latest: float | None = None       # 最新季 EPS
    eps_quarter: str | None = None         # 季別（例：2024Q4）
    revenue_yoy_pct: float | None = None   # 最新月營收 YoY%
    revenue_mom_pct: float | None = None   # 最新月營收 MoM%
    revenue_month: str | None = None        # 月份（例：2025-04）
    per: float | None = None                # 本益比
    pbr: float | None = None                # 股價淨值比
    dividend_yield: float | None = None    # 殖利率（%）

    def has_data(self) -> bool:
        return any([self.eps_latest, self.revenue_yoy_pct, self.per])

    def to_dict(self) -> dict:
        return asdict(self)


class FundamentalsFetcher:
    """基本面資料抓取 + 快取。

    用 JSON 檔快取在 data/fundamentals_cache.json，
    每日重新抓取一次，盤後寫入。
    """

    def __init__(self, token: str, cache_path: Path | str | None = None):
        self.token = token
        if cache_path is None:
            cache_path = Path(__file__).resolve().parent.parent.parent / "data" / "fundamentals_cache.json"
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict] = {}
        self._cache_date: str | None = None
        self._load_cache()

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            self._cache_date = data.get("date")
            self._cache = data.get("symbols", {})
        except Exception:
            self._cache = {}

    def _save_cache(self) -> None:
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump({
                "date": self._cache_date or date.today().isoformat(),
                "symbols": self._cache,
            }, f, ensure_ascii=False, indent=2)

    def _is_cache_fresh(self) -> bool:
        return self._cache_date == date.today().isoformat()

    def fetch_batch(self, symbols: list[str]) -> dict[str, FundamentalData]:
        """批次抓取多檔股票基本面（自動用快取）。

        Returns:
            {symbol: FundamentalData}（缺資料的會有空欄位）
        """
        # 若快取是今日的，直接讀
        if self._is_cache_fresh():
            return {
                sid: FundamentalData(**self._cache[sid])
                for sid in symbols if sid in self._cache
            }

        # 抓新的（會耗時間，每檔 ~0.5-1s）
        logger.info(f"抓取基本面資料：{len(symbols)} 檔...")
        results: dict[str, FundamentalData] = {}
        for sid in symbols:
            try:
                data = self._fetch_one(sid)
                results[sid] = data
                self._cache[sid] = data.to_dict()
            except Exception as e:
                logger.debug(f"[{sid}] 基本面抓取失敗：{e}")
                results[sid] = FundamentalData(symbol=sid)
                self._cache[sid] = results[sid].to_dict()

        self._cache_date = date.today().isoformat()
        self._save_cache()
        logger.info(f"基本面快取已更新（{len(results)} 檔）")
        return results

    def _fetch_one(self, symbol: str) -> FundamentalData:
        """抓單檔基本面（EPS / 月營收 / PER）。"""
        import httpx

        fd = FundamentalData(symbol=symbol)
        base = "https://api.finmindtrade.com/api/v4/data"
        end_date = date.today()
        start_date = end_date - timedelta(days=365)

        with httpx.Client(timeout=15.0) as client:
            # 1. 月營收（最新 3 個月）
            try:
                r = client.get(base, params={
                    "dataset": "TaiwanStockMonthRevenue",
                    "data_id": symbol,
                    "start_date": (end_date - timedelta(days=120)).isoformat(),
                    "end_date": end_date.isoformat(),
                    "token": self.token,
                })
                body = r.json()
                if body.get("status") == 200 and body.get("data"):
                    # 抓「足夠長」的歷史（拉到 14 個月，方便算 YoY）
                    r2 = client.get(base, params={
                        "dataset": "TaiwanStockMonthRevenue",
                        "data_id": symbol,
                        "start_date": (end_date - timedelta(days=450)).isoformat(),
                        "end_date": end_date.isoformat(),
                        "token": self.token,
                    })
                    body2 = r2.json()
                    data = sorted(body2.get("data", []), key=lambda x: x.get("date", ""))
                    if len(data) >= 1:
                        latest = data[-1]
                        fd.revenue_month = latest.get("date", "")[:7]

                        curr_rev = float(latest.get("revenue") or 0)
                        # MoM：當月 vs 上月
                        if len(data) >= 2:
                            prev_rev = float(data[-2].get("revenue") or 0)
                            if prev_rev > 0:
                                fd.revenue_mom_pct = round((curr_rev - prev_rev) / prev_rev * 100, 1)
                        # YoY：當月 vs 去年同月（往回找 12 個月）
                        if len(data) >= 13:
                            yoy_rev = float(data[-13].get("revenue") or 0)
                            if yoy_rev > 0:
                                fd.revenue_yoy_pct = round((curr_rev - yoy_rev) / yoy_rev * 100, 1)
            except Exception:
                pass

            # 2. PER / PBR
            try:
                r = client.get(base, params={
                    "dataset": "TaiwanStockPER",
                    "data_id": symbol,
                    "start_date": (end_date - timedelta(days=14)).isoformat(),
                    "end_date": end_date.isoformat(),
                    "token": self.token,
                })
                body = r.json()
                if body.get("status") == 200 and body.get("data"):
                    latest = body["data"][-1]
                    per = latest.get("PER")
                    pbr = latest.get("PBR")
                    dy  = latest.get("dividend_yield") or latest.get("dividendYield")
                    fd.per = round(float(per), 1) if per else None
                    fd.pbr = round(float(pbr), 2) if pbr else None
                    fd.dividend_yield = round(float(dy), 2) if dy else None
            except Exception:
                pass

            # 3. EPS（季報，較慢，可選）
            try:
                r = client.get(base, params={
                    "dataset": "TaiwanStockFinancialStatements",
                    "data_id": symbol,
                    "start_date": (end_date - timedelta(days=400)).isoformat(),
                    "end_date": end_date.isoformat(),
                    "token": self.token,
                })
                body = r.json()
                if body.get("status") == 200 and body.get("data"):
                    eps_rows = [r for r in body["data"] if r.get("type") == "EPS"]
                    if eps_rows:
                        latest = sorted(eps_rows, key=lambda x: x["date"])[-1]
                        eps_val = latest.get("value")
                        date_str = latest.get("date", "")
                        if eps_val is not None:
                            fd.eps_latest = round(float(eps_val), 2)
                            # 把日期轉成季別（粗略）
                            try:
                                d = datetime.strptime(date_str, "%Y-%m-%d").date()
                                quarter = (d.month - 1) // 3 + 1
                                fd.eps_quarter = f"{d.year}Q{quarter}"
                            except Exception:
                                fd.eps_quarter = date_str
            except Exception:
                pass

        return fd
