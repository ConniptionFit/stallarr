from stallarr.config import Config
from stallarr.web.routes import _merge, _redact_config


def test_secrets_are_redacted_but_presence_is_visible():
    cfg = Config()
    cfg.qbittorrent.password = "hunter2"
    cfg.notify.telegram_bot_token = "12345:abc"

    data = _redact_config(cfg)

    assert data["qbittorrent"]["password"] == "__SET__"
    assert data["notify"]["telegram_bot_token"] == "__SET__"
    assert data["qbittorrent"]["url"] == cfg.qbittorrent.url


def test_empty_secrets_are_not_marked_as_set():
    assert _redact_config(Config())["qbittorrent"]["password"] == ""


def test_nested_tracker_secrets_are_redacted():
    cfg = Config.model_validate(
        {"trackers": {"darkpeers": {"enabled": True, "api_key": "tok", "passkey": "pk"}}}
    )

    data = _redact_config(cfg)

    assert data["trackers"]["darkpeers"]["api_key"] == "__SET__"
    assert data["trackers"]["darkpeers"]["enabled"] is True


def test_merge_keeps_existing_secret_when_ui_sends_the_sentinel():
    base = {"qbittorrent": {"password": "real", "url": "http://old"}}
    merged = _merge(base, {"qbittorrent": {"password": "__SET__", "url": "http://new"}})

    assert merged["qbittorrent"]["password"] == "real"
    assert merged["qbittorrent"]["url"] == "http://new"


def test_merge_is_deep_and_leaves_untouched_branches_alone():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    merged = _merge(base, {"a": {"y": 9}})

    assert merged == {"a": {"x": 1, "y": 9}, "b": 3}


def test_merge_allows_clearing_a_secret_explicitly():
    merged = _merge({"web": {"password": "old"}}, {"web": {"password": ""}})
    assert merged["web"]["password"] == ""


def test_config_roundtrips_through_validation():
    cfg = Config()
    assert Config.model_validate(cfg.model_dump(mode="json")) == cfg


# --- session hardening -------------------------------------------------------


def test_changing_the_password_invalidates_existing_sessions():
    """Otherwise rotating a leaked password locks nobody out."""
    import time

    from stallarr.web.routes import _session_key, _session_subject, _sign

    cfg = Config()
    cfg.web.session_secret = "s" * 64
    cfg.web.password = "old"
    expires = int(time.time()) + 3600
    cookie = _sign(_session_key(cfg.web), expires)

    assert _session_subject(_session_key(cfg.web), cookie) == "local"

    cfg.web.password = "new"
    assert _session_subject(_session_key(cfg.web), cookie) is None


def test_rotating_the_oidc_secret_invalidates_existing_sessions():
    """Revoking the application in the IdP has to end the sessions it issued."""
    import time

    from stallarr.web.routes import _session_key, _session_subject, _sign

    cfg = Config()
    cfg.web.session_secret = "s" * 64
    cfg.web.oidc_client_secret = "old-secret"
    cookie = _sign(_session_key(cfg.web), int(time.time()) + 3600, "alice")

    assert _session_subject(_session_key(cfg.web), cookie) == "alice"

    cfg.web.oidc_client_secret = "new-secret"
    assert _session_subject(_session_key(cfg.web), cookie) is None


def test_an_expired_cookie_is_rejected():
    import time

    from stallarr.web.routes import _session_key, _session_subject, _sign

    cfg = Config()
    cfg.web.session_secret = "s" * 64
    cfg.web.password = "pw"
    stale = _sign(_session_key(cfg.web), int(time.time()) - 1)

    assert _session_subject(_session_key(cfg.web), stale) is None


def test_a_forged_cookie_is_rejected():
    import time

    from stallarr.web.routes import _session_key, _session_subject

    cfg = Config()
    cfg.web.session_secret = "s" * 64
    cfg.web.password = "pw"
    forged = f"{int(time.time()) + 3600}.{'0' * 64}"

    assert _session_subject(_session_key(cfg.web), forged) is None


def test_a_session_subject_cannot_be_swapped_for_another():
    """The subject is signed, not merely carried — otherwise anyone with a valid
    session could rewrite the cookie to claim someone else's identity."""
    import base64
    import time

    from stallarr.web.routes import _session_key, _session_subject, _sign

    cfg = Config()
    cfg.web.session_secret = "s" * 64
    cfg.web.oidc_client_secret = "x"
    expires = int(time.time()) + 3600
    mine = _sign(_session_key(cfg.web), expires, "alice")

    _, _, digest = mine.split(".", 2)
    theirs = base64.urlsafe_b64encode(b"admin").decode().rstrip("=")
    tampered = f"{expires}.{theirs}.{digest}"

    assert _session_subject(_session_key(cfg.web), tampered) is None


def test_split_cookie_halves_are_both_treated_as_secrets():
    """Both match _SECRET_FIELDS via "cookie". A new secret field that matches
    none of those markers is served to the browser in the clear."""
    cfg = Config.model_validate(
        {"trackers": {"iptorrents": {"cookie_uid": "123456", "cookie_pass": "deadbeef"}}}
    )

    data = _redact_config(cfg)

    assert data["trackers"]["iptorrents"]["cookie_uid"] == "__SET__"
    assert data["trackers"]["iptorrents"]["cookie_pass"] == "__SET__"


def test_a_new_recovery_step_is_granted_to_an_existing_config():
    """A saved ladder is a permission list. Without migration, every step added
    after a config was written is silently disabled on that install forever."""
    from stallarr.config import LADDER_VERSION, _migrate_ladder

    cfg = Config.model_validate(
        {"recovery": {"ladder": ["resume", "reannounce", "escalate"], "ladder_version": 1}}
    )

    changed = _migrate_ladder(cfg)

    assert changed
    assert "lift_share_limits" in cfg.recovery.ladder
    assert cfg.recovery.ladder[-1] == "escalate", "escalate must stay the last resort"
    assert cfg.recovery.ladder_version == LADDER_VERSION


def test_migration_does_not_resurrect_a_deliberately_removed_step():
    """Only steps introduced *after* this config was written are added back."""
    from stallarr.config import LADDER_VERSION, _migrate_ladder

    cfg = Config.model_validate(
        {"recovery": {"ladder": ["resume", "escalate"], "ladder_version": LADDER_VERSION}}
    )

    assert _migrate_ladder(cfg) is False
    assert "recheck" not in cfg.recovery.ladder


def test_a_fresh_config_already_has_every_step():
    from stallarr.models import DEFAULT_LADDER

    cfg = Config()

    assert cfg.recovery.ladder == [str(step) for step in DEFAULT_LADDER]
