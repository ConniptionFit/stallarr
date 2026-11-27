"""Downloads that will never finish: re-grab a different release, and remove
the dead one only when that cannot cost a hit-and-run.

The repair ladder can revive a download whose announce went missing or whose
client got confused. It cannot revive one nobody is seeding: no reannounce
conjures a seeder. Left alone, such a download sits in the client forever while
Sonarr or Radarr waits on it ("The download is stalled with no connections"),
and the episode never arrives.

Two separate things help, and they are separate switches:

  * **Re-grab** (`recovery.stalled_regrab`). Only the *arr that grabbed the
    torrent knows what it was for and what else could satisfy it, so stallarr
    asks that app to blocklist the release and search again
    (`integrations/arr.py`). It never lets the app remove the torrent itself.
  * **Purge** (`recovery.stalled_purge`). Remove the dead torrent from the
    client, with its partial data. This is where the private-tracker rules
    bite, so it is the part built around refusing.

What has to hold before *anything* happens to a download, re-grab included,
because each is a different way a healthy release can look dead:

  * **No data moved for `stalled_dead_hours`**, read from the client's own
    last-activity stamp (or the add time, for one that never moved a byte).
  * **The client has a connection.** A dropped VPN stalls every download at
    once with exactly the message in Sonarr's queue.
  * **A tracker is answering, and reports no seeders.** A tracker that is down
    cannot say whether the swarm is empty, and one that reports seeders we
    cannot reach points at a port or connectivity problem on this side, which a
    different release would hit too.

And what has to hold before a purge, on top:

  * **Nothing is owed.** The tracker has not listed it in its seeding
    requirements or flagged it as a hit-and-run, and the amount downloaded is
    within what that site lets you take before a hit-and-run can count — 0 by
    default, raised per tracker to the site's published buffer. Removing a
    torrent with data downloaded beyond that is exactly how a hit-and-run is
    made, so such a torrent is kept and reported instead, still seeding what it
    has if the swarm ever returns.
  * **Not vetoed** by the tracker's `purge_stalled` switch or a
    `stallarr-keep` tag.
  * **The *arr is done with it.** When re-grab is on and an app tracks the
    download, the purge waits until that app has blocklisted it. If any app did
    not answer this scan, the purge waits too: it may be the one tracking it.

Then the same rails as every other removal: dry run, a per-scan cap, and the
.torrent kept in the graveyard first, with a failed export cancelling the
removal.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .. import db
from ..clients.base import ClientError, TorrentClient
from ..config import RecoveryConfig, TrackerConfig
from ..diagnosis.rules import idle_for
from ..integrations.arr import ArrError, ArrRouter, QueueMatch
from ..models import ActionKind, ActionResult, Ailment, Diagnosis
from ..utils import human_bytes
from .reap import export_torrent_file, tracker_of

log = logging.getLogger(__name__)

#: The tag the ratio builder puts on its grabs, and the one people use to say
#: "never remove this". Deliberately not `stallarr:`-prefixed; see CLAUDE.md.
KEEP_TAG = "stallarr-keep"

_AILMENT = str(Ailment.STALLED_DOWNLOAD)
#: Attempt-table steps. "regrab" counts tries, so a failing *arr is capped and
#: cooled down like a ladder step; "regrabbed" is written only on success and is
#: what lets the purge go ahead.
_TRY = "regrab"
_DONE = "regrabbed"


@dataclass
class StalledOutcome:
    """What one pass did, and what it is still holding back and why."""

    results: list[tuple[Diagnosis, ActionResult]] = field(default_factory=list)
    #: Infohashes actually removed from the client.
    purged: set[str] = field(default_factory=set)
    #: One row per dead download, for the dashboard. A held or kept download
    #: must be visible: "nothing happened" and "refused, here is why" look the
    #: same otherwise.
    report: list[dict[str, Any]] = field(default_factory=list)


def dead_for(diagnosis: Diagnosis, cfg: RecoveryConfig, now: float) -> float | None:
    """Seconds this download has been dead, or None if it is not dead.

    Unregistered downloads are left to `reap.py`: the site deleting a torrent is
    a different verdict with its own guards.
    """
    torrent = diagnosis.torrent
    if torrent.is_complete or torrent.is_paused:
        return None
    ailments = diagnosis.ailments
    if Ailment.STALLED_DOWNLOAD not in ailments or Ailment.UNREGISTERED in ailments:
        return None
    idle = idle_for(torrent, now)
    if idle < max(cfg.stalled_dead_hours, 1) * 3600:
        return None
    return idle


def hold_reason(diagnosis: Diagnosis) -> str | None:
    """Why the swarm evidence does not support "this release is dead"."""
    torrent = diagnosis.torrent
    real = torrent.real_trackers
    if not real:
        return "no tracker to confirm the swarm is empty"
    working = [entry for entry in real if entry.working]
    if not working:
        return "no tracker is answering, so an empty swarm cannot be told from a tracker fault"
    seeds = max(entry.num_seeds for entry in working)
    if seeds > 0:
        return (
            f"the tracker reports {seeds} seeder(s) we cannot reach, which points at a "
            "connection problem here rather than a dead release"
        )
    if torrent.num_seeds > 0 or torrent.availability >= 1:
        return "a full copy is reachable; it may yet finish"
    return None


def keep_reason(
    diagnosis: Diagnosis,
    tracker_cfg: TrackerConfig | None,
    cfg: RecoveryConfig,
) -> str | None:
    """Why removing this download could cost a hit-and-run, or None if it cannot."""
    torrent = diagnosis.torrent
    if Ailment.HIT_AND_RUN_RISK in diagnosis.ailments:
        return "the tracker counts it towards a hit-and-run"
    if KEEP_TAG in torrent.tags:
        return f"tagged {KEEP_TAG}"
    if tracker_cfg is not None and not tracker_cfg.purge_stalled:
        return "removal is switched off for this tracker"

    allowance = cfg.stalled_purge_max_downloaded_pct
    if tracker_cfg is not None and tracker_cfg.hnr_free_download_pct is not None:
        allowance = tracker_cfg.hnr_free_download_pct
    allowance = max(allowance, 0.0)

    downloaded = max(torrent.downloaded, 0)
    if downloaded == 0:
        return None
    if torrent.size <= 0:
        # Metadata never arrived, yet bytes did: there is nothing to measure the
        # download against, and a guess is not good enough here.
        return f"{human_bytes(downloaded)} downloaded of an unknown size"
    share = 100 * downloaded / torrent.size
    if share > allowance:
        return (
            f"{human_bytes(downloaded)} downloaded ({share:.1f}%), over the "
            f"{allowance:g}% this tracker allows before a hit-and-run can count"
        )
    return None


async def handle_dead(
    client: TorrentClient,
    arrs: ArrRouter | None,
    diagnoses: list[Diagnosis],
    cfg: RecoveryConfig,
    trackers: dict[str, TrackerConfig],
    now: float | None = None,
) -> StalledOutcome:
    outcome = StalledOutcome()
    now = now or time.time()

    dead = [(d, idle) for d in diagnoses if (idle := dead_for(d, cfg, now)) is not None]
    if not dead:
        return outcome
    # Longest-dead first: those have had the most scans to come back.
    dead.sort(key=lambda pair: pair[1], reverse=True)

    def row(diagnosis: Diagnosis, idle: float, status: str, managed: str = "") -> None:
        torrent = diagnosis.torrent
        outcome.report.append(
            {
                "infohash": torrent.infohash,
                "name": torrent.name,
                "tracker": tracker_of(diagnosis),
                "progress": round(torrent.progress, 4),
                "downloaded": torrent.downloaded,
                "size": torrent.size,
                "idle_hours": round(idle / 3600, 1),
                "managed_by": managed,
                "status": status,
            }
        )

    active = cfg.enabled and (cfg.stalled_regrab or cfg.stalled_purge)
    if not active:
        for diagnosis, idle in dead:
            row(diagnosis, idle, "dead; re-grab and removal are switched off")
        return outcome

    try:
        connection = await client.connection_status()
    except ClientError as exc:
        log.debug("could not read connection status: %s", exc)
        connection = None
    if connection == "disconnected":
        log.warning(
            "%d download(s) look dead, but the client reports no connection; holding all of them",
            len(dead),
        )
        for diagnosis, idle in dead:
            row(diagnosis, idle, "held: the client reports no network connection")
        return outcome

    queues: dict[str, QueueMatch] = {}
    unanswered: list[str] = []
    if arrs:
        queues, unanswered = await arrs.queues()

    budget = max(cfg.stalled_max_per_scan, 0)
    for diagnosis, idle in dead:
        torrent = diagnosis.torrent
        match = queues.get(torrent.infohash)
        managed = match.instance if match else ""

        held = hold_reason(diagnosis)
        if held:
            row(diagnosis, idle, f"held: {held}", managed)
            continue
        if budget <= 0:
            row(diagnosis, idle, "waiting: per-scan limit reached", managed)
            continue

        acted = False
        attempts = await db.get_attempts(torrent.infohash, _AILMENT)
        regrabbed = _DONE in attempts
        regrab_note = ""

        if cfg.stalled_regrab and match and not regrabbed:
            result = await _regrab(arrs, diagnosis, match, cfg, attempts, now)
            if result is not None:
                outcome.results.append((diagnosis, result))
                acted = True
                if result.dry_run or result.ok:
                    # A dry run reports the purge that would follow it, so the
                    # preview shows the whole decision rather than half of it.
                    regrabbed = True
            if not regrabbed:
                regrab_note = f"waiting for {match.instance} to blocklist it"
        if regrabbed:
            regrab_note = "replacement requested"

        if not cfg.stalled_purge:
            row(diagnosis, idle, _join(regrab_note, "removal is switched off"), managed)
        elif keep := keep_reason(diagnosis, trackers.get(diagnosis.tracker_id or ""), cfg):
            row(diagnosis, idle, _join(regrab_note, f"kept: {keep}"), managed)
        elif match and cfg.stalled_regrab and not regrabbed:
            # Purging first would leave the app waiting on a download that no
            # longer exists, and it would never search for a replacement.
            row(diagnosis, idle, regrab_note, managed)
        elif not match and unanswered:
            row(
                diagnosis,
                idle,
                f"held: {', '.join(unanswered)} did not answer and may still be tracking it",
                managed,
            )
        else:
            result = await _purge(client, diagnosis, cfg, idle, now)
            outcome.results.append((diagnosis, result))
            acted = True
            if result.ok and not result.dry_run:
                outcome.purged.add(torrent.infohash)
                budget -= 1
                continue  # gone; nothing left to report
            status = result.message if result.dry_run else f"removal failed: {result.message}"
            row(diagnosis, idle, _join(regrab_note, status), managed)

        if acted:
            budget -= 1

    return outcome


def _join(*parts: str) -> str:
    return "; ".join(part for part in parts if part)


async def _regrab(
    arrs: ArrRouter | None,
    diagnosis: Diagnosis,
    match: QueueMatch,
    cfg: RecoveryConfig,
    attempts: dict[str, dict[str, int]],
    now: float,
) -> ActionResult | None:
    """Ask the owning app to blocklist and re-search. None when not due."""
    record = attempts.get(_TRY, {"count": 0, "last_at": 0})
    if record["count"] >= cfg.max_attempts_per_step:
        return None
    cooldown = cfg.cooldown_minutes.get(_TRY, 60) * 60
    if record["count"] and now - record["last_at"] < cooldown:
        return None

    what = f"{match.instance} to blocklist this release and search for another"
    if cfg.dry_run:
        return ActionResult(ActionKind.REGRAB, True, f"would ask {what}", dry_run=True)
    if arrs is None:
        return None

    # Before the call, as with re-downloads: an app that errors every time
    # must still run out of attempts rather than be asked forever.
    await db.record_attempt(diagnosis.torrent.infohash, _AILMENT, _TRY)
    try:
        await arrs.blocklist_and_search(match)
    except ArrError as exc:
        return ActionResult(ActionKind.REGRAB, False, str(exc))
    await db.record_attempt(diagnosis.torrent.infohash, _AILMENT, _DONE)
    rows = f" ({len(match.ids)} queue items)" if len(match.ids) > 1 else ""
    return ActionResult(ActionKind.REGRAB, True, f"asked {what}{rows}")


async def _purge(
    client: TorrentClient, diagnosis: Diagnosis, cfg: RecoveryConfig, idle: float, now: float
) -> ActionResult:
    torrent = diagnosis.torrent
    have = max(torrent.size - torrent.amount_left, 0)
    what = f"with {human_bytes(have)} of partial data" if cfg.stalled_purge_files else "torrent only"
    reason = (
        f"dead download: no data for {idle / 3600:.0f}h, no seeders, "
        f"{human_bytes(max(torrent.downloaded, 0))} downloaded"
    )

    if cfg.dry_run:
        return ActionResult(ActionKind.PURGE, True, f"would remove ({what}) — {reason}", dry_run=True)

    export_path = ""
    if cfg.delete_export_torrents:
        try:
            export_path = await export_torrent_file(
                client, diagnosis, cfg, reason=reason, files_deleted=cfg.stalled_purge_files
            )
        except (ClientError, OSError) as exc:
            log.error("not removing %s: could not keep its .torrent (%s)", torrent.name[:60], exc)
            return ActionResult(ActionKind.PURGE, False, f"kept: could not export its .torrent first ({exc})")

    try:
        await client.delete(torrent.infohash, delete_files=cfg.stalled_purge_files)
    except ClientError as exc:
        return ActionResult(ActionKind.PURGE, False, f"client refused the removal: {exc}")

    await db.record_deletion(
        infohash=torrent.infohash,
        name=torrent.name,
        tracker=tracker_of(diagnosis),
        # What was actually on disk, so "reclaimed" on the dashboard counts the
        # partial data and not the size of a download that never happened.
        size=have,
        files_deleted=cfg.stalled_purge_files,
        reason=reason,
        condemned_at=int(now - idle),
        export_path=export_path,
    )
    await db.clear_attempts(torrent.infohash)
    kept = f", .torrent kept at {export_path}" if export_path else ""
    return ActionResult(ActionKind.PURGE, True, f"removed ({what}) — {reason}{kept}")
