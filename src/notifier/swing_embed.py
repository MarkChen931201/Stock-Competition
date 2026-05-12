"""短期波段策略 Discord embed builder v2 — 加入題材/基本面/技術分析。

每檔分四個區塊：
  📌 題材標籤    產業分類 + 熱門題材
  📊 基本面     EPS / 月營收 YoY / PER
  📈 技術指標   均線、RSI、MACD、KD、量能、布林位置
  💰 進場計畫   限價、停損、停利、張數

由於 Discord embed 一則最多 25 個 field、6000 字元，
推 10 檔時拆成 2~3 則 embed 推送。
"""
from __future__ import annotations

from datetime import date

from discord_webhook import DiscordEmbed

from src.data.fundamentals import FundamentalData
from src.data.themes import format_themes_short, get_themes
from src.risk.swing_position import SwingPosition, SwingStatus
from src.strategies.swing_breakout import SwingCandidate


_COLOR_PRESELECT = "f57f17"
_COLOR_CONFIRM   = "2e7d32"
_COLOR_HOLDING   = "1565c0"
_COLOR_EXIT      = "c62828"


def _fmt_macd_signal(c: SwingCandidate) -> str:
    """MACD 訊號文字化。"""
    if c.macd_hist > 0 and c.macd > c.macd_signal:
        return "🟢 多頭（金叉）"
    elif c.macd_hist > 0:
        return "🟢 偏多"
    elif c.macd_hist < 0 and c.macd < c.macd_signal:
        return "🔴 空頭（死叉）"
    else:
        return "🟡 整理"


def _fmt_kd_signal(c: SwingCandidate) -> str:
    """KD 訊號文字化。"""
    if c.k_value > c.d_value:
        if c.k_value < 80:
            return "🟢 黃金交叉"
        else:
            return "⚠️ 超買區"
    else:
        if c.k_value > 20:
            return "🔴 死亡交叉"
        else:
            return "🟢 超賣反彈"


def _fmt_bb_signal(c: SwingCandidate) -> str:
    """布林通道位置文字化。"""
    if c.bb_position_pct >= 80:
        return f"⚠️ 上軌 ({c.bb_position_pct:.0f}%)"
    elif c.bb_position_pct >= 60:
        return f"🟢 偏上 ({c.bb_position_pct:.0f}%)"
    elif c.bb_position_pct >= 40:
        return f"🟡 中軌 ({c.bb_position_pct:.0f}%)"
    else:
        return f"🔴 偏下 ({c.bb_position_pct:.0f}%)"


def _fmt_themes(symbol: str) -> str:
    """題材標籤（顯示完整）。"""
    themes = get_themes(symbol)
    if not themes:
        return "—"
    return " · ".join(themes[:5])


def _fmt_fundamentals(fd: FundamentalData | None) -> str:
    """基本面區塊文字。"""
    if fd is None or not fd.has_data():
        return "（基本面資料暫無）"

    lines = []
    if fd.eps_latest is not None:
        eps_str = f"**EPS** {fd.eps_latest}"
        if fd.eps_quarter:
            eps_str += f"（{fd.eps_quarter}）"
        lines.append(eps_str)

    if fd.revenue_yoy_pct is not None:
        emoji = "📈" if fd.revenue_yoy_pct > 0 else "📉"
        rev_str = f"{emoji} **營收YoY** {fd.revenue_yoy_pct:+.1f}%"
        if fd.revenue_month:
            rev_str += f"（{fd.revenue_month}）"
        if fd.revenue_mom_pct is not None:
            rev_str += f"｜MoM {fd.revenue_mom_pct:+.1f}%"
        lines.append(rev_str)

    if fd.per is not None:
        per_str = f"**PER** {fd.per}"
        if fd.pbr is not None:
            per_str += f"｜PBR {fd.pbr}"
        if fd.dividend_yield is not None:
            per_str += f"｜殖利率 {fd.dividend_yield:.2f}%"
        lines.append(per_str)

    return "\n".join(lines) if lines else "（基本面資料暫無）"


