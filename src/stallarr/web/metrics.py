"""Prometheus exposition for the numbers the dashboard already computes.

Deliberately hand-rolled rather than pulling in a client library: everything
here is a gauge read once per scrape from state the service already holds, and
the format is four lines of string building. No new dependency earns its place
for that.

What is *not* exported: torrent names, infohashes, tracker URLs, anything from
config. Tracker ids appear as label values, which is why the endpoint is opt-in
(`web.metrics_enabled`) — knowing which private trackers a host is on is worth
something to the wrong reader.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import db

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from ..service import StallarrService

#: Metric name -> help text. Kept beside the values so a metric cannot be added
#: without saying what it means.
_HELP = {
    "stallarr_torrents_total": "Torrents seen by the last scan",
    "stallarr_torrents_healthy": "Torrents passing every check on the last scan",
    "stallarr_torrents_sick": "Torrents with at least one finding on the last scan",
    "stallarr_torrents_skipped": "Torrents excluded from diagnosis on the last scan",
    "stallarr_actions_total": "Recovery actions taken during the last scan",
    "stallarr_scan_duration_seconds": "Duration of the last scan",
    "stallarr_scan_finished_timestamp": "Unix time the last scan finished",
    "stallarr_client_up": "1 when the torrent client answered, 0 otherwise",
    "stallarr_tracker_up": "1 when a tracker plugin answered, 0 otherwise",
    "stallarr_seeding_outstanding": "Seeding obligations currently owed",
    "stallarr_seeding_averted": "Hit-and-runs averted in the metrics window",
    "stallarr_condemned_total": "Torrents queued for deletion",
    "stallarr_deletion_blocked": "1 when a tracker's deletions are refused by the circuit breaker",
    "stallarr_deletions_total": "Torrents deleted, all time",
    "stallarr_deleted_bytes_total": "Bytes reclaimed by deleting torrent data, all time",
    "stallarr_disk_free_bytes": "Free space reported by the torrent client",
    "stallarr_torrents_seeding": "Complete, not-stopped torrents in the client",
    "stallarr_torrents_uploading": "Seeding torrents sending data at the last scan",
    "stallarr_torrents_recovered": "Previously-sick torrents that came back healthy on the last scan",
    "stallarr_client_uploaded_bytes": "Bytes uploaded by torrents currently in the client",
    "stallarr_client_downloaded_bytes": "Bytes downloaded by torrents currently in the client",
    "stallarr_client_upload_speed_bytes": "Combined upload speed at the last scan, bytes per second",
    "stallarr_repairs_attempted": "Recovery actions taken in the metrics window, dry runs excluded",
    "stallarr_repairs_succeeded": "Recovery actions the client accepted in the metrics window",
    "stallarr_tracker_client_uploaded_bytes": "Bytes uploaded by this tracker's torrents in the client",
}


def _line(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    if not labels:
        return f"{name} {value}"
    rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
    return f"{name}{{{rendered}}} {value}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


async def render(service: StallarrService) -> str:
    """The full exposition body for one scrape."""
    scan = service.last_scan
    seeding = await db.seeding_metrics(7)
    totals = await db.deletion_totals()
    condemned = await db.list_condemned()
    repairs = await db.repair_stats(7)
    library = service.library

    values: list[tuple[str, float, dict[str, str] | None]] = [
        ("stallarr_torrents_total", scan.total, None),
        ("stallarr_torrents_healthy", scan.healthy, None),
        ("stallarr_torrents_sick", scan.sick, None),
        ("stallarr_torrents_skipped", scan.skipped, None),
        ("stallarr_actions_total", scan.actions, None),
        ("stallarr_scan_duration_seconds", round(scan.duration, 2), None),
        ("stallarr_scan_finished_timestamp", scan.finished_at, None),
        ("stallarr_client_up", 1 if service.client_status.get("connected") else 0, None),
        ("stallarr_seeding_outstanding", seeding.get("outstanding", 0), None),
        ("stallarr_seeding_averted", seeding.get("averted", 0), None),
        ("stallarr_condemned_total", len(condemned), None),
        ("stallarr_deletions_total", totals["count"], None),
        ("stallarr_deleted_bytes_total", totals["reclaimed"], None),
        ("stallarr_torrents_seeding", library.get("seeding", 0), None),
        ("stallarr_torrents_uploading", library.get("uploading", 0), None),
        ("stallarr_torrents_recovered", scan.recovered, None),
        ("stallarr_client_uploaded_bytes", library.get("uploaded", 0), None),
        ("stallarr_client_downloaded_bytes", library.get("downloaded", 0), None),
        ("stallarr_client_upload_speed_bytes", library.get("upspeed", 0), None),
        ("stallarr_repairs_attempted", repairs["attempts"], None),
        ("stallarr_repairs_succeeded", repairs["succeeded"], None),
    ]

    for row in library.get("by_tracker", []):
        values.append(
            ("stallarr_tracker_client_uploaded_bytes", row["uploaded"], {"tracker": row["tracker"]})
        )

    for tracker_id, status in service.tracker_status.items():
        values.append(("stallarr_tracker_up", 1 if status.get("ok") else 0, {"tracker": tracker_id}))

    # Exported even when empty is not possible for a per-tracker gauge, so a
    # tracker only appears here while it is actually blocked. Alert on presence.
    for block in service.deletion_blocks:
        values.append(("stallarr_deletion_blocked", 1, {"tracker": str(block["tracker"])}))

    if service.disk.get("free") is not None:
        values.append(("stallarr_disk_free_bytes", service.disk["free"], None))

    out: list[str] = []
    emitted: set[str] = set()
    for name, value, labels in values:
        if name not in emitted:
            out.append(f"# HELP {name} {_HELP.get(name, name)}")
            out.append(f"# TYPE {name} gauge")
            emitted.add(name)
        out.append(_line(name, value, labels))
    return "\n".join(out) + "\n"
