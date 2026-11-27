"""Sonarr / Radarr / Lidarr / Readarr: hand a dead download back for a re-grab.

stallarr cannot pick a replacement release itself. Only the *arr that grabbed
a torrent knows which episode or movie it was for, which qualities are
acceptable and which other releases exist, so the right move for a download
that will never finish is to tell that app: "this one failed, blocklist it and
search again". That is one call — the same thing the *arr UI's "Remove from
queue → Blocklist release" button does.

Two choices here are deliberate:

  * **`removeFromClient=false`, always.** The *arr would otherwise delete the
    torrent (and its data) itself, bypassing every guard stallarr puts in front
    of a removal: the seeding-obligation check, the per-tracker switch, the
    .torrent export, dry run. stallarr does the client side on its own terms
    in `recovery/stalled.py`, or leaves the torrent alone when it owes seeding.
  * **The bulk endpoint, with every queue row for the torrent.** A season pack
    is one torrent but one queue row *per episode*, all sharing a
    `downloadId`. Sending all of them in one request means the pack is
    blocklisted once, as one release, rather than racing itself.

Field names and parameters were checked against the published OpenAPI specs
for Sonarr v4 and Radarr v5 (`/api/v3/queue`, `/api/v3/queue/bulk`). Lidarr
and Readarr expose the same queue API under `/api/v1`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import ArrConfig
from ..utils import redact_secrets

log = logging.getLogger(__name__)

#: kind -> (API prefix, the "include items we can't map to a library entry"
#: flag). Without that flag a torrent grabbed for something since removed from
#: the library is invisible, and would be reported as "not managed by any app".
_KINDS: dict[str, tuple[str, str]] = {
    "sonarr": ("/api/v3", "includeUnknownSeriesItems"),
    "radarr": ("/api/v3", "includeUnknownMovieItems"),
    "lidarr": ("/api/v1", "includeUnknownArtistItems"),
    "readarr": ("/api/v1", "includeUnknownAuthorItems"),
}

#: Large enough to read a whole queue in one request on any real install. The
#: queue is what is downloading right now, not the library.
_PAGE_SIZE = 1000


class ArrError(RuntimeError):
    pass


def kind_of(instance_id: str, cfg: ArrConfig) -> str:
    """The app type, from config or else from the instance's name.

    Lets `arr.sonarr` and `arr.sonarr4k` work with no `kind` field, which is
    how most people will name them.
    """
    if cfg.kind:
        return cfg.kind.strip().lower()
    for kind in _KINDS:
        if instance_id.lower().startswith(kind):
            return kind
    return ""


def usable(instance_id: str, cfg: ArrConfig) -> bool:
    return bool(cfg.enabled and cfg.url and cfg.api_key and kind_of(instance_id, cfg) in _KINDS)


@dataclass
class QueueMatch:
    """Every queue row one *arr instance holds for one torrent."""

    instance: str
    kind: str
    ids: list[int] = field(default_factory=list)
    title: str = ""


class ArrClient:
    def __init__(self, instance_id: str, cfg: ArrConfig, *, transport: Any = None) -> None:
        self.id = instance_id
        self.kind = kind_of(instance_id, cfg)
        if self.kind not in _KINDS:
            raise ArrError(f"{instance_id}: unknown app type {cfg.kind!r}")
        prefix, self._unknown_flag = _KINDS[self.kind]
        self._client = httpx.AsyncClient(
            base_url=cfg.url.rstrip("/") + prefix,
            headers={"X-Api-Key": cfg.api_key},
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ArrError(redact_secrets(f"{self.id}: {path} failed: {exc}")) from exc
        if resp.status_code in (401, 403):
            raise ArrError(f"{self.id}: API key rejected")
        if resp.status_code >= 400:
            raise ArrError(f"{self.id}: {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ArrError(f"{self.id}: {path} did not return JSON") from exc

    async def queue(self) -> dict[str, QueueMatch]:
        """Torrent infohash (lowercase) -> the queue rows for it."""
        body = await self._call(
            "GET",
            "/queue",
            params={"page": 1, "pageSize": _PAGE_SIZE, self._unknown_flag: "true"},
        )
        records = body.get("records", []) if isinstance(body, dict) else []
        out: dict[str, QueueMatch] = {}
        for record in records:
            # Usenet rows carry a SABnzbd/NZBGet id here, never a 40-hex hash,
            # so they cannot collide with a torrent.
            if str(record.get("protocol", "torrent")).lower() != "torrent":
                continue
            download_id = str(record.get("downloadId") or "").lower()
            queue_id = record.get("id")
            if not download_id or not isinstance(queue_id, int):
                continue
            match = out.setdefault(
                download_id,
                QueueMatch(instance=self.id, kind=self.kind, title=str(record.get("title") or "")),
            )
            match.ids.append(queue_id)
        return out

    async def blocklist_and_search(self, match: QueueMatch) -> None:
        """Mark the release failed and have the app look for another.

        Leaves the torrent in the client (`removeFromClient=false`); see the
        module docstring for why that is never delegated.
        """
        await self._call(
            "DELETE",
            "/queue/bulk",
            params={
                "removeFromClient": "false",
                "blocklist": "true",
                "skipRedownload": "false",
            },
            json={"ids": match.ids},
        )

    async def status(self) -> str:
        """App name and version, for a connection test."""
        body = await self._call("GET", "/system/status")
        if not isinstance(body, dict):
            return self.kind
        return f"{body.get('appName') or self.kind} {body.get('version') or ''}".strip()


class ArrRouter:
    """Every configured *arr, queried together, once per scan that needs it."""

    def __init__(self, configs: dict[str, ArrConfig], *, transport: Any = None) -> None:
        self.clients: dict[str, ArrClient] = {}
        for instance_id, cfg in configs.items():
            if not usable(instance_id, cfg):
                continue
            try:
                self.clients[instance_id] = ArrClient(instance_id, cfg, transport=transport)
            except ArrError as exc:
                log.warning("%s", exc)

    def __bool__(self) -> bool:
        return bool(self.clients)

    async def close(self) -> None:
        for client in self.clients.values():
            await client.close()

    async def queues(self) -> tuple[dict[str, QueueMatch], list[str]]:
        """Merged infohash -> match, plus the instances that failed to answer.

        The failures matter to the caller. A torrent absent from every queue
        that *answered* is "not managed"; one that might be in a queue that did
        not answer is unknown, and purging it would leave that app waiting on a
        download that no longer exists.
        """
        merged: dict[str, QueueMatch] = {}
        failed: list[str] = []
        for instance_id, client in self.clients.items():
            try:
                rows = await client.queue()
            except ArrError as exc:
                log.warning("could not read the %s queue: %s", instance_id, exc)
                failed.append(instance_id)
                continue
            for infohash, match in rows.items():
                merged.setdefault(infohash, match)
        return merged, failed

    async def blocklist_and_search(self, match: QueueMatch) -> None:
        client = self.clients.get(match.instance)
        if client is None:
            raise ArrError(f"{match.instance} is no longer configured")
        await client.blocklist_and_search(match)
