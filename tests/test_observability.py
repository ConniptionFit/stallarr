"""Scan snapshots, the permanent deletion record, /metrics and account warnings.

These all exist because the dashboard could only ever answer "right now", which
cannot tell a step change from a slow drift — the exact distinction the deletion
circuit breaker's threshold turns on.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from stallarr import db
from stallarr.config import TrackerConfig
from stallarr.service import _account_warnings

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _db():
    await db.init()
    for table in ("scan_snapshots", "deletions", "condemned", "history"):
        await asyncio.to_thread(db._exec, f"DELETE FROM {table}", ())


def hexhash() -> str:
    return uuid.uuid4().hex + "0" * 8


# --- scan snapshots -----------------------------------------------------------


async def test_a_snapshot_is_kept_per_scan():
    now = int(time.time())
    await db.record_snapshot(ts=now - 600, total=10, healthy=8, sick=2, actions=1)
    await db.record_snapshot(ts=now, total=11, healthy=9, sick=2, actions=0)

    rows = await db.scan_history(days=7)

    assert [r["ts"] for r in rows] == [now - 600, now], "oldest first, so it plots left to right"
    assert rows[1]["total"] == 11


async def test_rerunning_a_scan_in_the_same_second_replaces_rather_than_duplicates():
    now = int(time.time())
    await db.record_snapshot(ts=now, total=1)
    await db.record_snapshot(ts=now, total=2)

    rows = await db.scan_history(days=7)

    assert len(rows) == 1 and rows[0]["total"] == 2


async def test_snapshots_outside_the_window_are_not_returned():
    await db.record_snapshot(ts=int(time.time()) - 40 * 86400, total=99)
    await db.record_snapshot(ts=int(time.time()), total=1)

    assert [r["total"] for r in await db.scan_history(days=7)] == [1]


async def test_pruning_snapshots_drops_only_the_old_ones():
    await db.record_snapshot(ts=int(time.time()) - 400 * 86400, total=99)
    await db.record_snapshot(ts=int(time.time()), total=1)

    await db.prune_snapshots(180)

    assert [r["total"] for r in await db.scan_history(days=3650)] == [1]


# --- the permanent deletion record --------------------------------------------


async def record(name: str = "X", *, size: int = 1024, files: bool = True) -> str:
    infohash = hexhash()
    await db.record_deletion(
        infohash=infohash,
        name=name,
        tracker="seedpool",
        size=size,
        files_deleted=files,
        reason="no longer listed",
        condemned_at=int(time.time()) - 3600,
        export_path=f"/graveyard/{infohash}.torrent",
    )
    return infohash


async def test_deleting_the_same_torrent_twice_leaves_one_row():
    """A torrent can be re-added and deleted again; that is one history, not a
    crash."""
    infohash = hexhash()
    for name in ("first", "second"):
        await db.record_deletion(
            infohash=infohash,
            name=name,
            tracker="seedpool",
            size=1,
            files_deleted=False,
            reason="r",
            condemned_at=0,
        )

    rows = await db.list_deletions()
    assert len(rows) == 1 and rows[0]["name"] == "second"


async def test_totals_split_all_time_from_the_recent_window():
    await record(size=1024)
    old = await record(size=2048)
    await asyncio.to_thread(
        db._exec,
        "UPDATE deletions SET deleted_at = ? WHERE infohash = ?",
        (int(time.time()) - 30 * 86400, old),
    )

    totals = await db.deletion_totals(days=7)

    assert totals["count"] == 2, "all-time count includes the old one"
    assert totals["recent_count"] == 1
    assert totals["reclaimed"] == 3072
    assert totals["recent_reclaimed"] == 1024


async def test_totals_are_zero_rather_than_null_on_an_empty_table():
    """SUM over no rows is NULL in SQLite, and a NULL reaching the dashboard
    renders as a blank card rather than a zero."""
    totals = await db.deletion_totals()

    assert totals == {
        "window_days": 7,
        "count": 0,
        "bytes": 0,
        "reclaimed": 0,
        "recent_count": 0,
        "recent_reclaimed": 0,
    }


# --- account warnings ---------------------------------------------------------


class Stats:
    def __init__(self, ratio=None, hit_and_runs=None):
        self.ratio = ratio
        self.hit_and_runs = hit_and_runs


async def test_no_warnings_when_the_thresholds_are_unset():
    """0 means "do not check", so a fresh install is not full of alarms."""
    assert _account_warnings(Stats(ratio=0.1, hit_and_runs=9), TrackerConfig()) == []


async def test_a_ratio_at_the_floor_warns():
    cfg = TrackerConfig(min_ratio=1.0)

    assert _account_warnings(Stats(ratio=1.0), cfg), "at the floor is already too late"
    assert _account_warnings(Stats(ratio=0.9), cfg)
    assert _account_warnings(Stats(ratio=1.1), cfg) == []


async def test_hit_and_runs_warn_on_reaching_the_limit_not_after_it():
    cfg = TrackerConfig(max_hit_and_runs=2)

    assert _account_warnings(Stats(hit_and_runs=2), cfg)
    assert _account_warnings(Stats(hit_and_runs=1), cfg) == []


async def test_a_tracker_with_no_config_is_not_an_error():
    assert _account_warnings(Stats(ratio=0.1), None) == []


async def test_missing_stats_do_not_masquerade_as_zero():
    """A tracker that does not report ratio must not read as ratio 0.0 and fire
    every warning at once."""
    cfg = TrackerConfig(min_ratio=1.0, max_hit_and_runs=1)

    assert _account_warnings(Stats(ratio=None, hit_and_runs=None), cfg) == []
