"""Credential redaction.

Every string here was produced by a real tracker during deployment. The original
implementation only understood the IPTorrents announce layout, so UNIT3D
passkeys and download links went to disk in the clear — which is most of the
trackers people actually run. Over-redaction is the intended bias.
"""

import pytest

from stallarr.utils import redact_secrets

KEY = "bab386608f9a285afeb458a5c1d2e3f4"


@pytest.mark.parametrize(
    "label,url",
    [
        # IPTorrents: key before the verb.
        ("ipt announce", f"http://localhost.stackoverflow.tech/{KEY}/announce"),
        # UNIT3D: key after the verb. The shape the old regex missed.
        ("unit3d announce", f"https://darkpeers.org/announce/{KEY}"),
        # UNIT3D download link: `<id>.<rsskey>`, seen in every torrent payload.
        ("unit3d download", f"https://darkpeers.org/torrent/download/115069.{KEY}"),
        ("query param", f"https://x.org/announce?passkey={KEY}"),
        ("rsskey param", f"https://x.org/rss?rsskey={KEY}&foo=1"),
        ("ntfy style auth", f"https://ntfy.sh/?auth={KEY}"),
        ("basic auth userinfo", "https://someuser:somepassword@tracker.example.org/announce"),
    ],
)
def test_no_credential_survives(label, url):
    assert KEY not in redact_secrets(url), label
    assert "somepassword" not in redact_secrets(url), label


def test_the_useful_part_of_the_url_is_kept():
    """Redaction has to leave enough to debug with, or people turn it off."""
    out = redact_secrets(f"https://darkpeers.org/announce/{KEY}")

    assert "darkpeers.org" in out
    assert "announce" in out


def test_it_survives_being_embedded_in_a_sentence():
    message = f"rewrote announce URL to https://darkpeers.org/announce/{KEY}"

    assert KEY not in redact_secrets(message)


def test_a_tracker_error_quoting_a_download_link_is_clean():
    message = (
        f"darkpeers: GET https://darkpeers.org/torrent/download/115069.{KEY} "
        "failed: Server disconnected"
    )

    out = redact_secrets(message)

    assert KEY not in out
    assert "Server disconnected" in out


def test_infohashes_are_left_alone():
    """40 hex chars, not a secret — and losing them makes history unusable."""
    infohash = "a" * 40
    assert infohash in redact_secrets(f"torrent {infohash} is unregistered")


def test_release_names_are_not_mangled():
    name = "Some.Release.2024.1080p.WEB-DL.DDP5.1.Atmos.H.264-GROUP"
    assert redact_secrets(f"repairing {name}") == f"repairing {name}"


def test_empty_input_is_safe():
    assert redact_secrets("") == ""


# --- enforcement at the choke point -----------------------------------------


async def test_history_rows_are_redacted_even_if_an_action_forgets():
    """The engine redacts on write, so no repair step can leak by omission."""
    from conftest import make_torrent

    from stallarr import db
    from stallarr.models import ActionKind, ActionResult, Ailment, Diagnosis, Finding
    from stallarr.recovery.engine import RecoveryEngine

    await db.init()
    engine = RecoveryEngine.__new__(RecoveryEngine)
    engine.cfg = type("C", (), {"tagging_enabled": False})()

    infohash = "b" * 40
    diagnosis = Diagnosis(
        torrent=make_torrent(infohash=infohash),
        findings=[Finding(Ailment.TRACKER_AUTH, "bad passkey")],
        tracker_id="darkpeers",
    )
    # A step that hands back a raw URL — exactly the mistake being guarded.
    leaky = ActionResult(ActionKind.REFRESH_ANNOUNCE, True, f"set https://darkpeers.org/announce/{KEY}")

    await engine._log(diagnosis, leaky)

    [row] = await db.get_history(infohash=infohash)
    assert KEY not in row["message"]


# --- notification endpoints --------------------------------------------------
# These carry the credential inside the URL, and an httpx connection error
# quotes the URL it failed on. Their tokens contain ':', '_' and '-', so the
# generic alphanumeric-segment rule does not cover them.


def test_a_telegram_bot_token_is_stripped_from_an_error():
    token = "123456789:AAH_ttPBvGxYm-abcdefghijklmnopqrst"
    message = f"telegram send failed: connecting to https://api.telegram.org/bot{token}/sendMessage"

    out = redact_secrets(message)

    assert token not in out
    assert "api.telegram.org" in out


def test_a_discord_webhook_token_is_stripped_from_an_error():
    secret = "aBcDeF_gHiJkL-mNoPqRsTuVwXyZ0123456789aBcDeF_gHiJkL-mNoPqRsTuV"
    message = f"discord webhook failed: https://discord.com/api/webhooks/987654321/{secret}"

    out = redact_secrets(message)

    assert secret not in out
    assert "discord.com" in out
