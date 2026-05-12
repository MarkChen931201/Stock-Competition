"""Discord Embed 排版 — 將 Signal + TradeCost 組成美觀的推播卡片。"""
from __future__ import annotations

from discord_webhook import DiscordEmbed

from src.risk.cost_calculator import AssetType, TradeCost
from src.strategies.base import Direction, Signal, SignalType

# Discord embed 顏色
_COLOR_LONG = "00c853"   # 綠
_COLOR_SHORT = "d50000"  # 紅
_COLOR_WATCH = "ff6f00"  # 橘（預警）


def build_signal_embed(
    signal: Signal,
    cost: TradeCost,
    lots: int = 1,
    asset_type: AssetType = AssetType.STOCK,
) -> DiscordEmbed:
    """把訊號與成本資料包成一張 Discord Embed 卡片。

    Args:
        signal:     策略產出的訊號
        cost:       cost_calculator 計算的成本結果
        lots:       建議下單張數（顯示用）
        asset_type: STOCK 或 ETF
    """
    is_watch = signal.signal_type == SignalType.WATCH
    is_long = signal.direction == Direction.LONG

    # --- 標題與顏色 ---
    if is_watch:
        direction_tag = "👀 WATCH"
        color = _COLOR_WATCH
    elif is_long:
        direction_tag = "🟢 做多 LONG"
        color = _COLOR_LONG
    else:
        direction_tag = "🔴 做空 SHORT"
        color = _COLOR_SHORT

    asset_tag = "ETF" if asset_type == AssetType.ETF else "股票"
    title = f"{direction_tag}｜{signal.symbol} {signal.name}（{asset_tag}）"

    embed = DiscordEmbed(title=title, color=color)
    embed.set_timestamp()  # 自動填入現在時間

    # --- 策略來源 ---
    embed.add_embed_field(
        name="📊 策略",
        value=signal.strategy_name,
        inline=True,
    )
    embed.add_embed_field(
        name="⏱️ 觸發時間",
        value=signal.generated_at.strftime("%H:%M:%S"),
        inline=True,
    )
    # --- 訊號評分（若有）---
    score = signal.extra.get("signal_score") if signal.extra else None
    if score is not None:
        breakdown = signal.extra.get("score_breakdown", {})
        breakdown_str = "  ".join(f"{k}:{v}" for k, v in breakdown.items())
        # 滿分 16（v3 新增趨勢維度），每 2 分一顆星
        stars = "⭐" * int(score / 2)
        embed.add_embed_field(
            name=f"🎯 訊號評分  {stars}  {score}/16",
            value=f"`{breakdown_str}`",
            inline=False,
        )

    embed.add_embed_field(name="​", value="​", inline=True)  # 佔位，強制換行

    # --- 價格資訊 ---
    embed.add_embed_field(
        name="💰 觸發價",
        value=f"**{signal.trigger_price:.2f}**",
        inline=True,
    )
    if signal.stop_loss:
        embed.add_embed_field(
            name="🛑 停損",
            value=f"{signal.stop_loss:.2f}",
            inline=True,
        )
    if signal.take_profit:
        embed.add_embed_field(
            name="🎯 停利",
            value=f"{signal.take_profit:.2f}",
            inline=True,
        )

    # --- 交易成本區塊 ---
    cost_lines = [
        f"買手續費：**NT${cost.buy_fee:,.0f}**",
        f"賣手續費：**NT${cost.sell_fee:,.0f}**",
        f"{'ETF' if asset_type == AssetType.ETF else '當沖'}稅：**NT${cost.tax:,.0f}**",
        f"合計成本：**NT${cost.total:,.0f}**",
        f"損益兩平賣價：**{cost.breakeven_sell_price:.2f}**（+{cost.breakeven_pct:.2%}）",
    ]
    embed.add_embed_field(
        name=f"💸 交易成本（{lots} 張，以觸發價計）",
        value="\n".join(cost_lines),
        inline=False,
    )

    # --- 內外盤比（若有）---
    uptick = signal.extra.get("uptick_ratio") if signal.extra else None
    if uptick is not None:
        if uptick >= 55:
            pressure = f"🔥 強買壓（外盤 {uptick:.1f}%）"
        elif uptick >= 50:
            pressure = f"📈 偏買壓（外盤 {uptick:.1f}%）"
        elif uptick <= 40:
            pressure = f"❄️ 強賣壓（外盤 {uptick:.1f}%）"
        elif uptick <= 45:
            pressure = f"📉 偏賣壓（外盤 {uptick:.1f}%）"
        else:
            pressure = f"↔️ 均衡（外盤 {uptick:.1f}%）"
        embed.add_embed_field(name="⚖️ 買賣壓", value=pressure, inline=True)

    # --- 訊號觸發原因 ---
    if signal.reason:
        embed.add_embed_field(
            name="📝 觸發原因",
            value=signal.reason,
            inline=False,
        )

    # --- WATCH 專屬：完整交易執行卡 ---
    if is_watch and signal.extra and "entry_low" in signal.extra:
        ex = signal.extra
        direction_word = "做多 LONG" if is_long else "做空 SHORT"
        action_word = "買進" if is_long else "賣出（融券）"

        # 1️⃣ 進場區間
        embed.add_embed_field(
            name="🎯 進場區間（限價）",
            value=(
                f"**{ex['entry_low']:.2f} ~ {ex['entry_high']:.2f}**\n"
                f"建議掛 `{ex['entry_mid']:.2f}` 限價單"
            ),
            inline=True,
        )

        # 2️⃣ 觸發確認價（必須突破才進場）
        break_word = "突破" if is_long else "跌破"
        embed.add_embed_field(
            name=f"⚡ 觸發條件（{break_word}）",
            value=(
                f"**{ex['trigger_break']:.2f}**\n"
                f"{ex['valid_minutes']} 分鐘內未{break_word}則放棄"
            ),
            inline=True,
        )

        # 佔位換行
        embed.add_embed_field(name="​", value="​", inline=True)

        # 3️⃣ 停損 / 停利 T1 / T2
        embed.add_embed_field(
            name="🛑 停損（嚴守）",
            value=f"**{ex['stop_loss']:.2f}**\n風險 NT${ex['risk_per_lot']:,.0f}/張",
            inline=True,
        )
        embed.add_embed_field(
            name="✅ 停利 T1 (+1R)",
            value=f"**{ex['take_profit_1']:.2f}**\n出 1/2 倉",
            inline=True,
        )
        embed.add_embed_field(
            name="🚀 停利 T2 (+2R)",
            value=f"**{ex['take_profit_2']:.2f}**\n剩餘 trail",
            inline=True,
        )

        # 4️⃣ 建議張數
        embed.add_embed_field(
            name="📦 建議下單",
            value=(
                f"**{action_word} {ex['suggested_lots']} 張**（{direction_word}）\n"
                f"單筆風險上限 NT$100,000"
            ),
            inline=False,
        )

        # 5️⃣ 執行步驟摘要
        steps = (
            f"1. 確認 `{ex['trigger_break']:.2f}` 被{break_word}（{ex['valid_minutes']}分鐘內）\n"
            f"2. 在 `{ex['entry_low']:.2f}~{ex['entry_high']:.2f}` 掛限價{action_word} **{ex['suggested_lots']} 張**\n"
            f"3. 設停損 `{ex['stop_loss']:.2f}`（嚴守不可砍）\n"
            f"4. 到 `{ex['take_profit_1']:.2f}` 出半倉，剩餘移動停利至 `{ex['take_profit_2']:.2f}`"
        )
        embed.add_embed_field(
            name="📋 執行步驟",
            value=steps,
            inline=False,
        )
    elif is_watch:
        # 沒有交易計畫的 WATCH（退回原本提示）
        embed.add_embed_field(
            name="⚠️ 注意",
            value="此為**預警訊號**，請人工確認後再決定是否下單。",
            inline=False,
        )

    embed.set_footer(text="Stock-Competition · 僅供模擬競賽參考，非投資建議")
    return embed
