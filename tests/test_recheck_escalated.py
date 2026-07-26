"""Re-checking torrents stallarr has already given up on.

Escalation is normally terminal for tracker calls: `_already_escalated` stops
enrichment so a dead torrent cannot cost hundreds of lookups a day. That saves
the calls but hides one thing worth knowing — private sites delete torrents that
have been superseded (a single episode once the season pack lands), and a
torrent deleted *after* we gave up on it keeps its stale diagnosis forever. The
re-check exists to turn that into `unregistered`, the one verdict that means the
torrent can be removed.

Everything here is about keeping the cost bounded while still asking.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from stallarr import db
from stallarr.config import Config
from stallarr.models import Torrent, TorrentState
from stallarr.service import StallarrService

pytestmark = pytest.mark.asyncio

ESCALATED = "stallarr:needs-help"


@pytest.fixture(autouse=True)
async def _db():
    await db.init()
    await asyncio.to_thread(db._exec, "DELETE FROM tracker_checks", ())


def torrent(name: str, *, tags=(ESCALATED,), infohash: str | None = None) -> Torrent:
    return Torrent(
        infohash=infohash or f"{name:0>40}"[:40],
        name=name,
        state=TorrentState.STALLED_UP,
        raw_state="stalledUP",
        progress=1.0,
        size=1,
        tags=list(tags),
    )


def service(**rules) -> StallarrService:
    cfg = Config()
    for key, value in rules.items():
        setattr(cfg.rules, key, value)
    return StallarrService(cfg)


async def checked_days_ago(infohash: str, days: float) -> None:
    await asyncio.to_thread(
        db._exec,
        "INSERT INTO tracker_checks (infohash, checked_at) VALUES (?, ?) "
        "ON CONFLICT(infohash) DO UPDATE SET checked_at = excluded.checked_at",
        (infohash, int(time.time() - days * 86400)),
    )


# --- who gets picked ----------------------------------------------------------


async def test_an_escalated_torrent_never_checked_is_due():
    svc = service()

    due = await svc._recheck_candidates([torrent("one")], set())

    assert due == {torrent("one").infohash}


async def test_a_torrent_checked_recently_is_left_alone():
    one = torrent("one")
    await checked_days_ago(one.infohash, 1)

    assert await service(recheck_escalated_days=7)._recheck_candidates([one], set()) == set()


async def test_a_torrent_checked_long_enough_ago_comes_back_round():
    one = torrent("one")
    await checked_days_ago(one.infohash, 8)

    assert await service(recheck_escalated_days=7)._recheck_candidates([one], set()) == {one.infohash}


async def test_a_torrent_that_was_not_escalated_is_not_a_candidate():
    """It is enriched every scan already — picking it here would spend the
    budget re-asking a question that was just answered."""
    svc = service()

    assert await svc._recheck_candidates([torrent("one", tags=[])], set()) == set()


async def test_an_ignored_torrent_is_not_re_checked():
    one = torrent("one")

    assert await service()._recheck_candidates([one], {one.infohash}) == set()


# --- keeping the cost down ----------------------------------------------------


async def test_the_budget_caps_how_many_are_checked_per_scan():
    """The failure this prevents: a restart makes every escalated torrent look
    due at once, and a library with hundreds of them fires hundreds of lookups
    into a private tracker in one scan."""
    torrents = [torrent(f"t{i:02d}") for i in range(20)]

    due = await service(recheck_escalated_max_per_scan=3)._recheck_candidates(torrents, set())

    assert len(due) == 3


async def test_the_least_recently_checked_go_first():
    """Otherwise the budget is spent on whatever the client listed first, and
    the same few torrents get re-checked forever while others never are."""
    stale, fresh, never = torrent("stale"), torrent("fresh"), torrent("never")
    await checked_days_ago(stale.infohash, 30)
    await checked_days_ago(fresh.infohash, 8)

    due = await service(recheck_escalated_max_per_scan=2)._recheck_candidates(
        [fresh, stale, never], set()
    )

    assert due == {never.infohash, stale.infohash}


async def test_the_re_check_can_be_switched_off():
    assert await service(recheck_escalated_days=0)._recheck_candidates([torrent("one")], set()) == set()


async def test_a_zero_budget_switches_it_off_too():
    svc = service(recheck_escalated_max_per_scan=0)

    assert await svc._recheck_candidates([torrent("one")], set()) == set()


# --- what counts as "checked" -------------------------------------------------


async def test_only_a_real_answer_marks_a_torrent_checked():
    """A tracker that times out has told us nothing. Recording that as a check
    would push the torrent to the back of the queue and, with a long enough
    outage, mean it is never really looked up again."""
    from stallarr.config import RuleConfig
    from stallarr.diagnosis import enrich
    from stallarr.models import Diagnosis

    diagnosis = Diagnosis(torrent=torrent("one"))

    # No plugin at all is the simplest form of "no answer available".
    assert await enrich(diagnosis, None, RuleConfig()) is False


async def test_a_tracker_answer_is_recorded_and_then_suppresses_the_re_check():
    one = torrent("one")
    svc = service(recheck_escalated_days=7)
    assert await svc._recheck_candidates([one], set()) == {one.infohash}

    await db.mark_verified(one.infohash)

    assert await svc._recheck_candidates([one], set()) == set()


async def test_stale_check_records_are_pruned():
    one, two = torrent("one"), torrent("two")
    await checked_days_ago(one.infohash, 90)
    await checked_days_ago(two.infohash, 1)

    await db.prune_tracker_checks(30)

    assert set(await db.verified_map()) == {two.infohash}
