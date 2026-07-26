import time

from conftest import make_torrent

from stallarr.diagnosis import diagnose_local, should_skip
from stallarr.models import Ailment, TorrentState, TrackerEntry


def ailments(torrent, rules):
    return diagnose_local(torrent, rules).ailments


def test_healthy_seed_is_not_flagged(rules):
    assert diagnose_local(make_torrent(), rules).healthy


def test_idle_seed_with_no_swarm_is_not_flagged(rules):
    """The common case: seeding something nobody wants. That is not a fault."""
    torrent = make_torrent(
        upspeed=0,
        num_leechs=0,
        last_activity=int(time.time() - 30 * 86400),
        trackers=[TrackerEntry(url="https://darkpeers.org/announce/k", status=2, num_leeches=0)],
    )
    assert diagnose_local(torrent, rules).healthy


def test_idle_seed_with_waiting_leechers_is_flagged(rules):
    """Tracker says people want it, we have no peers — our announce isn't landing."""
    torrent = make_torrent(
        upspeed=0,
        num_leechs=0,
        last_activity=int(time.time() - 30 * 86400),
        trackers=[TrackerEntry(url="https://darkpeers.org/announce/k", status=2, num_leeches=7)],
    )
    assert Ailment.STALLED_UPLOAD in ailments(torrent, rules)


def test_new_torrent_is_inside_grace_period(rules):
    torrent = make_torrent(
        added_on=int(time.time() - 60),
        progress=0.0,
        dlspeed=0,
        last_activity=int(time.time() - 60),
        state=TorrentState.STALLED_DL,
    )
    assert diagnose_local(torrent, rules).healthy


def test_stalled_download_is_flagged(rules):
    torrent = make_torrent(
        progress=0.42,
        dlspeed=0,
        state=TorrentState.STALLED_DL,
        last_activity=int(time.time() - 3 * 3600),
    )
    assert Ailment.STALLED_DOWNLOAD in ailments(torrent, rules)


def test_unregistered_message_is_detected(rules):
    torrent = make_torrent(
        trackers=[
            TrackerEntry(url="https://darkpeers.org/announce/k", status=4, message="Unregistered torrent")
        ]
    )
    assert Ailment.UNREGISTERED in ailments(torrent, rules)


def test_auth_message_is_detected(rules):
    torrent = make_torrent(
        trackers=[TrackerEntry(url="https://darkpeers.org/announce/k", status=4, message="Invalid passkey")]
    )
    assert Ailment.TRACKER_AUTH in ailments(torrent, rules)


def test_one_dead_backup_tracker_is_tolerated(rules):
    torrent = make_torrent(
        trackers=[
            TrackerEntry(url="https://darkpeers.org/announce/k", status=2, num_leeches=3),
            TrackerEntry(url="https://dead.example.org/announce", status=4, message="timed out"),
        ]
    )
    assert Ailment.TRACKER_DOWN not in ailments(torrent, rules)


def test_all_trackers_down_is_flagged(rules):
    torrent = make_torrent(
        trackers=[
            TrackerEntry(url="https://darkpeers.org/announce/k", status=4, message="timed out"),
            TrackerEntry(url="https://dead.example.org/announce", status=4, message="timed out"),
        ]
    )
    assert Ailment.TRACKER_DOWN in ailments(torrent, rules)


def test_dht_pseudo_entries_are_ignored(rules):
    torrent = make_torrent(
        trackers=[
            TrackerEntry(url="** [DHT] **", status=4),
            TrackerEntry(url="https://darkpeers.org/announce/k", status=2, num_leeches=3),
        ]
    )
    assert diagnose_local(torrent, rules).healthy


def test_user_paused_torrent_is_left_alone_by_default(rules):
    torrent = make_torrent(state=TorrentState.PAUSED, raw_state="pausedUP", upspeed=0, num_leechs=0)
    assert diagnose_local(torrent, rules).healthy


def test_pause_we_caused_is_flagged(rules):
    torrent = make_torrent(
        state=TorrentState.PAUSED, raw_state="pausedUP", upspeed=0, num_leechs=0,
        tags=["stallarr:repairing"],
    )
    assert Ailment.UNEXPECTEDLY_PAUSED in ailments(torrent, rules)


