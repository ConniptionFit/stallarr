"""Dashboard statistics: library totals, repair effectiveness, trend thinning.

These numbers are what someone reads to decide whether stallarr is working, so
the tests pin the distinctions the UI labels promise: seeding vs uploading,
client ratio vs account ratio, "step accepted" vs "torrent recovered", and a
breakdown that adds up to the total beside it.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from conftest import make_torrent

from stallarr import db
from stallarr.config import Config, TrackerConfig
from stallarr.models import Ailment, Diagnosis, Finding, TorrentState, TrackerEntry
from stallarr.service import StallarrService
from stallarr.stats import UNTRACKED, download_headroom, library_stats, problems_by_ailment
from stallarr.trackers import TrackerRouter
from stallarr.trackers.base import TrackerPlugin
from stallarr.web import metrics

GiB = 1024**3


class Plugin(TrackerPlugin):
    id = "darkpeers"
    name = "DarkPeers"
    hosts = ("darkpeers.org",)

    def __init__(self):
        super().__init__(TrackerConfig(enabled=True))


def router() -> TrackerRouter:
    plugin = Plugin()
    return TrackerRouter({plugin.id: plugin})


def hexhash() -> str:
    return uuid.uuid4().hex + "0" * 8


# --- library totals -----------------------------------------------------------


def test_seeding_and_uploading_are_counted_separately():
    torrents = [
        make_torrent(infohash=hexhash(), upspeed=500, uploaded=3 * GiB, downloaded=GiB),
        make_torrent(infohash=hexhash(), upspeed=0, uploaded=GiB, downloaded=GiB),
        make_torrent(infohash=hexhash(), state=TorrentState.PAUSED, upspeed=0),
        make_torrent(infohash=hexhash(), progress=0.4, state=TorrentState.DOWNLOADING, upspeed=0),
    ]

    lib = library_stats(torrents, router())

    assert lib["torrents"] == 4
    assert lib["seeding"] == 2, "an idle complete seed is still seeding"
    assert lib["uploading"] == 1, "only the one sending data is uploading"
    assert lib["stopped"] == 1
    assert lib["downloading"] == 1
    assert lib["upspeed"] == 500
    assert lib["ratio"] == 2.0


def test_ratio_is_none_when_nothing_was_downloaded():
    lib = library_stats([make_torrent(uploaded=GiB, downloaded=0)], router())

    assert lib["ratio"] is None, "a cross-seed downloaded nothing; 0 or infinity would mislead"


def test_unconfigured_trackers_get_their_own_bucket_listed_last():
    torrents = [
        make_torrent(infohash=hexhash(), uploaded=GiB),
        make_torrent(
            infohash=hexhash(),
            uploaded=10 * GiB,
            trackers=[TrackerEntry(url="https://unknown.example/announce", status=2)],
        ),
    ]

    rows = library_stats(torrents, router())["by_tracker"]

    assert [r["tracker"] for r in rows] == ["darkpeers", UNTRACKED]
    assert rows[0]["tracker_name"] == "DarkPeers"
    assert rows[1]["uploaded"] == 10 * GiB


def test_empty_library_is_all_zeros():
    lib = library_stats([])

    assert lib["torrents"] == 0 and lib["ratio"] is None and lib["by_tracker"] == []


# --- problem breakdown --------------------------------------------------------


def test_problem_breakdown_counts_each_torrent_once_under_its_worst_finding():
    double = Diagnosis(
        torrent=make_torrent(infohash=hexhash()),
        findings=[
            Finding(Ailment.STALLED_UPLOAD, "no peers"),
            Finding(Ailment.UNREGISTERED, "gone"),
        ],
    )
    single = Diagnosis(
        torrent=make_torrent(infohash=hexhash()),
        findings=[Finding(Ailment.STALLED_UPLOAD, "no peers")],
    )

    rows = problems_by_ailment([double, single])

    assert sum(r["count"] for r in rows) == 2, "must add up to the Problems tile"
    assert {r["ailment"]: r["count"] for r in rows} == {"unregistered": 1, "stalled_upload": 1}


# --- ratio headroom -----------------------------------------------------------


def test_headroom_is_measured_against_the_floor():
    # 100 up / 50 down at a 1.0 floor: 50 more can come down.
    assert download_headroom(100, 50, 0) == 50
    # Same account against a 2.0 floor: already exactly at it.
    assert download_headroom(100, 50, 2.0) == 0
    # Below the floor reads negative.
    assert download_headroom(100, 80, 2.0) == -30
    assert download_headroom(None, 50, 1.0) is None


# --- repair effectiveness -----------------------------------------------------


@pytest.fixture
async def clean_db():
    await db.init()
    for table in ("history", "scan_snapshots"):
        await asyncio.to_thread(db._exec, f"DELETE FROM {table}", ())


@pytest.mark.asyncio
async def test_repair_stats_split_by_action_and_exclude_dry_runs(clean_db):
    a, b = hexhash(), hexhash()
    await db.add_history(infohash=a, action="reannounce", ok=True)
    await db.add_history(infohash=a, action="reannounce", ok=False)
    await db.add_history(infohash=b, action="resume", ok=True)
    await db.add_history(infohash=b, action="resume", ok=True, dry_run=True)

    stats = await db.repair_stats(7)

    assert stats["attempts"] == 3, "the dry run changed nothing and is not a repair"
    assert stats["succeeded"] == 2 and stats["failed"] == 1
    assert stats["torrents"] == 2
    assert stats["dry_run"] == 1
    assert stats["success_rate"] == pytest.approx(2 / 3, abs=0.001)
    by_action = {r["action"]: r for r in stats["by_action"]}
    assert by_action["reannounce"]["success_rate"] == 0.5
    assert by_action["resume"]["attempts"] == 1


@pytest.mark.asyncio
async def test_recovered_is_summed_from_snapshots(clean_db):
    now = int(time.time())
    await db.record_snapshot(ts=now - 600, recovered=2)
    await db.record_snapshot(ts=now, recovered=3)
    await db.record_snapshot(ts=now - 30 * 86400, recovered=99)

    assert (await db.repair_stats(7))["recovered"] == 5


@pytest.mark.asyncio
async def test_empty_window_has_no_success_rate(clean_db):
    stats = await db.repair_stats(7)

    assert stats["attempts"] == 0 and stats["success_rate"] is None


# --- snapshots ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshots_keep_library_totals(clean_db):
    await db.record_snapshot(ts=int(time.time()), uploaded=5 * GiB, seeding=12, uploading=3)

    row = (await db.scan_history(1))[0]

    assert row["uploaded"] == 5 * GiB and row["seeding"] == 12 and row["uploading"] == 3


@pytest.mark.asyncio
async def test_long_windows_are_thinned_evenly_not_truncated(clean_db):
    now = int(time.time())
    for i in range(50):
        await db.record_snapshot(ts=now - (49 - i) * 60, total=i)

    rows = await db.scan_history(7, limit=10)

    assert len(rows) == 10
    assert rows[0]["total"] == 0, "the oldest row survives, so the window really is the window"
    assert rows[-1]["total"] == 49, "the newest row survives"


# --- service wiring -----------------------------------------------------------


class FakeClient:
    def __init__(self, torrents):
        self._torrents = torrents

    async def list_torrents(self):
        return self._torrents

    async def free_space(self):
        return None


@pytest.mark.asyncio
async def test_a_scan_counts_recoveries_and_records_library_totals(clean_db):
    recovered = make_torrent(infohash=hexhash(), tags=["stallarr:working"], uploaded=2 * GiB)
    svc = StallarrService(Config())
    svc.client = FakeClient([recovered])
    svc.router = router()

    class Engine:
        async def on_recovered(self, diagnosis):
            pass

    svc.engine = Engine()
    summary = await svc._scan_once()

    assert summary.recovered == 1
    assert svc.library["uploaded"] == 2 * GiB
    row = (await db.scan_history(1))[-1]
    assert row["recovered"] == 1 and row["uploaded"] == 2 * GiB

    body = await metrics.render(svc)
    assert "stallarr_torrents_recovered 1" in body
    assert 'stallarr_tracker_client_uploaded_bytes{tracker="darkpeers"}' in body
