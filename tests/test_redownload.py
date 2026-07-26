"""Re-acquiring torrents the tracker wants seeded but the client does not hold."""

from __future__ import annotations

import time

import pytest

from stallarr import db
from stallarr.config import RecoveryConfig
from stallarr.models import ActionKind, SeedingRequirement
from stallarr.recovery.redownload import attempt_key, redownload
from stallarr.trackers.base import Capability, TrackerError, TrackerPlugin

pytestmark = pytest.mark.asyncio


class FakePlugin(TrackerPlugin):
    id = "faketracker"
    name = "FakeTracker"

    def __init__(self, *, data=b"d8:announce", caps=(Capability.TORRENT_FILE_BY_ID,), raises=None):
        self._data = data
        self._caps = frozenset(caps)
        self._raises = raises
        self.fetched: list[str] = []

    @property
    def capabilities(self):
        return self._caps

    async def fetch_torrent_file_by_id(self, torrent_id: str):
        self.fetched.append(torrent_id)
        if self._raises:
            raise self._raises
        return self._data


class FakeClient:
    def __init__(self, fail=None):
        self.added: list[dict] = []
        self._fail = fail

    async def add_torrent_file(self, data, **kwargs):
        if self._fail:
            raise self._fail
        self.added.append({"data": data, **kwargs})


def requirement(torrent_id="4242", name="Some.Release-GRP"):
    return SeedingRequirement(torrent_id=torrent_id, name=name, gap=1024**3)


@pytest.fixture(autouse=True)
async def _db():
    """Attempt bookkeeping is the thing under test in half of these, and the
    temp database is shared across the module — so wipe it, not just create it."""
    import asyncio

    await db.init()
    await asyncio.to_thread(db._exec, "DELETE FROM attempts", ())


async def test_a_missing_torrent_is_fetched_and_added():
    client, plugin = FakeClient(), FakePlugin()
    cfg = RecoveryConfig(redownload_missing=True, redownload_category="music")

    result = await redownload(client, plugin, requirement(), cfg)

    assert result is not None and result.ok
    assert result.kind is ActionKind.REDOWNLOAD
    assert plugin.fetched == ["4242"]
    assert client.added[0]["category"] == "music"


async def test_the_add_is_always_checked():
    """The point of the step is that surviving files turn a download into an
    instant seed. Skipping the check would redownload data already on disk."""
    client, plugin = FakeClient(), FakePlugin()

    await redownload(client, plugin, requirement(), RecoveryConfig())

    assert client.added[0]["skip_checking"] is False
    assert client.added[0]["paused"] is False


async def test_a_tracker_that_cannot_download_by_id_is_skipped():
    client = FakeClient()
    plugin = FakePlugin(caps=(Capability.TORRENT_FILE,))

    assert await redownload(client, plugin, requirement(), RecoveryConfig()) is None
    assert plugin.fetched == []


async def test_dry_run_fetches_nothing():
    client, plugin = FakeClient(), FakePlugin()

    result = await redownload(client, plugin, requirement(), RecoveryConfig(dry_run=True))

    assert result is not None and result.dry_run
    assert plugin.fetched == [] and client.added == []


async def test_a_second_attempt_waits_for_the_cooldown():
    client, plugin = FakeClient(), FakePlugin()
    cfg = RecoveryConfig()

    assert await redownload(client, plugin, requirement(), cfg) is not None
    assert await redownload(client, plugin, requirement(), cfg) is None
    assert len(plugin.fetched) == 1


async def test_attempts_are_capped():
    """A site that lists a torrent it will not serve must not be asked forever."""
    client, plugin = FakeClient(), FakePlugin()
    cfg = RecoveryConfig(max_attempts_per_step=2, cooldown_minutes={"redownload": 0})

    for _ in range(5):
        await redownload(client, plugin, requirement(), cfg)

    assert len(plugin.fetched) == 2


async def test_a_failing_fetch_still_burns_an_attempt():
    """Otherwise a permanently broken download is retried on every scan."""
    client = FakeClient()
    plugin = FakePlugin(raises=TrackerError("faketracker: HTTP 404"))
    cfg = RecoveryConfig(cooldown_minutes={"redownload": 0})

    result = await redownload(client, plugin, requirement(), cfg)

    assert result is not None and not result.ok
    key = attempt_key("faketracker", "4242")
    attempts = await db.get_attempts(key, "hit_and_run_risk")
    assert attempts["redownload"]["count"] == 1


async def test_a_client_refusal_is_reported_not_raised():
    from stallarr.clients.base import ClientError

    client = FakeClient(fail=ClientError("torrent already in session"))
    plugin = FakePlugin()

    result = await redownload(client, plugin, requirement(), RecoveryConfig())

    assert result is not None and not result.ok
    assert "already in session" in result.message


async def test_an_empty_download_does_not_reach_the_client():
    client, plugin = FakeClient(), FakePlugin(data=b"")

    result = await redownload(client, plugin, requirement(), RecoveryConfig())

    assert result is not None and not result.ok
    assert client.added == []


async def test_attempt_keys_are_namespaced_away_from_infohashes():
    """The key lands in a column full of infohashes; a collision would let a
    real torrent's ladder and a re-download overwrite each other."""
    key = attempt_key("iptorrents", "2082626")
    assert key.startswith("redownload:")
    assert key == "redownload:iptorrents:2082626"