def test_flag_user_paused_opt_in(rules):
    rules.flag_user_paused = True
    torrent = make_torrent(state=TorrentState.PAUSED, raw_state="pausedUP", upspeed=0, num_leechs=0)
    assert Ailment.UNEXPECTEDLY_PAUSED in ailments(torrent, rules)


def test_missing_files_is_critical(rules):
    torrent = make_torrent(state=TorrentState.MISSING_FILES, raw_state="missingFiles")
    diagnosis = diagnose_local(torrent, rules)
    assert Ailment.MISSING_FILES in diagnosis.ailments
    assert diagnosis.severity == 3


def test_not_announcing_when_no_tracker_contacted(rules):
    torrent = make_torrent(
        time_active=7200,
        trackers=[TrackerEntry(url="https://darkpeers.org/announce/k", status=1)],
    )
    assert Ailment.NOT_ANNOUNCING in ailments(torrent, rules)


def test_should_skip_respects_tags_categories_and_ignores(rules):
    assert should_skip(make_torrent(tags=["stallarr:ignore"]), rules, set()) is not None
    assert should_skip(make_torrent(), rules, {"a" * 40}) is not None

    rules.ignore_categories = ["archive"]
    assert should_skip(make_torrent(category="archive"), rules, set()) is not None

    rules.ignore_categories = []
    rules.only_categories = ["tv"]
    assert should_skip(make_torrent(category="movies"), rules, set()) is not None
    assert should_skip(make_torrent(category="tv"), rules, set()) is None


def test_checking_torrent_is_skipped(rules):
    torrent = make_torrent(state=TorrentState.CHECKING, raw_state="checkingUP")
    assert should_skip(torrent, rules, set()) is not None


# --- enrichment budget -------------------------------------------------------
# enrich() costs a private-tracker API call per sick torrent per scan. On a large
# library that is thousands of calls a day, so the scan loop must not spend one
# on a torrent it has already given up on.


def _service_with(tags):
    """A StallarrService stub carrying just what _already_escalated reads."""
    from stallarr.config import Config
    from stallarr.service import StallarrService

    service = StallarrService.__new__(StallarrService)
    service.cfg = Config()
    return service, make_torrent(tags=tags)


def test_an_escalated_torrent_is_not_enriched_again():
    service, torrent = _service_with(["stallarr:needs-help"])
    assert service._already_escalated(torrent)


def test_a_torrent_under_active_repair_is_still_enriched():
    """Only escalation stops enrichment — repair still needs fresh tracker data."""
    service, torrent = _service_with(["stallarr:repairing"])
    assert not service._already_escalated(torrent)


def test_an_untagged_torrent_is_enriched():
    service, torrent = _service_with([])
    assert not service._already_escalated(torrent)


def test_a_blank_escalation_tag_never_matches():
    service, torrent = _service_with([""])
    service.cfg.recovery.escalation_tag = ""
    assert not service._already_escalated(torrent)


def test_a_torrent_stopped_at_its_share_limit_says_so(rules):
    """`stoppedUP` covers both a hand-pause and a client-enforced cap, and the
    two need completely different responses. Reporting the wrong one sends
    someone hunting for a bug in stallarr that isn't there."""
    torrent = make_torrent(
        state=TorrentState.PAUSED,
        raw_state="stoppedUP",
        progress=1.0,
        tags=["stallarr:needs-help"],
        seeding_time=100_001 * 60,
        seeding_time_limit=100_000,
    )

    diagnosis = diagnose_local(torrent, rules)

    finding = next(f for f in diagnosis.findings if f.ailment is Ailment.UNEXPECTEDLY_PAUSED)
    assert "seeding time reached its limit" in finding.detail


def test_a_plain_pause_is_still_reported_as_a_pause(rules):
    torrent = make_torrent(
        state=TorrentState.PAUSED, raw_state="stoppedUP", progress=1.0,
        tags=["stallarr:needs-help"],
    )

    diagnosis = diagnose_local(torrent, rules)

    finding = next(f for f in diagnosis.findings if f.ailment is Ailment.UNEXPECTEDLY_PAUSED)
    assert finding.detail == "paused (stoppedUP)"
