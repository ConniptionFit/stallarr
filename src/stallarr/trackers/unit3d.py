"""Shared base for UNIT3D-powered trackers.

UNIT3D is the most common private-tracker codebase, so most new sites need
nothing but a subclass with an id, a URL and announce hostnames. See
darkpeers.py for the canonical example.

API shape (UNIT3D v6/v7):
    Authorization: Bearer <api_token>
    GET /api/torrents/filter?info_hash=<hex>   -> JSON:API style list
    GET /api/torrents/<id>                     -> single torrent
    attributes include: name, info_hash, seeders, leechers, download_link,
                        times_completed, size, ...

Sites drift. Every URL is overridable through the tracker's `options` block, so
a mismatch is a config change rather than a code change:

    "options": {
      "announce_template": "https://tracker.example.org/announce/{passkey}",
      "filter_path": "/api/torrents/filter",
      "info_hash_param": "info_hash",
      "download_template": "{base}/torrent/download/{id}.{passkey}"
    }
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import TrackerHealth, TrackerTorrentInfo, UserStats
from ..utils import looks_like_torrent
from .base import Capability, TrackerError, TrackerPlugin

log = logging.getLogger(__name__)


class Unit3dPlugin(TrackerPlugin):
    """Base class for UNIT3D sites. Subclasses normally only set class attrs."""

    capabilities = frozenset(
        {
            Capability.ANNOUNCE_URL,
            Capability.TORRENT_FILE,
            Capability.TORRENT_INFO,
            Capability.USER_STATS,
        }
    )

    #: UNIT3D's account endpoint. Stock across deployments; override per site
    #: with `"options": {"user_stats_path": "..."}` if one has moved it.
    user_stats_path: str = "/api/user"

    #: Announce endpoint. `{passkey}` is substituted; override per site if the
    #: announce host differs from the web host.
    announce_template: str = "{base}/announce/{passkey}"

    def default_headers(self) -> dict[str, str]:
        headers = super().default_headers()
        headers["Accept"] = "application/json"
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        return headers

    # --- helpers --------------------------------------------------------------

    @property
    def _passkey(self) -> str:
        # UNIT3D calls it an rsskey in some places; both live in the same field.
        return self.cfg.passkey or self.option("rsskey", "") or ""

    def _json(self, resp) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise TrackerError(f"{self.id}: expected JSON, got {resp.text[:120]!r}") from exc

    @staticmethod
    def _attributes(payload: Any) -> list[dict]:
        """Flatten JSON:API-ish payloads into a list of attribute dicts."""
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []

        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            attrs = item.get("attributes")
            record = dict(attrs) if isinstance(attrs, dict) else dict(item)
            record.setdefault("id", item.get("id"))
            out.append(record)
        return out

    # --- plugin API -----------------------------------------------------------

    async def health(self) -> TrackerHealth:
        if not self.cfg.api_key:
            return TrackerHealth(ok=False, detail="no API token configured")
        try:
            resp = await self.get(
                self.option("filter_path", "/api/torrents/filter"), params={"perPage": 1}
            )
        except TrackerError as exc:
            return TrackerHealth(ok=False, detail=str(exc))
        if resp.status_code in (401, 403):
            return TrackerHealth(ok=False, detail="API token rejected")
        if resp.status_code >= 400:
            return TrackerHealth(ok=False, detail=f"HTTP {resp.status_code}")
        return TrackerHealth(ok=True, detail="API reachable")

    async def announce_url(self, infohash: str, current: str | None = None) -> str | None:
        passkey = self._passkey
        if not passkey:
            return None
        template = self.option("announce_template", self.announce_template)
        rebuilt = template.format(base=self.base_url, passkey=passkey, infohash=infohash)
        return None if rebuilt == current else rebuilt

    async def torrent_info(self, infohash: str) -> TrackerTorrentInfo | None:
        if not self.cfg.api_key:
            return None
        param = self.option("info_hash_param", "info_hash")
        path = self.option("filter_path", "/api/torrents/filter")
        resp = await self.get(path, params={param: infohash, "perPage": 1})

        if resp.status_code in (401, 403):
            raise TrackerError(f"{self.id}: API token rejected (HTTP {resp.status_code})")
        if resp.status_code == 404:
            return TrackerTorrentInfo(infohash=infohash, exists=False)
        if resp.status_code >= 400:
            raise TrackerError(f"{self.id}: torrent lookup returned HTTP {resp.status_code}")

        records = self._attributes(self._json(resp))
        if not records:
            # An empty result set is a real signal: the site no longer has it.
            return TrackerTorrentInfo(infohash=infohash, exists=False)

        record = records[0]
        return TrackerTorrentInfo(
            infohash=infohash,
            torrent_id=str(record.get("id")) if record.get("id") is not None else None,
            name=record.get("name"),
            seeders=_as_int(record.get("seeders")),
            leechers=_as_int(record.get("leechers")),
            exists=True,
            raw=record,
        )

    async def fetch_torrent_file(self, infohash: str) -> bytes | None:
        info = await self.torrent_info(infohash)
        if info is None or not info.exists:
            return None

        url = self._download_url(info)
        if not url:
            return None

        resp = await self.get(url)
        if resp.status_code >= 400:
            raise TrackerError(f"{self.id}: download returned HTTP {resp.status_code}")
        if not looks_like_torrent(resp.content):
            raise TrackerError(f"{self.id}: download did not return a .torrent file")
        return resp.content

    def _download_url(self, info: TrackerTorrentInfo) -> str | None:
        template = self.option("download_template")
        if template and info.torrent_id:
            return template.format(base=self.base_url, id=info.torrent_id, passkey=self._passkey)

        link = info.raw.get("download_link")
        if isinstance(link, str) and link:
            return link

        if info.torrent_id and self._passkey:
            return f"{self.base_url}/torrent/download/{info.torrent_id}.{self._passkey}"
        return None

    async def user_stats(self) -> UserStats | None:
        """Account totals, including the tracker's own hit-and-run count.

        Note UNIT3D returns these pre-formatted for humans ("772.27 GiB",
        "216.3"), not as numbers — hence `_as_bytes` rather than `int()`.
        """
        path = self.option("user_stats_path", self.user_stats_path)
        if not path or not self.cfg.api_key:
            return None
        resp = await self.get(path)
        if resp.status_code >= 400:
            return None
        payload = self._json(resp)
        record = payload.get("data", payload) if isinstance(payload, dict) else {}
        if isinstance(record, dict) and isinstance(record.get("attributes"), dict):
            record = record["attributes"]
        if not isinstance(record, dict):
            return None
        return UserStats(
            uploaded=_as_bytes(record.get("uploaded")),
            downloaded=_as_bytes(record.get("downloaded")),
            ratio=_as_float(record.get("ratio")),
            bonus=_as_float(_first(record, "seedbonus", "bonus")),
            # Spelling varies across UNIT3D versions.
            hit_and_runs=_as_int(_first(record, "hit_and_runs", "hitandruns")),
            seeding=_as_int(record.get("seeding")),
            raw=record,
        )


def _first(record: dict, *keys: str) -> Any:
    """First key that is actually present, spelling variants in preference order.

    Not `a or b`: zero hit-and-runs is the good case, and treating it as missing
    would report a clean account as "unknown".
    """
    for key in keys:
        if record.get(key) is not None:
            return record[key]
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


_UNITS = {
    "b": 1,
    "kb": 1000,
    "kib": 1024,
    "mb": 1000**2,
    "mib": 1024**2,
    "gb": 1000**3,
    "gib": 1024**3,
    "tb": 1000**4,
    "tib": 1024**4,
    "pb": 1000**5,
    "pib": 1024**5,
}


def _as_bytes(value: Any) -> int | None:
    """Parse a size that may be raw bytes or a formatted string like '772.27 GiB'."""
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None

    text = value.replace(",", "").strip().lower()
    if not text:
        return None

    number, _, unit = text.partition(" ")
    if not unit:
        # No unit at all means it really was a byte count.
        return _as_int(number)

    scale = _UNITS.get(unit.strip())
    amount = _as_float(number)
    if scale is None or amount is None:
        return None
    return int(amount * scale)
