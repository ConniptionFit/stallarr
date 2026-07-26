"""Discord / Telegram / ntfy alerts with severity filtering and dedupe."""

from __future__ import annotations

import base64
import hashlib
import logging
from urllib.parse import urlsplit

import httpx

from .. import db
from ..config import NotifyConfig
from ..models import ActionResult, Diagnosis, Severity
from ..utils import redact_secrets, truncate

log = logging.getLogger(__name__)

_COLOURS = {
    Severity.CRITICAL: 0xE24C4B,
    Severity.WARNING: 0xE0A800,
    Severity.INFO: 0x4B9FE2,
    Severity.OK: 0x3FB950,
}

#: ntfy priority per severity. Deliberately tops out at 4 (high) rather than 5:
#: priority 5 bypasses Do Not Disturb, and a stuck torrent does not warrant
#: waking someone at 3am. Anyone who disagrees can raise it on the ntfy side
#: with a topic-level rule.
_NTFY_PRIORITY = {
    Severity.CRITICAL: 4,
    Severity.WARNING: 3,
    Severity.INFO: 2,
    Severity.OK: 3,
}

#: ntfy renders these as emoji next to the title.
_NTFY_TAGS = {
    Severity.CRITICAL: "rotating_light",
    Severity.WARNING: "warning",
    Severity.INFO: "information_source",
    Severity.OK: "white_check_mark",
}


