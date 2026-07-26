"""Tracker-declared seeding obligations.

The tracker naming a torrent is the strongest signal stallarr gets: it is a live
hit-and-run countdown *and* proof the torrent still exists. Getting either half
wrong is expensive — a missed obligation costs the account, and a false
`unregistered` stops the very torrent the site is waiting on.
"""

from __future__ import annotations

from stallarr.config import TrackerConfig
from stallarr.models import (
    Ailment,
    Diagnosis,
    Finding,
    SeedingRequirement,
    Torrent,
    TorrentState,
)
from stallarr.service import _apply_seeding_requirement
from stallarr.trackers.base import Capability
from stallarr.trackers.iptorrents import IPTorrents, _parse_seeding_required
from stallarr.utils import normalise_release

# A cut-down copy of the real page: unclosed <td>, rows nested inside a <tr>,
# and a hover tooltip in the seed-time cell holding a *different* duration.
PAGE = """
<table id="body"><tr><th class=al>Torrent<th>Uploaded<th>Downloaded<th>Gap<th>Seeding Time
<tr id="line2082626"><td><a href="/t/2082626">The Weeknd-Starboy-CD-FLAC-2016-PERFECT</a>
<td class=ac><nobr>0.00 kB</nobr><td class=ac><nobr>57.47 MB</nobr>
<td class=ac><nobr>57.47 MB</nobr>
<td class="ac tTipWrap">0.2 hours<div class="tTip ttop">22.4 hours ago</div></td>
<td><form method="post" class="zap"><input type=submit value="6.73 Points"></form>
<tr id="line7230603"><td><a href="/t/7230603">Carpenter Brut-Leather Temple-WEB-FLAC-2026</a>
<td class=ac><nobr>3.78 MB</nobr><td class=ac><nobr>540.53 MB</nobr>
<td class=ac><nobr>536.75 MB</nobr>
<td class="ac tTipWrap">143.6 hours<div class="tTip ttop">23.6 hours ago</div></td>
</table>
"""


def test_the_seeding_page_is_parsed_into_requirements():
    rows = _parse_seeding_required(PAGE)

    assert len(rows) == 2
    first = rows[0]
    assert first.torrent_id == "2082626"
    assert first.name == "The Weeknd-Starboy-CD-FLAC-2016-PERFECT"
    assert first.uploaded == 0
    assert first.downloaded == int(57.47 * 1024**2)
    assert first.gap == int(57.47 * 1024**2)


def test_the_hover_tooltip_is_not_mistaken_for_seed_time():
    """The tooltip says when it was snatched, not how long it has seeded.
    Reading it would make every torrent look like it had already served its
    time — the exact failure that silently disables this whole feature."""
    rows = _parse_seeding_required(PAGE)

    assert rows[0].seeded_hours == 0.2
    assert rows[1].seeded_hours == 143.6


def test_the_header_row_is_not_a_requirement():
    assert all(row.name != "Torrent" for row in _parse_seeding_required(PAGE))


def test_an_empty_list_means_nothing_is_owed_not_an_error():
    assert _parse_seeding_required("<table id=body></table>") == []


def test_seeding_requirements_need_a_cookie():
    assert Capability.SEEDING_REQUIREMENTS not in IPTorrents(TrackerConfig(passkey="x")).capabilities
    with_cookie = IPTorrents(TrackerConfig(cookie_uid="1", cookie_pass="2"))
    assert Capability.SEEDING_REQUIREMENTS in with_cookie.capabilities


# --- folding a requirement into a diagnosis ----------------------------------


def _torrent(name: str, *, state=TorrentState.PAUSED, upspeed=0, tags=()) -> Torrent:
    return Torrent(
        infohash="a" * 40,
        name=name,
        state=state,
        raw_state="stoppedUP",
        progress=1.0,
        size=1,
        tags=list(tags),
        upspeed=upspeed,
    )


def _requirements(name: str, **kw):
    req = SeedingRequirement(torrent_id="1", name=name, gap=1024, **kw)
    return {"iptorrents": {normalise_release(name): req}}


