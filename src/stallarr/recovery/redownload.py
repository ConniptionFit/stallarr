"""Re-acquire a torrent the tracker still expects us to seed.

Every step in `actions.py` repairs a torrent the client holds. This one exists
for the case none of them can touch: the tracker names a torrent in its seeding
requirements and there is no local torrent at all — removed from the client,
lost in a rebuild, never re-added after a move. Nothing can be resumed,
reannounced or rechecked, so without this the obligation simply runs out the
clock and becomes a hit-and-run.

It is off by default (`recovery.redownload_missing`) because it is the only
thing stallarr does that puts data on your disk rather than fixing data already
there. Two properties make it safe enough to switch on:

  * **The add is checked, not skipped.** If the files are still on disk under
    the category's path, qBittorrent hashes them and seeds immediately — the
    debt clears at zero cost. Only genuinely missing data is downloaded again.
  * **The add is idempotent.** qBittorrent keys torrents by infohash, so a
    requirement wrongly classed as unmatched (name matching is fuzzy; the
    tracker's name and the client's need not agree) re-adds nothing.

The honest cost when the data *is* gone: re-downloading counts against the
account's download total a second time. That is a worse ratio, against a
hit-and-run and possibly a lost account — which is why the trade is worth
making, and why it is a switch rather than a default.
"""

from __future__ import annotations

import logging
import time

from .. import db
from ..clients.base import ClientError, TorrentClient
from ..config import RecoveryConfig
from ..models import ActionKind, ActionResult, Ailment, SeedingRequirement
from ..trackers.base import Capability, TrackerError, TrackerPlugin

log = logging.getLogger(__name__)

#: Attempts are bookkept under the ailment the requirement raises, so a
#: re-download and the repairs that follow it read as one story in the history.
_AILMENT = str(Ailment.HIT_AND_RUN_RISK)
_STEP = str(ActionKind.REDOWNLOAD)


def attempt_key(tracker_id: str, torrent_id: str) -> str:
    """Bookkeeping key for a torrent that has no infohash.

    The attempts table is keyed by infohash everywhere else, and an unmatched
    requirement has none — that absence is the definition of unmatched. The
    tracker's own id is the only stable identifier available, so it stands in,
    namespaced to keep it out of the infohash space it shares a column with.
    """
    return f"redownload:{tracker_id}:{torrent_id}"


async def redownload(
    client: TorrentClient,
    plugin: TrackerPlugin,
    requirement: SeedingRequirement,
    cfg: RecoveryConfig,
) -> ActionResult | None:
    """Fetch and re-add one missing torrent. None when nothing was due.

    Attempts are capped and cooled down like a ladder step. That matters more
    here than elsewhere: without it, a torrent the site lists but will not serve
    would be fetched on every single scan, forever.
    """
    if not plugin.can(Capability.TORRENT_FILE_BY_ID):
        return None
    if not requirement.torrent_id:
        return None

    key = attempt_key(plugin.id, requirement.torrent_id)
    record = (await db.get_attempts(key, _AILMENT)).get(_STEP, {"count": 0, "last_at": 0})
    if record["count"] >= cfg.max_attempts_per_step:
        return None
    cooldown = cfg.cooldown_minutes.get(_STEP, 720) * 60
    if record["count"] and time.time() - record["last_at"] < cooldown:
        return None

    if cfg.dry_run:
        return ActionResult(
            ActionKind.REDOWNLOAD, True, f"would re-add {requirement.name}", dry_run=True
        )

    # Recorded before the fetch, not after, so a tracker that errors every time
    # still exhausts its attempts instead of being retried forever.
    await db.record_attempt(key, _AILMENT, _STEP)

    try:
        data = await plugin.fetch_torrent_file_by_id(requirement.torrent_id)
    except TrackerError as exc:
        return ActionResult(ActionKind.REDOWNLOAD, False, str(exc))
    if not data:
        return ActionResult(ActionKind.REDOWNLOAD, False, "tracker returned no .torrent")

    tags = [cfg.redownload_tag] if cfg.tagging_enabled and cfg.redownload_tag else []
    try:
        await client.add_torrent_file(
            data,
            category=cfg.redownload_category,
            tags=tags,
            save_path=cfg.redownload_save_path,
            # Never skip the check. The whole hope of this step is that the
            # files survived and only the torrent entry was lost, in which case
            # hashing them turns a download into an instant seed.
            skip_checking=False,
            paused=False,
            use_auto_tmm=cfg.redownload_use_auto_tmm,
        )
    except ClientError as exc:
        return ActionResult(ActionKind.REDOWNLOAD, False, f"client refused the torrent: {exc}")

    where = cfg.redownload_save_path or cfg.redownload_category or "the client default path"
    return ActionResult(
        ActionKind.REDOWNLOAD,
        True,
        f"re-added from {plugin.name} into {where}; checking for existing data",
    )