class Notifier:
    def __init__(self, cfg: NotifyConfig, base_url: str = "") -> None:
        self.cfg = cfg
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def enabled(self) -> bool:
        return bool(self._channels())

    def _channels(self) -> list[str]:
        """Channels that are both switched on and actually configured.

        A channel enabled but missing its topic or token is not a channel; it
        would silently drop every alert. Keeping this in one place means
        `enabled`, `_send` and `test` can never disagree about that.
        """
        active = []
        if self.cfg.discord_enabled and self.cfg.discord_webhook_url:
            active.append("discord")
        if self.cfg.telegram_enabled and self.cfg.telegram_bot_token and self.cfg.telegram_chat_id:
            active.append("telegram")
        if self.cfg.ntfy_enabled and self.cfg.ntfy_url and self.cfg.ntfy_topic:
            active.append("ntfy")
        return active

    async def alert(self, diagnosis: Diagnosis, result: ActionResult | None) -> None:
        """Send an alert if this event clears the severity and dedupe gates."""
        finding = diagnosis.primary
        if finding is None or not self.enabled:
            return
        if int(diagnosis.severity) < self.cfg.min_severity:
            return

        key = f"{diagnosis.torrent.infohash}:{finding.ailment}"
        if not await db.should_notify(key, self.cfg.dedupe_hours * 3600):
            return

        title = f"{_icon(diagnosis.severity)} {_label(finding.ailment)}"
        fields = {
            "Torrent": truncate(diagnosis.torrent.name, 120),
            "Tracker": diagnosis.tracker_id or "unknown",
            "Detail": finding.detail,
        }
        if result is not None:
            outcome = "✅" if result.ok else "❌"
            prefix = "Would run" if result.dry_run else "Action"
            fields[prefix] = f"{outcome} `{result.kind}` — {result.message}"
        else:
            fields["Action"] = "none available — recovery is exhausted or disabled"

        await self._send(title, fields, diagnosis.severity)

    async def seeding_digest(self, rows: list[dict[str, str]]) -> None:
        """One alert covering every torrent every tracker still wants seeded.

        Deliberately a digest and not per-torrent. These arrive in batches — a
        site publishes its whole hit-and-run list at once — and eight separate
        notifications for one situation is how people mute a channel.

        Dedupe keys on *which* torrents are owed, not on their state. A torrent
        going from stopped to seeding while still owing data is progress, not
        news; a torrent appearing or disappearing is news, and re-alerts at once.
        """
        if not (self.cfg.seeding_digest_enabled and self.enabled and rows):
            return
        if int(Severity.CRITICAL) < self.cfg.min_severity:
            return

        identity = sorted(f"{r['tracker']}|{r['name']}" for r in rows)
        key = "seeding:" + hashlib.sha256("\n".join(identity).encode()).hexdigest()
        if not await db.should_notify(key, self.cfg.dedupe_hours * 3600):
            return

        by_tracker = _group_by_tracker(rows)
        title = (
            f"🔴 Seeding required — {len(rows)} torrent(s) "
            f"on {len(by_tracker)} tracker(s)"
        )
        await self._send(title, _digest_fields(by_tracker), Severity.CRITICAL)

    async def seeding_cleared(self) -> None:
        """Say so when the last obligation clears.

        Exempt from `min_severity` on purpose. It resolves an alert that was
        already delivered, and suppressing it would leave someone believing a
        hit-and-run is still counting down — worse than the noise of one line.
        """
        if not (self.cfg.seeding_digest_enabled and self.enabled):
            return
        await self._send(
            "✅ Seeding requirements cleared",
            {"Status": "No tracker is asking for more seeding right now."},
            Severity.OK,
        )

    async def _send(self, title: str, fields: dict[str, str], severity: Severity) -> None:
        for channel in self._channels():
            if channel == "discord":
                await self._discord(title, fields, _COLOURS.get(severity, 0x888888))
            elif channel == "telegram":
                await self._telegram(title, fields)
            elif channel == "ntfy":
                await self._ntfy(title, fields, severity)

    async def _discord(self, title: str, fields: dict[str, str], colour: int) -> None:
        embed = {
            "title": title,
            "color": colour,
            "fields": [
                {"name": name, "value": truncate(value, 1000) or "—", "inline": False}
                for name, value in fields.items()
            ],
            "footer": {"text": "stallarr"},
        }
        if self.base_url:
            embed["url"] = self.base_url
        try:
            resp = await self._client.post(self.cfg.discord_webhook_url, json={"embeds": [embed]})
            if resp.status_code >= 400:
                log.warning("discord webhook returned HTTP %s: %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            log.warning("discord webhook failed: %s", redact_secrets(str(exc)))

    async def _telegram(self, title: str, fields: dict[str, str]) -> None:
        lines = [f"*{_escape(title)}*"]
        lines += [f"*{_escape(name)}:* {_escape(value)}" for name, value in fields.items()]
        url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": "\n".join(lines),
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            resp = await self._client.post(url, json=payload)
            if resp.status_code >= 400:
                log.warning("telegram send returned HTTP %s: %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            log.warning("telegram send failed: %s", redact_secrets(str(exc)))

    async def _ntfy(self, title: str, fields: dict[str, str], severity: Severity) -> None:
        """Publish via ntfy's JSON endpoint rather than its header protocol.

        The header form (`X-Title: …`) cannot carry the emoji and release names
        that end up in these messages: HTTP headers are latin-1, and httpx
        raises on anything outside it. The JSON body is UTF-8 and sidesteps the
        problem entirely.
        """
        body = {
            "topic": self.cfg.ntfy_topic,
            "title": truncate(title, 250),
            "message": "\n".join(f"{name}: {value}" for name, value in fields.items()),
            "priority": _NTFY_PRIORITY.get(severity, 3),
            "tags": [_NTFY_TAGS.get(severity, "loudspeaker")],
        }
        if self.base_url:
            # Tapping the notification opens the dashboard.
            body["click"] = self.base_url

        endpoint = _ntfy_endpoint(self.cfg.ntfy_url)
        try:
            resp = await self._client.post(endpoint, json=body, headers=self._ntfy_auth())
            if resp.status_code >= 400:
                log.warning("ntfy publish returned HTTP %s: %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            log.warning("ntfy publish failed: %s", redact_secrets(str(exc)))

    def _ntfy_auth(self) -> dict[str, str]:
        """Bearer token, else basic auth. A token wins when both are set."""
        if self.cfg.ntfy_token:
            return {"Authorization": f"Bearer {self.cfg.ntfy_token}"}
        if self.cfg.ntfy_username or self.cfg.ntfy_password:
            raw = f"{self.cfg.ntfy_username}:{self.cfg.ntfy_password}".encode()
            return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}
        return {}

    async def test(self) -> dict[str, str]:
        """Fire a test message to every enabled channel; used by the WebUI."""
        fields = {"Status": "If you can read this, notifications are wired up correctly."}
        channels = self._channels()
        if not channels:
            return {"error": "no notification channel is enabled and fully configured"}

        await self._send("🔔 stallarr test", fields, Severity.OK)
        return dict.fromkeys(channels, "sent")


#: Discord caps an embed field at 1024 characters and an embed at 25 fields.
#: One tracker is one field, so these keep a long list inside both without the
#: message being silently rejected — a dropped alert is worse than a trimmed one.
_DIGEST_MAX_PER_TRACKER = 12
_DIGEST_MAX_TRACKERS = 20


def _group_by_tracker(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """Group by tracker name, sorted by tracker then by torrent name.

    Case-insensitive, so a lowercase plugin id and a capitalised display name
    order the way a reader expects rather than the way ASCII does.
    """
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in sorted(
        rows, key=lambda r: (r["tracker_name"].casefold(), r["name"].casefold())
    ):
        grouped.setdefault(row["tracker_name"], []).append(row)
    return grouped


def _digest_fields(by_tracker: dict[str, list[dict[str, str]]]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for tracker_name, items in list(by_tracker.items())[:_DIGEST_MAX_TRACKERS]:
        lines = [
            f"• {truncate(item['name'], 70)} — {item['detail']} · {item['status']}"
            for item in items[:_DIGEST_MAX_PER_TRACKER]
        ]
        if len(items) > _DIGEST_MAX_PER_TRACKER:
            lines.append(f"…and {len(items) - _DIGEST_MAX_PER_TRACKER} more")
        fields[f"{tracker_name} ({len(items)})"] = "\n".join(lines)

    hidden = len(by_tracker) - _DIGEST_MAX_TRACKERS
    if hidden > 0:
        fields["…"] = f"and {hidden} more tracker(s)"
    return fields


def _ntfy_endpoint(url: str) -> str:
    """The JSON publish endpoint, with any credentials stripped off.

    ntfy also accepts auth as `?auth=<token>` in the query string, and people
    paste URLs in that form. We send credentials as a header instead, so any
    query string here is at best redundant and at worst a token that would end
    up in an httpx error message. Userinfo (`https://user:pass@host`) goes the
    same way. The path is kept — self-hosted instances live under one.
    """
    parts = urlsplit(url.strip())
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    scheme = parts.scheme or "https"
    return f"{scheme}://{host}{parts.path}".rstrip("/")


def _icon(severity: Severity) -> str:
    return {Severity.CRITICAL: "🔴", Severity.WARNING: "🟠", Severity.INFO: "🔵"}.get(severity, "⚪")


def _label(ailment: str) -> str:
    return str(ailment).replace("_", " ").title()


def _escape(text: str) -> str:
    for char in ("_", "*", "[", "]", "`"):
        text = text.replace(char, "\\" + char)
    return text


__all__ = ["Notifier"]
