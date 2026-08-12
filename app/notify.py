"""Optional push relays.

The dashboard is the primary surface — it streams alerts over SSE to any
device with the link. These relays are for when your phone is locked and the
browser tab is not open. Both are free; leave the env vars unset to disable.
"""

from __future__ import annotations

import logging

import httpx

from .config import Settings
from .models import Alert, AlertKind

log = logging.getLogger(__name__)

_EMOJI = {
    AlertKind.VCP_SETUP: "◆",
    AlertKind.BREAKOUT: "▲",
    AlertKind.MOMENTUM: "⚡",
    AlertKind.SYSTEM: "•",
}


def _plain(alert: Alert) -> str:
    bits = [f"{_EMOJI.get(alert.kind, '•')} {alert.headline}"]
    if alert.detail:
        bits.append(alert.detail)
    if alert.pivot:
        bits.append(f"pivot {alert.pivot}  stop {alert.stop}")
    bits.append(alert.ts.strftime("%d %b %H:%M IST"))
    return "\n".join(bits)


class Notifier:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    @property
    def enabled(self) -> bool:
        return bool(
            (self.s.telegram_bot_token and self.s.telegram_chat_id)
            or self.s.discord_webhook_url
        )

    async def send(self, alert: Alert) -> None:
        if alert.kind is AlertKind.SYSTEM:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            if self.s.telegram_bot_token and self.s.telegram_chat_id:
                await self._telegram(client, alert)
            if self.s.discord_webhook_url:
                await self._discord(client, alert)

    async def _telegram(self, client: httpx.AsyncClient, alert: Alert) -> None:
        url = f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage"
        body = {
            "chat_id": self.s.telegram_chat_id,
            "text": _plain(alert),
            "disable_web_page_preview": True,
        }
        try:
            r = await client.post(url, json=body)
            if r.status_code >= 400:
                log.warning("telegram rejected alert: %s %s", r.status_code, r.text[:200])
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram send failed: %s", exc)

    async def _discord(self, client: httpx.AsyncClient, alert: Alert) -> None:
        colour = {
            AlertKind.VCP_SETUP: 0xF2A03D,
            AlertKind.BREAKOUT: 0x31C48D,
            AlertKind.MOMENTUM: 0xE8C46A,
        }.get(alert.kind, 0x8A857B)
        fields = [{"name": "Price", "value": f"{alert.price:,.2f}", "inline": True}]
        if alert.change_pct is not None:
            fields.append(
                {"name": "Move", "value": f"{alert.change_pct:+.2f}%", "inline": True}
            )
        if alert.pivot:
            fields.append(
                {"name": "Pivot / Stop", "value": f"{alert.pivot} / {alert.stop}", "inline": True}
            )
        body = {
            "embeds": [
                {
                    "title": f"{_EMOJI.get(alert.kind, '•')} {alert.headline}",
                    "description": alert.detail or "",
                    "color": colour,
                    "fields": fields,
                    "timestamp": alert.ts.isoformat(),
                }
            ]
        }
        try:
            r = await client.post(self.s.discord_webhook_url, json=body)
            if r.status_code >= 400:
                log.warning("discord rejected alert: %s %s", r.status_code, r.text[:200])
        except Exception as exc:  # noqa: BLE001
            log.warning("discord send failed: %s", exc)