async def test_different_trackers_with_the_same_id_stay_separate():
    client = FakeClient()
    one, two = FakePlugin(), FakePlugin()
    two.id = "othertracker"
    cfg = RecoveryConfig()

    assert await redownload(client, one, requirement(), cfg) is not None
    assert await redownload(client, two, requirement(), cfg) is not None
    assert len(client.added) == 2


async def test_auto_tmm_is_passed_through():
    client, plugin = FakeClient(), FakePlugin()
    cfg = RecoveryConfig(redownload_use_auto_tmm=True, redownload_category="music")

    await redownload(client, plugin, requirement(), cfg)

    assert client.added[0]["use_auto_tmm"] is True


async def test_the_tag_is_dropped_when_tagging_is_off():
    client, plugin = FakeClient(), FakePlugin()

    await redownload(client, plugin, requirement(), RecoveryConfig(tagging_enabled=False))

    assert client.added[0]["tags"] == []


async def test_a_requirement_with_no_torrent_id_is_skipped():
    client, plugin = FakeClient(), FakePlugin()
    bare = SeedingRequirement(torrent_id="", name="Nameless")

    assert await redownload(client, plugin, bare, RecoveryConfig()) is None
    assert plugin.fetched == []


# --- the scan-level wiring ----------------------------------------------------


def service_with(cfg_overrides: dict, plugins: dict):
    """A StallarrService with only the parts `_redownload_missing` touches."""
    from stallarr.config import Config
    from stallarr.recovery import RecoveryEngine
    from stallarr.service import StallarrService
    from stallarr.trackers import TrackerRouter

    cfg = Config()
    for key, value in cfg_overrides.items():
        setattr(cfg.recovery, key, value)

    service = StallarrService(cfg)
    service.client = FakeClient()
    service.router = TrackerRouter(plugins)
    service.engine = RecoveryEngine(service.client, service.router, cfg.recovery)
    return service


async def test_the_per_scan_budget_is_enforced():
    """A tracker changing how it renders names would make every requirement look
    unmatched at once. The cap is what stops that becoming a bulk download."""
    from stallarr.service import ScanSummary

    plugin = FakePlugin()
    service = service_with(
        {"redownload_missing": True, "redownload_max_per_scan": 2}, {"faketracker": plugin}
    )
    unmatched = [("faketracker", requirement(str(i), f"Release.{i}")) for i in range(10)]

    recovered = await service._redownload_missing(unmatched, ScanSummary())

    assert len(plugin.fetched) == 2
    assert len(recovered) == 2


async def test_nothing_is_fetched_while_the_setting_is_off():
    from stallarr.service import ScanSummary

    plugin = FakePlugin()
    service = service_with({"redownload_missing": False}, {"faketracker": plugin})

    recovered = await service._redownload_missing(
        [("faketracker", requirement())], ScanSummary()
    )

    assert plugin.fetched == [] and recovered == set()


async def test_disabling_recovery_also_disables_re_downloads():
    """`recovery.enabled` is the master switch; a second switch that ignored it
    would make "auto-recovery off" untrue."""
    from stallarr.service import ScanSummary

    plugin = FakePlugin()
    service = service_with(
        {"enabled": False, "redownload_missing": True}, {"faketracker": plugin}
    )

    await service._redownload_missing([("faketracker", requirement())], ScanSummary())

    assert plugin.fetched == []


async def test_a_recovered_torrent_stops_being_reported_as_unfixable():
    from stallarr.service import ScanSummary

    plugin = FakePlugin()
    service = service_with({"redownload_missing": True}, {"faketracker": plugin})

    recovered = await service._redownload_missing(
        [("faketracker", requirement("77", "Gone.Release"))], ScanSummary()
    )

    assert ("faketracker", "77") in recovered


async def test_a_re_download_is_recorded_in_history():
    from stallarr.service import ScanSummary

    plugin = FakePlugin()
    service = service_with({"redownload_missing": True}, {"faketracker": plugin})

    await service._redownload_missing(
        [("faketracker", requirement("88", "Recorded.Release"))], ScanSummary()
    )

    # By key, not by recency: the whole suite writes history within the same
    # second, and `ts` alone cannot order rows inside one.
    rows = await db.get_history(infohash=attempt_key("faketracker", "88"))
    assert [r["action"] for r in rows] == ["redownload"]
    assert rows[0]["name"] == "Recorded.Release"
    assert rows[0]["tracker"] == "faketracker"


async def test_cooldown_expiry_allows_another_attempt():
    import asyncio

    client, plugin = FakeClient(), FakePlugin()
    cfg = RecoveryConfig(cooldown_minutes={"redownload": 1})

    await redownload(client, plugin, requirement(), cfg)
    # Backdate the recorded attempt rather than sleeping through the cooldown.
    await asyncio.to_thread(
        db._exec,
        "UPDATE attempts SET last_at = ? WHERE infohash = ?",
        (int(time.time()) - 600, attempt_key("faketracker", "4242")),
    )

    assert await redownload(client, plugin, requirement(), cfg) is not None
    assert len(plugin.fetched) == 2
