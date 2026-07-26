"""Prowlarr import. Fixtures are trimmed copies of a real /api/v1/indexer body."""

import pytest

from stallarr.config import Config
from stallarr.integrations import prowlarr


def indexer(name, definition, urls, fields, protocol="torrent"):
    return {
        "name": name,
        "definitionName": definition,
        "protocol": protocol,
        "indexerUrls": urls,
        "fields": [{"name": k, "value": v} for k, v in fields.items()],
    }


UNIT3D = indexer(
    "Darkpeers (API)", "darkpeers", ["https://darkpeers.org/"], {"apikey": "tok-dp"}
)
COOKIE_SITE = indexer(
    "IPTorrents",
    "IPTorrents",
    ["https://iptorrents.com/", "https://ipt.lol/"],
    # Prowlarr masks password-class fields rather than omitting them.
    {"cookie": "********", "baseUrl": "https://iptorrents.com/"},
)
UNKNOWN = indexer("seedpool (API)", "seedpool-api", ["https://seedpool.org/"], {"apikey": "tok-sp"})


async def test_matches_an_existing_plugin_by_announce_host():
    [proposal] = await prowlarr.plan([UNIT3D], probe=False)

    assert proposal.tracker_id == "darkpeers"
    assert proposal.values["api_key"] == "tok-dp"
    assert proposal.plugin == ""  # a plugin file owns this one


async def test_masked_secrets_are_never_treated_as_real():
    """Writing '********' as a cookie would look configured and fail every call."""
    [proposal] = await prowlarr.plan([COOKIE_SITE], probe=False)

    assert proposal.tracker_id == "iptorrents"
    assert "cookie" not in proposal.values


async def test_unknown_site_needs_a_confirmed_unit3d_api(monkeypatch):
    async def confirms(url, api_key):
        return True

    monkeypatch.setattr(prowlarr, "_speaks_unit3d", confirms)
    [proposal] = await prowlarr.plan([UNKNOWN])

    assert proposal.tracker_id == "seedpool"
    assert proposal.plugin == "unit3d"
    assert proposal.supported


async def test_unknown_site_that_fails_the_probe_is_reported_not_guessed():
    async def denies(url, api_key):
        return False

    original = prowlarr._speaks_unit3d
    prowlarr._speaks_unit3d = denies
    try:
        [proposal] = await prowlarr.plan([UNKNOWN])
    finally:
        prowlarr._speaks_unit3d = original

    assert not proposal.supported
    assert not proposal.values
    assert "ADDING_A_TRACKER" in proposal.detail


async def test_usenet_indexers_are_ignored():
    usenet = indexer("NZBgeek", "nzbgeek", ["https://nzbgeek.info/"], {}, protocol="usenet")
    assert await prowlarr.plan([usenet], probe=False) == []


# --- apply -------------------------------------------------------------------


async def test_import_fills_blanks_and_enables_new_trackers():
    cfg = Config()
    proposals = await prowlarr.plan([UNIT3D], probe=False)

    result = prowlarr.apply(cfg, proposals)

    tracker = cfg.trackers["darkpeers"]
    assert tracker.api_key == "tok-dp"
    assert tracker.enabled
    assert tracker.source == "prowlarr"
    assert "created" in result["imported"][0]["changed"]


async def test_import_does_not_clobber_hand_entered_secrets():
    """Prowlarr cannot supply passkeys or cookies, so it must not overwrite them."""
    cfg = Config.model_validate(
        {"trackers": {"darkpeers": {"enabled": True, "api_key": "mine", "passkey": "pk"}}}
    )
    proposals = await prowlarr.plan([UNIT3D], probe=False)

    prowlarr.apply(cfg, proposals)

    assert cfg.trackers["darkpeers"].api_key == "mine"
    assert cfg.trackers["darkpeers"].passkey == "pk"


async def test_overwrite_replaces_a_rotated_key():
    cfg = Config.model_validate({"trackers": {"darkpeers": {"api_key": "stale"}}})
    proposals = await prowlarr.plan([UNIT3D], probe=False)

    prowlarr.apply(cfg, proposals, overwrite=True)

    assert cfg.trackers["darkpeers"].api_key == "tok-dp"


async def test_resync_leaves_a_deliberately_disabled_tracker_off():
    cfg = Config.model_validate({"trackers": {"darkpeers": {"enabled": False}}})
    proposals = await prowlarr.plan([UNIT3D], probe=False)

    prowlarr.apply(cfg, proposals, enable=True)

    assert cfg.trackers["darkpeers"].enabled is False


async def test_announce_hosts_from_prowlarr_are_added_not_replaced():
    cfg = Config.model_validate({"trackers": {"iptorrents": {"extra_hosts": ["kept.example"]}}})
    proposals = await prowlarr.plan([COOKIE_SITE], probe=False)

    prowlarr.apply(cfg, proposals)

    hosts = cfg.trackers["iptorrents"].extra_hosts
    assert "kept.example" in hosts
    assert "ipt.lol" in hosts


async def test_public_view_never_carries_secret_values():
    [proposal] = await prowlarr.plan([UNIT3D], probe=False)

    payload = proposal.public()

    assert payload["provides"] == ["api_key"]
    assert "tok-dp" not in repr(payload)


@pytest.mark.parametrize(
    "definition,expected",
    [("seedpool-api", "seedpool"), ("yuscene-api", "yuscene"), ("oldtoonsworld", "oldtoonsworld")],
)
def test_definition_names_become_stable_tracker_ids(definition, expected):
    assert prowlarr._slug(definition) == expected
