"""Tracker plugin contract.

A plugin's whole job is to answer questions the torrent client cannot:

  * "is this torrent still registered on the site?"
  * "what is my current announce URL?"      (passkey rotation, domain moves)
  * "give me a fresh .torrent for this hash"
  * "am I about to eat a hit-and-run?"
  * "what are my account stats?"

Every method is optional. Declare what you actually implement in
``capabilities`` and the recovery engine will skip the steps you can't support
instead of failing. See ADDING_A_TRACKER.md for the full walkthrough.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import StrEnum
from typing import Any

import httpx

from ..config import TrackerConfig
from ..models import SeedingRequirement, TrackerHealth, TrackerTorrentInfo, UserStats
from ..utils import normalise_host, redact_secrets

log = logging.getLogger(__name__)


class TrackerError(RuntimeError):
    pass


class Capability(StrEnum):
    #: build a working announce URL from the configured passkey
    ANNOUNCE_URL = "announce_url"
    #: fetch the .torrent file for a given infohash
    TORRENT_FILE = "torrent_file"
    #: fetch the .torrent file for one of the *site's* torrent ids. Separate
    #: from TORRENT_FILE because the caller is different: this one serves
    #: torrents the client does not hold, so there is no infohash to look up —
    #: only the id the tracker itself put in its seeding-requirement row.
    TORRENT_FILE_BY_ID = "torrent_file_by_id"
    #: look a torrent up on the site (seeders, existence, seed-time)
    TORRENT_INFO = "torrent_info"
    #: report account-level stats
    USER_STATS = "user_stats"
    #: report hit-and-run exposure for a torrent
    HNR = "hnr"
    #: list the torrents the site says we still owe seeding on
    SEEDING_REQUIREMENTS = "seeding_requirements"


#: Tracker messages that mean "this torrent is gone, stop retrying".
UNREGISTERED_MARKERS = (
    "unregistered torrent",
    "torrent not registered",
    "torrent not found",
    "not registered with this tracker",
    "unknown torrent",
    "info hash not found",
    "invalid infohash",
    "torrent has been deleted",
    "torrent is deleted",
    "trumped",
    "nuked",
    "dupe",
)

#: Tracker messages that mean "your credentials are wrong", not "the site is down".
AUTH_MARKERS = (
    "passkey",
    "invalid passkey",
    "unauthorized",
    "unauthorised",
    "not authorized",
    "authentication failed",
    "invalid user",
    "user not found",
    "account disabled",
    "invalid announce key",
    "missing passkey",
)


class TrackerPlugin:
    """Base class. Subclass, set the class attributes, override what you can do."""

    #: Stable identifier used as the config key. Lowercase, no spaces.
    id: str = ""
    #: Human-readable name for the UI.
    name: str = ""
    #: Default site base URL, used when config leaves `url` blank.
    default_url: str = ""
    #: Announce hostnames that identify this tracker. Matched as suffixes, so
    #: "iptorrents.com" also matches "sg.iptorrents.com".
    hosts: tuple[str, ...] = ()
    #: What this plugin can actually do.
    capabilities: frozenset[Capability] = frozenset()
    #: Minimum seconds between HTTP requests — private trackers ban hammering.
    rate_limit_seconds: float = 2.0
    #: Which credential fields this plugin actually reads. The settings form is
    #: built from this, so a UNIT3D site stops showing cookie boxes it will
    #: never look at — and a new plugin gets the right form with no frontend
    #: change, which is the whole point of the one-file rule.
    credential_fields: tuple[str, ...] = ("api_key", "passkey")

    def __init__(self, cfg: TrackerConfig) -> None:
        self.cfg = cfg
        self.base_url = (cfg.url or self.default_url).rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._throttle = asyncio.Lock()
        self._last_request = 0.0

    # --- identity -------------------------------------------------------------

    @property
    def all_hosts(self) -> tuple[str, ...]:
        extra = tuple(normalise_host(h) for h in self.cfg.extra_hosts if h.strip())
        return self.hosts + extra

    def matches_host(self, host: str) -> bool:
        host = normalise_host(host)
        if not host:
            return False
        return any(host == known or host.endswith("." + known) for known in self.all_hosts)

    def can(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def option(self, key: str, default: Any = None) -> Any:
        return self.cfg.options.get(key, default)

    # --- HTTP ----------------------------------------------------------------

    def default_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": "stallarr/0.1 (+torrent health monitor)",
            "Accept": "application/json, text/html;q=0.8",
        }
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        return headers

    @property
    def cookie_header(self) -> str:
        """The Cookie header, from either the raw paste or the split fields.

        A raw header wins: it is the "copy the whole line out of devtools" path
        and may carry session cookies beyond uid/pass. Otherwise both halves are
        required — `uid` alone is not a session, and sending it would look
        configured while failing every request.

        The `uid=` / `pass=` prefixes are stripped if present, because a value
        copied out of a cookie table is as likely to arrive with them as without.
        """
        raw = self.cfg.cookie.strip().strip(";")
        if raw:
            return raw
        uid = self.cfg.cookie_uid.strip().strip(";").removeprefix("uid=")
        secret = self.cfg.cookie_pass.strip().strip(";").removeprefix("pass=")
        if uid and secret:
            return f"uid={uid}; pass={secret}"
        return ""

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(30.0, connect=15.0),
                follow_redirects=True,
                headers=self.default_headers(),
            )
        return self._client

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Rate-limited request. Always use this instead of self.http directly."""
        async with self._throttle:
            gap = time.monotonic() - self._last_request
            if gap < self.rate_limit_seconds:
                await asyncio.sleep(self.rate_limit_seconds - gap)
            self._last_request = time.monotonic()
        try:
            return await self.http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            # `url` is not always a bare path: fetch_torrent_file passes the
            # site's own download link, which on UNIT3D embeds the rsskey. This
            # message reaches ActionResult -> history -> notifications, so it
            # has to be clean before the exception is even raised.
            detail = redact_secrets(f"{method} {url} failed: {exc}")
            raise TrackerError(f"{self.id}: {detail}") from exc

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- overridable behaviour -----------------------------------------------

    def classify_message(self, message: str) -> str | None:
        """Map a tracker's announce error to 'unregistered' / 'auth' / None.

        Override when a site uses wording the shared marker lists don't cover.
        """
        low = (message or "").lower()
        if not low:
            return None
        if any(marker in low for marker in UNREGISTERED_MARKERS):
            return "unregistered"
        if any(marker in low for marker in AUTH_MARKERS):
            return "auth"
        return None

    async def health(self) -> TrackerHealth:
        """Cheap reachability check shown on the dashboard."""
        if not self.base_url:
            return TrackerHealth(ok=False, detail="no site URL configured")
        started = time.monotonic()
        try:
            resp = await self.get("/")
        except TrackerError as exc:
            return TrackerHealth(ok=False, detail=str(exc))
        latency = int((time.monotonic() - started) * 1000)
        ok = resp.status_code < 400
        return TrackerHealth(ok=ok, detail=f"HTTP {resp.status_code}", latency_ms=latency)

    async def announce_url(self, infohash: str, current: str | None = None) -> str | None:
        """The announce URL this torrent *should* be using, or None if unsupported.

        `current` is the URL the client has now, when there is one. Prefer
        rewriting it over building a URL from scratch: sites rotate announce
        domains, and the domain already on the torrent is the known-good one.
        Return None when nothing needs to change.
        """
        return None

    async def torrent_info(self, infohash: str) -> TrackerTorrentInfo | None:
        """What the site knows about this infohash, or None if unknown/unsupported."""
        return None

    async def fetch_torrent_file(self, infohash: str) -> bytes | None:
        """Raw .torrent bytes for re-adding a broken torrent."""
        return None

    async def fetch_torrent_file_by_id(self, torrent_id: str) -> bytes | None:
        """Raw .torrent bytes for one of the site's own torrent ids.

        Used to re-acquire a torrent that is missing from the client entirely,
        where `fetch_torrent_file` cannot help: there is no local torrent, so
        there is no infohash to search for. A seeding-requirement row carries
        the site's id precisely so this is possible.
        """
        return None

    async def user_stats(self) -> UserStats | None:
        return None

    async def seeding_requirements(self) -> list[SeedingRequirement]:
        """Torrents the site says we still owe seeding on.

        One call per tracker per scan, not one per torrent — that is what makes
        this affordable where per-torrent enrichment is not. Return `[]` when
        the site has no such page; never raise for "nothing owed".
        """
        return []

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} id={self.id!r} base={self.base_url!r}>"
