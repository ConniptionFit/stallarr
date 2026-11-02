"""Add one ratio-builder candidate to the client, protected from auto-purge.

This puts new data on disk, so every check sits in front of the download, not
after it, and each one fails closed:

  * **A disk budget** counted over the discovery category. Zero (the default)
    refuses everything: there is no safe guess at how much of someone's disk
    to fill.
  * **A free-space floor.** Unknown free space refuses too.
  * **A per-tracker daily cap**, so a scoring bug or an eager clicker costs a
    handful of downloads, not a catalogue.
  * **`recovery.dry_run`**, which reports and touches nothing.

Protection from purging is three layers, because there are three kinds of
purger: per-torrent "never" share limits (qBittorrent's own share-limit
action ignores tags and categories), a dedicated category (*arr apps only
remove from their own), and tags (for qbit_manage-style tools to exclude).
"""

from __future__ import annotations

import asyncio
import logging
import time

from .. import db
from ..clients.base import ClientError, TorrentClient
from ..config import Config
from ..models import ActionKind, ActionResult
from ..trackers.base import Capability, TrackerError, TrackerPlugin
from ..utils import human_bytes, infohash_from_torrent, redact_secrets
from .score import GB, Scored

log = logging.getLogger(__name__)

#: qBittorrent's "never" for every share limit.
NO_LIMIT = -1

#: How long to wait for a fresh add to appear before setting its limits.
_APPEAR_TRIES = 5
_APPEAR_DELAY = 1.0


class GrabRefused(Exception):
    """A pre-download check said no. The message is shown to the user."""


async def preflight(client: TorrentClient, scored: Scored, cfg: Config) -> None:
    """Raise GrabRefused unless every budget allows this grab."""
    disc = cfg.discovery
    size = scored.listing.size or 0
    if size <= 0:
        raise GrabRefused("size unknown, so the disk budget cannot be checked")
    if disc.max_disk_gb <= 0:
        raise GrabRefused("set a disk budget (Settings, Ratio builder) before grabbing")
    if disc.freeleech_only and (scored.listing.free_pct or 0) < 100:
        raise GrabRefused("not freeleech, and freeleech-only is on")

    since = time.time() - 86400
    done_today = await db.grabs_since(scored.listing.tracker, since)
    if done_today >= disc.max_grabs_per_day:
        raise GrabRefused(
            f"daily cap reached for {scored.listing.tracker} ({done_today}/{disc.max_grabs_per_day})"
        )

    torrents = await client.list_torrents()
    used = sum(t.size for t in torrents if disc.category and t.category == disc.category)
    budget = int(disc.max_disk_gb * GB)
    if used + size > budget:
        raise GrabRefused(
            f"over the disk budget: {human_bytes(used)} used + {human_bytes(size)} "
            f"> {human_bytes(budget)}"
        )

    free = await client.free_space()
    if free is None:
        raise GrabRefused("the client did not report free space")
    floor = int(disc.min_free_gb * GB)
    if free - size < floor:
        raise GrabRefused(
            f"would leave {human_bytes(max(free - size, 0))} free, under the {human_bytes(floor)} floor"
        )


async def _fetch(plugin: TrackerPlugin, scored: Scored) -> bytes | None:
    listing = scored.listing
    download = getattr(plugin, "download", None)
    if listing.download_url and download is not None:
        return await download(listing.download_url)
    if plugin.can(Capability.TORRENT_FILE_BY_ID):
        return await plugin.fetch_torrent_file_by_id(listing.torrent_id)
    return None


async def _lift_limits(client: TorrentClient, infohash: str) -> str:
    """Set every share limit to "never". Returns a note for the history row."""
    for _ in range(_APPEAR_TRIES):
        try:
            if await client.get_torrent(infohash) is not None:
                await client.set_share_limits(
                    infohash, ratio=NO_LIMIT, seeding_minutes=NO_LIMIT, inactive_minutes=NO_LIMIT
                )
                return "share limits set to unlimited"
        except ClientError as exc:
            return f"share limits NOT set ({exc}); set them by hand"
        await asyncio.sleep(_APPEAR_DELAY)
    return "share limits NOT set (torrent did not appear in time); set them by hand"


async def grab(
    client: TorrentClient, plugin: TrackerPlugin, scored: Scored, cfg: Config
) -> ActionResult:
    disc = cfg.discovery
    listing = scored.listing

    try:
        await preflight(client, scored, cfg)
    except GrabRefused as exc:
        return ActionResult(ActionKind.GRAB, False, f"refused: {exc}")
    except ClientError as exc:
        return ActionResult(ActionKind.GRAB, False, f"client unavailable: {exc}")

    if cfg.recovery.dry_run:
        result = ActionResult(
            ActionKind.GRAB,
            True,
            f"would grab ({human_bytes(listing.size or 0)}, score {scored.score})",
            dry_run=True,
        )
        await _log(listing, "", result)
        return result

    try:
        data = await _fetch(plugin, scored)
    except TrackerError as exc:
        result = ActionResult(ActionKind.GRAB, False, str(exc))
        await _log(listing, "", result)
        return result
    if not data:
        result = ActionResult(ActionKind.GRAB, False, f"{plugin.name} returned no .torrent")
        await _log(listing, "", result)
        return result

    infohash = infohash_from_torrent(data)
    if not infohash:
        result = ActionResult(ActionKind.GRAB, False, "download was not a valid .torrent")
        await _log(listing, "", result)
        return result
    if listing.infohash and listing.infohash != infohash:
        result = ActionResult(ActionKind.GRAB, False, "the .torrent does not match the listing")
        await _log(listing, infohash, result)
        return result

    try:
        if await client.get_torrent(infohash) is not None:
            result = ActionResult(ActionKind.GRAB, False, "already in the client")
            await _log(listing, infohash, result)
            return result
        await client.add_torrent_file(
            data,
            category=disc.category,
            tags=list(disc.tags),
            save_path=disc.save_path,
            # Checked, never skipped: if the data is already on disk under the
            # category path, hashing it turns the grab into an instant seed.
            skip_checking=False,
            paused=False,
            use_auto_tmm=disc.use_auto_tmm,
        )
    except ClientError as exc:
        result = ActionResult(ActionKind.GRAB, False, f"client refused the torrent: {exc}")
        await _log(listing, infohash, result)
        return result

    await db.record_grab(
        tracker=listing.tracker,
        torrent_id=listing.torrent_id,
        infohash=infohash,
        name=listing.name,
        size=listing.size or 0,
        score=scored.score,
        strategy=scored.strategy,
        free_pct=listing.free_pct or 0,
    )

    notes = [f"added to {disc.category or 'no category'} ({human_bytes(listing.size or 0)})"]
    if disc.unlimited_share_limits:
        notes.append(await _lift_limits(client, infohash))
    result = ActionResult(ActionKind.GRAB, True, "; ".join(notes))
    await _log(listing, infohash, result)
    return result


async def _log(listing, infohash: str, result: ActionResult) -> None:
    """History row for a grab. Redacted here because download links carry keys."""
    message = redact_secrets(result.message)
    log.info("ratio builder: %s on %s: %s", listing.name, listing.tracker, message)
    await db.add_history(
        infohash=infohash or f"discovery:{listing.tracker}:{listing.torrent_id}",
        name=listing.name,
        tracker=listing.tracker,
        ailment="",
        action=str(ActionKind.GRAB),
        ok=result.ok,
        dry_run=result.dry_run,
        message=message,
    )
