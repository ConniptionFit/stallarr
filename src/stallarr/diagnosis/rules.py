"""Turn a torrent's client-side state into a list of concrete ailments.

Diagnosis is deliberately two-phase:

  * :func:`diagnose_local` uses only data the torrent client already gave us, so
    it costs nothing and runs on every torrent every scan.
  * :func:`enrich` asks the tracker plugin follow-up questions, and only runs
    for torrents that already look suspicious. Private trackers ban you for
    hammering their API, and most torrents are fine.
"""

from __future__ import annotations

import logging
import time

from ..config import RuleConfig
from ..models import (
    TRACKER_DISABLED,
    TRACKER_NOT_CONTACTED,
    Ailment,
    Diagnosis,
    Finding,
    Torrent,
    TorrentState,
)
from ..trackers.base import TrackerError, TrackerPlugin

log = logging.getLogger(__name__)


def should_skip(torrent: Torrent, rules: RuleConfig, ignored: set[str]) -> str | None:
    """Return a reason string if this torrent is out of scope, else None."""
    if torrent.infohash in ignored:
        return "manually ignored"
    if rules.only_categories and torrent.category not in rules.only_categories:
        return "category not in only_categories"
    if torrent.category in rules.ignore_categories:
        return "category ignored"
    if any(tag in rules.ignore_tags for tag in torrent.tags):
        return "ignore tag present"
    if rules.ignore_queued and torrent.state is TorrentState.QUEUED:
        return "queued by client"
    if torrent.state in (TorrentState.CHECKING, TorrentState.MOVING):
        return f"client is busy ({torrent.raw_state})"
    return None


def diagnose_local(
    torrent: Torrent,
    rules: RuleConfig,
    plugin: TrackerPlugin | None = None,
    now: float | None = None,
) -> Diagnosis:
    now = now or time.time()
    diagnosis = Diagnosis(torrent=torrent, tracker_id=plugin.id if plugin else None)

    # Freshly added torrents get a grace period; everything looks stalled for
    # the first few minutes while metadata and peers come in.
    age = now - torrent.added_on if torrent.added_on else 1e9
    if age < rules.new_torrent_grace_minutes * 60:
        return diagnosis

    findings = diagnosis.findings
    _check_client_state(torrent, findings)
    _check_trackers(torrent, plugin, findings)
    _check_paused(torrent, rules, findings)
    _check_announcing(torrent, rules, findings)
    _check_stalls(torrent, rules, now, findings)
    return diagnosis


def _check_client_state(torrent: Torrent, findings: list[Finding]) -> None:
    if torrent.state is TorrentState.MISSING_FILES:
        findings.append(Finding(Ailment.MISSING_FILES, "client reports missing files on disk"))
    elif torrent.state is TorrentState.ERROR:
        findings.append(Finding(Ailment.CLIENT_ERROR, f"client error state ({torrent.raw_state})"))


def _check_trackers(torrent: Torrent, plugin: TrackerPlugin | None, findings: list[Finding]) -> None:
    real = torrent.real_trackers
    if not real:
        return

    for entry in real:
        verdict = plugin.classify_message(entry.message) if plugin else _default_classify(entry.message)
        if verdict == "unregistered":
            findings.append(Finding(Ailment.UNREGISTERED, f"{entry.host}: {entry.message}"))
            return
        if verdict == "auth":
            findings.append(Finding(Ailment.TRACKER_AUTH, f"{entry.host}: {entry.message}"))
            return

    # Only call the tracker down when *every* real tracker is failing; a torrent
    # with one dead backup tracker is still perfectly healthy.
    if all(not entry.working for entry in real):
        detail = next((e.message for e in real if e.message), "no working tracker")
        findings.append(Finding(Ailment.TRACKER_DOWN, f"all {len(real)} tracker(s) failing: {detail}"))


def _default_classify(message: str) -> str | None:
    from ..trackers.base import AUTH_MARKERS, UNREGISTERED_MARKERS

    low = (message or "").lower()
    if not low:
        return None
    if any(marker in low for marker in UNREGISTERED_MARKERS):
        return "unregistered"
    if any(marker in low for marker in AUTH_MARKERS):
        return "auth"
    return None


def _check_paused(torrent: Torrent, rules: RuleConfig, findings: list[Finding]) -> None:
    if not torrent.is_paused:
        return
    # A torrent the user paused on purpose is not a fault. We only flag pauses
    # we can attribute to ourselves, unless the operator opts in explicitly.
    #
    # "Attribute to ourselves" is a guess, and a weak one: the escalation tag is
    # permanent, so after a torrent is escalated *every* later pause looks
    # self-inflicted — including the client stopping it at its own share limit.
    # Naming that cause when we can see it is the difference between a report
    # someone can act on and one that sends them looking for a bug in stallarr.
    ours = any(tag.startswith("stallarr:") for tag in torrent.tags)
    if not (ours or rules.flag_user_paused):
        return

    reason = _share_limit_reason(torrent)
    detail = f"paused ({torrent.raw_state})"
    if reason:
        detail = f"stopped by the client: {reason}"
    findings.append(Finding(Ailment.UNEXPECTEDLY_PAUSED, detail))


