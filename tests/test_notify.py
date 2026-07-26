"""Notification routing, with ntfy covered end to end."""

import base64
import secrets

import httpx
import pytest
from conftest import make_torrent

from stallarr.config import NotifyConfig
from stallarr.models import Ailment, Diagnosis, Finding, Severity
from stallarr.notify import Notifier, _ntfy_endpoint
from stallarr.web.routes import _redact_config


def notifier(**overrides):
    """A Notifier whose HTTP client records requests instead of sending them."""
    cfg = NotifyConfig(**overrides)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, text="")

    note = Notifier(cfg, base_url="https://stallarr.example")
    note._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return note, sent


def ntfy_kwargs(**extra):
    return {"ntfy_enabled": True, "ntfy_topic": "torrents", "min_severity": 1, **extra}


# --- channel selection -------------------------------------------------------


def test_ntfy_without_a_topic_is_not_a_channel():
    """Enabled but unconfigured would silently drop every alert."""
    note, _ = notifier(ntfy_enabled=True)
    assert not note.enabled


def test_ntfy_with_a_topic_is_a_channel():
    note, _ = notifier(**ntfy_kwargs())
    assert note._channels() == ["ntfy"]


def test_channels_are_independent():
    note, _ = notifier(**ntfy_kwargs(discord_enabled=True, discord_webhook_url="https://d.example"))
    assert set(note._channels()) == {"ntfy", "discord"}


# --- publishing --------------------------------------------------------------


async def send_one(severity=Severity.CRITICAL, **cfg):
    """Drive one alert all the way through `alert()`, including the dedupe gate.

    Each call gets a fresh infohash: dedupe is keyed on infohash+ailment with a
    12h window, so reusing one would silently suppress every later test here.
    """
    from stallarr import db

    await db.init()

    note, sent = notifier(**ntfy_kwargs(**cfg))
    ailment = {
        Severity.CRITICAL: Ailment.UNREGISTERED,
        Severity.WARNING: Ailment.STALLED_DOWNLOAD,
        Severity.INFO: Ailment.TRACKER_DOWN,
    }[severity]
    diagnosis = Diagnosis(
        torrent=make_torrent(name="Some.Release.2024", infohash=secrets.token_hex(20)),
        findings=[Finding(ailment, "detail here")],
        tracker_id="darkpeers",
    )
    await note.alert(diagnosis, None)
    return sent


async def test_publishes_json_to_the_server_root():
    [request] = await send_one()

    assert str(request.url) == "https://ntfy.sh"
    assert request.headers["content-type"] == "application/json"


async def test_payload_carries_topic_title_and_click_through():
    import json

    [request] = await send_one()
    body = json.loads(request.content)

    assert body["topic"] == "torrents"
    assert "Unregistered" in body["title"]
    assert "Some.Release.2024" in body["message"]
    assert body["click"] == "https://stallarr.example"


@pytest.mark.parametrize(
    "severity,priority",
    [(Severity.CRITICAL, 4), (Severity.WARNING, 3), (Severity.INFO, 2)],
)
async def test_priority_tracks_severity_but_never_bypasses_do_not_disturb(severity, priority):
    """Priority 5 wakes people at 3am. A stuck torrent does not qualify."""
    import json

    [request] = await send_one(severity)

    assert json.loads(request.content)["priority"] == priority
    assert json.loads(request.content)["priority"] < 5


async def test_unicode_survives_the_json_body():
    """The header protocol (X-Title) is latin-1 and would raise on this."""
    import json

    [request] = await send_one()
    body = json.loads(request.content)

    assert body["title"].startswith("🔴")


# --- auth --------------------------------------------------------------------


async def test_a_token_is_sent_as_a_bearer_header():
    [request] = await send_one(ntfy_token="tk_secret")

    assert request.headers["authorization"] == "Bearer tk_secret"


async def test_username_and_password_become_basic_auth():
    [request] = await send_one(ntfy_username="u", ntfy_password="p")

    expected = base64.b64encode(b"u:p").decode()
    assert request.headers["authorization"] == f"Basic {expected}"


async def test_a_token_wins_over_basic_auth():
    [request] = await send_one(ntfy_token="tk_secret", ntfy_username="u", ntfy_password="p")

    assert request.headers["authorization"] == "Bearer tk_secret"


async def test_no_credentials_sends_no_auth_header():
    [request] = await send_one()

    assert "authorization" not in request.headers


# --- URL hygiene -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://ntfy.sh", "https://ntfy.sh"),
        ("https://ntfy.sh/", "https://ntfy.sh"),
        # A pasted `?auth=` token must never reach an httpx error message.
        ("https://ntfy.sh/?auth=tk_secret", "https://ntfy.sh"),
        ("https://user:pw@ntfy.example.org", "https://ntfy.example.org"),
        # Self-hosted instances live under a path; keep it.
        ("https://example.org/ntfy/", "https://example.org/ntfy"),
        ("http://ntfy:8080", "http://ntfy:8080"),
    ],
)
def test_endpoint_strips_credentials_but_keeps_the_path(raw, expected):
    assert _ntfy_endpoint(raw) == expected


async def test_a_token_in_the_url_is_not_used_as_the_request_target():
    [request] = await send_one(ntfy_url="https://ntfy.sh/?auth=tk_secret")

    assert "tk_secret" not in str(request.url)


# --- secret handling ---------------------------------------------------------


def test_ntfy_secrets_are_redacted_before_reaching_the_browser():
    """`ntfy_token` matches no marker until "token" is in _SECRET_FIELDS."""
    from stallarr.config import Config

    cfg = Config()
    cfg.notify.ntfy_token = "tk_secret"
    cfg.notify.ntfy_password = "hunter2"
    cfg.notify.ntfy_topic = "torrents"

    data = _redact_config(cfg)

    assert data["notify"]["ntfy_token"] == "__SET__"
    assert data["notify"]["ntfy_password"] == "__SET__"
    # The topic is not a secret — it has to stay visible to be editable.
    assert data["notify"]["ntfy_topic"] == "torrents"


# --- test button -------------------------------------------------------------


async def test_the_test_button_reports_every_configured_channel():
    note, sent = notifier(**ntfy_kwargs(discord_enabled=True, discord_webhook_url="https://d.test"))

    result = await note.test()

    assert result == {"discord": "sent", "ntfy": "sent"}
    assert len(sent) == 2


async def test_the_test_button_explains_an_unconfigured_channel():
    note, sent = notifier(ntfy_enabled=True)  # enabled, but no topic

    result = await note.test()

    assert "error" in result
    assert not sent
