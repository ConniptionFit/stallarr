"""Seeding-obligation bookkeeping and the dashboard metrics built from it.

The headline number here — "hit-and-runs averted" — is an inference, and every
test in this file exists to stop it becoming a flattering one. A tracker never
says "you avoided a hit-and-run"; all that is observable is that it stopped
asking after stallarr acted. So the count must exclude debts that cleared on
their own, debts settled only because a tracker was unreachable, and actions
that were dry runs or failures.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from stallarr import db
from stallarr.config import Config
from stallarr.models import ActionKind, ActionResult, SeedingRequirement
from stallarr.service import StallarrService
from stallarr.utils import normalise_release

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _db():
    await db.init()
    await asyncio.to_thread(db._exec, "DELETE FROM seeding_obligations", ())


def owed(*names: str) -> list[tuple[str, str]]:
    return [(normalise_release(n), n) for n in names]


async def rows() -> list[dict]:
    return await asyncio.to_thread(db._rows, "SELECT * FROM seeding_obligations", ())


async def backdate(tracker: str, name: str, *, cleared_days_ago: float) -> None:
    """Move a settled obligation into the past, to test the metrics window."""
    when = int(time.time() - cleared_days_ago * 86400)
    await asyncio.to_thread(
        db._exec,
        "UPDATE seeding_obligations SET cleared_at = ?, last_seen = ? WHERE tracker = ? AND key = ?",
        (when, when, tracker, normalise_release(name)),
    )


# --- recording ----------------------------------------------------------------


async def test_an_obligation_is_recorded_the_first_time_it_is_seen():
    await db.record_seeding_obligations("ipt", owed("Some.Release-GRP"))

    (row,) = await rows()
    assert row["tracker"] == "ipt"
    assert row["name"] == "Some.Release-GRP"
    assert row["cleared_at"] == 0
    assert row["actions"] == 0


async def test_seeing_it_again_does_not_restart_the_clock():
    """`first_seen` is how long a debt has been outstanding. Refreshing it every
    scan would make a week-old obligation look brand new forever."""
    await db.record_seeding_obligations("ipt", owed("Some.Release-GRP"))
    (before,) = await rows()
    await asyncio.to_thread(
        db._exec,
        "UPDATE seeding_obligations SET first_seen = ?, last_seen = ?",
        (before["first_seen"] - 5000, before["last_seen"] - 5000),
    )

    await db.record_seeding_obligations("ipt", owed("Some.Release-GRP"))

    (after,) = await rows()
    assert after["first_seen"] == before["first_seen"] - 5000
    assert after["last_seen"] > after["first_seen"]


async def test_the_same_release_on_two_trackers_is_two_obligations():
    await db.record_seeding_obligations("ipt", owed("Some.Release-GRP"))
    await db.record_seeding_obligations("sp", owed("Some.Release-GRP"))

    assert len(await rows()) == 2


# --- settling -----------------------------------------------------------------


async def test_an_obligation_the_tracker_stops_listing_is_settled():
    await db.record_seeding_obligations("ipt", owed("One", "Two"))

    settled = await db.clear_seeding_obligations("ipt", {normalise_release("One")})

    assert settled == 1
    by_name = {r["name"]: r for r in await rows()}
    assert by_name["One"]["cleared_at"] == 0
    assert by_name["Two"]["cleared_at"] > 0


async def test_settling_one_tracker_leaves_another_alone():
    await db.record_seeding_obligations("ipt", owed("One"))
    await db.record_seeding_obligations("sp", owed("Two"))

    await db.clear_seeding_obligations("ipt", set())

    by_tracker = {r["tracker"]: r for r in await rows()}
    assert by_tracker["ipt"]["cleared_at"] > 0
    assert by_tracker["sp"]["cleared_at"] == 0


async def test_a_settled_obligation_that_returns_starts_a_fresh_cycle():
    """Otherwise a repair that cleared last month's debt would be credited with
    averting this month's, without stallarr doing anything at all."""
    await db.record_seeding_obligations("ipt", owed("Some.Release-GRP"))
    await db.note_seeding_intervention("ipt", normalise_release("Some.Release-GRP"), "resume")
    await db.clear_seeding_obligations("ipt", set())

    await db.record_seeding_obligations("ipt", owed("Some.Release-GRP"))

    (row,) = await rows()
    assert row["cleared_at"] == 0
    assert row["actions"] == 0
    assert row["last_action"] == ""


