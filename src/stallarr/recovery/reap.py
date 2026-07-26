"""Deleting torrents that the tracker has itself deleted.

Every other action in this package tries to keep a torrent alive. This one
removes it, optionally with its data, and is the only thing stallarr does that
cannot be undone — so the interesting part is not the delete call, it is the
four things that have to agree before it happens.

The failure this is built around: `unregistered` is inferred from an empty
search result (`unit3d.torrent_info`) or an announce message. Both are normally
authoritative, and both look identical to a tracker having a bad minute. A
search index that returns nothing for ten minutes would condemn an entire
library at once. So:

  * **A soak period.** A torrent must have been *continuously* unregistered for
    `delete_min_age_hours` (`db.condemned`, cleared the moment the verdict
    lifts). A single bad scan can never delete anything, because the verdict has
    to still be there hours later.
  * **A circuit breaker.** If more than `delete_max_tracker_fraction` of one
    tracker's torrents are unregistered in the same scan, nothing on that
    tracker is touched. Past that share the likeliest explanation is the
    tracker, not your library — and that is precisely when the soak period is
    least protective, because a long outage ages every verdict together.
  * **A per-scan cap**, so even a fault that defeats both costs a handful of
    torrents rather than all of them.
  * **Dry run**, which reports what it would delete and touches nothing.

None of these help against a tracker that is genuinely wrong for a day. That is
why the whole thing is off by default, and why deleting files is a second
switch rather than a flag on the first.
"""

from __future__ import annotations

import logging
import time
from collections import Counter

from ..clients.base import ClientError, TorrentClient
from ..config import RecoveryConfig
from ..models import ActionKind, ActionResult, Ailment, Diagnosis
from ..utils import human_bytes

log = logging.getLogger(__name__)


def tracker_of(diagnosis: Diagnosis) -> str:
    """A stable grouping key, even for a torrent with no plugin configured.

    The announce host stands in for an unrecognised tracker: those torrents can
    still be condemned by their announce message, so they need a bucket of their
    own for the circuit breaker rather than being lumped together.
    """
    if diagnosis.tracker_id:
        return diagnosis.tracker_id
    hosts = diagnosis.torrent.tracker_hosts
    return hosts[0] if hosts else "unknown"


def condemned(diagnoses: list[Diagnosis]) -> list[Diagnosis]:
    """Torrents whose *primary* finding is that the site no longer has them.

    Primary rather than "has the finding anywhere": a torrent can pick up an
    `unregistered` reading from one dead tracker in a multi-tracker torrent
    while another still works, and that is not something to delete.
    """
    return [
        d for d in diagnoses if d.primary and d.primary.ailment is Ailment.UNREGISTERED
    ]


def tripped_breakers(
    diagnoses: list[Diagnosis], totals: dict[str, int], fraction: float
) -> set[str]:
    """Trackers reporting too many deletions at once to be believed."""
    counts = Counter(tracker_of(d) for d in condemned(diagnoses))
    tripped = set()
    for tracker, count in counts.items():
        total = totals.get(tracker, 0)
        if not total:
            # No denominator means the breaker cannot judge this tracker at all.
            # For a step that erases data, "cannot judge" has to mean "do not
            # act" — the caller counts every torrent it diagnosed, so a missing
            # total is a bug, and failing open would make that bug destructive.
            tripped.add(tracker)
            log.error("refusing to delete on %s: no torrent count to judge %d deletion(s) against",
                      tracker, count)
            continue
        if count / total > fraction:
            tripped.add(tracker)
            log.error(
                "refusing to delete on %s: %d of %d torrent(s) (%.0f%%) came back unregistered, "
                "which looks like a tracker fault rather than %d real deletions",
                tracker,
                count,
                total,
                100 * count / total,
                count,
            )
    return tripped


async def reap(
    client: TorrentClient,
    diagnoses: list[Diagnosis],
    cfg: RecoveryConfig,
    totals: dict[str, int],
) -> list[tuple[Diagnosis, ActionResult]]:
    """Delete what has been confirmed gone for long enough.

    Always keeps `db.condemned` in step with what is unregistered right now,
    even when deleting is switched off — so the soak period reflects how long
    the verdict has actually stood, not how long the feature has been enabled.
    """
    from .. import db

    doomed = condemned(diagnoses)
    await db.sync_condemned({d.torrent.infohash for d in doomed})

    ages: dict[str, int] = {}
    for diagnosis in doomed:
        ages[diagnosis.torrent.infohash] = await db.condemn(
            diagnosis.torrent.infohash, diagnosis.torrent.name, tracker_of(diagnosis)
        )

    if not (cfg.enabled and cfg.delete_unregistered) or not doomed:
        return []

    blocked = tripped_breakers(diagnoses, totals, cfg.delete_max_tracker_fraction)
    soak = max(cfg.delete_min_age_hours, 0) * 3600
    now = time.time()

    ready = [
        d
        for d in doomed
        if tracker_of(d) not in blocked and now - ages[d.torrent.infohash] >= soak
    ]
    # Longest-condemned first: those have had the most scans to be contradicted.
    ready.sort(key=lambda d: ages[d.torrent.infohash])

    waiting = len(doomed) - len(ready) - sum(1 for d in doomed if tracker_of(d) in blocked)
    if waiting > 0:
        log.info("%d unregistered torrent(s) still inside the %dh soak period", waiting, cfg.delete_min_age_hours)

    results: list[tuple[Diagnosis, ActionResult]] = []
    for diagnosis in ready[: max(cfg.delete_max_per_scan, 0)]:
        results.append((diagnosis, await _delete(client, diagnosis, cfg, ages)))
    return results


async def _delete(
    client: TorrentClient, diagnosis: Diagnosis, cfg: RecoveryConfig, ages: dict[str, int]
) -> ActionResult:
    from .. import db

    torrent = diagnosis.torrent
    held = (time.time() - ages[torrent.infohash]) / 3600
    what = f"{human_bytes(torrent.size)} of data" if cfg.delete_unregistered_files else "the torrent only"

    if cfg.dry_run:
        return ActionResult(
            ActionKind.DELETE,
            True,
            f"would delete ({what}) — unregistered for {held:.0f}h",
            dry_run=True,
        )

    try:
        await client.delete(torrent.infohash, delete_files=cfg.delete_unregistered_files)
    except ClientError as exc:
        return ActionResult(ActionKind.DELETE, False, f"client refused the delete: {exc}")

    # Only once the client has confirmed, and only this torrent's row — clearing
    # the ledger wholesale would restart every other torrent's soak period.
    await db.clear_attempts(torrent.infohash)
    await db.reprieve(torrent.infohash)
    return ActionResult(
        ActionKind.DELETE,
        True,
        f"deleted ({what}) — {diagnosis.primary.detail if diagnosis.primary else 'unregistered'}, "
        f"confirmed for {held:.0f}h",
    )
