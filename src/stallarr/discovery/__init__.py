"""The ratio builder: find torrents worth seeding long-term, and grab them.

Runs on its own loop, never inside the health scan. The scan's tracker calls are
lookups of torrents you already hold and are budgeted per sick torrent; browsing
a catalogue is a different kind of traffic, so it gets its own, slower clock
(`discovery.interval_minutes`) and one request per tracker per run.

Listings are cached per tracker, so a refresh that fails for one site keeps the
others' candidates, and the ranking is redone over the whole cache each run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from .. import db
from ..clients import ClientError
from ..models import TrackerListing
from ..trackers.base import Capability, TrackerError
from ..utils import normalise_release
from .grab import grab as grab_one
from .score import Scored, rank

if TYPE_CHECKING:
    from ..service import StallarrService

log = logging.getLogger(__name__)

#: Floor between two browses of the same tracker, whatever triggers them. The
#: Refresh button is the reason: without it, an impatient click is a request.
MIN_BROWSE_GAP = 300


class Discovery:
    def __init__(self, service: StallarrService) -> None:
        self.service = service
        self.candidates: list[Scored] = []
        self.excluded: dict[str, int] = {}
        #: Per tracker: last_at, ok, detail, listed.
        self.status: dict[str, dict[str, Any]] = {}
        self.last_run = 0.0
        self.running = False
        self._listings: dict[str, list[TrackerListing]] = {}
        self._last_browse: dict[str, float] = {}
        self._run_lock = asyncio.Lock()
        self._grab_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False

    @property
    def cfg(self):
        return self.service.cfg.discovery

    # --- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="stallarr-discovery-loop")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def trigger(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                if self.cfg.enabled and self.cfg.trackers:
                    await self.run()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("discovery run failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=max(self.cfg.interval_minutes, 5) * 60
                )
            except TimeoutError:
                pass

    # --- browsing -----------------------------------------------------------------

    async def run(self) -> None:
        if self._run_lock.locked():
            return
        async with self._run_lock:
            self.running = True
            try:
                await self._run_once()
            finally:
                self.running = False

    async def _run_once(self) -> None:
        wanted = [t for t in self.cfg.trackers if t]
        # Forget trackers that were opted out, so their candidates disappear.
        for stale in set(self._listings) - set(wanted):
            self._listings.pop(stale, None)
            self.status.pop(stale, None)

        await asyncio.gather(*(self._browse(tracker_id) for tracker_id in wanted))
        await self._rerank()
        self.last_run = time.time()

    async def _browse(self, tracker_id: str) -> None:
        plugin = self.service.router.plugins.get(tracker_id)
        if plugin is None:
            self.status[tracker_id] = {"ok": False, "detail": "tracker not enabled", "last_at": 0}
            return
        if not plugin.can(Capability.BROWSE):
            self.status[tracker_id] = {
                "ok": False,
                "detail": f"{plugin.name} has no browse support yet",
                "last_at": 0,
            }
            return
        now = time.time()
        if now - self._last_browse.get(tracker_id, 0) < MIN_BROWSE_GAP:
            return
        self._last_browse[tracker_id] = now
        try:
            listings = await plugin.browse()
        except TrackerError as exc:
            self.status[tracker_id] = {"ok": False, "detail": str(exc), "last_at": now}
            return
        self._listings[tracker_id] = listings
        self.status[tracker_id] = {
            "ok": True,
            "detail": f"{len(listings)} listed",
            "last_at": now,
            "name": plugin.name,
        }

    async def _rerank(self) -> None:
        held_hashes: set[str] = set()
        held_names: set[str] = set()
        client = self.service.client
        if client is not None:
            try:
                for torrent in await client.list_torrents():
                    held_hashes.add(torrent.infohash.lower())
                    held_names.add(normalise_release(torrent.name))
            except ClientError as exc:
                log.debug("discovery could not list torrents: %s", exc)
        listings = [row for rows in self._listings.values() for row in rows]
        self.candidates, self.excluded = rank(
            listings,
            self.cfg,
            held_hashes=held_hashes,
            held_names=held_names,
            grabbed=await db.grabbed_keys(),
        )

    # --- grabbing -----------------------------------------------------------------

    async def grab(self, tracker_id: str, torrent_id: str) -> dict[str, Any]:
        """Grab a candidate from the current list. Ids only: nothing else the
        browser sends is trusted, the listing comes from our own cache."""
        scored = next(
            (
                c
                for c in self.candidates
                if c.listing.tracker == tracker_id and c.listing.torrent_id == torrent_id
            ),
            None,
        )
        if scored is None:
            return {"ok": False, "message": "not a current candidate; refresh and try again"}
        plugin = self.service.router.plugins.get(tracker_id)
        client = self.service.client
        if plugin is None or client is None:
            return {"ok": False, "message": "tracker or client unavailable"}

        async with self._grab_lock:
            result = await grab_one(client, plugin, scored, self.service.cfg)
        if result.ok and not result.dry_run:
            self.candidates = [c for c in self.candidates if c is not scored]
        return {"ok": result.ok, "dry_run": result.dry_run, "message": result.message}

    # --- view ---------------------------------------------------------------------

    async def view(self) -> dict[str, Any]:
        disc = self.cfg
        used = 0
        client = self.service.client
        if client is not None and disc.category:
            try:
                used = sum(
                    t.size for t in await client.list_torrents() if t.category == disc.category
                )
            except ClientError:
                used = 0
        return {
            "enabled": disc.enabled,
            "trackers": list(disc.trackers),
            "freeleech_only": disc.freeleech_only,
            "dry_run": self.service.cfg.recovery.dry_run,
            "running": self.running,
            "last_run": self.last_run,
            "interval_minutes": disc.interval_minutes,
            "status": self.status,
            "excluded": self.excluded,
            "candidates": [c.as_dict() for c in self.candidates],
            "budget": {
                "category": disc.category,
                "used": used,
                "max": int(disc.max_disk_gb * 1024**3),
                "free": (self.service.disk or {}).get("free"),
                "min_free": int(disc.min_free_gb * 1024**3),
                "max_grabs_per_day": disc.max_grabs_per_day,
            },
            "grabs": await db.list_grabs(50),
        }

    def refresh_allowed_in(self) -> int:
        """Seconds until every opted-in tracker may be browsed again (0 = now)."""
        now = time.time()
        waits = [
            MIN_BROWSE_GAP - (now - self._last_browse.get(t, 0)) for t in self.cfg.trackers if t
        ]
        return max(int(min(waits, default=0)), 0)