def test_a_required_torrent_is_flagged_as_hit_and_run_risk():
    diagnosis = Diagnosis(torrent=_torrent("Some.Release-GRP"), tracker_id="iptorrents")

    _apply_seeding_requirement(diagnosis, _requirements("Some.Release-GRP"), {})

    assert Ailment.HIT_AND_RUN_RISK in diagnosis.ailments


def test_a_requirement_overrides_an_unregistered_finding():
    """A tracker cannot simultaneously demand seeding and have deleted the
    torrent. `unregistered` escalates terminally, so leaving it in place is how
    a live obligation ends up stopped and abandoned."""
    diagnosis = Diagnosis(
        torrent=_torrent("Some.Release-GRP"),
        findings=[Finding(Ailment.UNREGISTERED, "gone")],
        tracker_id="iptorrents",
    )

    _apply_seeding_requirement(diagnosis, _requirements("Some.Release-GRP"), {})

    assert Ailment.UNREGISTERED not in diagnosis.ailments
    assert diagnosis.primary.ailment is Ailment.HIT_AND_RUN_RISK


def test_the_requirement_drives_the_ladder_over_other_critical_findings():
    """Severity ties are broken by list order, so this has to be first."""
    diagnosis = Diagnosis(
        torrent=_torrent("Some.Release-GRP"),
        findings=[Finding(Ailment.MISSING_FILES, "gone from disk")],
        tracker_id="iptorrents",
    )

    _apply_seeding_requirement(diagnosis, _requirements("Some.Release-GRP"), {})

    assert diagnosis.primary.ailment is Ailment.HIT_AND_RUN_RISK


def test_a_torrent_already_uploading_is_left_alone():
    """It is doing the one thing that clears the debt; reannouncing it would
    burn the ladder for nothing."""
    diagnosis = Diagnosis(
        torrent=_torrent("Some.Release-GRP", state=TorrentState.SEEDING, upspeed=50_000),
        tracker_id="iptorrents",
    )

    _apply_seeding_requirement(diagnosis, _requirements("Some.Release-GRP"), {})

    assert diagnosis.healthy


def test_names_match_across_separator_and_case_differences():
    diagnosis = Diagnosis(torrent=_torrent("some release grp"), tracker_id="iptorrents")

    _apply_seeding_requirement(diagnosis, _requirements("Some.Release-GRP"), {})

    assert Ailment.HIT_AND_RUN_RISK in diagnosis.ailments


def test_a_requirement_from_another_tracker_is_not_applied():
    diagnosis = Diagnosis(torrent=_torrent("Some.Release-GRP"), tracker_id="seedpool")

    _apply_seeding_requirement(diagnosis, _requirements("Some.Release-GRP"), {})

    assert diagnosis.healthy


def test_matching_is_recorded_so_unmatched_rows_can_be_reported():
    matched: dict[tuple[str, str], str] = {}
    diagnosis = Diagnosis(torrent=_torrent("Some.Release-GRP"), tracker_id="iptorrents")

    _apply_seeding_requirement(diagnosis, _requirements("Some.Release-GRP"), matched)

    assert set(matched) == {("iptorrents", normalise_release("Some.Release-GRP"))}


def test_the_local_state_is_recorded_alongside_the_match():
    """The digest needs to say *why* a torrent is not paying off its debt —
    "stopped at its seeding time limit" and "stopped" send you to two entirely
    different fixes."""
    from stallarr.models import TorrentState

    matched: dict[tuple[str, str], str] = {}
    torrent = _torrent("Some.Release-GRP")
    torrent.state = TorrentState.PAUSED
    torrent.upspeed = 0
    torrent.seeding_time_limit = 100
    torrent.seeding_time = 100 * 60 + 1
    diagnosis = Diagnosis(torrent=torrent, tracker_id="iptorrents")

    _apply_seeding_requirement(diagnosis, _requirements("Some.Release-GRP"), matched)

    assert matched[("iptorrents", normalise_release("Some.Release-GRP"))] == (
        "stopped at its seeding time limit"
    )
