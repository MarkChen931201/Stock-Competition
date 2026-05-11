"""動態停利管理器（Trailing Stop）。

三階段保護機制：
  Phase 1 — 初始停損：
    停損 = max(ORB 中點 / VWAP-1.5σ, 進場價 × (1 - 0.8%))
    R = 進場價 - 初始停損

  Phase 2 — 保本啟動（Breakeven Stop）：
    條件：未實現獲利 ≥ 1R
    停損上移至「進場價 + 總交易成本」（確保零虧損）

  Phase 3 — K 線追蹤停利（Trailing Stop）：
    條件：獲利 ≥ 1R（從 Phase 2 直接進入）
    停損 = max(當前停損, 前兩根 1分K 的最低點)
    → 股價每創新高就上移，直到價格跌破停損線

每根 bar 更新後：
  - 若停損上移 → 推播「停損上移」提醒
  - 若價格觸發停損 → 推播「停損出場」訊號並移除持倉
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from loguru import logger

from src.strategies.base import Direction

if TYPE_CHECKING:
    from src.data.fugle_client import Bar
    from src.notifier.discord_bot import DiscordNotifier


@dataclass
class OpenPosition:
    """記錄一筆已推播的開倉訊號狀態。"""
    symbol: str
    name: str
    direction: Direction
    entry_price: float
    initial_stop: float       # 原始停損價
    R: float                  # 風險距離（R = 進場價 - 初始停損）
    breakeven_price: float    # 保本停損 = 進場價 + 成本（買入方向）

    current_stop: float = field(init=False)   # 當前有效停損（會隨行情上移）
    peak_price: float   = field(init=False)   # 進場後的最高價（多單）或最低價（空單）
    phase: str          = field(init=False)   # "initial" | "breakeven_trailing"
    recent_bars: list   = field(default_factory=list)  # 最近幾根 bar 供追蹤

    opened_at: datetime = field(default_factory=datetime.now)
    last_notified_stop: float = field(init=False)   # 上次通知時的停損價，避免重複通知

    def __post_init__(self) -> None:
        self.current_stop = self.initial_stop
        self.peak_price   = self.entry_price
        self.phase        = "initial"
        self.last_notified_stop = self.initial_stop

    @property
    def unrealized_r(self) -> float:
        """以 R 為單位的浮動損益（多單：正 = 獲利，空單：正 = 獲利）。"""
        if self.R <= 0:
            return 0.0
        if self.direction == Direction.LONG:
            return (self.peak_price - self.entry_price) / self.R
        else:
            return (self.entry_price - self.peak_price) / self.R


class TrailingStopManager:
    """管理所有開倉的動態停損，每根 bar 更新後呼叫 update()。

    使用方式：
        mgr = TrailingStopManager(notifier)
        # 推播訊號後登記持倉
        mgr.add_position(signal, cost, lots)
        # 每根 bar 更新時呼叫
        mgr.update(bar)
    """

    def __init__(self, notifier: "DiscordNotifier"):
        self._notifier = notifier
        self._positions: dict[str, OpenPosition] = {}   # key = symbol

    def add_position(
        self,
        symbol: str,
        name: str,
        direction: Direction,
        entry_price: float,
        initial_stop: float,
        breakeven_cost_pct: float = 0.0043,   # 股票約 0.43%，ETF 約 0.385%
    ) -> None:
        """登記一筆新倉位。"""
        R = abs(entry_price - initial_stop)
        if R <= 0:
            logger.warning(f"[{symbol}] R=0，不追蹤停損")
            return

        if direction == Direction.LONG:
            breakeven = round(entry_price * (1 + breakeven_cost_pct), 2)
        else:
            breakeven = round(entry_price * (1 - breakeven_cost_pct), 2)

        pos = OpenPosition(
            symbol=symbol,
            name=name,
            direction=direction,
            entry_price=entry_price,
            initial_stop=initial_stop,
            R=R,
            breakeven_price=breakeven,
        )
        self._positions[symbol] = pos
        logger.info(
            f"[{symbol}] 登記持倉 {direction.value} @ {entry_price}  "
            f"停損={initial_stop}  R={R:.2f}  保本={breakeven}"
        )

    def remove_position(self, symbol: str) -> None:
        self._positions.pop(symbol, None)

    def update(self, bar: "Bar") -> None:
        """每根 bar 更新後呼叫，判斷是否觸發停損或需要上移。"""
        pos = self._positions.get(bar.symbol)
        if pos is None:
            return

        current_price = bar.close
        high = bar.high
        low  = bar.low

        if pos.direction == Direction.LONG:
            self._update_long(pos, bar, current_price, high, low)
        else:
            self._update_short(pos, bar, current_price, high, low)

    # ── 多單更新 ──

    def _update_long(self, pos: OpenPosition, bar: "Bar",
                     price: float, high: float, low: float) -> None:
        # 更新最高價
        if high > pos.peak_price:
            pos.peak_price = high

        # 計算當前 R 水位
        r_level = (pos.peak_price - pos.entry_price) / pos.R if pos.R > 0 else 0

        # 更新追蹤 bar 列表（保留最近 3 根）
        pos.recent_bars.append(bar)
        if len(pos.recent_bars) > 3:
            pos.recent_bars.pop(0)

        # Phase 1 → Phase 2/3：獲利達到 1R
        if r_level >= 1.0 and pos.phase == "initial":
            pos.phase = "breakeven_trailing"
            new_stop = pos.breakeven_price
            if new_stop > pos.current_stop:
                pos.current_stop = new_stop
                self._notify_stop_move(pos, "🛡️ 保本啟動", pos.current_stop)

        # Phase 2/3：動態追蹤
        if pos.phase == "breakeven_trailing" and len(pos.recent_bars) >= 2:
            # 前兩根 bar 的最低點
            prev_2_low = min(b.low for b in pos.recent_bars[-2:])
            trailing_stop = prev_2_low
            if trailing_stop > pos.current_stop:
                pos.current_stop = trailing_stop
                # 只在停損上移超過 1 個 tick 才通知（避免太頻繁）
                if pos.current_stop - pos.last_notified_stop >= self._tick(pos.current_stop):
                    self._notify_stop_move(pos, "📈 追蹤停損上移", pos.current_stop)
                    pos.last_notified_stop = pos.current_stop

        # 觸發停損：價格跌破停損線
        if price <= pos.current_stop:
            self._notify_stop_hit(pos, price)
            del self._positions[pos.symbol]

    # ── 空單更新 ──

    def _update_short(self, pos: OpenPosition, bar: "Bar",
                      price: float, high: float, low: float) -> None:
        # 更新最低價（空單方向）
        if low < pos.peak_price:
            pos.peak_price = low

        r_level = (pos.entry_price - pos.peak_price) / pos.R if pos.R > 0 else 0

        pos.recent_bars.append(bar)
        if len(pos.recent_bars) > 3:
            pos.recent_bars.pop(0)

        # Phase 1 → Phase 2/3
        if r_level >= 1.0 and pos.phase == "initial":
            pos.phase = "breakeven_trailing"
            new_stop = pos.breakeven_price   # 空單保本 = 進場價 - 成本
            if new_stop < pos.current_stop:
                pos.current_stop = new_stop
                self._notify_stop_move(pos, "🛡️ 保本啟動", pos.current_stop)

        # 動態追蹤：空單停損下移（空單停損在上方）
        if pos.phase == "breakeven_trailing" and len(pos.recent_bars) >= 2:
            prev_2_high = max(b.high for b in pos.recent_bars[-2:])
            trailing_stop = prev_2_high
            if trailing_stop < pos.current_stop:
                pos.current_stop = trailing_stop
                if pos.last_notified_stop - pos.current_stop >= self._tick(pos.current_stop):
                    self._notify_stop_move(pos, "📉 追蹤停損下移", pos.current_stop)
                    pos.last_notified_stop = pos.current_stop

        # 觸發停損：價格漲破停損線
        if price >= pos.current_stop:
            self._notify_stop_hit(pos, price)
            del self._positions[pos.symbol]

    # ── 通知 ──

    def _notify_stop_move(self, pos: OpenPosition, title: str, new_stop: float) -> None:
        from discord_webhook import DiscordEmbed
        r_now = pos.unrealized_r
        color = "00b0f4"  # 藍色

        embed = DiscordEmbed(
            title=f"{title} | {pos.symbol} {pos.name}",
            color=color,
        )
        embed.set_timestamp()
        embed.add_embed_field(
            name="📌 停損更新",
            value=(
                f"方向：{'🟢 多' if pos.direction == Direction.LONG else '🔴 空'}\n"
                f"進場：**{pos.entry_price}**  →  新停損：**{new_stop}**\n"
                f"階段：{pos.phase}  |  浮盈：**{r_now:.1f}R**"
            ),
            inline=False,
        )
        embed.set_footer(text="Stock-Competition · 請手動更新停損單")
        try:
            self._notifier._send(embed)
        except Exception as e:
            logger.warning(f"[{pos.symbol}] 停損通知失敗：{e}")

    def _notify_stop_hit(self, pos: OpenPosition, price: float) -> None:
        from discord_webhook import DiscordEmbed
        pnl_r = (price - pos.entry_price) / pos.R if pos.direction == Direction.LONG \
                else (pos.entry_price - price) / pos.R
        color = "57f287" if pnl_r > 0 else "ed4245"  # 綠=獲利, 紅=虧損

        embed = DiscordEmbed(
            title=f"🚨 停損出場 | {pos.symbol} {pos.name}",
            color=color,
        )
        embed.set_timestamp()
        embed.add_embed_field(
            name="出場資訊",
            value=(
                f"方向：{'🟢 多' if pos.direction == Direction.LONG else '🔴 空'}\n"
                f"進場：**{pos.entry_price}**  |  出場參考：**{price}**\n"
                f"損益：**{pnl_r:+.2f}R**\n"
                f"⚠️ 請立即平倉！"
            ),
            inline=False,
        )
        embed.set_footer(text="Stock-Competition · Trailing Stop 觸發")
        try:
            self._notifier._send(embed, content="@here")
        except Exception as e:
            logger.warning(f"[{pos.symbol}] 出場通知失敗：{e}")
        logger.info(f"[{pos.symbol}] 🚨 停損出場 @ {price}，損益 {pnl_r:+.2f}R")

    @staticmethod
    def _tick(price: float) -> float:
        """台股最小升降單位（避免停損通知太頻繁）。"""
        if price < 10:    return 0.01
        if price < 50:    return 0.05
        if price < 100:   return 0.1
        if price < 500:   return 0.5
        if price < 1000:  return 1.0
        return 5.0

    @property
    def open_count(self) -> int:
        return len(self._positions)

    def summary(self) -> list[dict]:
        return [
            {
                "symbol": p.symbol,
                "name": p.name,
                "direction": p.direction.value,
                "entry": p.entry_price,
                "stop": p.current_stop,
                "phase": p.phase,
                "r_level": round(p.unrealized_r, 2),
            }
            for p in self._positions.values()
        ]
