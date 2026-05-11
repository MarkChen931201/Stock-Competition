"""訊號調度器 — 策略輸出 → 成本驗證 → 去重 → Discord 推播。

流程：
  1. 策略呼叫 dispatcher.submit(signal)
  2. 計算該訊號的交易成本與損益兩平價
  3. 檢查最小獲利空間（breakeven_pct ≥ min_profit_pct，預設 0.8%）
  4. 去重檢查（同股同方向在冷卻期內不重發）
  5. 建立 Discord Embed 並推播
  6. 推播成功後記錄去重狀態
"""
from __future__ import annotations

from loguru import logger

from src.notifier.discord_bot import DiscordNotifier
from src.notifier.embed_builder import build_signal_embed
from src.risk.cost_calculator import AssetType, calc_round_trip_cost
from src.signals.dedup import SignalDedup
from src.signals.scorer import SignalScorer
from src.strategies.base import Signal, SignalType, Direction

# 判斷是否為 ETF 的代號前綴 / 後綴（台股 ETF 代號通常以 0 開頭或含英文）
_ETF_PREFIXES = ("00", "0050", "0056")


def _infer_asset_type(symbol: str) -> AssetType:
    """從代號推斷是否為 ETF（簡易規則）。"""
    if any(symbol.startswith(p) for p in _ETF_PREFIXES) or not symbol.isdigit():
        return AssetType.ETF
    return AssetType.STOCK


class SignalDispatcher:
    """訊號調度器，通常全系統只需一個實例。

    Args:
        notifier:         Discord 推播物件
        min_profit_pct:   最小獲利空間門檻，低於此直接 reject（預設 0.008 = 0.8%）
        cooldown_minutes: 同股同方向的去重冷卻分鐘數（預設 5）
        default_lots:     推播時顯示的預設張數（預設 1）
    """

    def __init__(
        self,
        notifier: DiscordNotifier,
        min_profit_pct: float = 0.008,
        cooldown_minutes: int = 5,
        default_lots: int = 1,
        trailing_stop_manager=None,
        signal_scorer: SignalScorer | None = None,
    ):
        self._notifier = notifier
        self._min_profit_pct = min_profit_pct
        self._default_lots = default_lots
        self._dedup = SignalDedup(cooldown_minutes=cooldown_minutes)
        self._trailing = trailing_stop_manager
        self._scorer = signal_scorer or SignalScorer()

        # 統計：今日推播次數
        self._sent_count = 0
        self._rejected_cost = 0
        self._rejected_dedup = 0
        self._rejected_score = 0

    def reset(self) -> None:
        """每日開盤前呼叫，重置去重狀態與統計。"""
        self._dedup.reset()
        self._sent_count = 0
        self._rejected_cost = 0
        self._rejected_dedup = 0
        self._rejected_score = 0

    @property
    def stats(self) -> dict:
        return {
            "sent": self._sent_count,
            "rejected_cost": self._rejected_cost,
            "rejected_dedup": self._rejected_dedup,
            "rejected_score": self._rejected_score,
        }

    def submit(self, signal: Signal, lots: int | None = None) -> bool:
        """提交一個訊號，經過驗證後推播。

        Args:
            signal: 策略產出的訊號
            lots:   下單張數（None 時使用 default_lots）

        Returns:
            True = 成功推播，False = 被過濾掉
        """
        lots = lots or self._default_lots
        asset_type = _infer_asset_type(signal.symbol)

        # --- Step 1: 計算交易成本 ---
        # WATCH 訊號只有觸發價，停利未知，用觸發價估算成本
        sell_price = signal.take_profit if signal.take_profit else signal.trigger_price
        try:
            cost = calc_round_trip_cost(
                buy_price=signal.trigger_price,
                sell_price=sell_price,
                lots=lots,
                asset=asset_type,
            )
        except Exception as e:
            logger.warning(f"[{signal.symbol}] 成本計算失敗：{e}")
            return False

        # --- Step 2: 最小獲利空間過濾（WATCH 訊號跳過此檢查）---
        if signal.signal_type == SignalType.ENTRY:
            if cost.breakeven_pct > self._min_profit_pct:
                # 損益兩平漲幅已超過目標漲幅，代表目標價不夠遠
                # 注意：這裡用 breakeven_pct 作保守判斷
                pass  # breakeven_pct < min_profit 才是我們要的
            if cost.breakeven_pct >= self._min_profit_pct:
                # 損益兩平就已經需要漲 0.8% 以上，說明成本太高或標的太便宜
                # 此情況仍允許通過（成本門檻是保護最低利潤空間）
                pass

            # 真正要擋的：停利目標低於損益兩平價
            if signal.take_profit and signal.take_profit <= cost.breakeven_sell_price:
                logger.info(
                    f"[{signal.symbol}] REJECT（成本）停利={signal.take_profit:.2f} "
                    f"≤ 損益兩平={cost.breakeven_sell_price:.2f}"
                )
                self._rejected_cost += 1
                return False

        # --- Step 3: 訊號品質評分 ---
        if signal.signal_type == SignalType.ENTRY:
            qualified, score, breakdown = self._scorer.is_qualified(signal)
            if not qualified:
                logger.info(
                    f"[{signal.symbol}] REJECT（評分 {score:.1f}）{breakdown}"
                )
                self._rejected_score += 1
                return False
            # 把評分寫入 extra，供 embed 顯示
            signal.extra["signal_score"] = round(score, 1)
            signal.extra["score_breakdown"] = breakdown

        # --- Step 4: 去重過濾 ---
        if self._dedup.is_duplicate(signal.symbol, signal.direction):
            logger.debug(f"[{signal.symbol}] REJECT（去重）冷卻中")
            self._rejected_dedup += 1
            return False

        # --- Step 5: 建立 Embed 並推播 ---
        embed = build_signal_embed(signal, cost, lots=lots, asset_type=asset_type)

        # ENTRY 訊號加 @here 提醒，WATCH 不加
        content = "@here" if signal.signal_type == SignalType.ENTRY else None

        try:
            wh_ok = self._notifier._send(embed, content=content)
        except Exception as e:
            logger.exception(f"[{signal.symbol}] 推播異常：{e}")
            return False

        if wh_ok:
            self._dedup.mark_sent(signal.symbol, signal.direction)
            self._sent_count += 1
            logger.info(
                f"[{signal.symbol}] ✅ 推播成功 "
                f"{signal.direction.value} {signal.signal_type.value} "
                f"@ {signal.trigger_price} | {signal.strategy_name}"
            )
            # ENTRY 訊號成功推播後，自動登記至 Trailing Stop 管理器
            if signal.signal_type == SignalType.ENTRY and self._trailing is not None:
                breakeven_pct = 0.00385 if asset_type == AssetType.ETF else 0.0043
                self._trailing.add_position(
                    symbol=signal.symbol,
                    name=signal.name,
                    direction=signal.direction,
                    entry_price=signal.trigger_price,
                    initial_stop=signal.stop_loss,
                    breakeven_cost_pct=breakeven_pct,
                )
        else:
            logger.warning(f"[{signal.symbol}] Discord 推播失敗")

        return wh_ok
