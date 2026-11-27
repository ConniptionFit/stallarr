"""Downloads that will never finish: re-grab, and remove only when nothing is owed.

Most of these tests are about refusing. A removal cannot be undone, and the
two ways it goes wrong are both expensive: removing a torrent that owed seeding
manufactures a hit-and-run, and calling a whole queue dead because a VPN or a
tracker dropped throws away good downloads.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import httpx
import pytest
from conftest import make_torrent

from stallarr import db
from stallarr.config import ArrConfig, Config, RecoveryConfig, RuleConfig, TrackerConfig
from stallarr.diagnosis import diagnose_local
from stallarr.integrations.arr import ArrClient, ArrError, ArrRouter, QueueMatch, kind_of
from stallarr.models import (
    ActionKind,
    Ailment,
    Diagnosis,
    Finding,
    TorrentState,
    TrackerEntry,
)
from stallarr.recovery.stalled import dead_for, handle_dead, hold_reason, keep_reason

NOW = time.time()
GB = 1024**3


@pytest.fixture(autouse=True)
async def _db():
    await db.init()
    for table in ("history", "attempts", "deletions"):
        await asyncio.to_thread(db._exec, f"DELETE FROM {table}", ())


class FakeClient:
    def __init__(self, connection: str | None = "connected", fail: bool = False):
        self.connection = connection
        self.fail = fail
        self.deleted: list[tuple[str, bool]] = []
        self.exported: list[str] = []

    async def connection_status(self) -> str | None:
        return self.connection

    async def delete(self, infohash: str, delete_files: bool = False) -> None:
        if self.fail:
            from stallarr.clients.base import ClientError

            raise ClientError("qBittorrent said no")
        self.deleted.append((infohash, delete_files))

    async def export_torrent(self, infohash: str) -> bytes:
        self.exported.append(infohash)
        return b"d8:announce4:htted"


class FakeArrs:
    """Stands in for ArrRouter: a fixed queue, recording blocklist calls."""

    def __init__(self, queue: dict[str, QueueMatch] | None = None, *, failed=(), error=False):
        self.queue = queue or {}
        self.failed = list(failed)
        self.error = error
        self.blocklisted: list[QueueMatch] = []

    def __bool__(self) -> bool:
        return True

    async def queues(self):
        return dict(self.queue), list(self.failed)

    async def blocklist_and_search(self, match: QueueMatch) -> None:
        if self.error:
            raise ArrError("sonarr: API key rejected")
        self.blocklisted.append(match)
        # Like the real thing: once blocklisted, the app stops tracking it.
        self.queue = {k: v for k, v in self.queue.items() if v is not match}


def dead(
    *,
    downloaded: int = 0,
    seeds: int = 0,
    tracker_status: int = 2,
    idle_hours: float = 30,
    tracker: str = "darkpeers",
    tags: list[str] | None = None,
    size: int = GB,
    extra: list[Finding] | None = None,
) -> Diagnosis:
    """A download that has moved nothing for `idle_hours`, on a working tracker."""
    torrent = make_torrent(
        infohash=uuid.uuid4().hex + "0" * 8,
        name="DIG.2026.S01E03.1080p.AMZN.WEB-DL-RAWR",
        state=TorrentState.STALLED_DL,
        raw_state="stalledDL",
        progress=downloaded / size if size else 0.0,
        size=size,
        amount_left=max(size - downloaded, 0),
        downloaded=downloaded,
        uploaded=0,
        dlspeed=0,
        upspeed=0,
        num_leechs=0,
        num_seeds=0,
        added_on=int(NOW - idle_hours * 3600 - 60),
        last_activity=0,
        tags=tags or [],
        trackers=[
            TrackerEntry(
                url="https://darkpeers.org/announce/key",
                status=tracker_status,
                num_seeds=seeds,
                num_leeches=2,
            )
        ],
    )
    findings = [Finding(Ailment.STALLED_DOWNLOAD, "no data"), *(extra or [])]
    return Diagnosis(torrent=torrent, findings=findings, tracker_id=tracker)


def recovery(**overrides) -> RecoveryConfig:
    base = {"stalled_regrab": True, "stalled_purge": True, "delete_export_dir": "test-graveyard"}
    base.update(overrides)
    return RecoveryConfig(**base)


# --- detection ------------------------------------------------------------------


def test_a_download_that_never_moved_a_byte_is_flagged_stalled():
    """qBittorrent has no activity stamp for a torrent that never connected to
    anyone. Reading that as "idle for 0s" hid the deadest download of all."""
    torrent = make_torrent(
        state=TorrentState.STALLED_DL,
        progress=0.0,
        dlspeed=0,
        upspeed=0,
        last_activity=0,
        added_on=int(NOW - 3 * 3600),
    )
    diagnosis = diagnose_local(torrent, RuleConfig(), now=NOW)
    assert Ailment.STALLED_DOWNLOAD in diagnosis.ailments


def test_dead_needs_the_full_quiet_period():
    cfg = RecoveryConfig(stalled_dead_hours=12)
    assert dead_for(dead(idle_hours=11), cfg, NOW) is None
    assert dead_for(dead(idle_hours=13), cfg, NOW) is not None


def test_unregistered_downloads_are_left_to_the_reaper():
    diagnosis = dead(extra=[Finding(Ailment.UNREGISTERED, "gone")])
    assert dead_for(diagnosis, RecoveryConfig(), NOW) is None


# --- is the swarm really empty? -------------------------------------------------


def test_a_dead_tracker_cannot_vouch_for_an_empty_swarm():
    assert "no tracker is answering" in hold_reason(dead(tracker_status=4))


def test_seeders_we_cannot_reach_point_at_our_connection_not_the_release():
    assert "seeder" in hold_reason(dead(seeds=3))


def test_a_working_tracker_reporting_no_seeders_is_a_dead_swarm():
    assert hold_reason(dead()) is None


# --- does it owe anything? ------------------------------------------------------


def test_nothing_downloaded_owes_nothing():
    assert keep_reason(dead(), None, RecoveryConfig()) is None


def test_any_download_is_kept_under_the_strict_default():
    reason = keep_reason(dead(downloaded=50 * 1024**2), None, RecoveryConfig())
    assert "over the 0% this tracker allows" in reason


def test_a_tracker_buffer_allows_a_small_download():
    tracker = TrackerConfig(hnr_free_download_pct=10)
    assert keep_reason(dead(downloaded=50 * 1024**2), tracker, RecoveryConfig()) is None
    assert keep_reason(dead(downloaded=GB // 2), tracker, RecoveryConfig()) is not None


def test_a_tracker_level_zero_is_stricter_than_the_global_setting():
    tracker = TrackerConfig(hnr_free_download_pct=0)
    cfg = RecoveryConfig(stalled_purge_max_downloaded_pct=10)
    assert keep_reason(dead(downloaded=1024**2), tracker, cfg) is not None


def test_blank_tracker_buffer_means_inherit_not_zero():
    assert TrackerConfig(hnr_free_download_pct="").hnr_free_download_pct is None


def test_a_tracker_can_veto_removal():
    assert "switched off" in keep_reason(dead(), TrackerConfig(purge_stalled=False), RecoveryConfig())


def test_a_hit_and_run_flag_always_keeps_it():
    diagnosis = dead(extra=[Finding(Ailment.HIT_AND_RUN_RISK, "owed")])
    assert "hit-and-run" in keep_reason(diagnosis, None, RecoveryConfig())


def test_the_keep_tag_is_honoured():
    assert keep_reason(dead(tags=["stallarr-keep"]), None, RecoveryConfig()) is not None


def test_bytes_against_an_unknown_size_are_not_guessed_at():
    assert keep_reason(dead(downloaded=1024, size=0), None, RecoveryConfig()) is not None


# --- the pass as a whole --------------------------------------------------------


async def test_switched_off_reports_and_touches_nothing():
    client = FakeClient()
    out = await handle_dead(client, None, [dead()], RecoveryConfig(), {}, NOW)
    assert client.deleted == [] and out.results == []
    assert "switched off" in out.report[0]["status"]


async def test_a_dropped_connection_holds_everything():
    client, arrs = FakeClient(connection="disconnected"), FakeArrs()
    out = await handle_dead(client, arrs, [dead(), dead()], recovery(), {}, NOW)
    assert client.deleted == [] and arrs.blocklisted == []
    assert all("no network connection" in r["status"] for r in out.report)


async def test_an_unmanaged_dead_download_owing_nothing_is_removed_with_its_data():
    client = FakeClient()
    diagnosis = dead()
    out = await handle_dead(client, None, [diagnosis], recovery(), {}, NOW)

    assert client.deleted == [(diagnosis.torrent.infohash, True)]
    assert client.exported == [diagnosis.torrent.infohash]
    assert out.purged == {diagnosis.torrent.infohash}
    [(_, result)] = out.results
    assert result.kind is ActionKind.PURGE and result.ok
    [row] = await db.list_deletions()
    assert "dead download" in row["reason"]


async def test_one_that_owes_seeding_is_regrabbed_but_kept():
    client = FakeClient()
    diagnosis = dead(downloaded=GB // 4)
    arrs = FakeArrs({diagnosis.torrent.infohash: QueueMatch("sonarr", "sonarr", [7])})

    out = await handle_dead(client, arrs, [diagnosis], recovery(), {}, NOW)

    assert [m.ids for m in arrs.blocklisted] == [[7]]
    assert client.deleted == []
    assert "replacement requested" in out.report[0]["status"]
    assert "kept:" in out.report[0]["status"]


async def test_regrab_then_purge_in_one_scan():
    client = FakeClient()
    diagnosis = dead()
    arrs = FakeArrs({diagnosis.torrent.infohash: QueueMatch("sonarr", "sonarr", [7, 8])})

    out = await handle_dead(client, arrs, [diagnosis], recovery(), {}, NOW)

    assert [r.kind for _, r in out.results] == [ActionKind.REGRAB, ActionKind.PURGE]
    assert client.deleted


async def test_a_failed_regrab_holds_the_purge():
    """Removing it anyway would leave Sonarr waiting on a download that no
    longer exists, and it would never look for a replacement."""
    client = FakeClient()
    diagnosis = dead()
    arrs = FakeArrs({diagnosis.torrent.infohash: QueueMatch("sonarr", "sonarr", [7])}, error=True)

    out = await handle_dead(client, arrs, [diagnosis], recovery(), {}, NOW)

    assert client.deleted == []
    assert not out.results[0][1].ok
    assert "waiting for sonarr" in out.report[0]["status"]


async def test_a_regrab_is_not_repeated_on_the_next_scan():
    diagnosis = dead(downloaded=GB // 4)
    match = QueueMatch("sonarr", "sonarr", [7])
    arrs = FakeArrs({diagnosis.torrent.infohash: match})
    await handle_dead(FakeClient(), arrs, [diagnosis], recovery(), {}, NOW)
    arrs.queue = {diagnosis.torrent.infohash: match}  # even if it reappeared
    await handle_dead(FakeClient(), arrs, [diagnosis], recovery(), {}, NOW)
    assert len(arrs.blocklisted) == 1


async def test_an_app_that_did_not_answer_holds_the_purge():
    client = FakeClient()
    out = await handle_dead(client, FakeArrs(failed=["radarr"]), [dead()], recovery(), {}, NOW)
    assert client.deleted == []
    assert "radarr did not answer" in out.report[0]["status"]


async def test_dry_run_changes_nothing_anywhere():
    client = FakeClient()
    diagnosis = dead()
    arrs = FakeArrs({diagnosis.torrent.infohash: QueueMatch("sonarr", "sonarr", [7])})

    out = await handle_dead(client, arrs, [diagnosis], recovery(dry_run=True), {}, NOW)

    assert client.deleted == [] and arrs.blocklisted == []
    assert [r.kind for _, r in out.results] == [ActionKind.REGRAB, ActionKind.PURGE]
    assert all(r.dry_run for _, r in out.results)
    assert await db.list_deletions() == []


async def test_the_per_scan_cap_holds():
    client = FakeClient()
    batch = [dead() for _ in range(5)]
    out = await handle_dead(client, None, batch, recovery(stalled_max_per_scan=2), {}, NOW)
    assert len(client.deleted) == 2
    assert sum("per-scan limit" in r["status"] for r in out.report) == 3


async def test_a_held_download_is_neither_regrabbed_nor_removed():
    client = FakeClient()
    diagnosis = dead(tracker_status=4)
    arrs = FakeArrs({diagnosis.torrent.infohash: QueueMatch("sonarr", "sonarr", [7])})
    await handle_dead(client, arrs, [diagnosis], recovery(), {}, NOW)
    assert client.deleted == [] and arrs.blocklisted == []


async def test_per_tracker_settings_gate_removal():
    client = FakeClient()
    trackers = {"darkpeers": TrackerConfig(purge_stalled=False)}
    await handle_dead(client, None, [dead()], recovery(), trackers, NOW)
    assert client.deleted == []


# --- the *arr client --------------------------------------------------------------


def _transport(seen: list[httpx.Request], queue: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and request.url.path.endswith("/queue"):
            return httpx.Response(200, json={"records": queue})
        return httpx.Response(200)

    return httpx.MockTransport(handler)


async def test_a_season_pack_is_one_match_with_every_queue_row():
    seen: list[httpx.Request] = []
    pack = "ABCDEF" + "0" * 34
    queue = [
        {"id": 1, "downloadId": pack, "protocol": "torrent", "title": "Show.S01"},
        {"id": 2, "downloadId": pack, "protocol": "torrent", "title": "Show.S01"},
        {"id": 3, "downloadId": "SABnzbd_nzo_x", "protocol": "usenet"},
    ]
    client = ArrClient(
        "sonarr", ArrConfig(url="http://sonarr:8989", api_key="k"), transport=_transport(seen, queue)
    )
    rows = await client.queue()

    assert list(rows) == [pack.lower()]
    assert rows[pack.lower()].ids == [1, 2]
    assert seen[0].url.path == "/api/v3/queue"
    assert seen[0].url.params["includeUnknownSeriesItems"] == "true"
    assert seen[0].headers["X-Api-Key"] == "k"


async def test_blocklisting_never_lets_the_app_remove_the_torrent():
    seen: list[httpx.Request] = []
    client = ArrClient(
        "radarr", ArrConfig(url="http://radarr:7878", api_key="k"), transport=_transport(seen, [])
    )
    await client.blocklist_and_search(QueueMatch("radarr", "radarr", [4, 5]))

    [request] = seen
    assert request.method == "DELETE" and request.url.path == "/api/v3/queue/bulk"
    assert request.url.params["removeFromClient"] == "false"
    assert request.url.params["blocklist"] == "true"
    assert request.url.params["skipRedownload"] == "false"
    assert json.loads(request.content) == {"ids": [4, 5]}


async def test_a_router_reports_which_apps_did_not_answer():
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    router = ArrRouter(
        {"sonarr": ArrConfig(url="http://sonarr:8989", api_key="k")},
        transport=httpx.MockTransport(broken),
    )
    rows, failed = await router.queues()
    assert rows == {} and failed == ["sonarr"]
    await router.close()


def test_the_app_type_comes_from_the_name_when_not_given():
    assert kind_of("sonarr4k", ArrConfig()) == "sonarr"
    assert kind_of("movies", ArrConfig(kind="Radarr")) == "radarr"
    assert kind_of("movies", ArrConfig()) == ""


def test_arr_api_keys_never_reach_the_browser():
    from stallarr.web.routes import _redact_config

    cfg = Config(arr={"sonarr": ArrConfig(url="http://s", api_key="secret-key")})
    assert _redact_config(cfg)["arr"]["sonarr"]["api_key"] == "__SET__"


def test_sonarr_env_vars_are_reported_as_pinned(monkeypatch):
    from stallarr import config as config_module

    monkeypatch.setenv("STALLARR_SONARR_API_KEY", "abc")
    assert config_module.env_pinned_paths()["arr.sonarr.api_key"] == "STALLARR_SONARR_API_KEY"


# --- wired into the scan ----------------------------------------------------------


class ScanClient(FakeClient):
    """Enough of a client for a whole scan: one dead download, one healthy seed."""

    def __init__(self, torrents):
        super().__init__()
        self.torrents = torrents

    async def list_torrents(self):
        return [t for t in self.torrents if t.infohash not in {h for h, _ in self.deleted}]

    async def free_space(self):
        return None

    async def add_tags(self, *_a, **_k):
        pass

    async def remove_tags(self, *_a, **_k):
        pass

    async def reannounce(self, *_a, **_k):
        pass


async def test_a_scan_removes_the_dead_download_and_leaves_the_seed():
    from stallarr.recovery import RecoveryEngine
    from stallarr.service import StallarrService

    corpse = dead().torrent
    seed = make_torrent(infohash="b" * 40)
    cfg = Config()
    cfg.recovery = recovery()
    svc = StallarrService(cfg)
    svc.client = ScanClient([corpse, seed])
    svc.engine = RecoveryEngine(svc.client, svc.router, cfg.recovery)

    summary = await svc.scan()

    assert svc.client.deleted == [(corpse.infohash, True)]
    assert corpse.infohash not in {d.torrent.infohash for d in svc.last_diagnoses}
    assert summary.actions >= 1
    history = await db.get_history(limit=10)
    assert any(h["action"] == "purge" and h["ok"] for h in history)
