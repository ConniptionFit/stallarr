"""The individual repair steps.

Each function takes the shared context and returns an ActionResult. They are
intentionally small and independent — the escalation logic lives in engine.py.
"""

from __future__ import annotations

import asyncio
import logging

from ..clients.base import ClientError, TorrentClient
from ..config import RecoveryConfig
from ..models import ActionKind, ActionResult, Torrent
from ..trackers.base import Capability, TrackerError, TrackerPlugin
from ..utils import infohash_from_torrent, redact_secrets

log = logging.getLogger(__name__)


async def resume(client: TorrentClient, torrent: Torrent, **_) -> ActionResult:
    await client.resume(torrent.infohash)
    return ActionResult(ActionKind.RESUME, True, "resumed torrent")


async def reannounce(client: TorrentClient, torrent: Torrent, **_) -> ActionResult:
    await client.reannounce(torrent.infohash)
    return ActionResult(ActionKind.REANNOUNCE, True, "forced a tracker reannounce")


async def refresh_announce(
    client: TorrentClient, torrent: Torrent, plugin: TrackerPlugin | None = None, **_
) -> ActionResult:
    """Repair the announce URL itself — the fix for passkey and domain changes."""
    if plugin is None or not plugin.can(Capability.ANNOUNCE_URL):
        return ActionResult(ActionKind.REFRESH_ANNOUNCE, False, "tracker cannot supply announce URLs")

    current_entry = next(
        (e for e in torrent.real_trackers if plugin.matches_host(e.host)),
        torrent.primary_tracker,
    )
    current_url = current_entry.url if current_entry else None

    try:
        fresh = await plugin.announce_url(torrent.infohash, current_url)
    except TrackerError as exc:
        return ActionResult(ActionKind.REFRESH_ANNOUNCE, False, str(exc))

    if not fresh:
        return ActionResult(ActionKind.REFRESH_ANNOUNCE, False, "announce URL already correct")

    if current_url:
        await client.replace_tracker(torrent.infohash, current_url, fresh)
        message = f"rewrote announce URL to {_redact(fresh)}"
    else:
        await client.add_tracker(torrent.infohash, fresh)
        message = f"added announce URL {_redact(fresh)}"

    await client.reannounce(torrent.infohash)
    return ActionResult(ActionKind.REFRESH_ANNOUNCE, True, message)


async def recheck(client: TorrentClient, torrent: Torrent, **_) -> ActionResult:
    await client.recheck(torrent.infohash)
    return ActionResult(ActionKind.RECHECK, True, "queued a force recheck")


async def lift_share_limits(client: TorrentClient, torrent: Torrent, **_) -> ActionResult:
    """Clear a per-torrent share limit that is stopping the torrent.

    Only reached from the hit-and-run ladder, and deliberately so: this changes
    a setting the user chose. It is justified there because the alternative is
    worse — a torrent that has hit its seeding-time or ratio cap is stopped by
    the client and *re-stopped within seconds* of any resume, so every other
    repair step reports success while the tracker counts down to a penalty.

    The limits go back to "use the global setting" rather than "unlimited", so a
    global cap the user sets later still applies. Whatever is not blocking is
    left exactly as it was.
    """
    blocking = blocking_limits(torrent)
    if not blocking:
        return ActionResult(
            ActionKind.LIFT_SHARE_LIMITS, False, "no share limit is stopping this torrent"
        )

    await client.set_share_limits(
        torrent.infohash,
        ratio=None if "ratio" in blocking else torrent.ratio_limit,
        seeding_minutes=None if "seeding time" in blocking else torrent.seeding_time_limit,
    )
    await client.resume(torrent.infohash)
    return ActionResult(
        ActionKind.LIFT_SHARE_LIMITS,
        True,
        f"cleared {' and '.join(blocking)} limit and resumed",
    )


def blocking_limits(torrent: Torrent) -> list[str]:
    """Which per-torrent share limits this torrent has already met."""
    blocking = []
    if torrent.ratio_limit > 0 and torrent.ratio >= torrent.ratio_limit:
        blocking.append("ratio")
    if torrent.seeding_time_limit > 0 and torrent.seeding_time / 60 >= torrent.seeding_time_limit:
        blocking.append("seeding time")
    return blocking