def _share_limit_reason(torrent: Torrent) -> str:
    """Why the client stopped this torrent, when a share limit explains it.

    qBittorrent enforces per-torrent ratio and seeding-time caps by *stopping*
    the torrent, and reports no distinct state for it — a capped torrent and a
    hand-paused one are both `stoppedUP`. Resuming one is futile: it is stopped
    again within seconds.
    """
    if torrent.ratio_limit > 0 and torrent.ratio >= torrent.ratio_limit:
        return f"ratio {torrent.ratio:.2f} reached its limit of {torrent.ratio_limit:g}"
    if torrent.seeding_time_limit > 0 and torrent.seeding_time / 60 >= torrent.seeding_time_limit:
        days = torrent.seeding_time_limit / 1440
        return f"seeding time reached its limit of {torrent.seeding_time_limit} min ({days:.1f} days)"
    return ""


def _check_announcing(torrent: Torrent, rules: RuleConfig, findings: list[Finding]) -> None:
    if torrent.is_paused:
        return
    real = torrent.real_trackers
    if not real:
        return
    # qBittorrent has no per-tracker "last announce" field, so we infer: a
    # torrent that has been active for longer than the announce interval and
    # still shows no contacted tracker never got its announce out.
    if torrent.time_active < rules.announce_timeout_minutes * 60:
        return
    if all(entry.status in (TRACKER_NOT_CONTACTED, TRACKER_DISABLED) for entry in real):
        findings.append(
            Finding(
                Ailment.NOT_ANNOUNCING,
                f"active {torrent.time_active // 60}m with no tracker contacted",
            )
        )


def _check_stalls(torrent: Torrent, rules: RuleConfig, now: float, findings: list[Finding]) -> None:
    if torrent.is_paused:
        return
    idle_seconds = now - torrent.last_activity if torrent.last_activity else 0

    if not torrent.is_complete:
        if torrent.dlspeed == 0 and idle_seconds > rules.stalled_download_minutes * 60:
            findings.append(
                Finding(
                    Ailment.STALLED_DOWNLOAD,
                    f"{torrent.progress:.1%} done, no data for {int(idle_seconds // 60)}m",
                )
            )
        return

    # Seeding with nobody to seed to is normal and must not be flagged. The
    # real fault is: the tracker says leechers exist, but we are connected to
    # none of them — that means our announce is not reaching them.
    if torrent.upspeed > 0 or torrent.num_leechs > 0:
        return
    if idle_seconds <= rules.stalled_upload_minutes * 60:
        return

    swarm_leechers = max((e.num_leeches for e in torrent.real_trackers), default=-1)
    trackers_broken = bool(torrent.real_trackers) and all(not e.working for e in torrent.real_trackers)

    if swarm_leechers > 0:
        findings.append(
            Finding(
                Ailment.STALLED_UPLOAD,
                f"tracker reports {swarm_leechers} leecher(s) but we have 0 peers "
                f"for {int(idle_seconds // 60)}m",
            )
        )
    elif trackers_broken:
        findings.append(
            Finding(
                Ailment.STALLED_UPLOAD,
                f"idle {int(idle_seconds // 60)}m with no working tracker",
            )
        )


async def enrich(diagnosis: Diagnosis, plugin: TrackerPlugin | None, rules: RuleConfig) -> bool:
    """Confirm or refine a suspicious diagnosis using the tracker plugin.

    Mutates `diagnosis` in place. Never raises: a tracker being unreachable is
    itself useful information, not a reason to abort the scan.

    Returns whether the tracker actually answered. The caller uses that to
    decide it has a fresh verdict on this torrent — a timeout or a missing
    capability must not be mistaken for one, or a torrent would be marked
    checked without ever having been looked up.
    """
    from ..trackers.base import Capability

    if plugin is None or not plugin.can(Capability.TORRENT_INFO):
        return False

    torrent = diagnosis.torrent
    try:
        info = await plugin.torrent_info(torrent.infohash)
    except TrackerError as exc:
        log.debug("tracker lookup failed for %s: %s", torrent.name, exc)
        return False
    if info is None:
        return False

    if not info.exists:
        # Authoritative: the site does not have this torrent any more. Replace
        # softer findings with the terminal one so we stop retrying.
        if Ailment.UNREGISTERED not in diagnosis.ailments:
            diagnosis.findings = [
                f for f in diagnosis.findings if f.ailment is not Ailment.TRACKER_DOWN
            ]
            diagnosis.findings.append(
                Finding(Ailment.UNREGISTERED, f"{plugin.name} no longer lists this torrent")
            )
        return True

    # The torrent exists, so a "tracker down" reading was a transient blip.
    diagnosis.findings = [f for f in diagnosis.findings if f.ailment is not Ailment.UNREGISTERED]

    _check_hit_and_run(diagnosis, info, plugin, rules)
    return True


def _check_hit_and_run(diagnosis, info, plugin, rules: RuleConfig) -> None:
    from ..trackers.base import Capability

    if not plugin.can(Capability.HNR):
        return
    if info.hit_and_run:
        diagnosis.findings.append(
            Finding(Ailment.HIT_AND_RUN_RISK, f"{plugin.name} has flagged this as a hit-and-run")
        )
        return

    required, current = info.seedtime_required, info.seedtime_current
    if not required or current is None:
        return
    remaining = required - current
    if 0 < remaining <= required * rules.hnr_margin:
        hours = remaining / 3600
        diagnosis.findings.append(
            Finding(Ailment.HIT_AND_RUN_RISK, f"{hours:.1f}h of required seed time left")
        )
