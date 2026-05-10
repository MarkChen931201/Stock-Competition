# Stock-Competition

2026 校園股神－凱基證券｜兩週台股當沖競賽（5/11–5/22）即時訊號系統。

## 功能模組

- **盤前篩選 (`scripts/prefetch_universe.py`)**：每日盤前自動跑，依
  「平均成交量(張) / 週轉率 / ATR% / 價格區間」篩出當日交易池，
  寫入 `logs/screener_YYYYMMDD.csv` 並推播 Discord。
- **成本計算器 (`src/risk/cost_calculator.py`)**：含 0.1425% 手續費 +
  20 元低消、股票 0.15% / ETF 0.1% 當沖稅，與台股 tick 跳動規則。
- 後續會加上：Fugle 即時 K 棒、ORB / VWAP 策略、訊號分發器、Discord 訊號卡片。

## 快速開始

```bash
# 1. 安裝相依
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 設定 .env
cp .env.example .env
#  編輯 FUGLE_API_KEY / FINMIND_TOKEN / DISCORD_WEBHOOK_URL

# 3. 跑盤前篩選 (建議排程在 08:30)
python -m scripts.prefetch_universe

# 4. 跑單元測試
pytest -q
```

## 排程建議 (cron)

```
30 8 * * 1-5  cd /path/to/Stock-Competition && /path/.venv/bin/python -m scripts.prefetch_universe
```

## 篩選邏輯

1. 從 `config/universe.yaml` 讀候選 (seed_universe + etf_universe)。
2. 用 FinMind 抓近 `SCREENER_LOOKBACK_DAYS` 日的日 K。
3. 過濾條件：
   - `avg_volume_lots ≥ SCREENER_MIN_AVG_VOLUME_LOTS` (預設 1 萬張)
   - `turnover_rate ≥ SCREENER_MIN_TURNOVER_RATE` (僅股票)
   - `atr_pct ≥ SCREENER_MIN_ATR_PCT` (預設 2.5%)
   - 價格落在 `[PRICE_MIN, PRICE_MAX]`
4. 綜合分數 = 0.4 × Z(量) + 0.4 × Z(ATR%) + 0.2 × Z(週轉率)
5. 取 Top N 寫入 CSV 並推播 Discord。
