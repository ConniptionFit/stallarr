import time
import uuid

import pytest
from conftest import make_torrent

from stallarr import db
from stallarr.config import RecoveryConfig, TrackerConfig
from stallarr.diagnosis import diagnose_local
from stallarr.models import ActionKind, Ailment, TorrentState, TrackerEntry
from stallarr.recovery import RecoveryEngine, is_terminal
from stallarr.trackers import TrackerRouter
from stallarr.trackers.iptorrents import IPTorrents


class FakeClient:
    """Records every call so tests can assert on what the engine actually did."""

    name = "fake"

    def __init__(self):
        self.calls: list[tuple] = []

    async def _record(self, *call):
        self.calls.append(call)

    async def resume(self, infohash): await self._record("resume", infohash)
    async def pause(self, infohash): await self._record("pause", infohash)
    async def reannounce(self, infohash): await self._record("reannounce", infohash)
    async def recheck(self, infohash): await self._record("recheck", infohash)
    async def add_tags(self, infohash, tags): await self._record("add_tags", infohash, tuple(tags))
    async def remove_tags(self, infohash, tags): await self._record("remove_tags", infohash, tuple(tags))
    async def add_tracker(self, infohash, url): await self._record("add_tracker", infohash, url)
    async def replace_tracker(self, infohash, old, new):
        await self._record("replace_tracker", infohash, old, new)

    async def set_share_limits(self, infohash, *, ratio=None, seeding_minutes=None):
        await self._record("set_share_limits", infohash, ratio, seeding_minutes)

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture(autouse=True)
async def _db():
    await db.init()


@pytest.fixture
def engine():
    client = FakeClient()
    router = TrackerRouter({})
    return RecoveryEngine(client, router, RecoveryConfig(toggle_pause_seconds=0)), client


def sick_torrent(**overrides):
    """A stalled download with a unique infohash so attempt state never leaks."""
    return make_torrent(
        infohash=uuid.uuid4().hex + "0" * 8,
        progress=0.4,
        dlspeed=0,
        state=TorrentState.STALLED_DL,
        last_activity=int(time.time() - 6 * 3600),
        **overrides,
    )


async def test_first_step_is_the_cheapest_applicable_one(engine, rules):
    eng, client = engine
    diagnosis = diagnose_local(sick_torrent(), rules)

    result = await eng.repair(diagnosis)

    assert result.kind is ActionKind.REANNOUNCE
    assert "reannounce" in client.kinds()


async def test_ladder_escalates_after_max_attempts(engine, rules):
    eng, _ = engine
    eng.cfg.max_attempts_per_step = 2
    eng.cfg.cooldown_minutes = dict.fromkeys(eng.cfg.cooldown_minutes, 0)
    diagnosis = diagnose_local(sick_torrent(), rules)

    steps = []
    for _ in range(12):
        result = await eng.repair(diagnosis)
        if result is None:
            break
        steps.append(result.kind)

    assert steps[:2] == [ActionKind.REANNOUNCE, ActionKind.REANNOUNCE]
    # refresh_announce and replace_torrent need a tracker plugin, so with none
    # configured the ladder skips straight to recheck, then toggle, then gives up.
    assert steps[2:4] == [ActionKind.RECHECK, ActionKind.RECHECK]
    assert steps[4:6] == [ActionKind.TOGGLE, ActionKind.TOGGLE]
    assert ActionKind.ESCALATE in steps


async def test_cooldown_blocks_a_repeat_instead_of_escalating(engine, rules):
    eng, _ = engine
    eng.cfg.cooldown_minutes = {"reannounce": 60}
    diagnosis = diagnose_local(sick_torrent(), rules)

    first = await eng.repair(diagnosis)
    second = await eng.repair(diagnosis)

    assert first.kind is ActionKind.REANNOUNCE
    assert second is None, "a cooling-down step must wait, not skip to the next rung"


async def test_unregistered_goes_straight_to_escalation(engine, rules):
    eng, client = engine
    torrent = sick_torrent(
        trackers=[
            TrackerEntry(url="https://darkpeers.org/announce/k", status=4, message="Unregistered torrent")
        ]
    )
    diagnosis = diagnose_local(torrent, rules)
    assert is_terminal(diagnosis)

    result = await eng.repair(diagnosis)

    assert result.kind is ActionKind.ESCALATE
    assert ("add_tags", torrent.infohash, ("stallarr:needs-help",)) in client.calls
    assert "reannounce" not in client.kinds(), "never retry an unregistered torrent"


async def test_dry_run_reports_without_touching_the_client(engine, rules):
    eng, client = engine
    eng.cfg.dry_run = True
    diagnosis = diagnose_local(sick_torrent(), rules)

    result = await eng.repair(diagnosis)

    assert result.dry_run and result.ok
    assert client.calls == []