def _build_candidate_field(c: SwingCandidate, fd: FundamentalData | None, rank: int) -> tuple[str, str]:
    """單檔股票的 field（name + value）。"""
    stars = "⭐" * int(c.score / 2)
    breakdown_str = " ".join(f"{k}{v}" for k, v in c.breakdown.items())

    # 標題
    field_name = f"#{rank}  {c.symbol} {c.name}  {stars} {c.score}/10"

    # 內容（分區塊）
    themes_str = _fmt_themes(c.symbol)
    fund_str   = _fmt_fundamentals(fd)
    macd_sig   = _fmt_macd_signal(c)
    kd_sig     = _fmt_kd_signal(c)
    bb_sig     = _fmt_bb_signal(c)

    # 法人加分
    inst_str = ""
    if c.inst_net_buy_lots is not None and c.inst_net_buy_lots > 0:
        inst_str = f"\n🏦 法人買超 **{c.inst_net_buy_lots:,}** 張"

    field_value = (
        f"📌 **題材** {themes_str}\n"
        f"\n"
        f"📊 **基本面**\n{fund_str}\n"
        f"\n"
        f"📈 **技術指標**\n"
        f"• 收盤 **{c.close}**（{c.change_pct:+.2f}%）｜突破 10 日高 {c.high_10d}\n"
        f"• 均線 EMA5/10/20 = `{c.ema5}/{c.ema10}/{c.ema20}` ✅ 多頭排列\n"
        f"• RSI(14) = **{c.rsi14}**（健康）｜連紅 {c.consec_red_bars} 根\n"
        f"• MACD = {c.macd:.3f}（{macd_sig}）\n"
        f"• KD = {c.k_value:.1f}/{c.d_value:.1f}（{kd_sig}）\n"
        f"• 布林位置 {bb_sig}\n"
        f"• 量比 **{c.vol_ratio:.1f}x**（20日均量 {c.avg_vol_lots:,.0f} 張）"
        f"{inst_str}\n"
        f"\n"
        f"💰 **進場計畫**\n"
        f"• 限價 **{c.entry_limit}** × **{c.suggested_lots} 張**\n"
        f"• 🛑 停損 **{c.stop_loss}**（風險 NT${c.risk_per_share*1000*c.suggested_lots:,.0f}）\n"
        f"• ✅ T1 (+6%) **{c.take_profit_1}**  /  🚀 T2 (+12%) **{c.take_profit_2}**\n"
        f"\n"
        f"📊 評分 `{breakdown_str}`"
    )
    return field_name, field_value


def build_preselect_embeds(
    candidates: list[SwingCandidate],
    fundamentals: dict[str, FundamentalData],
    per_embed: int = 5,
) -> list[DiscordEmbed]:
    """盤後預選清單 — 拆成多則 embed（每則最多 per_embed 檔）。

    Returns:
        list[DiscordEmbed]，按順序推送
    """
    today = date.today().strftime("%Y-%m-%d")
    total = len(candidates)
    embeds: list[DiscordEmbed] = []

    for chunk_start in range(0, total, per_embed):
        chunk = candidates[chunk_start: chunk_start + per_embed]
        is_first = chunk_start == 0
        chunk_end = chunk_start + len(chunk)

        if is_first:
            title = f"📅 明日波段預選 Top {total} 檔 | {today}"
            desc = (
                "**策略**：突破 + 均線 + 量能 三重確認｜**操作**：明日 09:35 進場確認｜"
                "**持有**：3-5 個交易日\n"
                f"**第 1-{min(per_embed, total)} 名**"
            )
        else:
            title = f"📅 波段預選（接續）"
            desc = f"**第 {chunk_start+1}-{chunk_end} 名**"

        embed = DiscordEmbed(title=title, description=desc, color=_COLOR_PRESELECT)

        for i, c in enumerate(chunk, start=chunk_start + 1):
            name, value = _build_candidate_field(c, fundamentals.get(c.symbol), rank=i)
            # Discord field value 上限 1024 字
            if len(value) > 1020:
                value = value[:1020] + "…"
            embed.add_embed_field(name=name, value=value, inline=False)

        embed.set_footer(text="Stock-Competition · 波段預選 / 隔日 09:35 將確認")
        embeds.append(embed)

    return embeds


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

    # 加入題材標籤
    themes = format_themes_short(position.symbol, max_tags=3)
    embed.add_embed_field(name="📌 題材", value=themes, inline=False)
    embed.add_embed_field(name="📋 動作", value=action, inline=False)
    embed.set_footer(text="Stock-Competition · 波段確認")
    return embed


def build_holding_check_embed(positions: list[SwingPosition], today: date) -> DiscordEmbed:
    """每日盤後持倉檢查 embed。"""
    title = f"📊 波段持倉檢查 ({len(positions)} 檔) | {today.strftime('%Y-%m-%d')}"
    embed = DiscordEmbed(title=title, color=_COLOR_HOLDING)

    for p in positions:
        days = p.days_held(today)
        time_warning = " ⚠️ 接近時間止損" if days >= 4 else ""

        entry = p.entry_actual or p.entry_limit
        themes = format_themes_short(p.symbol, max_tags=2)

        field_value = (
            f"📌 {themes}\n"
            f"📅 持有 {days} 天{time_warning}\n"
            f"💰 進場 {entry} / 停損 {p.stop_loss}\n"
            f"🎯 T1 {p.take_profit_1} / T2 {p.take_profit_2}\n"
            f"📊 數量 {p.lots} 張"
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
