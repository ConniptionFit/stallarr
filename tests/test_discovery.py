"""The ratio builder: browsing, scoring, and the guards in front of a grab.

The guards get most of the tests. A bad score costs a poor pick; a guard that
lets something through puts data on someone's disk and a seeding obligation on
their account.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from conftest import make_torrent

from stallarr import db
from stallarr.config import Config, DiscoveryConfig, TrackerConfig
from stallarr.discovery import grab as grab_module
from stallarr.discovery.grab import grab
from stallarr.discovery.score import GB, exclusion, rank, score, strategy_for
from stallarr.models import ActionKind, TrackerListing
from stallarr.service import StallarrService
from stallarr.trackers import TrackerRouter
from stallarr.trackers.base import Capability, TrackerPlugin
from stallarr.trackers.unit3d import Unit3dPlugin
from stallarr.utils import bencode

NOW = 1_800_000_000
DAY = 86400


def listing(**overrides) -> TrackerListing:
    defaults = {
        "tracker": "faketracker",
        "torrent_id": "101",
        "name": "Some.Show.S01.1080p.WEB-DL-GRP",
        "size": 10 * GB,
        "seeders": 4,
        "leechers": 6,
        "times_completed": 120,
        "created_at": NOW - 60 * DAY,
        "free_pct": 100,
    }
    defaults.update(overrides)
    return TrackerListing(**defaults)


def torrent_bytes(name: str = "x") -> bytes:
    return bencode({b"info": {b"name": name.encode(), b"length": 1, b"piece length": 16384, b"pieces": b"0" * 20}})


@pytest.fixture(autouse=True)
async def _db():
    await db.init()
    await asyncio.to_thread(db._exec, "DELETE FROM discovered", ())
    await asyncio.to_thread(db._exec, "DELETE FROM history", ())


# --- UNIT3D browse --------------------------------------------------------------


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


async def test_unit3d_browse_parses_the_resource_shape():
    plugin = Unit3dPlugin(TrackerConfig(enabled=True, api_key="tok"))
    plugin.id = "site"
    seen = {}

    async def fake_get(url, **kwargs):
        seen["params"] = kwargs.get("params")
        return _Resp(
            {
                "data": [
                    {
                        "id": 7,
                        "attributes": {
                            "name": "A.Movie.2020.1080p-GRP",
                            "size": 4 * GB,
                            "seeders": 3,
                            "leechers": 9,
                            "times_completed": 40,
                            "created_at": "2025-01-01T00:00:00.000000Z",
                            "freeleech": "100%",
                            "double_upload": True,
                            "info_hash": "AB" * 20,
                            "download_link": "https://site/torrent/download/7.secret",
                        },
                    }
                ]
            }
        )

    plugin.get = fake_get
    rows = await plugin.browse()

    assert seen["params"]["sortField"] == "leechers"
    row = rows[0]
    assert (row.torrent_id, row.seeders, row.leechers, row.times_completed) == ("7", 3, 9, 40)
    assert row.free_pct == 100
    assert row.double_upload is True
    assert row.infohash == "ab" * 20
    assert row.created_at == 1735689600
    assert row.download_url.endswith("7.secret")


async def test_unit3d_browse_params_merge_from_options():
    plugin = Unit3dPlugin(
        TrackerConfig(enabled=True, api_key="tok", options={"browse_params": {"free": 100}})
    )
    seen = {}

    async def fake_get(url, **kwargs):
        seen.update(kwargs["params"])
        return _Resp({"data": []})

    plugin.get = fake_get
    await plugin.browse()
    assert seen["free"] == 100 and seen["alive"] == 1


async def test_missing_freeleech_reads_as_unknown_not_zero():
    plugin = Unit3dPlugin(TrackerConfig(enabled=True, api_key="tok"))

    async def fake_get(url, **kwargs):
        return _Resp({"data": [{"id": 1, "name": "X-GRP"}]})

    plugin.get = fake_get
    (row,) = await plugin.browse()
    assert row.free_pct is None and row.leechers is None


def test_unit3d_can_browse_and_fetch_by_id_with_a_passkey():
    plugin = Unit3dPlugin(TrackerConfig(enabled=True, api_key="tok", passkey="pk"))
    assert Capability.BROWSE in plugin.capabilities
    assert Capability.TORRENT_FILE_BY_ID in plugin.capabilities
    assert Capability.TORRENT_FILE_BY_ID not in Unit3dPlugin(TrackerConfig(api_key="t")).capabilities


# --- scoring --------------------------------------------------------------------


def test_strategy_bands():
    assert strategy_for(1) == "burst"
    assert strategy_for(10) == "cooling"
    assert strategy_for(90) == "long-tail"
    assert strategy_for(None) == "unknown"


def test_more_demand_scores_higher():
    cfg = DiscoveryConfig()
    low = score(listing(leechers=1), cfg, NOW)
    high = score(listing(leechers=20), cfg, NOW)
    assert high.score > low.score


def test_freeleech_beats_paid_and_double_upload_doubles():
    cfg = DiscoveryConfig()
    free = score(listing(free_pct=100), cfg, NOW)
    paid = score(listing(free_pct=0), cfg, NOW)
    doubled = score(listing(double_upload=True), cfg, NOW)
    assert free.score == pytest.approx(paid.score * 2, abs=0.01)
    assert doubled.score == pytest.approx(free.score * 2, abs=0.01)


def test_crowded_and_seedbox_swarms_are_penalised():
    cfg = DiscoveryConfig()
    base = score(listing(seeders=40, leechers=40), cfg, NOW)
    crowded = score(listing(seeders=200, leechers=200), cfg, NOW)
    seedbox = score(listing(seeders=40, leechers=40, highspeed=True), cfg, NOW)
    assert crowded.parts["competition"] < 1
    assert seedbox.score == pytest.approx(base.score / 2, abs=0.01)


def test_long_tail_outranks_the_same_swarm_mid_decline():
    cfg = DiscoveryConfig()
    old = score(listing(created_at=NOW - 60 * DAY, times_completed=60), cfg, NOW)
    mid = score(listing(created_at=NOW - 15 * DAY, times_completed=15), cfg, NOW)
    # Same snatches/day and demand; only the age curve differs.
    assert old.parts["snatches_per_day"] == mid.parts["snatches_per_day"]
    assert old.score > mid.score


def test_a_brand_new_torrent_does_not_read_as_thousands_of_snatches_a_day():
    s = score(listing(created_at=NOW - 3600, times_completed=50), DiscoveryConfig(), NOW)
    assert s.parts["snatches_per_day"] == 50


# --- exclusion ------------------------------------------------------------------


def _excl(lst, cfg=None, **kw):
    kw.setdefault("held_hashes", set())
    kw.setdefault("held_names", set())
    kw.setdefault("grabbed", set())
    return exclusion(lst, cfg or DiscoveryConfig(), **kw)


def test_exclusions():
    assert _excl(listing()) is None
    assert _excl(listing(free_pct=50)) == "not freeleech"
    assert _excl(listing(free_pct=None)) == "not freeleech"
    assert _excl(listing(free_pct=0), DiscoveryConfig(freeleech_only=False)) is None
    assert _excl(listing(seeders=0)) == "no seeders"
    assert _excl(listing(size=None)) == "size unknown"
    assert _excl(listing(size=500 * GB)) == "too large"
    assert _excl(listing(), grabbed={("faketracker", "101")}) == "already grabbed"
    assert _excl(listing(infohash="c" * 40), held_hashes={"c" * 40}) == "already in the client"


def test_held_by_name_is_excluded():
    from stallarr.utils import normalise_release

    name = "Some.Show.S01.1080p.WEB-DL-GRP"
    assert _excl(listing(name=name), held_names={normalise_release(name)}) == "already in the client"


def test_rank_sorts_caps_and_counts_exclusions():
    cfg = DiscoveryConfig(max_candidates=2)
    rows = [
        listing(torrent_id="1", leechers=1),
        listing(torrent_id="2", leechers=30),
        listing(torrent_id="3", leechers=10),
        listing(torrent_id="4", free_pct=0),
        listing(torrent_id="2", leechers=30),  # duplicate row
    ]
    top, excluded = rank(rows, cfg, held_hashes=set(), held_names=set(), grabbed=set(), now=NOW)
    assert [s.listing.torrent_id for s in top] == ["2", "3"]
    assert excluded == {"not freeleech": 1}


def test_candidates_never_expose_the_download_link():
    s = score(listing(download_url="https://site/dl/1.secretkey"), DiscoveryConfig(), NOW)
    assert "download_url" not in s.as_dict()
    assert "secretkey" not in repr(s.as_dict())


# --- grabbing -------------------------------------------------------------------


class GrabClient:
    def __init__(self, torrents=(), free=500 * GB):
        self.torrents = list(torrents)
        self.free = free
        self.calls: list[tuple] = []
        self.added = None

    async def list_torrents(self):
        return self.torrents

    async def free_space(self):
        return self.free

    async def get_torrent(self, infohash):
        if self.added == infohash:
            return make_torrent(infohash=infohash)
        return next((t for t in self.torrents if t.infohash == infohash), None)

    async def add_torrent_file(self, data, **kwargs):
        from stallarr.utils import infohash_from_torrent

        self.added = infohash_from_torrent(data)
        self.calls.append(("add", kwargs))

    async def set_share_limits(self, infohash, **kwargs):
        self.calls.append(("limits", infohash, kwargs))


class GrabPlugin(TrackerPlugin):
    id = "faketracker"
    name = "FakeTracker"

    def __init__(self, data=None):
        super().__init__(TrackerConfig(enabled=True))
        self.data = data if data is not None else torrent_bytes()
        self.fetched = []

    @property
    def capabilities(self):
        return frozenset({Capability.BROWSE, Capability.TORRENT_FILE_BY_ID})

    async def fetch_torrent_file_by_id(self, torrent_id):
        self.fetched.append(torrent_id)
        return self.data


def grab_cfg(**discovery) -> Config:
    cfg = Config()
    cfg.discovery = DiscoveryConfig(max_disk_gb=100, min_free_gb=10, **discovery)
    return cfg


async def test_a_grab_is_added_checked_categorised_tagged_and_unlimited(monkeypatch):
    monkeypatch.setattr(grab_module, "_APPEAR_DELAY", 0)
    client, plugin = GrabClient(), GrabPlugin()

    result = await grab(client, plugin, score(listing(), DiscoveryConfig(), NOW), grab_cfg())

    assert result.ok and result.kind is ActionKind.GRAB
    kind, kwargs = client.calls[0]
    assert kind == "add"
    assert kwargs["category"] == "stallarr-ratio"
    assert kwargs["skip_checking"] is False
    assert "stallarr-keep" in kwargs["tags"]
    # Not stallarr:-prefixed: that prefix makes a user pause read as ours.
    assert not any(t.startswith("stallarr:") for t in kwargs["tags"])
    assert client.calls[1][2] == {"ratio": -1, "seeding_minutes": -1, "inactive_minutes": -1}
    assert await db.grabbed_keys() == {("faketracker", "101")}
    history = await db.get_history(limit=5)
    assert history[0]["action"] == "grab"


async def test_no_disk_budget_refuses_every_grab():
    client, plugin = GrabClient(), GrabPlugin()
    cfg = Config()  # max_disk_gb defaults to 0
    result = await grab(client, plugin, score(listing(), cfg.discovery, NOW), cfg)
    assert not result.ok and "disk budget" in result.message
    assert plugin.fetched == [] and client.calls == []


async def test_the_disk_budget_counts_the_category():
    held = make_torrent(infohash="e" * 40, category="stallarr-ratio", size=95 * GB)
    client, plugin = GrabClient([held]), GrabPlugin()
    result = await grab(client, plugin, score(listing(), DiscoveryConfig(), NOW), grab_cfg())
    assert not result.ok and "over the disk budget" in result.message
    assert plugin.fetched == []


async def test_the_free_space_floor_and_unknown_free_space_refuse():
    plugin = GrabPlugin()
    low = await grab(GrabClient(free=15 * GB), plugin, score(listing(), DiscoveryConfig(), NOW), grab_cfg())
    unknown = await grab(GrabClient(free=None), plugin, score(listing(), DiscoveryConfig(), NOW), grab_cfg())
    assert "floor" in low.message
    assert "free space" in unknown.message
    assert plugin.fetched == []


async def test_the_daily_cap_is_per_tracker():
    for i in range(2):
        await db.record_grab(
            tracker="faketracker", torrent_id=f"old{i}", infohash="", name="", size=0,
            score=0, strategy="", free_pct=100,
        )
    client, plugin = GrabClient(), GrabPlugin()
    result = await grab(
        client, plugin, score(listing(), DiscoveryConfig(), NOW), grab_cfg(max_grabs_per_day=2)
    )
    assert not result.ok and "daily cap" in result.message


async def test_paid_grab_refused_under_freeleech_only():
    client, plugin = GrabClient(), GrabPlugin()
    result = await grab(client, plugin, score(listing(free_pct=0), DiscoveryConfig(), NOW), grab_cfg())
    assert not result.ok and "freeleech" in result.message


async def test_dry_run_downloads_nothing_and_records_no_grab():
    client, plugin = GrabClient(), GrabPlugin()
    cfg = grab_cfg()
    cfg.recovery.dry_run = True
    result = await grab(client, plugin, score(listing(), DiscoveryConfig(), NOW), cfg)
    assert result.ok and result.dry_run
    assert plugin.fetched == [] and client.calls == []
    assert await db.grabbed_keys() == set()


async def test_a_torrent_that_does_not_match_the_listing_is_refused():
    client, plugin = GrabClient(), GrabPlugin()
    result = await grab(
        client, plugin, score(listing(infohash="f" * 40), DiscoveryConfig(), NOW), grab_cfg()
    )
    assert not result.ok and "does not match" in result.message
    assert client.calls == []


async def test_a_non_torrent_download_is_refused():
    client, plugin = GrabClient(), GrabPlugin(data=b"<html>login</html>")
    result = await grab(client, plugin, score(listing(), DiscoveryConfig(), NOW), grab_cfg())
    assert not result.ok and client.calls == []


# --- the service wiring ----------------------------------------------------------


class BrowsePlugin(GrabPlugin):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.browses = 0

    async def browse(self):
        self.browses += 1
        return self.rows


def discovery_service(plugin, client) -> StallarrService:
    cfg = grab_cfg(enabled=True, trackers=[plugin.id])
    svc = StallarrService(cfg)
    svc.client = client
    svc.router = TrackerRouter({plugin.id: plugin})
    return svc


async def test_run_ranks_and_skips_what_the_client_holds():
    now = time.time()
    rows = [
        listing(torrent_id="1", name="Held.Release-GRP", created_at=now - 60 * DAY),
        listing(torrent_id="2", name="New.Release-GRP", created_at=now - 60 * DAY),
    ]
    held = make_torrent(infohash="1" * 40, name="Held.Release-GRP")
    svc = discovery_service(BrowsePlugin(rows), GrabClient([held]))

    await svc.discovery.run()

    assert [c.listing.torrent_id for c in svc.discovery.candidates] == ["2"]
    assert svc.discovery.excluded == {"already in the client": 1}


async def test_browsing_the_same_tracker_is_rate_floored():
    plugin = BrowsePlugin([listing()])
    svc = discovery_service(plugin, GrabClient())
    await svc.discovery.run()
    await svc.discovery.run()
    assert plugin.browses == 1
    assert svc.discovery.refresh_allowed_in() > 0


async def test_grab_only_accepts_current_candidates(monkeypatch):
    monkeypatch.setattr(grab_module, "_APPEAR_DELAY", 0)
    plugin = BrowsePlugin([listing(created_at=time.time() - 60 * DAY)])
    svc = discovery_service(plugin, GrabClient())
    await svc.discovery.run()

    missing = await svc.discovery.grab("faketracker", "nope")
    ok = await svc.discovery.grab("faketracker", "101")

    assert missing["ok"] is False
    assert ok["ok"] is True
    assert svc.discovery.candidates == []


async def test_an_unknown_tracker_reports_rather_than_raises():
    plugin = BrowsePlugin([])
    svc = discovery_service(plugin, GrabClient())
    svc.cfg.discovery.trackers = ["notconfigured"]
    await svc.discovery.run()
    assert svc.discovery.status["notconfigured"]["ok"] is False
