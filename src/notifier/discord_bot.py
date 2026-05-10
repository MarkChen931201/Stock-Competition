"""Discord Webhook 推播 (盤前 screener + 盤中訊號共用)."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from discord_webhook import DiscordEmbed, DiscordWebhook
from loguru import logger


class DiscordNotifier:
    def __init__(self, webhook_url: str):
        if not webhook_url:
            raise ValueError("DISCORD_WEBHOOK_URL is required")
        self.webhook_url = webhook_url

    def _send(self, embed: DiscordEmbed, content: str | None = None) -> bool:
        wh = DiscordWebhook(url=self.webhook_url, content=content or "")
        wh.add_embed(embed)
        try:
            resp = wh.execute()
            ok = 200 <= getattr(resp, "status_code", 0) < 300
            if not ok:
                logger.warning(f"Discord webhook returned {resp.status_code}: {resp.text}")
            return ok
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Discord webhook send failed: {e}")
            return False

    # --- 盤前篩選結果 ---
    def send_screener_result(
        self,
        candidates: Iterable[dict],
        as_of: datetime,
        criteria: dict,
    ) -> bool:
        """candidates: list of dict with keys
        stock_id, name, close, avg_volume_lots, turnover_rate, atr_pct, score, asset_type
        """
        candidates = list(candidates)
        title = f"📋 盤前篩選池 | {as_of:%Y-%m-%d %H:%M}  (共 {len(candidates)} 檔)"
        desc_lines = [
            "**篩選條件**",
            f"• 平均量 ≥ {criteria.get('min_avg_volume_lots'):,} 張",
            f"• 週轉率 ≥ {criteria.get('min_turnover_rate'):.2%}",
            f"• ATR% ≥ {criteria.get('min_atr_pct'):.2%}",
            f"• 價格區間 {criteria.get('price_min')}–{criteria.get('price_max')}",
        ]

        embed = DiscordEmbed(
            title=title,
            description="\n".join(desc_lines),
            color="03b2f8",
        )

        if not candidates:
            embed.add_embed_field(
                name="結果", value="⚠️ 無符合條件的標的", inline=False
            )
            return self._send(embed)

        # 依 score 由高到低，每 10 筆一個 field（Discord field 上限 1024 字元）
        header = f"`{'代號':<6}{'名稱':<8}{'收盤':>8}{'量(張)':>9}{'ATR%':>7}{'分數':>7}`"

        def _fmt_row(c: dict) -> str:
            return "`{sid:<6}{name:<8}{close:>8.2f}{vol:>9,.0f}{atr:>7.1%}{score:>7.2f}`".format(
                sid=c["stock_id"],
                name=(c.get("name") or "-")[:6],
                close=float(c["close"]),
                vol=float(c["avg_volume_lots"]),
                atr=float(c["atr_pct"]),
                score=float(c["score"]),
            )

        top = candidates[:20]
        for chunk_start in range(0, len(top), 10):
            chunk = top[chunk_start: chunk_start + 10]
            rows = [header] + [_fmt_row(c) for c in chunk]
            embed.add_embed_field(
                name=f"候選池 {chunk_start + 1}–{chunk_start + len(chunk)}",
                value="\n".join(rows),
                inline=False,
            )

        embed.set_footer(text="Stock-Competition · prefetch_universe")
        return self._send(embed)
