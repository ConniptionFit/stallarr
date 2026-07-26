"""Deleting torrents the tracker has deleted — and refusing to when unsure.

This is the only irreversible thing stallarr does, and `unregistered` is an
inference from an empty search result. A tracker whose search index has a bad
minute looks *exactly* like a tracker that deleted your entire library. Almost
every test here is about that failure, not about the happy path.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time
import uuid

import pytest

from stallarr import db
from stallarr.config import RecoveryConfig
from stallarr.models import Ailment, Diagnosis, Finding, Torrent, TorrentState, TrackerEntry
from stallarr.recovery.reap import breaker_report, reap, tracker_of, tripped_breakers

pytestmark = pytest.mark.asyncio


class FakeClient:
    """Records deletes and exports; can be told to refuse either."""

    def __init__(self, fail: bool = False, export_fail: bool = False):
        self.deleted: list[tuple[str, bool]] = []
        self.exported: list[str] = []
        self.fail = fail
        self.export_fail = export_fail

    async def delete(self, infohash: str, delete_files: bool = False) -> None:
        if self.fail:
            from stallarr.clients.base import ClientError

            raise ClientError("qBittorrent said no")
        self.deleted.append((infohash, delete_files))

    async def export_torrent(self, infohash: str) -> bytes:
        if self.export_fail:
            from stallarr.clients.base import ClientError

            raise ClientError("qBittorrent has no metainfo for this")
        self.exported.append(infohash)
        return b"d8:announce4:htted"


#: Where exported .torrent files go during tests. Set per-test by the fixture
#: below so nothing is ever written into the real data directory.
EXPORT_DIR = ""


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    global EXPORT_DIR
    EXPORT_DIR = str(tmp_path / "graveyard")
    await db.init()
    for table in ("condemned", "history", "attempts", "deletions"):
        await asyncio.to_thread(db._exec, f"DELETE FROM {table}", ())


def diagnosis(
    name: str = "Some.Release-GRP",
    *,
    ailment: Ailment = Ailment.UNREGISTERED,
    tracker: str = "seedpool",
    host: str = "seedpool.org",
    size: int = 1024**3,
) -> Diagnosis:
    torrent = Torrent(
        infohash=uuid.uuid4().hex + "0" * 8,
        name=name,
        state=TorrentState.STALLED_UP,
        raw_state="stalledUP",
        progress=1.0,
        size=size,
        trackers=[TrackerEntry(url=f"https://{host}/announce")],
    )
    return Diagnosis(
        torrent=torrent,
        findings=[Finding(ailment, "no longer listed")],
        tracker_id=tracker,
    )


def cfg(**kwargs) -> RecoveryConfig:
    base = {
        "enabled": True,
        "delete_unregistered": True,
        "delete_min_age_hours": 24,
        "delete_max_per_scan": 5,
        "delete_max_tracker_fraction": 0.10,
        "delete_export_dir": EXPORT_DIR,
    }
    return RecoveryConfig(**{**base, **kwargs})


async def age(infohash: str, hours: float) -> None:
    """Backdate a condemned record, standing in for hours of scans agreeing."""
    await asyncio.to_thread(
        db._exec,
        "UPDATE condemned SET first_at = ? WHERE infohash = ?",
        (int(time.time() - hours * 3600), infohash),
    )


async def soak_all(client, batch: list[Diagnosis], config: RecoveryConfig, totals: dict, hours=48):
    """Condemn the whole batch in one scan, backdate it, then run the scan that
    deletes. Condemning one at a time would be wrong: the ledger tracks what is
    unregistered *this* scan, so the others would read as having recovered."""
    await reap(client, batch, config, totals)
    for d in batch:
        await age(d.torrent.infohash, hours)
    return await reap(client, batch, config, totals)


async def soak(client, d: Diagnosis, config: RecoveryConfig, totals: dict, hours: float = 48):
    return await soak_all(client, [d], config, totals, hours)


# --- the soak period ----------------------------------------------------------


async def test_a_freshly_unregistered_torrent_is_not_deleted():
    """The single most important behaviour here: one bad answer from a tracker
    can never delete anything, because the verdict has to still be standing
    hours later."""
    client, d = FakeClient(), diagnosis()

    results = await reap(client, [d], cfg(), {"seedpool": 100})

    assert results == []
    assert client.deleted == []


async def test_it_is_deleted_once_the_verdict_has_stood_long_enough():
    client, d = FakeClient(), diagnosis()

    results = await soak(client, d, cfg(), {"seedpool": 100})

    assert len(results) == 1 and results[0][1].ok
    assert client.deleted == [(d.torrent.infohash, False)]


async def test_a_torrent_that_recovers_loses_its_accrued_age():
    """The soak has to mean "continuously unregistered". If a torrent drops off
    the list and comes back, its clock restarts — otherwise a tracker that
    flickers once a day would eventually age everything into deletion."""
    client, d = FakeClient(), diagnosis()
    await reap(client, [d], cfg(), {"seedpool": 100})
    await age(d.torrent.infohash, 48)

    await reap(client, [], cfg(), {"seedpool": 100})  # scan where it looked fine
    results = await reap(client, [d], cfg(), {"seedpool": 100})

    assert results == []
    assert client.deleted == []


async def test_the_clock_is_not_restarted_by_every_scan():
    client, d = FakeClient(), diagnosis()
    off = cfg(delete_unregistered=False)
    await reap(client, [d], off, {"seedpool": 100})
    await age(d.torrent.infohash, 48)

    await reap(client, [d], off, {"seedpool": 100})  # seen again, still condemned

    (row,) = await db.list_condemned()
    assert time.time() - row["first_at"] > 47 * 3600


async def test_a_prior_verdict_in_history_counts_towards_the_soak():
    """A restart or upgrade must not hand every known-dead torrent a fresh
    24 hours: history already proves how long the verdict has stood."""
    client, d = FakeClient(), diagnosis()
    await db.add_history(
        infohash=d.torrent.infohash, name=d.torrent.name, ailment="unregistered", action="escalate"
    )
    await asyncio.to_thread(
        db._exec,
        "UPDATE history SET ts = ? WHERE infohash = ?",
        (int(time.time() - 72 * 3600), d.torrent.infohash),
    )

    results = await reap(client, [d], cfg(), {"seedpool": 100})

    assert len(results) == 1 and results[0][1].ok


# --- the circuit breaker ------------------------------------------------------


async def test_too_many_at_once_on_one_tracker_deletes_nothing():
    """A search index returning empty for everything is indistinguishable from
    mass deletion. Past a threshold, disbelieve it."""
    client = FakeClient()
    doomed = [diagnosis(f"R{i}") for i in range(30)]

    results = await soak_all(client, doomed, cfg(), {"seedpool": 100})

    assert results == []
    assert client.deleted == []


async def test_a_believable_number_still_goes_through():
    client = FakeClient()
    doomed = [diagnosis(f"R{i}") for i in range(3)]

    results = await soak_all(client, doomed, cfg(), {"seedpool": 100})

    assert len(results) == 3


async def test_one_tracker_tripping_does_not_block_another():
    client = FakeClient()
    bad = [diagnosis(f"B{i}", tracker="seedpool") for i in range(30)]
    good = diagnosis("G", tracker="iptorrents", host="ipt.org")

    results = await soak_all(
        client, [*bad, good], cfg(), {"seedpool": 100, "iptorrents": 100}
    )

    assert [r[0].torrent.infohash for r in results] == [good.torrent.infohash]


async def test_a_tracker_with_no_denominator_is_refused_not_waved_through():
    """The breaker cannot judge a tracker it has no torrent count for, and
    "cannot judge" has to fail closed when the action erases data."""
    assert tripped_breakers([diagnosis()], {}, 0.10) == {"seedpool"}

    client, d = FakeClient(), diagnosis()
    assert await soak(client, d, cfg(), {}) == []
    assert client.deleted == []


async def test_an_unmatched_torrent_is_bucketed_by_its_announce_host():
    d = diagnosis(tracker="", host="cow.milkie.cc")
    d.tracker_id = None

    assert tracker_of(d) == "cow.milkie.cc"


# --- what qualifies at all ----------------------------------------------------


async def test_only_a_primary_unregistered_finding_qualifies():
    """A stalled torrent is not a deleted one, however long it has been stuck."""
    client = FakeClient()
    d = diagnosis(ailment=Ailment.STALLED_UPLOAD)

    assert await soak(client, d, cfg(), {"seedpool": 100}) == []
    assert client.deleted == []


async def test_a_secondary_unregistered_finding_does_not_qualify():
    """One dead tracker on a multi-tracker torrent is not grounds to delete it;
    another tracker may still be serving it perfectly well."""
    client = FakeClient()
    d = diagnosis(ailment=Ailment.MISSING_FILES)
    d.findings.append(Finding(Ailment.UNREGISTERED, "one tracker says no"))

    assert await soak(client, d, cfg(), {"seedpool": 100}) == []


# --- the switches -------------------------------------------------------------


async def test_nothing_is_deleted_while_the_feature_is_off():
    client, d = FakeClient(), diagnosis()

    assert await soak(client, d, cfg(delete_unregistered=False), {"seedpool": 100}) == []
    assert client.deleted == []


async def test_the_ledger_is_kept_even_while_the_feature_is_off():
    """So switching it on acts on verdicts that have genuinely stood for days,
    rather than starting every clock from the moment of the config save."""
    client, d = FakeClient(), diagnosis()

    await reap(client, [d], cfg(delete_unregistered=False), {"seedpool": 100})

    assert [r["infohash"] for r in await db.list_condemned()] == [d.torrent.infohash]


async def test_disabling_recovery_entirely_also_stops_deletion():
    client, d = FakeClient(), diagnosis()

    assert await soak(client, d, cfg(enabled=False), {"seedpool": 100}) == []


async def test_files_are_only_deleted_when_that_switch_is_on():
    client, d = FakeClient(), diagnosis()

    await soak(client, d, cfg(delete_unregistered_files=True), {"seedpool": 100})

    assert client.deleted == [(d.torrent.infohash, True)]


async def test_a_dry_run_deletes_nothing_but_reports_what_it_would():
    client, d = FakeClient(), diagnosis()

    results = await soak(client, d, cfg(dry_run=True, delete_unregistered_files=True), {"seedpool": 100})

    assert client.deleted == []
    assert results[0][1].dry_run and results[0][1].ok
    assert "would delete" in results[0][1].message


async def test_the_per_scan_cap_limits_the_damage_of_any_fault_that_gets_through():
    client = FakeClient()
    doomed = [diagnosis(f"R{i}") for i in range(6)]

    results = await soak_all(
        client, doomed, cfg(delete_max_per_scan=2), {"seedpool": 1000}
    )

    assert len(results) == 2 and len(client.deleted) == 2


async def test_the_longest_condemned_go_first():
    """They have had the most scans in which the tracker could have changed its
    mind, so they are the safest of the batch to act on."""
    client = FakeClient()
    old, new = diagnosis("old"), diagnosis("new")
    await reap(client, [old, new], cfg(delete_unregistered=False), {"seedpool": 1000})
    await age(old.torrent.infohash, 200)
    await age(new.torrent.infohash, 30)

    await reap(client, [new, old], cfg(delete_max_per_scan=1), {"seedpool": 1000})

    assert client.deleted == [(old.torrent.infohash, False)]


# --- failure handling ---------------------------------------------------------


async def test_a_refused_delete_is_reported_and_keeps_its_place():
    """The record must survive, or the torrent starts its soak period again and
    the failure quietly turns into a reprieve on every retry."""
    client, d = FakeClient(fail=True), diagnosis()

    results = await soak(client, d, cfg(), {"seedpool": 100})

    assert not results[0][1].ok
    assert "client refused" in results[0][1].message
    assert [r["infohash"] for r in await db.list_condemned()] == [d.torrent.infohash]


async def test_a_deleted_torrent_leaves_no_ledger_row_behind():
    client, d = FakeClient(), diagnosis()

    await soak(client, d, cfg(), {"seedpool": 100})

    assert await db.list_condemned() == []


# --- keeping the .torrent -----------------------------------------------------


async def test_the_torrent_file_is_kept_before_the_data_goes():
    client, d = FakeClient(), diagnosis()

    await soak(client, d, cfg(delete_unregistered_files=True), {"seedpool": 100})

    kept = pathlib.Path(EXPORT_DIR) / f"{d.torrent.infohash}.torrent"
    assert kept.read_bytes().startswith(b"d")
    assert client.exported == [d.torrent.infohash]


async def test_the_kept_file_carries_enough_context_to_identify_it():
    """The file is named by infohash, which tells a human nothing on its own."""
    client, d = FakeClient(), diagnosis("Some.Great.Show.S01E01-GRP")

    await soak(client, d, cfg(), {"seedpool": 100})

    side = pathlib.Path(EXPORT_DIR) / f"{d.torrent.infohash}.json"
    meta = json.loads(side.read_text())
    assert meta["name"] == "Some.Great.Show.S01E01-GRP"
    assert meta["tracker"] == "seedpool"
    assert meta["reason"] == "no longer listed"


async def test_a_failed_export_cancels_the_deletion():
    """The export exists to make this recoverable. Deleting anyway would erase
    the thing it was protecting, so the failure has to stop the delete."""
    client, d = FakeClient(export_fail=True), diagnosis()

    results = await soak(client, d, cfg(delete_unregistered_files=True), {"seedpool": 100})

    assert client.deleted == []
    assert not results[0][1].ok and "could not export" in results[0][1].message
    # Still condemned, so it retries rather than quietly never being considered.
    assert [r["infohash"] for r in await db.list_condemned()] == [d.torrent.infohash]


async def test_the_export_can_be_switched_off():
    client, d = FakeClient(export_fail=True), diagnosis()

    results = await soak(client, d, cfg(delete_export_torrents=False), {"seedpool": 100})

    assert results[0][1].ok and client.deleted == [(d.torrent.infohash, False)]
    assert client.exported == []


async def test_a_dry_run_writes_no_file():
    client, d = FakeClient(), diagnosis()

    await soak(client, d, cfg(dry_run=True), {"seedpool": 100})

    assert client.exported == []
    assert not pathlib.Path(EXPORT_DIR).exists()


# --- the permanent record -----------------------------------------------------


async def test_a_deletion_is_recorded_permanently():
    """`history` is pruned to a retention window; this must outlive it."""
    client, d = FakeClient(), diagnosis("Gone.Forever-GRP", size=5 * 1024**3)

    await soak(client, d, cfg(delete_unregistered_files=True), {"seedpool": 100})

    (row,) = await db.list_deletions()
    assert row["name"] == "Gone.Forever-GRP"
    assert row["tracker"] == "seedpool"
    assert row["size"] == 5 * 1024**3
    assert row["files_deleted"] == 1
    assert row["reason"] == "no longer listed"
    assert row["export_path"].endswith(f"{d.torrent.infohash}.torrent")


async def test_pruning_history_does_not_touch_the_deletion_record():
    client, d = FakeClient(), diagnosis()
    await soak(client, d, cfg(), {"seedpool": 100})

    await db.prune_history(0)

    assert len(await db.list_deletions()) == 1


async def test_only_deletions_that_took_the_data_count_as_reclaimed():
    """Removing the torrent entry alone frees nothing, and reporting it as
    reclaimed space would overstate what this feature actually did."""
    client = FakeClient()
    entry_only, with_data = diagnosis("A", size=1024**3), diagnosis("B", size=2 * 1024**3)

    await soak(client, entry_only, cfg(delete_unregistered_files=False), {"seedpool": 100})
    await soak(client, with_data, cfg(delete_unregistered_files=True), {"seedpool": 100})

    totals = await db.deletion_totals(7)
    assert totals["count"] == 2
    assert totals["bytes"] == 3 * 1024**3
    assert totals["reclaimed"] == 2 * 1024**3


async def test_a_failed_delete_is_not_recorded_as_one():
    client, d = FakeClient(fail=True), diagnosis()

    await soak(client, d, cfg(), {"seedpool": 100})

    assert await db.list_deletions() == []


# --- explaining a tripped breaker ---------------------------------------------


async def test_a_tripped_breaker_reports_what_it_saw():
    """A guard that trips silently reads exactly like having nothing to do."""
    doomed = [diagnosis(f"R{i}") for i in range(30)]

    report = breaker_report(doomed, {"seedpool": 100}, 0.10)

    assert set(report) == {"seedpool"}
    assert report["seedpool"]["condemned"] == 30
    assert report["seedpool"]["total"] == 100
    assert report["seedpool"]["fraction"] == pytest.approx(0.30)
    assert report["seedpool"]["limit"] == 0.10


async def test_a_missing_denominator_is_reported_as_its_own_reason():
    report = breaker_report([diagnosis()], {}, 0.10)

    assert report["seedpool"]["total"] == 0
    assert report["seedpool"]["fraction"] is None
    assert "no torrent count" in report["seedpool"]["reason"]


async def test_nothing_is_reported_when_the_share_is_believable():
    doomed = [diagnosis(f"R{i}") for i in range(3)]

    assert breaker_report(doomed, {"seedpool": 100}, 0.10) == {}
