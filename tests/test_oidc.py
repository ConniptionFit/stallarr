"""OIDC single sign-on: the transaction envelope and the group gate.

The network halves (discovery, token exchange, userinfo) are thin httpx calls;
what is worth testing is everything that decides whether a login is *accepted*.
"""

from __future__ import annotations

import time

import pytest

from stallarr.config import Config
from stallarr.web import oidc
from stallarr.web.oidc import Identity, OIDCError

pytestmark = pytest.mark.asyncio


def web(**kwargs):
    cfg = Config()
    cfg.web.session_secret = "s" * 64
    cfg.web.oidc_enabled = True
    cfg.web.oidc_issuer = "https://auth.example.com/application/o/stallarr/"
    cfg.web.oidc_client_id = "client"
    cfg.web.oidc_client_secret = "secret"
    for key, value in kwargs.items():
        setattr(cfg.web, key, value)
    return cfg.web


# --- configured ---------------------------------------------------------------


async def test_sso_is_off_until_every_field_is_present():
    """Enabled-but-incomplete has to read as off, or a half-filled form locks
    everyone out of the settings page that would fix it."""
    assert oidc.configured(web())
    assert not oidc.configured(web(oidc_enabled=False))
    assert not oidc.configured(web(oidc_issuer=""))
    assert not oidc.configured(web(oidc_client_id=""))
    assert not oidc.configured(web(oidc_client_secret=""))


# --- the transaction cookie ---------------------------------------------------


async def test_a_tampered_transaction_is_rejected():
    cfg = web()
    sealed = oidc._seal(cfg.session_secret, {"state": "abc", "exp": time.time() + 60})
    body, digest = sealed.split(".", 1)

    with pytest.raises(OIDCError):
        oidc._unseal(cfg.session_secret, f"{body}x.{digest}")


async def test_a_transaction_signed_with_another_secret_is_rejected():
    sealed = oidc._seal("other-secret", {"state": "abc", "exp": time.time() + 60})

    with pytest.raises(OIDCError):
        oidc._unseal(web().session_secret, sealed)


async def test_an_expired_transaction_is_rejected():
    cfg = web()
    sealed = oidc._seal(cfg.session_secret, {"state": "abc", "exp": time.time() - 1})

    with pytest.raises(OIDCError, match="too long"):
        oidc._unseal(cfg.session_secret, sealed)


async def test_a_missing_transaction_is_rejected_not_crashed():
    with pytest.raises(OIDCError):
        oidc._unseal(web().session_secret, "")


async def test_a_round_trip_survives():
    cfg = web()
    payload = {"state": "abc", "verifier": "v", "exp": time.time() + 60}
    assert oidc._unseal(cfg.session_secret, oidc._seal(cfg.session_secret, payload)) == payload


# --- redirect URI -------------------------------------------------------------


async def test_an_explicit_redirect_url_wins_over_the_request():
    cfg = web(oidc_redirect_url="https://stallarr.example.com", base_url="https://ignored")
    assert oidc.redirect_uri(cfg, "http://spoofed") == (
        "https://stallarr.example.com" + oidc.CALLBACK_PATH
    )


async def test_a_redirect_url_that_already_names_the_callback_is_left_alone():
    """Otherwise pasting the value the IdP shows you doubles the path."""
    full = "https://stallarr.example.com" + oidc.CALLBACK_PATH
    assert oidc.redirect_uri(web(oidc_redirect_url=full), "http://x") == full


async def test_the_base_url_is_used_before_the_request():
    cfg = web(base_url="https://stallarr.example.com/")
    assert oidc.redirect_uri(cfg, "http://spoofed") == (
        "https://stallarr.example.com" + oidc.CALLBACK_PATH
    )


async def test_the_request_origin_is_the_last_resort():
    assert oidc.redirect_uri(web(), "http://docker:8383") == (
        "http://docker:8383" + oidc.CALLBACK_PATH
    )


async def test_no_derivable_redirect_url_is_an_error():
    with pytest.raises(OIDCError):
        oidc.redirect_uri(web(), "")


# --- the group gate -----------------------------------------------------------


def identity(groups=()):
    return Identity(subject="sub", username="alice", groups=list(groups))


async def test_an_empty_allow_list_admits_anyone():
    oidc._check_groups(web(), identity())


async def test_a_member_of_an_allowed_group_is_admitted():
    oidc._check_groups(web(oidc_allowed_groups=["media", "admins"]), identity(["media"]))


async def test_a_non_member_is_refused():
    with pytest.raises(OIDCError, match="not in a group"):
        oidc._check_groups(web(oidc_allowed_groups=["admins"]), identity(["users"]))


async def test_a_missing_groups_claim_fails_closed():
    """The usual cause is the scope not being granted. Reading that as "no
    groups, so no restriction" would turn a tightening into an opening."""
    with pytest.raises(OIDCError, match="groups"):
        oidc._check_groups(web(oidc_allowed_groups=["admins"]), identity())


async def test_an_all_blank_allow_list_counts_as_no_restriction():
    """A list field left empty in the UI can arrive as [""]. Reading that as a
    restriction nobody satisfies would lock the operator out of their own
    dashboard on a save they thought changed nothing."""
    oidc._check_groups(web(oidc_allowed_groups=["", "  "]), identity(["users"]))


async def test_blanks_alongside_a_real_group_do_not_widen_it():
    with pytest.raises(OIDCError, match="not in a group"):
        oidc._check_groups(web(oidc_allowed_groups=["", "admins"]), identity(["users"]))


# --- begin --------------------------------------------------------------------


async def test_begin_sends_a_pkce_challenge_and_keeps_the_verifier_private(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    async def fake_discover(_cfg):
        return {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "userinfo_endpoint": "https://auth.example.com/userinfo",
        }

    monkeypatch.setattr(oidc, "discover", fake_discover)
    cfg = web(base_url="https://stallarr.example.com")

    url, txn = await oidc.begin(cfg, "http://docker:8383", "/settings")
    params = parse_qs(urlparse(url).query)

    assert params["code_challenge_method"] == ["S256"]
    assert "code_verifier" not in params  # the verifier must never hit the wire
    assert params["redirect_uri"] == ["https://stallarr.example.com" + oidc.CALLBACK_PATH]

    payload = oidc._unseal(cfg.session_secret, txn)
    assert payload["state"] == params["state"][0]
    assert payload["next"] == "/settings"
    # The redirect URI is replayed from the transaction, not recomputed: the
    # token endpoint compares it byte for byte with the one sent here.
    assert payload["uri"] == params["redirect_uri"][0]


async def test_a_mismatched_state_is_refused(monkeypatch):
    cfg = web()
    txn = oidc._seal(cfg.session_secret, {"state": "expected", "exp": time.time() + 60})

    with pytest.raises(OIDCError, match="state"):
        await oidc.complete(cfg, code="c", state="attacker-supplied", txn=txn)


# --- discovery ----------------------------------------------------------------


async def test_discovery_is_cached_then_forgettable(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "authorization_endpoint": "https://a/authorize",
                "token_endpoint": "https://a/token",
                "userinfo_endpoint": "https://a/userinfo",
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, url):
            calls.append(url)
            return FakeResponse()

    monkeypatch.setattr(oidc.httpx, "AsyncClient", lambda **_: FakeClient())
    oidc.forget_discovery()
    cfg = web()

    await oidc.discover(cfg)
    await oidc.discover(cfg)
    assert len(calls) == 1
    assert calls[0].endswith("/.well-known/openid-configuration")

    oidc.forget_discovery()
    await oidc.discover(cfg)
    assert len(calls) == 2