async def test_dry_run_does_not_consume_attempts(engine, rules):
    eng, _ = engine
    eng.cfg.dry_run = True
    diagnosis = diagnose_local(sick_torrent(), rules)

    for _ in range(3):
        assert (await eng.repair(diagnosis)).kind is ActionKind.REANNOUNCE


async def test_auth_failure_fixes_the_passkey_before_reannouncing(rules):
    """The whole point of per-ailment ordering: announcing with a known-bad
    passkey fails identically, so the rewrite has to come first."""
    client = FakeClient()
    plugin = IPTorrents(TrackerConfig(passkey="n" * 32))
    router = TrackerRouter({"iptorrents": plugin})
    eng = RecoveryEngine(client, router, RecoveryConfig())

    torrent = sick_torrent(
        trackers=[
            TrackerEntry(
                url="http://empirehost.me/" + "o" * 32 + "/announce",
                status=4,
                message="Invalid passkey",
            )
        ]
    )
    result = await eng.repair(diagnose_local(torrent, rules, plugin))

    assert result.kind is ActionKind.REFRESH_ANNOUNCE, "must not waste an attempt on a bare reannounce"


async def test_refresh_announce_rewrites_the_tracker_url(rules):
    client = FakeClient()
    plugin = IPTorrents(TrackerConfig(passkey="n" * 32))
    router = TrackerRouter({"iptorrents": plugin})
    eng = RecoveryEngine(client, router, RecoveryConfig(ladder=["refresh_announce"]))

    old_url = "http://localhost.stackoverflow.tech:80/" + "o" * 32 + "/announce"
    torrent = sick_torrent(
        trackers=[TrackerEntry(url=old_url, status=4, message="Invalid announce key")]
    )
    diagnosis = diagnose_local(torrent, rules, plugin)
    assert Ailment.TRACKER_AUTH in diagnosis.ailments

    result = await eng.repair(diagnosis)

    assert result.ok and result.kind is ActionKind.REFRESH_ANNOUNCE
    replaced = next(c for c in client.calls if c[0] == "replace_tracker")
    assert replaced[2] == old_url
    assert "n" * 32 in replaced[3]
    assert "reannounce" in client.kinds(), "a rewritten URL must be announced immediately"


async def test_refresh_announce_never_leaks_the_passkey(rules):
    client = FakeClient()
    plugin = IPTorrents(TrackerConfig(passkey="s3cr3t" + "x" * 26))
    router = TrackerRouter({"iptorrents": plugin})
    eng = RecoveryEngine(client, router, RecoveryConfig(ladder=["refresh_announce"]))

    torrent = sick_torrent(
        trackers=[
            TrackerEntry(
                url="http://empirehost.me/" + "o" * 32 + "/announce",
                status=4,
                message="Invalid announce key",
            )
        ]
    )
    result = await eng.repair(diagnose_local(torrent, rules, plugin))

    assert "s3cr3t" not in result.message
    assert "***" in result.message


async def test_steps_needing_a_plugin_are_skipped_without_one(engine, rules):
    eng, _ = engine
    eng.cfg.ladder = ["refresh_announce", "replace_torrent", "escalate"]
    diagnosis = diagnose_local(sick_torrent(), rules)

    result = await eng.repair(diagnosis)

    assert result.kind is ActionKind.ESCALATE


async def test_recovered_torrent_gets_its_ladder_and_tags_cleared(engine, rules):
    eng, client = engine
    torrent = sick_torrent(tags=["stallarr:repairing"])
    diagnosis = diagnose_local(torrent, rules)
    await eng.repair(diagnosis)

    await eng.on_recovered(diagnosis)

    assert await db.get_attempts(torrent.infohash, Ailment.STALLED_DOWNLOAD) == {}
    assert ("remove_tags", torrent.infohash, ("stallarr:repairing",)) in client.calls


async def test_history_records_every_action(engine, rules):
    eng, _ = engine
    torrent = sick_torrent()
    await eng.repair(diagnose_local(torrent, rules))

    rows = await db.get_history(infohash=torrent.infohash)

    assert len(rows) == 1
    assert rows[0]["action"] == "reannounce"
    assert rows[0]["ailment"] == "stalled_download"


async def test_unknown_ladder_step_in_config_is_ignored(engine, rules):
    eng, _ = engine
    eng.cfg.ladder = ["nonsense", "reannounce"]
    result = await eng.repair(diagnose_local(sick_torrent(), rules))
    assert result.kind is ActionKind.REANNOUNCE


async def test_disabling_a_step_in_config_skips_it(engine, rules):
    eng, _ = engine
    eng.cfg.ladder = ["recheck"]  # reannounce removed entirely
    result = await eng.repair(diagnose_local(sick_torrent(), rules))
    assert result.kind is ActionKind.RECHECK


async def test_escalation_cannot_be_disabled(engine, rules):
    """Without escalation a torrent could be retried forever with no way out."""
    eng, _ = engine
    eng.cfg.ladder = []
    result = await eng.repair(diagnose_local(sick_torrent(), rules))
    assert result.kind is ActionKind.ESCALATE


