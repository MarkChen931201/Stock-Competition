"""黑名單工具 — 載入 config/blacklist.yaml，提供查詢函數。

設計：
  - 模組級單例載入（啟動一次）
  - 提供 is_blacklisted(symbol) -> bool 查詢
  - 黑名單股票不發訊號，但 broad_scanner / morning_ranking 仍可掃
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from loguru import logger


_BLACKLIST_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "blacklist.yaml"


@lru_cache(maxsize=1)
def _load_blacklist() -> dict[str, str]:
    """載入黑名單 → {symbol: reason}。"""
    if not _BLACKLIST_PATH.exists():
        return {}
    try:
        with open(_BLACKLIST_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        bl = data.get("blacklist", {}) or {}
        if bl:
            logger.info(f"已載入訊號黑名單 {len(bl)} 檔：{list(bl.keys())}")
        return bl
    except Exception as e:
        logger.warning(f"載入黑名單失敗：{e}")
        return {}


def is_blacklisted(symbol: str) -> bool:
    """判斷股票是否在黑名單。"""
    return symbol in _load_blacklist()


def reason(symbol: str) -> str:
    """取得加入黑名單的原因。"""
    return _load_blacklist().get(symbol, "")


def all_blacklisted() -> list[str]:
    return list(_load_blacklist().keys())
