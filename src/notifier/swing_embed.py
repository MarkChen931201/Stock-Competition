"""短期波段策略 Discord embed builder。

兩種推播類型：
  1. 13:35 盤後：預選清單（明日候選 + 持倉檢查）
  2. 09:35 盤中：進場確認（昨日預選今日驗證）
"""
from __future__ import annotations

from datetime import date

from discord_webhook import DiscordEmbed

from src.risk.swing_position import SwingPosition, SwingStatus
from src.strategies.swing_breakout import SwingCandidate


_COLOR_PRESELECT = "f57f17"   # 橘黃（預警）
_COLOR_CONFIRM   = "2e7d32"   # 綠（確認進場）
_COLOR_HOLDING   = "1565c0"   # 藍（持倉檢查）
_COLOR_EXIT      = "c62828"   # 紅（出場警告）


def build_preselect_embed(candidates: list[SwingCandidate]) -> DiscordEmbed:
    """盤後預選清單 embed（推 1~5 檔）。"""
    today = date.today().strftime("%Y-%m-%d")
    title = f"📅 明日波段預選 ({len(candidates)} 檔) | {today}"
    embed = DiscordEmbed(
        title=title,
        description=(
            "**策略**：突破 + 法人 + 均線 三重確認\n"
            "**操作**：明日 09:35 後若條件仍成立，將推進場確認\n"
            "**持有**：3-5 個交易日"
        ),
        color=_COLOR_PRESELECT,
    )

    for i, c in enumerate(candidates[:5], start=1):
        # 分數 + 明細
        breakdown_str = " ".join(f"{k}{v}" for k, v in c.breakdown.items())
        stars = "⭐" * int(c.score / 2)

        # 法人加分顯示
        inst_str = ""
        if c.inst_net_buy_lots is not None and c.inst_net_buy_lots > 0:
            inst_str = f"\n🏦 法人買超 {c.inst_net_buy_lots:,} 張"

        field_value = (
            f"💰 收盤 **{c.close}**（突破 10 日高 {c.high_10d}）\n"
            f"📊 量比 {c.vol_ratio:.1f}x（20日均量 {c.avg_vol_lots:,.0f} 張）\n"
            f"📈 EMA: 5={c.ema5} / 10={c.ema10} / 20={c.ema20}\n"
            f"📐 RSI(14) = {c.rsi14}"
            f"{inst_str}\n\n"
            f"🎯 **建議掛單** {c.entry_limit}（{c.suggested_lots} 張）\n"
            f"🛑 停損 **{c.stop_loss}**（風險 NT${c.risk_per_share*1000*c.suggested_lots:,.0f}）\n"
            f"✅ T1 +6%: **{c.take_profit_1}** / T2 +12%: **{c.take_profit_2}**\n"
            f"📊 分數 {stars} {c.score}/10  `{breakdown_str}`"
        )
        embed.add_embed_field(
            name=f"{i}. {c.symbol} {c.name}",
            value=field_value,
            inline=False,
        )

    embed.set_footer(text="Stock-Competition · 波段預選 / 隔日 09:35 將確認")
    return embed


def build_confirm_embed(position: SwingPosition, current_price: float, ok: bool) -> DiscordEmbed:
    """09:35 進場確認 embed。"""
    if ok:
        title = f"✅ 波段進場確認 | {position.symbol} {position.name}"
        color = _COLOR_CONFIRM
        action = "**立即限價買進**"
    else:
        title = f"⛔ 波段取消進場 | {position.symbol} {position.name}"
        color = _COLOR_EXIT
        action = "**取消今日進場（條件不再成立）**"

    embed = DiscordEmbed(title=title, color=color)
    embed.add_embed_field(name="現價", value=f"**{current_price}**", inline=True)
    embed.add_embed_field(name="預定限價", value=f"{position.entry_limit}", inline=True)
    embed.add_embed_field(name="建議張數", value=f"{position.lots} 張", inline=True)

    if ok:
        embed.add_embed_field(name="🛑 停損", value=f"**{position.stop_loss}**", inline=True)
        embed.add_embed_field(name="✅ T1 (+6%)", value=f"{position.take_profit_1}", inline=True)
        embed.add_embed_field(name="🚀 T2 (+12%)", value=f"{position.take_profit_2}", inline=True)

    embed.add_embed_field(name="📋 動作", value=action, inline=False)
    embed.set_footer(text="Stock-Competition · 波段確認")
    return embed


def build_holding_check_embed(positions: list[SwingPosition], today: date) -> DiscordEmbed:
    """每日盤後持倉檢查 embed。"""
    title = f"📊 波段持倉檢查 ({len(positions)} 檔) | {today.strftime('%Y-%m-%d')}"
    embed = DiscordEmbed(title=title, color=_COLOR_HOLDING)

    for p in positions:
        days = p.days_held(today)
        # 預警時間止損（5 天）
        time_warning = " ⚠️ 接近時間止損" if days >= 4 else ""

        entry = p.entry_actual or p.entry_limit
        pnl_estimate = "（待開盤更新）"

        field_value = (
            f"📅 持有 {days} 天{time_warning}\n"
            f"💰 進場 {entry} / 停損 {p.stop_loss}\n"
            f"🎯 T1 {p.take_profit_1} / T2 {p.take_profit_2}\n"
            f"📊 數量 {p.lots} 張 | 預估目前損益 {pnl_estimate}"
        )
        embed.add_embed_field(
            name=f"{p.symbol} {p.name}",
            value=field_value,
            inline=False,
        )

    embed.set_footer(text="Stock-Competition · 波段持倉")
    return embed


def build_exit_embed(position: SwingPosition) -> DiscordEmbed:
    """出場通知 embed。"""
    pnl_emoji = "🟢" if position.pnl_pct >= 0 else "🔴"
    title = f"{pnl_emoji} 波段出場 | {position.symbol} {position.name}"
    embed = DiscordEmbed(title=title, color=_COLOR_EXIT if position.pnl_pct < 0 else _COLOR_CONFIRM)

    embed.add_embed_field(name="出場原因", value=position.exit_reason or "未指定", inline=False)
    embed.add_embed_field(name="進場價", value=f"{position.entry_actual}", inline=True)
    embed.add_embed_field(name="出場價", value=f"**{position.exit_price}**", inline=True)
    embed.add_embed_field(name="損益", value=f"**{position.pnl_pct:+.2f}%**", inline=True)

    embed.set_footer(text="Stock-Competition · 波段出場")
    return embed