# --- interventions ------------------------------------------------------------


async def test_an_intervention_is_recorded_against_an_open_obligation():
    await db.record_seeding_obligations("ipt", owed("Some.Release-GRP"))

    await db.note_seeding_intervention("ipt", normalise_release("Some.Release-GRP"), "lift_share_limits")

    (row,) = await rows()
    assert row["actions"] == 1
    assert row["last_action"] == "lift_share_limits"


async def test_an_intervention_after_the_debt_cleared_is_not_recorded():
    await db.record_seeding_obligations("ipt", owed("Some.Release-GRP"))
    await db.clear_seeding_obligations("ipt", set())

    await db.note_seeding_intervention("ipt", normalise_release("Some.Release-GRP"), "resume")

    (row,) = await rows()
    assert row["actions"] == 0


async def test_an_intervention_on_an_unknown_obligation_is_harmless():
    await db.note_seeding_intervention("ipt", "nothing-like-this", "resume")
    assert await rows() == []


# --- metrics ------------------------------------------------------------------


async def test_an_obligation_that_cleared_after_a_repair_counts_as_averted():
    await db.record_seeding_obligations("ipt", owed("Some.Release-GRP"))
    await db.note_seeding_intervention("ipt", normalise_release("Some.Release-GRP"), "resume")
    await db.clear_seeding_obligations("ipt", set())

    metrics = await db.seeding_metrics(7)

    assert metrics["averted"] == 1
    assert metrics["self_cleared"] == 0
    assert metrics["cleared"] == 1
    assert metrics["outstanding"] == 0


async def test_an_obligation_that_cleared_on_its_own_is_not_averted():
    """The common case on a healthy library: the torrent was seeding all along
    and simply paid off its debt. Counting these would make the number a
    measure of how many torrents you have, not of what stallarr did."""
    await db.record_seeding_obligations("ipt", owed("Some.Release-GRP"))
    await db.clear_seeding_obligations("ipt", set())

    metrics = await db.seeding_metrics(7)

    assert metrics["averted"] == 0
    assert metrics["self_cleared"] == 1


async def test_outstanding_and_working_describe_right_now():
    await db.record_seeding_obligations("ipt", owed("One", "Two"))
    await db.note_seeding_intervention("ipt", normalise_release("One"), "resume")

    metrics = await db.seeding_metrics(7)

    assert metrics["outstanding"] == 2
    assert metrics["working"] == 1
    assert metrics["averted"] == 0  # still owed — nothing has been averted yet


async def test_an_old_success_falls_out_of_the_window():
    await db.record_seeding_obligations("ipt", owed("Old", "Recent"))
    for name in ("Old", "Recent"):
        await db.note_seeding_intervention("ipt", normalise_release(name), "resume")
    await db.clear_seeding_obligations("ipt", set())
    await backdate("ipt", "Old", cleared_days_ago=30)

    metrics = await db.seeding_metrics(7)

    assert metrics["averted"] == 1
    assert metrics["seen"] == 1


async def test_metrics_are_broken_down_per_tracker():
    await db.record_seeding_obligations("ipt", owed("One", "Two"))
    await db.record_seeding_obligations("sp", owed("Three"))
    await db.note_seeding_intervention("ipt", normalise_release("One"), "resume")
    await db.clear_seeding_obligations("ipt", {normalise_release("Two")})

    metrics = await db.seeding_metrics(7)
    by_tracker = {r["tracker"]: r for r in metrics["by_tracker"]}

    assert by_tracker["ipt"]["averted"] == 1
    assert by_tracker["ipt"]["outstanding"] == 1
    assert by_tracker["sp"]["outstanding"] == 1
    assert by_tracker["sp"]["averted"] == 0


