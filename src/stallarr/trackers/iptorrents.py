"""IPTorrents — no JSON API, so this plugin works two ways.

**Passkey mode** (always on when `passkey` is set) is the reliable half. IPT
announce URLs carry the passkey in the path::

    http://localhost.stackoverflow.tech:80/<passkey>/announce

so announce repair is a path rewrite that preserves whichever announce domain
the torrent already uses — IPT rotates those domains, and guessing one is how
you end up with a dead tracker.

**Cookie mode** adds site lookups (does this torrent still exist, seeder counts,
fresh .torrent downloads) by scraping the HTML with your logged-in session.
Cookies expire; when that happens the plugin degrades back to passkey mode and
reports it on the dashboard rather than failing the scan.

Config::

    "iptorrents": {
      "enabled": true,
      "url": "https://iptorrents.com",
      "passkey": "<the long hex string in your announce URL>",
      "cookie_uid": "123456",
      "cookie_pass": "abcdef0123456789..."
    }

`cookie` still works and takes precedence — paste a whole Cookie header there
when a site needs more than uid/pass. The split fields exist because that is how
a browser's storage inspector shows them, and hand-assembling the header is
where people get the separator wrong.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse, urlunparse

from ..models import SeedingRequirement, TrackerHealth, TrackerTorrentInfo, UserStats
from ..utils import looks_like_torrent
from . import register
from .base import Capability, TrackerError, TrackerPlugin

log = logging.getLogger(__name__)

#: IPT rotates announce domains; this list is only used when we have to build a
#: URL from scratch. Rewriting an existing URL always keeps its own host.
KNOWN_ANNOUNCE_HOSTS = (
    "iptorrents.com",
    "localhost.stackoverflow.tech",
    "stackoverflow.tech",
    "empirehost.me",
    "bgp.technology",
    "ipt.beelyrics.net",
    "ipt.getcrazy.me",
    "ipt.findnemo.net",
    "ipt.lol",
    "ipt.world",
    "ipt.myserver.solutions",
)

#: /<40-hex-ish passkey>/announce
_PASSKEY_PATH = re.compile(r"^/([A-Za-z0-9]{20,64})/announce/?$")

_TORRENT_ID = re.compile(r"/download\.php/(\d+)/", re.IGNORECASE)
_DETAILS_ID = re.compile(r"details\.php\?id=(\d+)", re.IGNORECASE)
_LOGIN_HINT = re.compile(r"(login\.php|<title>[^<]*log\s*in)", re.IGNORECASE)


@register
class IPTorrents(TrackerPlugin):
    id = "iptorrents"
    name = "IPTorrents"
    default_url = "https://iptorrents.com"
    hosts = KNOWN_ANNOUNCE_HOSTS
    #: IPT is strict about request rates; stay well clear of the limit.
    rate_limit_seconds = 4.0
    #: No API token here — the site is passkey plus a browser session.
    credential_fields = ("passkey", "cookie_uid", "cookie_pass", "cookie")

    @property
    def capabilities(self) -> frozenset[Capability]:  # type: ignore[override]
        """Cookie presence decides what this plugin can do."""
        caps = set()
        if self.cfg.passkey:
            caps.add(Capability.ANNOUNCE_URL)
        if self.cookie_header:
            caps.update({
                Capability.TORRENT_INFO,
                Capability.TORRENT_FILE,
                Capability.TORRENT_FILE_BY_ID,
                Capability.USER_STATS,
                Capability.SEEDING_REQUIREMENTS,
            })
        return frozenset(caps)

    # --- announce repair (passkey mode) ---------------------------------------

    async def announce_url(self, infohash: str, current: str | None = None) -> str | None:
        passkey = self.cfg.passkey
        if not passkey:
            return None

        # If the torrent already has a recognisable IPT announce URL, that URL's
        # host is authoritative — only the passkey may be wrong. Returning None
        # here means "already correct", which is different from "can't tell".
        if current and _PASSKEY_PATH.match(_path_of(current)):
            return self._rewrite_passkey(current, passkey)

        host = self.option("announce_host", "localhost.stackoverflow.tech")
        scheme = self.option("announce_scheme", "http")
        return f"{scheme}://{host}/{passkey}/announce"

    @staticmethod
    def _rewrite_passkey(current: str, passkey: str) -> str | None:
        """Swap the passkey in an existing announce URL, keeping scheme/host/port.

        Returns None when the URL is already correct — there is nothing to fix.
        """
        try:
            parts = urlparse(current)
        except ValueError:
            return None
        match = _PASSKEY_PATH.match(parts.path)
        if not match or match.group(1) == passkey:
            return None
        return urlunparse(parts._replace(path=f"/{passkey}/announce"))

    def classify_message(self, message: str) -> str | None:
        low = (message or "").lower()
        # IPT answers a bad passkey with a generic phrase the shared list misses.
        if "torrent not found" in low and "passkey" not in low:
            return "unregistered"
        if "invalid" in low and "key" in low:
            return "auth"
        return super().classify_message(message)

    # --- cookie mode ----------------------------------------------------------

    async def health(self) -> TrackerHealth:
        if not self.cookie_header:
            if self.cfg.passkey:
                return TrackerHealth(ok=True, detail="passkey mode (no cookie — lookups disabled)")
            return TrackerHealth(ok=False, detail="neither cookie nor passkey configured")

        resp = await self.get(self.option("stats_path", "/t"))
        if resp.status_code >= 400:
            return TrackerHealth(ok=False, detail=f"HTTP {resp.status_code}")
        if self._is_logged_out(resp.text):
            return TrackerHealth(ok=False, detail="cookie expired — re-paste it in Settings")
        return TrackerHealth(ok=True, detail="cookie session valid")

    @staticmethod
    def _is_logged_out(html: str) -> bool:
        return bool(_LOGIN_HINT.search(html[:4000]))

    async def _search(self, infohash: str) -> str | None:
        """Return the search-results HTML for an infohash, or None."""
        if not self.cookie_header:
            return None
        resp = await self.get(self.option("search_path", "/t"), params={"q": infohash})
        if resp.status_code >= 400:
            raise TrackerError(f"{self.id}: search returned HTTP {resp.status_code}")
        if self._is_logged_out(resp.text):
            raise TrackerError(f"{self.id}: session cookie expired")
        return resp.text

    async def torrent_info(self, infohash: str) -> TrackerTorrentInfo | None:
        html = await self._search(infohash)
        if html is None:
            return None

        match = _TORRENT_ID.search(html) or _DETAILS_ID.search(html)
        if not match:
            # IPT's search does index infohashes, so no hit means it's gone.
            return TrackerTorrentInfo(infohash=infohash, exists=False)

        torrent_id = match.group(1)
        return TrackerTorrentInfo(
            infohash=infohash,
            torrent_id=torrent_id,
            exists=True,
            seeders=_first_int(re.search(r'class="t_seeders"[^>]*>(\d+)', html)),
            leechers=_first_int(re.search(r'class="t_leechers"[^>]*>(\d+)', html)),
        )

    async def fetch_torrent_file(self, infohash: str) -> bytes | None:
        info = await self.torrent_info(infohash)
        if info is None or not info.exists or not info.torrent_id:
            return None
        return await self.fetch_torrent_file_by_id(info.torrent_id)

    async def fetch_torrent_file_by_id(self, torrent_id: str) -> bytes | None:
        """Download by the site's own id, skipping the infohash search.

        This is the only path available for a torrent the client does not hold
        — a seeding requirement names the id, and there is nothing local to
        search IPT for. It is also strictly cheaper than the infohash route,
        which spends a search request arriving at the same id.
        """
        if not self.cookie_header:
            return None

        template = self.option("download_template", "{base}/download.php/{id}/stallarr.torrent")
        url = template.format(base=self.base_url, id=torrent_id)
        params = {"torrent_pass": self.cfg.passkey} if self.cfg.passkey else None

        resp = await self.get(url, params=params)
        if resp.status_code >= 400:
            raise TrackerError(f"{self.id}: download returned HTTP {resp.status_code}")
        if not looks_like_torrent(resp.content):
            raise TrackerError(f"{self.id}: download did not return a .torrent (session expired?)")
        return resp.content

    async def user_stats(self) -> UserStats | None:
        if not self.cookie_header:
            return None
        resp = await self.get(self.option("stats_path", "/t"))
        if resp.status_code >= 400 or self._is_logged_out(resp.text):
            return None
        html = resp.text
        return UserStats(
            uploaded=_parse_size(re.search(r"Uploaded[^0-9]*([\d.]+\s*[KMGTP]?i?B)", html, re.IGNORECASE)),
            downloaded=_parse_size(re.search(r"Downloaded[^0-9]*([\d.]+\s*[KMGTP]?i?B)", html, re.IGNORECASE)),
            ratio=_first_float(re.search(r"Ratio[^0-9]*([\d.]+)", html, re.IGNORECASE)),
            hit_and_runs=_first_int(re.search(r"H&?R[^0-9]*(\d+)", html, re.IGNORECASE)),
        )


    async def seeding_requirements(self) -> list[SeedingRequirement]:
        """Scrape /seeding_required.php — the site's own hit-and-run list.

        This is one request per scan and it replaces guesswork: rather than
        inferring risk from seed time, IPT names the torrents it will penalise
        us for. A row disappearing is the site confirming the debt is cleared.

        The markup is unclosed-tag soup (`<td>` never closed, `<tr>` nested
        inside a `<tr>`), so an HTML parser is no better than splitting on the
        row ids the page assigns each entry.
        """
        if not self.cookie_header:
            return []
        resp = await self.get(self.option("seeding_path", "/seeding_required.php"))
        if resp.status_code >= 400:
            raise TrackerError(f"{self.id}: seeding list returned HTTP {resp.status_code}")
        if self._is_logged_out(resp.text):
            raise TrackerError(f"{self.id}: session cookie expired")
        return _parse_seeding_required(resp.text)


#: `<tr id="line2082626">` … up to the next row id or the end of the document.
_SEED_ROW = re.compile(r'(?s)<tr id="line(\d+)">(.*?)(?=<tr id="line\d+">|\Z)')
#: Cells end at the next cell or row, because this page never closes a `<td>`.
_SEED_CELL = re.compile(r"(?s)<td[^>]*>(.*?)(?=<td|<tr|\Z)")
#: The seed-time cell carries a hover tooltip holding a *different* duration
#: ("22.4 hours ago" — when it was snatched). Drop it before reading the number,
#: or every torrent looks like it has already seeded for a day.
_TOOLTIP = re.compile(r'(?s)<div class="tTip.*?</div>')
_HOURS = re.compile(r"([\d.]+)\s*hours?", re.IGNORECASE)


#: Columns: torrent, uploaded, downloaded, gap, seeding time, then zap buttons.
_UPLOADED, _DOWNLOADED, _GAP, _SEED_TIME = 1, 2, 3, 4


def _parse_seeding_required(html: str) -> list[SeedingRequirement]:
    out: list[SeedingRequirement] = []
    for torrent_id, row in _SEED_ROW.findall(html):
        name = re.search(r'<a href="/t/\d+">([^<]+)</a>', row)
        if not name:
            continue  # header row, or a layout change we should not guess at
        cells = _SEED_CELL.findall(row)
        out.append(
            SeedingRequirement(
                torrent_id=torrent_id,
                name=name.group(1).strip(),
                uploaded=_size_to_bytes(_cell(cells, _UPLOADED)),
                downloaded=_size_to_bytes(_cell(cells, _DOWNLOADED)),
                gap=_size_to_bytes(_cell(cells, _GAP)),
                seeded_hours=_hours(_cell(cells, _SEED_TIME, keep_tooltip=False)),
            )
        )
    return out


def _cell(cells: list[str], index: int, *, keep_tooltip: bool = True) -> str:
    if len(cells) <= index:
        return ""
    raw = cells[index] if keep_tooltip else _TOOLTIP.sub("", cells[index])
    return _text(raw)


def _hours(text: str) -> float | None:
    match = _HOURS.search(text)
    try:
        return float(match.group(1)) if match else None
    except ValueError:
        return None


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def _path_of(url: str) -> str:
    try:
        return urlparse(url).path
    except ValueError:
        return ""


def _first_int(match: re.Match | None) -> int | None:
    try:
        return int(match.group(1)) if match else None
    except (ValueError, IndexError):
        return None


def _first_float(match: re.Match | None) -> float | None:
    try:
        return float(match.group(1)) if match else None
    except (ValueError, IndexError):
        return None


_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4, "pb": 1024**5}


def _parse_size(match: re.Match | None) -> int | None:
    """'1.42 TiB' -> bytes, from a regex match."""
    return _size_to_bytes(match.group(1)) if match else None


def _size_to_bytes(value: str) -> int | None:
    """'57.47 MB' -> bytes. Returns None for '', '-' and anything unparseable."""
    if not value:
        return None
    text = value.strip().lower().replace("i", "")
    size_match = re.match(r"([\d.]+)\s*([kmgtp]?b)", text)
    if not size_match:
        return None
    try:
        return int(float(size_match.group(1)) * _UNITS.get(size_match.group(2), 1))
    except ValueError:
        return None