# --- seeding obligations override the attempt limit --------------------------


async def _hnr_diagnosis(paused=True):
    from stallarr.models import Diagnosis, Finding

    torrent = make_torrent(
        infohash=uuid.uuid4().hex[:40],
        state=TorrentState.PAUSED if paused else TorrentState.SEEDING,
        raw_state="stoppedUP" if paused else "uploading",
        progress=1.0,
        tags=["stallarr:needs-help"],
    )
    return Diagnosis(
        torrent=torrent,
        findings=[Finding(Ailment.HIT_AND_RUN_RISK, "tracker requires seeding")],
        tracker_id="iptorrents",
    )


async def test_a_stopped_torrent_under_a_seeding_demand_is_resumed_past_the_limit():
    """Everywhere else, exhausting the ladder and waiting for a human is right.
    Here it would leave the torrent stopped while the tracker counts down — the
    exact way a seeding obligation becomes a penalty."""
    client = FakeClient()
    # Zero cooldown so this isolates the attempt-limit bypass; the cooldown is
    # covered separately below.
    eng = RecoveryEngine(client, TrackerRouter({}), RecoveryConfig(cooldown_minutes={"resume": 0}))
    diagnosis = await _hnr_diagnosis()

    for _ in range(eng.cfg.max_attempts_per_step + 2):
        await db.record_attempt(
            diagnosis.torrent.infohash, Ailment.HIT_AND_RUN_RISK, ActionKind.RESUME
        )

    result = await eng.repair(diagnosis)

    assert result is not None
    assert result.kind is ActionKind.RESUME
    assert ("resume", diagnosis.torrent.infohash) in client.calls


async def test_the_override_does_not_fire_for_a_running_torrent(engine):
    """It is already seeding; resuming it would be a no-op every scan."""
    eng, _client = engine
    diagnosis = await _hnr_diagnosis(paused=False)

    assert eng._forced_step(diagnosis, {}) is None


async def test_the_override_does_not_fire_without_a_seeding_demand(engine):
    from stallarr.models import Diagnosis, Finding

    eng, _client = engine
    torrent = make_torrent(
        infohash=uuid.uuid4().hex[:40], state=TorrentState.PAUSED, raw_state="stoppedUP"
    )
    diagnosis = Diagnosis(
        torrent=torrent, findings=[Finding(Ailment.STALLED_UPLOAD, "idle")], tracker_id="x"
    )

    assert eng._forced_step(diagnosis, {}) is None


async def test_the_override_still_respects_the_cooldown(engine):
    """Bypassing the attempt limit must not mean resuming on every single scan."""
    eng, _client = engine
    diagnosis = await _hnr_diagnosis()
    fresh = {ActionKind.RESUME: {"count": 1, "last_at": time.time()}}

    assert eng._forced_step(diagnosis, fresh) is None


async def test_a_torrent_stopped_by_its_own_share_limit_has_the_limit_lifted():
    """Resuming this torrent is not a fix — the client re-stops it within
    seconds and the history table records a success that changed nothing."""
    client = FakeClient()
    eng = RecoveryEngine(client, TrackerRouter({}), RecoveryConfig(cooldown_minutes={}))
    diagnosis = await _hnr_diagnosis()
    diagnosis.torrent.seeding_time = 100_001 * 60
    diagnosis.torrent.seeding_time_limit = 100_000

    result = await eng.repair(diagnosis)

    assert result.kind is ActionKind.LIFT_SHARE_LIMITS
    assert result.ok
    assert "seeding time" in result.message
    assert "set_share_limits" in client.kinds()
    assert "resume" in client.kinds(), "lifting the limit alone leaves it stopped"


async def test_a_limit_that_is_not_blocking_is_left_alone():
    """Only the limit actually stopping the torrent gets cleared; the other is
    resent unchanged rather than silently reset."""
    client = FakeClient()
    eng = RecoveryEngine(client, TrackerRouter({}), RecoveryConfig(cooldown_minutes={}))
    diagnosis = await _hnr_diagnosis()
    diagnosis.torrent.seeding_time = 100_001 * 60
    diagnosis.torrent.seeding_time_limit = 100_000
    diagnosis.torrent.ratio_limit = 1000.0
    diagnosis.torrent.ratio = 2.5

    await eng.repair(diagnosis)

    call = next(c for c in client.calls if c[0] == "set_share_limits")
    assert call[2] == 1000.0, "ratio limit was not blocking, so it must be preserved"
    assert call[3] is None, "the blocking seeding-time limit is the one cleared"


async def test_a_stopped_torrent_with_no_share_limit_is_simply_resumed():
    client = FakeClient()
    eng = RecoveryEngine(client, TrackerRouter({}), RecoveryConfig(cooldown_minutes={}))
    diagnosis = await _hnr_diagnosis()

    result = await eng.repair(diagnosis)

    assert result.kind is ActionKind.RESUME