async def test_recent_lists_what_was_averted_and_how():
    await db.record_seeding_obligations("ipt", owed("Some.Release-GRP"))
    await db.note_seeding_intervention("ipt", normalise_release("Some.Release-GRP"), "lift_share_limits")
    await db.clear_seeding_obligations("ipt", set())

    (entry,) = (await db.seeding_metrics(7))["recent"]

    assert entry["name"] == "Some.Release-GRP"
    assert entry["last_action"] == "lift_share_limits"


async def test_no_obligations_means_empty_metrics_not_an_error():
    metrics = await db.seeding_metrics(7)

    assert metrics["by_tracker"] == []
    assert metrics["outstanding"] == 0
    assert metrics["averted"] == 0


# --- pruning ------------------------------------------------------------------


async def test_pruning_drops_settled_obligations_but_never_open_ones():
    await db.record_seeding_obligations("ipt", owed("Old", "Still.Owed"))
    await db.clear_seeding_obligations("ipt", {normalise_release("Still.Owed")})
    await backdate("ipt", "Old", cleared_days_ago=60)

    await db.prune_seeding_obligations(30)

    assert [r["name"] for r in await rows()] == ["Still.Owed"]


# --- the service side ---------------------------------------------------------


def service() -> StallarrService:
    """A service with no client or trackers — enough for the bookkeeping paths."""
    return StallarrService(Config())


def requirement(name: str) -> SeedingRequirement:
    return SeedingRequirement(torrent_id="1", name=name, gap=1024)


async def test_a_tracker_that_did_not_answer_settles_nothing():
    """The failure that would quietly invent averted hit-and-runs: IPT times
    out, its whole list looks gone, every debt is marked settled — and any that
    stallarr had touched would be counted as saves."""
    svc = service()
    await svc._record_obligations({"ipt": {normalise_release("One"): requirement("One")}}, {"ipt"})

    await svc._record_obligations({}, set())  # the tracker errored: no answer

    (row,) = await rows()
    assert row["cleared_at"] == 0


async def test_a_tracker_answering_with_nothing_settles_everything():
    """The other half of that distinction: an empty list from a healthy tracker
    genuinely means the debts are paid."""
    svc = service()
    await svc._record_obligations({"ipt": {normalise_release("One"): requirement("One")}}, {"ipt"})

    await svc._record_obligations({}, {"ipt"})

    (row,) = await rows()
    assert row["cleared_at"] > 0


async def test_only_a_successful_real_action_is_credited():
    svc = service()
    await svc._record_obligations({"ipt": {normalise_release("One"): requirement("One")}}, {"ipt"})

    await svc._note_intervention("ipt", "One", ActionResult(ActionKind.RESUME, False, "failed"))
    await svc._note_intervention(
        "ipt", "One", ActionResult(ActionKind.RESUME, True, "would resume", dry_run=True)
    )
    await svc._note_intervention("ipt", "One", None)

    (row,) = await rows()
    assert row["actions"] == 0

    await svc._note_intervention("ipt", "One", ActionResult(ActionKind.RESUME, True, "resumed"))

    (row,) = await rows()
    assert row["actions"] == 1


async def test_the_dashboard_reports_the_metrics():
    svc = service()
    await svc._record_obligations({"ipt": {normalise_release("One"): requirement("One")}}, {"ipt"})

    payload = await svc.dashboard()

    assert payload["seeding"]["outstanding"] == 1
    # No plugin is configured for "ipt" here, so the id stands in for the name
    # rather than the row arriving with no label at all.
    assert payload["seeding"]["by_tracker"][0]["tracker_name"] == "ipt"
