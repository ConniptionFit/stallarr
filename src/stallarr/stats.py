"""Library-wide numbers for the dashboard, computed from data already in hand.

Everything here is a pure function of the torrent list the scan has just
fetched (or of rows already in the database), so none of it costs a tracker
call. That matters: the dashboard refreshes every ten seconds, and the rule
that tracker APIs stay out of the hot path applies to statistics too.

Two distinctions the dashboard used to blur, and that these figures keep apart:

* **Seeding** is a torrent that is complete and not stopped. **Uploading** is
  one actually sending data right now. A library can seed thousands and upload
  to nobody, which is normal (idle seeds are healthy) but is exactly the
  number someone chasing ratio wants to see.
* **Client ratio** is what qBittorrent has recorded for the torrents it holds
  today. It is *not* the tracker's account ratio, which counts everything you
  have ever transferred there, including torrents since removed. The Trackers
  tab shows the account figure; the two are labelled so they cannot be read as
  the same thing.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from .models import Diagnosis, Torrent, TorrentState

if TYPE_CHECKING:  # pragma: no cover
    from .trackers import TrackerRouter

#: Bucket for torrents whose announce host matches no configured plugin.
UNTRACKED = "other"


def _ratio(uploaded: int, downloaded: int) -> float | None:
    """Upload over download, or None when nothing has been downloaded.

    A torrent added from a file you already had downloads nothing; dividing by
    zero there and showing "∞" is accurate but reads as a bug, and showing 0 is
    simply wrong. None renders as a dash.
    """
    return round(uploaded / downloaded, 3) if downloaded > 0 else None


def _bucket() -> dict[str, Any]:
    return {
        "torrents": 0,
        "seeding": 0,
        "uploading": 0,
        "downloading": 0,
        "stopped": 0,
        "size": 0,
        "uploaded": 0,
        "downloaded": 0,
        "upspeed": 0,
        "dlspeed": 0,
    }


def _add(bucket: dict[str, Any], torrent: Torrent) -> None:
    bucket["torrents"] += 1
    bucket["size"] += torrent.size
    bucket["uploaded"] += torrent.uploaded
    bucket["downloaded"] += torrent.downloaded
    bucket["upspeed"] += torrent.upspeed
    bucket["dlspeed"] += torrent.dlspeed
    if torrent.is_paused:
        bucket["stopped"] += 1
    elif torrent.is_complete:
        bucket["seeding"] += 1
        if torrent.upspeed > 0:
            bucket["uploading"] += 1
    elif torrent.state is not TorrentState.QUEUED:
        bucket["downloading"] += 1


def library_stats(torrents: list[Torrent], router: TrackerRouter | None = None) -> dict[str, Any]:
    """Totals over every torrent the client holds, overall and per tracker.

    Counts the whole library, ignored and skipped torrents included: this is
    "what is in the client", not "what stallarr diagnosed", and the scan
    summary already answers the second question.
    """
    total = _bucket()
    by_tracker: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}

    for torrent in torrents:
        _add(total, torrent)
        plugin = router.for_hosts(torrent.tracker_hosts) if router else None
        key = plugin.id if plugin else UNTRACKED
        if plugin:
            names[key] = plugin.name or plugin.id
        _add(by_tracker.setdefault(key, _bucket()), torrent)

    rows = []
    for key, bucket in by_tracker.items():
        rows.append(
            {
                "tracker": key,
                "tracker_name": names.get(key, "Other / unconfigured"),
                **bucket,
                "ratio": _ratio(bucket["uploaded"], bucket["downloaded"]),
            }
        )
    # Biggest contributors first: the question this table answers is "where is
    # my upload coming from", and alphabetical order hides the answer.
    rows.sort(key=lambda r: (r["tracker"] == UNTRACKED, -r["uploaded"], r["tracker"]))

    return {
        **total,
        "ratio": _ratio(total["uploaded"], total["downloaded"]),
        "by_tracker": rows,
    }


def problems_by_ailment(diagnoses: list[Diagnosis]) -> list[dict[str, Any]]:
    """How many torrents have each problem as their *primary* finding.

    Primary only, so the counts add up to the number of problem torrents. A
    torrent with three findings counted three times would make the breakdown
    sum to more than the "Problems" tile beside it, which is the kind of
    mismatch that makes people stop trusting a dashboard.
    """
    counts: Counter[str] = Counter()
    worst: dict[str, int] = {}
    for diagnosis in diagnoses:
        finding = diagnosis.primary
        if finding is None:
            continue
        key = str(finding.ailment)
        counts[key] += 1
        worst[key] = max(worst.get(key, 0), int(finding.severity))
    return [
        {"ailment": key, "count": count, "severity": worst[key]}
        for key, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def download_headroom(uploaded: int | None, downloaded: int | None, floor: float) -> int | None:
    """How much more could be downloaded before ratio falls to `floor`.

    Solves `uploaded / (downloaded + x) = floor` for x. With no floor
    configured this uses 1.0, the ratio most sites treat as "in good standing";
    the response says which floor it used so the number is never ambiguous.
    Negative means the account is already below it, by that many bytes'
    worth of downloading.
    """
    if uploaded is None or downloaded is None:
        return None
    floor = floor if floor and floor > 0 else 1.0
    return int(uploaded / floor - downloaded)
