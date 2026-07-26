"""The seeding-required digest notification, and the history action filter."""

from __future__ import annotations

import pytest

from stallarr import db
from stallarr.config import NotifyConfig
from stallarr.models import Severity
from stallarr.notify import Notifier, _digest_fields, _group_by_tracker

pytestmark = pytest.mark.asyncio


def row(tracker, tracker_name, name, detail="1 GiB still owed", status="stopped"):
    return {
        "tracker": tracker,
        "tracker_name": tracker_name,
        "name": name,
        "detail": detail,
        "status": status,
    }


def notifier(**kwargs):
    cfg = NotifyConfig(
        discord_enabled=True, discord_webhook_url="https://example.invalid/hook", **kwargs
    )
    sent: list[tuple[str, dict, Severity]] = []

    n = Notifier(cfg)

    async def capture(title, fields, severity):
        sent.append((title, fields, severity))

    n._send = capture  # type: ignore[method-assign]
    return n, sent


@pytest.fixture(autouse=True)
async def _db():
    import asyncio

    await db.init()
    await asyncio.to_thread(db._exec, "DELETE FROM notified", ())


# --- grouping and ordering ----------------------------------------------------


async def test_rows_group_by_tracker_then_sort_by_torrent_name():
    grouped = _group_by_tracker([
        row("sp", "Seedpool", "Zulu.Release"),
        row("ipt", "IPTorrents", "Bravo.Release"),
        row("sp", "Seedpool", "Alpha.Release"),
        row("ipt", "IPTorrents", "Alpha.Release"),
    ])

    assert list(grouped) == ["IPTorrents", "Seedpool"]
    assert [r["name"] for r in grouped["IPTorrents"]] == ["Alpha.Release", "Bravo.Release"]
    assert [r["name"] for r in grouped["Seedpool"]] == ["Alpha.Release", "Zulu.Release"]


async def test_sorting_ignores_case():
    """A lowercase plugin id next to a capitalised display name should order the
    way a reader expects, not the way ASCII does."""
    grouped = _group_by_tracker([
        row("b", "beta", "zebra"),
        row("a", "Alpha", "Apple"),
        row("b", "beta", "Aardvark"),
    ])

    assert list(grouped) == ["Alpha", "beta"]
    assert [r["name"] for r in grouped["beta"]] == ["Aardvark", "zebra"]


async def test_each_tracker_becomes_one_field_with_a_count():
    fields = _digest_fields(_group_by_tracker([
        row("ipt", "IPTorrents", "One"),
        row("ipt", "IPTorrents", "Two"),
    ]))

    assert list(fields) == ["IPTorrents (2)"]
    assert fields["IPTorrents (2)"].count("•") == 2


async def test_a_line_carries_the_name_the_debt_and_the_local_state():
    fields = _digest_fields(_group_by_tracker([
        row("ipt", "IPTorrents", "Some.Release", "57.5 MiB still owed", "stopped at its ratio limit")
    ]))
    line = fields["IPTorrents (1)"]

    assert "Some.Release" in line
    assert "57.5 MiB still owed" in line
    assert "stopped at its ratio limit" in line


async def test_a_long_list_is_trimmed_rather_than_dropped():
    """Discord rejects an embed field over 1024 characters outright, so an
    untrimmed digest would send nothing at all."""
    rows = [row("ipt", "IPTorrents", f"Release.Number.{i:03d}") for i in range(40)]

    fields = _digest_fields(_group_by_tracker(rows))
    body = fields["IPTorrents (40)"]

    assert body.count("•") == 12
    assert "and 28 more" in body
    assert len(body) < 1024


async def test_many_trackers_are_trimmed_to_stay_inside_the_embed_limit():
    rows = [row(f"t{i}", f"Tracker{i:02d}", "Some.Release") for i in range(30)]

    fields = _digest_fields(_group_by_tracker(rows))

    assert len(fields) == 21  # 20 trackers plus the overflow note
    assert fields["…"] == "and 10 more tracker(s)"


# --- when it fires ------------------------------------------------------------


async def test_a_digest_is_sent_for_outstanding_obligations():
    n, sent = notifier()

    await n.seeding_digest([row("ipt", "IPTorrents", "Some.Release")])

    assert len(sent) == 1
    title, _fields, severity = sent[0]
    assert "1 torrent(s) on 1 tracker(s)" in title
    assert severity is Severity.CRITICAL


async def test_the_same_list_does_not_alert_twice():
    n, sent = notifier()
    rows = [row("ipt", "IPTorrents", "Some.Release")]

    await n.seeding_digest(rows)
    await n.seeding_digest(rows)

    assert len(sent) == 1


async def test_a_changed_state_alone_does_not_re_alert():
    """A torrent going from stopped to seeding while still owing data is
    progress, not news."""
    n, sent = notifier()

    await n.seeding_digest([row("ipt", "IPTorrents", "Some.Release", status="stopped")])
    await n.seeding_digest([row("ipt", "IPTorrents", "Some.Release", status="uploading")])

    assert len(sent) == 1


async def test_a_new_torrent_in_the_list_re_alerts_immediately():
    n, sent = notifier()

    await n.seeding_digest([row("ipt", "IPTorrents", "One")])
    await n.seeding_digest([row("ipt", "IPTorrents", "One"), row("ipt", "IPTorrents", "Two")])

    assert len(sent) == 2


async def test_the_same_release_on_two_trackers_is_two_obligations():
    """Keying on name alone would silently collapse a cross-seed into one, and
    under-report what is owed."""
    n, sent = notifier()

    await n.seeding_digest([row("ipt", "IPTorrents", "Some.Release")])
    await n.seeding_digest([
        row("ipt", "IPTorrents", "Some.Release"),
        row("sp", "Seedpool", "Some.Release"),
    ])

    assert len(sent) == 2


async def test_nothing_is_sent_for_an_empty_list():
    n, sent = notifier()
    await n.seeding_digest([])
    assert sent == []


async def test_the_digest_can_be_switched_off():
    n, sent = notifier(seeding_digest_enabled=False)
    await n.seeding_digest([row("ipt", "IPTorrents", "Some.Release")])
    assert sent == []


async def test_no_channel_means_no_send():
    n = Notifier(NotifyConfig())
    sent = []
    n._send = lambda *a: sent.append(a)  # type: ignore[method-assign]

    await n.seeding_digest([row("ipt", "IPTorrents", "Some.Release")])

    assert sent == []


# --- the all-clear ------------------------------------------------------------


async def test_the_all_clear_ignores_the_severity_floor():
    """It resolves an alert already delivered. Suppressing it would leave
    someone believing a hit-and-run is still counting down."""
    n, sent = notifier(min_severity=3)

    await n.seeding_cleared()

    assert len(sent) == 1
    assert sent[0][2] is Severity.OK


async def test_the_all_clear_respects_the_feature_switch():
    n, sent = notifier(seeding_digest_enabled=False)
    await n.seeding_cleared()
    assert sent == []
