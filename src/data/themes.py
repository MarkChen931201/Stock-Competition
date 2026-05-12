"""股票題材標籤庫 — 提供「話題性」資訊給波段策略。

兩個來源：
  1. universe.yaml 的註解標題（如「半導體 / 面板」「AI 伺服器」）→ 產業類別
  2. 預先建立的熱門題材標籤（AI、CoWoS、HBM、軍工、低軌衛星等）

提供：
  get_themes(symbol) → ["半導體", "AI 伺服器", "CoWoS"]
  get_industry(symbol) → "半導體 / 面板"
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path


# 熱門題材手動標籤（最新的市場熱門概念股）
# 鍵：股票代號；值：題材列表
_HOT_THEMES: dict[str, list[str]] = {
    # AI / HPC / 伺服器
    "2330": ["AI 晶片代工", "CoWoS", "先進製程", "權值王"],
    "2454": ["AI 晶片", "5G", "手機 SoC", "權值股"],
    "2376": ["AI 伺服器", "GB200", "輝達概念"],
    "2382": ["AI 伺服器", "GB200", "輝達概念", "雲端"],
    "2317": ["AI 伺服器", "輝達合作", "GB200", "iPhone"],
    "2353": ["AI 伺服器", "PC 龍頭"],
    "2356": ["AI 伺服器", "代工"],
    "3231": ["AI 伺服器 ODM", "輝達合作"],
    "5274": ["散熱模組", "AI 伺服器", "輝達"],
    "3017": ["散熱模組", "輝達概念", "氣冷/水冷"],
    "6669": ["AI 伺服器", "輝達合作", "GB200"],
    "4938": ["AI 伺服器", "技嘉概念"],
    "2308": ["AI 伺服器電源", "電源管理"],

    # CoWoS / 先進封裝
    "3034": ["IC 設計", "AI 概念", "高速傳輸"],
    "3035": ["IP 設計", "矽智財", "AI"],
    "3443": ["IP 設計", "矽智財", "CoWoS"],
    "2379": ["AI 晶片", "IC 設計", "IP"],
    "3711": ["先進封裝", "CoWoS 設備", "AI"],
    "6488": ["矽晶圓", "AI 受惠", "先進製程"],
    "8016": ["IC 設計", "電源管理"],
    "5347": ["先進封裝", "晶圓代工"],

    # HBM / 記憶體
    "2344": ["記憶體", "HBM", "DRAM"],
    "2408": ["記憶體", "DRAM", "AI 概念"],
    "2337": ["NAND Flash", "車用記憶體"],
    "6770": ["IC 設計", "晶圓代工", "成熟製程"],

    # 面板
    "3481": ["面板", "車用面板", "Micro LED"],
    "2409": ["面板", "車用面板", "Micro LED"],

    # PCB / ABF
    "3037": ["PCB", "AI 伺服器 PCB"],
    "6213": ["PCB", "AI 伺服器"],
    "3036": ["IC 通路", "AI 概念", "輝達代理"],
    "6285": ["EMS", "蘋果概念"],
    "8046": ["ABF 載板", "AI 概念"],
    "6116": ["PCB", "車用"],

    # 軍工 / 國防
    "3324": ["軍工", "國防"],
    "8033": ["軍工", "雷虎"],

    # 重電 / 電網
    "1513": ["重電", "電網升級", "綠能"],
    "1504": ["重電", "電網升級", "風電"],
    "1519": ["重電", "電網"],

    # 車用
    "2615": ["航運", "貨櫃"],
    "2603": ["航運", "貨櫃"],
    "2606": ["航運"],
    "2618": ["航空", "復航"],
    "2610": ["航空"],

    # 金融
    "2880": ["金融", "華南金"],
    "2881": ["金融", "富邦金", "壽險"],
    "2882": ["金融", "國泰金", "壽險"],
    "2884": ["金融", "玉山金"],
    "2885": ["金融", "元大金", "證券"],
    "2886": ["金融", "兆豐金", "高股息"],
    "2887": ["金融", "台新金"],
    "2890": ["金融", "永豐金"],
    "2891": ["金融", "中信金", "高股息"],
    "2892": ["金融", "第一金", "高股息"],
    "5880": ["金融", "合庫金", "公股"],

    # 傳產 / 食品
    "1216": ["食品", "民生消費"],
    "1227": ["食品", "OK 超商"],
    "9904": ["運動", "寶成集團"],

    # 生技
    "1707": ["生技保健", "葡萄王"],
    "4163": ["生技", "醫材"],

    # ETF
    "0050": ["市值型 ETF", "權值股", "被動投資"],
    "0056": ["高股息 ETF", "被動投資"],
    "00878": ["ESG 高股息 ETF", "被動投資"],
    "00919": ["高股息 ETF", "季配息"],
    "00940": ["高股息 ETF"],
    "006208": ["市值型 ETF", "權值股"],
    "00631L": ["槓桿 ETF", "台 50 正二"],

    # 矽光子 / 6G
    "3661": ["矽光子", "AI 互聯"],
    "2421": ["矽光子", "光通訊"],
    "8081": ["光通訊", "矽光子"],

    # 低軌衛星
    "3017": ["散熱模組", "低軌衛星"],   # 註：3017 上面已標 AI，這裡會被合併

    # 風電 / 綠能
    "1605": ["銅纜", "電網", "風電"],
    "1802": ["玻璃", "綠能"],
}


def _parse_industry_from_universe() -> dict[str, str]:
    """解析 universe.yaml 中的 # ── 標題 ── 區段，把每檔股票對應到產業類別。

    範例 yaml：
        # ── 半導體 / 面板 ──
        - "3481"   # 群創
        - "2409"   # 友達

    解析結果：{"3481": "半導體 / 面板", "2409": "半導體 / 面板", ...}
    """
    path = Path(__file__).resolve().parent.parent.parent / "config" / "universe.yaml"
    if not path.exists():
        return {}

    result: dict[str, str] = {}
    current_industry = "其他"
    header_re = re.compile(r"^\s*#\s*──\s*(.+?)\s*──")
    stock_re  = re.compile(r'^\s*-\s*"(\d{4,5})"')

    with open(path, encoding="utf-8") as f:
        for line in f:
            mh = header_re.match(line)
            if mh:
                current_industry = mh.group(1).strip()
                continue
            ms = stock_re.match(line)
            if ms:
                result[ms.group(1)] = current_industry
    return result


@lru_cache(maxsize=1)
def _industry_map() -> dict[str, str]:
    return _parse_industry_from_universe()


def get_industry(symbol: str) -> str:
    """取得股票所屬產業類別（來自 universe.yaml 註解）。"""
    return _industry_map().get(symbol, "其他")


def get_themes(symbol: str) -> list[str]:
    """取得股票題材標籤（產業 + 熱門題材）。"""
    industry = get_industry(symbol)
    hot = _HOT_THEMES.get(symbol, [])

    # 產業放第一個，後面接熱門題材
    if industry and industry != "其他":
        return [industry] + hot
    return hot


def format_themes_short(symbol: str, max_tags: int = 3) -> str:
    """格式化題材標籤為短字串（顯示用）。"""
    themes = get_themes(symbol)
    if not themes:
        return "—"
    display = themes[:max_tags]
    return " · ".join(display)
