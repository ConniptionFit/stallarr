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


def seeding_torrent(name: str, *, infohash: str, **overrides) -> "Torrent":
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
