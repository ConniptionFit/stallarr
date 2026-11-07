"""The Seeding tab: every actively-seeding torrent, grouped by tracker.

Ratio, uploaded bytes, seeding time and tags come free off the local
`Torrent` — no tracker call needed. Freeleech is best-effort and opt-in per
tracker; "recently recovered" is inferred from `history` rather than a
persisted flag, since tags are cleared the moment a torrent actually
recovers. That inference is the thing most worth getting wrong, hence the
edge-case test below.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import make_torrent

from stallarr import db
from stallarr.config import Config, TrackerConfig
from stallarr.models import Torrent, TorrentState, TrackerEntry, TrackerFreeleechEntry
from stallarr.service import StallarrService
from stallarr.trackers import TrackerRouter
from stallarr.trackers.base import Capability, TrackerPlugin

pytestmark = pytest.mark.asyncio


class FakeClient:
    def __init__(self, torrents):
        self._torrents = torrents

    async def list_torrents(self):
        return self._torrents


class FakeFreeleechPlugin(TrackerPlugin):
    id = "faketracker"
    name = "FakeTracker"
    hosts = ("faketracker.org",)

    def __init__(self, entries=()):
        super().__init__(TrackerConfig(enabled=True))
        self._entries = list(entries)

    @property
    def capabilities(self):
        return frozenset({Capability.FREELEECH_STATUS})

    async def freeleech_status(self):
        return self._entries


def seeding_torrent(name: str, *, infohash: str, **overrides) -> Torrent:
    return make_torrent(
        name=name,
        infohash=infohash,
        trackers=[TrackerEntry(url="https://faketracker.org/announce/key", status=2)],
        **overrides,
    )


def service(client, plugin) -> StallarrService:
    svc = StallarrService(Config())
    svc.client = client
    svc.router = TrackerRouter({plugin.id: plugin})
    return svc


@pytest.fixture(autouse=True)
async def _db():
    await db.init()
    await asyncio.to_thread(db._exec, "DELETE FROM history", ())


async def test_paused_and_incomplete_torrents_are_excluded():
    torrents = [
        seeding_torrent("Paused-One", infohash="p" * 40, state=TorrentState.PAUSED),
        seeding_torrent("Downloading-One", infohash="d" * 40, progress=0.5),
        seeding_torrent("Active-One", infohash="s" * 40),
    ]
    svc = service(FakeClient(torrents), FakeFreeleechPlugin())

    view = await svc.seeding_view()

    names = {row["name"] for rows in view["trackers"].values() for row in rows}
    assert names == {"Active-One"}


async def test_torrents_are_grouped_by_owning_tracker():
    torrents = [seeding_torrent("Active-One", infohash="s" * 40)]
    svc = service(FakeClient(torrents), FakeFreeleechPlugin())

    view = await svc.seeding_view()

    assert set(view["trackers"]) == {"faketracker"}
    assert view["trackers"]["faketracker"][0]["tracker_name"] == "FakeTracker"


async def test_freeleech_is_matched_by_normalised_release_name():
    torrents = [seeding_torrent("Some.Release-GRP", infohash="f" * 40)]
    plugin = FakeFreeleechPlugin(
        entries=[TrackerFreeleechEntry(name="Some.Release-GRP", freeleech=True)]
    )
    svc = service(FakeClient(torrents), plugin)

    view = await svc.seeding_view()

    assert view["trackers"]["faketracker"][0]["freeleech"] is True


async def test_a_torrent_the_tracker_never_mentioned_reports_unknown_freeleech():
    """`None`, not `False` — a tracker without the capability (or that just
    didn't list this torrent) must not read as "confirmed not freeleech"."""
    torrents = [seeding_torrent("Untracked-Release", infohash="u" * 40)]
    svc = service(FakeClient(torrents), FakeFreeleechPlugin(entries=[]))

    view = await svc.seeding_view()

    assert view["trackers"]["faketracker"][0]["freeleech"] is None


async def test_a_recent_successful_repair_is_flagged_recently_recovered():
    infohash = "r" * 40
    await db.add_history(infohash=infohash, name="x", action="reannounce", ok=True)
    torrents = [seeding_torrent("Recovered-Release", infohash=infohash)]
    svc = service(FakeClient(torrents), FakeFreeleechPlugin())

    view = await svc.seeding_view()

    assert view["trackers"]["faketracker"][0]["recently_recovered"] is True


async def test_a_torrent_still_carrying_an_active_tag_is_not_recently_recovered():
    """A successful history row from earlier in the same ladder does not mean
    the torrent is done — it might still be mid-repair, tagged
    `stallarr:needs-help` or `stallarr:repairing`. Only the *absence* of both
    means recovery actually finished."""
    infohash = "t" * 40
    await db.add_history(infohash=infohash, name="x", action="reannounce", ok=True)
    torrents = [
        seeding_torrent("Still-Working-Release", infohash=infohash, tags=["stallarr:needs-help"])
    ]
    svc = service(FakeClient(torrents), FakeFreeleechPlugin())

    view = await svc.seeding_view()

    assert view["trackers"]["faketracker"][0]["recently_recovered"] is False


async def test_no_history_means_not_recently_recovered():
    torrents = [seeding_torrent("Never-Sick-Release", infohash="n" * 40)]
    svc = service(FakeClient(torrents), FakeFreeleechPlugin())

    view = await svc.seeding_view()

    assert view["trackers"]["faketracker"][0]["recently_recovered"] is False


class CountingFreeleechPlugin(FakeFreeleechPlugin):
    def __init__(self, entries=()):
        super().__init__(entries)
        self.calls = 0

    async def freeleech_status(self):
        self.calls += 1
        return self._entries


async def test_freeleech_lookups_are_cached_across_refreshes():
    """The Seeding tab refreshes every ten seconds. Each refresh must not be a
    fresh call to every tracker — that is exactly the hammering private sites
    ban for."""
    torrents = [seeding_torrent("Some.Release-GRP", infohash="c" * 40)]
    plugin = CountingFreeleechPlugin(
        entries=[TrackerFreeleechEntry(name="Some.Release-GRP", freeleech=True)]
    )
    svc = service(FakeClient(torrents), plugin)

    for _ in range(3):
        view = await svc.seeding_view()

    assert plugin.calls == 1
    assert view["trackers"]["faketracker"][0]["freeleech"] is True


async def test_a_rebuilt_plugin_is_asked_again():
    """A reload builds new plugin objects (new credentials, say); the cache
    must not keep serving what the old ones said."""
    torrents = [seeding_torrent("Some.Release-GRP", infohash="c" * 40)]
    first = CountingFreeleechPlugin()
    svc = service(FakeClient(torrents), first)
    await svc.seeding_view()

    second = CountingFreeleechPlugin()
    svc.router = TrackerRouter({second.id: second})
    await svc.seeding_view()

    assert (first.calls, second.calls) == (1, 1)


class StatsPlugin(TrackerPlugin):
    id = "statstracker"
    name = "StatsTracker"
    hosts = ("statstracker.org",)

    def __init__(self, fail=False):
        super().__init__(TrackerConfig(enabled=True))
        self.calls = 0
        self.fail = fail

    @property
    def capabilities(self):
        return frozenset({Capability.USER_STATS})

    async def user_stats(self):
        from stallarr.models import UserStats
        from stallarr.trackers.base import TrackerError

        self.calls += 1
        if self.fail:
            raise TrackerError("site is down")
        return UserStats(uploaded=2048, downloaded=1024, ratio=2.0)


async def test_account_stats_are_cached_across_refreshes():
    plugin = StatsPlugin()
    svc = service(FakeClient([]), plugin)

    for _ in range(3):
        stats = await svc.tracker_stats()

    assert plugin.calls == 1
    assert stats["statstracker"]["ratio"] == 2.0


async def test_a_failing_site_is_not_retried_on_every_refresh():
    plugin = StatsPlugin(fail=True)
    svc = service(FakeClient([]), plugin)

    for _ in range(3):
        stats = await svc.tracker_stats()

    assert plugin.calls == 1
    assert stats["statstracker"]["error"] == "site is down"