async def toggle(
    client: TorrentClient, torrent: Torrent, recovery_cfg: RecoveryConfig | None = None, **_
) -> ActionResult:
    """Stop/start cycle — clears wedged libtorrent session state."""
    delay = recovery_cfg.toggle_pause_seconds if recovery_cfg else 10
    await client.pause(torrent.infohash)
    await asyncio.sleep(delay)
    await client.resume(torrent.infohash)
    await client.reannounce(torrent.infohash)
    return ActionResult(ActionKind.TOGGLE, True, f"paused for {delay}s and resumed")


async def replace_torrent(
    client: TorrentClient, torrent: Torrent, plugin: TrackerPlugin | None = None, **_
) -> ActionResult:
    """Re-add from a freshly downloaded .torrent, keeping the data on disk.

    This is the last automatic resort: it fixes corrupt resume data and stale
    metadata. It never deletes files, and it refuses to run unless the fresh
    .torrent has the same infohash — otherwise we would silently swap the
    torrent for a different release.
    """
    if plugin is None or not plugin.can(Capability.TORRENT_FILE):
        return ActionResult(ActionKind.REPLACE_TORRENT, False, "tracker cannot supply .torrent files")

    try:
        data = await plugin.fetch_torrent_file(torrent.infohash)
    except TrackerError as exc:
        return ActionResult(ActionKind.REPLACE_TORRENT, False, str(exc))
    if not data:
        return ActionResult(ActionKind.REPLACE_TORRENT, False, "tracker returned no .torrent")

    fresh_hash = infohash_from_torrent(data)
    if fresh_hash != torrent.infohash:
        return ActionResult(
            ActionKind.REPLACE_TORRENT,
            False,
            f"refusing to re-add: downloaded infohash {fresh_hash} != {torrent.infohash}",
        )

    await client.delete(torrent.infohash, delete_files=False)
    try:
        await client.add_torrent_file(
            data,
            category=torrent.category,
            tags=torrent.tags,
            save_path=torrent.save_path,
            skip_checking=False,
            paused=False,
        )
    except ClientError as exc:
        # The torrent is now out of the client but the data is untouched, so the
        # operator can re-add it by hand. Make that unmistakable in the log.
        return ActionResult(
            ActionKind.REPLACE_TORRENT,
            False,
            f"REMOVED FROM CLIENT BUT RE-ADD FAILED ({exc}) — data is intact at {torrent.save_path}",
        )
    return ActionResult(ActionKind.REPLACE_TORRENT, True, "re-added from a fresh .torrent, data kept")


async def escalate(
    client: TorrentClient, torrent: Torrent, recovery_cfg: RecoveryConfig | None = None, **_
) -> ActionResult:
    """Give up automatically and hand the torrent to a human."""
    if recovery_cfg and recovery_cfg.tagging_enabled:
        try:
            await client.add_tags(torrent.infohash, [recovery_cfg.escalation_tag])
            await client.remove_tags(torrent.infohash, [recovery_cfg.working_tag])
        except ClientError as exc:
            log.warning("could not tag %s: %s", torrent.name, exc)
    return ActionResult(ActionKind.ESCALATE, True, "exhausted automatic recovery — needs manual attention")


HANDLERS = {
    ActionKind.RESUME: resume,
    ActionKind.REANNOUNCE: reannounce,
    ActionKind.REFRESH_ANNOUNCE: refresh_announce,
    ActionKind.RECHECK: recheck,
    ActionKind.LIFT_SHARE_LIMITS: lift_share_limits,
    ActionKind.TOGGLE: toggle,
    ActionKind.REPLACE_TORRENT: replace_torrent,
    ActionKind.ESCALATE: escalate,
}

#: Steps that need a tracker plugin with a given capability.
REQUIRED_CAPABILITY = {
    ActionKind.REFRESH_ANNOUNCE: Capability.ANNOUNCE_URL,
    ActionKind.REPLACE_TORRENT: Capability.TORRENT_FILE,
}


#: Announce URLs contain passkeys; never write one to a log or webhook. The
#: engine redacts again before persisting, so this is belt and braces — it also
#: keeps the message readable at the point it is composed.
_redact = redact_secrets
