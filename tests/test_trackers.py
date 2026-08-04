import pathlib

import pytest

from stallarr.config import TrackerConfig
from stallarr.trackers import TrackerRouter, discover, registry
from stallarr.trackers.base import Capability
from stallarr.trackers.iptorrents import IPTorrents
from stallarr.trackers.unit3d import Unit3dPlugin


def test_all_three_trackers_are_registered():
    discover()
    assert {"iptorrents", "oldtoons", "darkpeers"} <= set(registry)


def test_registry_ids_are_unique_and_populated():
    discover()
    for plugin_id, cls in registry.items():
        assert cls.id == plugin_id
        assert cls.name, f"{plugin_id} has no display name"
        assert cls.hosts, f"{plugin_id} declares no announce hosts"


def test_host_matching_covers_subdomains():
    plugin = registry["darkpeers"](TrackerConfig(url="https://darkpeers.org"))
    assert plugin.matches_host("darkpeers.org")
    assert plugin.matches_host("tracker.darkpeers.org")
    assert not plugin.matches_host("darkpeers.org.evil.com")
    assert not plugin.matches_host("iptorrents.com")


def test_extra_hosts_extend_matching_without_code_changes():
    plugin = registry["oldtoons"](TrackerConfig(extra_hosts=["newdomain.example"]))
    assert plugin.matches_host("newdomain.example")


def test_router_picks_the_owning_plugin():
    discover()
    plugins = {pid: cls(TrackerConfig()) for pid, cls in registry.items()}
    router = TrackerRouter(plugins)
    assert router.for_hosts(["darkpeers.org"]).id == "darkpeers"
    assert router.for_hosts(["localhost.stackoverflow.tech"]).id == "iptorrents"
    assert router.for_hosts(["some.random.tracker"]) is None


# --- IPTorrents ---------------------------------------------------------------


async def test_ipt_rewrites_passkey_but_keeps_announce_host():
    """IPT rotates announce domains — the one already on the torrent is correct."""
    plugin = IPTorrents(TrackerConfig(passkey="n" * 32))
    fresh = await plugin.announce_url(
        "a" * 40, current="http://localhost.stackoverflow.tech:80/" + "o" * 32 + "/announce"
    )
    assert fresh == "http://localhost.stackoverflow.tech:80/" + "n" * 32 + "/announce"


async def test_ipt_reports_no_change_when_passkey_already_correct():
    plugin = IPTorrents(TrackerConfig(passkey="n" * 32))
    current = "http://empirehost.me/" + "n" * 32 + "/announce"
    assert await plugin.announce_url("a" * 40, current=current) is None


async def test_ipt_builds_a_url_when_there_is_nothing_to_rewrite():
    plugin = IPTorrents(TrackerConfig(passkey="n" * 32))
    assert (await plugin.announce_url("a" * 40, current=None)).endswith("/" + "n" * 32 + "/announce")


async def test_ipt_without_passkey_cannot_repair_announce():
    plugin = IPTorrents(TrackerConfig())
    assert await plugin.announce_url("a" * 40) is None


def test_ipt_capabilities_depend_on_credentials():
    passkey_only = IPTorrents(TrackerConfig(passkey="k" * 32))
    assert passkey_only.can(Capability.ANNOUNCE_URL)
    assert not passkey_only.can(Capability.TORRENT_INFO)

    with_cookie = IPTorrents(TrackerConfig(passkey="k" * 32, cookie="uid=1; pass=2"))
    assert with_cookie.can(Capability.TORRENT_INFO)
    assert with_cookie.can(Capability.TORRENT_FILE)


def test_ipt_classifies_its_own_error_wording():
    plugin = IPTorrents(TrackerConfig())
    assert plugin.classify_message("Torrent not found") == "unregistered"
    assert plugin.classify_message("Invalid announce key") == "auth"
    assert plugin.classify_message("") is None


# --- UNIT3D -------------------------------------------------------------------


def test_unit3d_flattens_jsonapi_payloads():
    payload = {
        "data": [
            {"type": "torrent", "id": "4211", "attributes": {"name": "Foo", "seeders": 9}},
        ]
    }
    records = Unit3dPlugin._attributes(payload)
    assert records == [{"name": "Foo", "seeders": 9, "id": "4211"}]


def test_unit3d_handles_flat_payloads_too():
    payload = {"data": {"id": 7, "name": "Bar"}}
    assert Unit3dPlugin._attributes(payload)[0]["name"] == "Bar"


def test_unit3d_ignores_malformed_payloads():
    assert Unit3dPlugin._attributes({"data": "nope"}) == []
    assert Unit3dPlugin._attributes(["wrong"]) == []


async def test_unit3d_announce_template_is_overridable():
    cfg = TrackerConfig(
        url="https://darkpeers.org",
        passkey="pk123",
        options={"announce_template": "https://tracker.darkpeers.org/announce/{passkey}"},
    )
    plugin = registry["darkpeers"](cfg)
    assert await plugin.announce_url("a" * 40) == "https://tracker.darkpeers.org/announce/pk123"


async def test_unit3d_announce_needs_a_passkey():
    plugin = registry["darkpeers"](TrackerConfig(url="https://darkpeers.org"))
    assert await plugin.announce_url("a" * 40) is None


# --- config-defined trackers -------------------------------------------------
# A site with no plugin file still works if it runs stock UNIT3D. This is what
# lets the Prowlarr import bring in trackers nobody has written code for.


def test_a_config_defined_tracker_behaves_like_a_real_plugin():
    from stallarr.config import TrackerConfig
    from stallarr.trackers import build_plugin

    plugin = build_plugin(
        "seedpool",
        TrackerConfig(
            enabled=True,
            plugin="unit3d",
            name="seedpool",
            url="https://seedpool.org",
            api_key="tok",
            hosts=["seedpool.org"],
        ),
    )

    assert plugin.id == "seedpool"
    assert plugin.name == "seedpool"
    assert plugin.matches_host("tracker.seedpool.org")
    assert plugin.can(Capability.TORRENT_INFO)


def test_a_plugin_file_wins_over_the_config_base():
    """Writing darkpeers.py must upgrade an entry that used the generic driver.

    Runs in a fresh interpreter on purpose. The failure this guards against only
    appears when nothing has populated the registry yet: build_plugin then hands
    a real tracker the generic driver, silently dropping its default URL and
    announce hosts. Any in-process test would have imported the plugins already.
    """
    import os
    import subprocess
    import sys

    import stallarr

    script = """
from stallarr.config import TrackerConfig
from stallarr.trackers import build_plugin

plugin = build_plugin("darkpeers", TrackerConfig(enabled=True, plugin="unit3d"))
print(type(plugin).__name__, plugin.base_url)
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(pathlib.Path(stallarr.__file__).parent.parent),
    }
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, env=env
    ).stdout.strip()

    assert out == "DarkPeers https://darkpeers.org"


def test_unknown_base_plugin_is_rejected_loudly():
    from stallarr.config import TrackerConfig
    from stallarr.trackers import build_plugin

    with pytest.raises(ValueError, match="unknown base plugin"):
        build_plugin("whatever", TrackerConfig(enabled=True, plugin="nonsense"))


def test_generic_bases_are_not_offered_as_trackers():
    """`unit3d` describes no site; enabling it as a tracker would be meaningless."""
    from stallarr.trackers import base_plugins, discover, registry

    discover()
    assert set(base_plugins()) & set(registry) == set()


# --- UNIT3D account stats ----------------------------------------------------
# UNIT3D returns these pre-formatted for humans, not as numbers.


class _StubResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


async def _stats_from(payload):
    plugin = Unit3dPlugin(TrackerConfig(enabled=True, url="https://x.test", api_key="tok"))
    plugin.get = lambda *a, **k: _async(_StubResponse(payload))
    return await plugin.user_stats()


async def _async(value):
    return value


async def test_human_readable_sizes_are_parsed_to_bytes():
    stats = await _stats_from({"uploaded": "772.27 GiB", "downloaded": "3.57 GiB"})

    assert stats.uploaded == int(772.27 * 1024**3)
    assert stats.downloaded == int(3.57 * 1024**3)


async def test_a_clean_account_reports_zero_not_unknown():
    """`x or y` would turn 0 hit-and-runs into None — the good case looking broken."""
    stats = await _stats_from({"hit_and_runs": 0, "seedbonus": 0})

    assert stats.hit_and_runs == 0
    assert stats.bonus == 0.0


async def test_alternate_unit3d_spellings_are_accepted():
    stats = await _stats_from({"hitandruns": 4, "bonus": "81267.95"})

    assert stats.hit_and_runs == 4
    assert stats.bonus == 81267.95


async def test_raw_byte_counts_still_work():
    assert (await _stats_from({"uploaded": 1234})).uploaded == 1234


async def test_unparseable_sizes_become_none_rather_than_wrong_numbers():
    assert (await _stats_from({"uploaded": "lots"})).uploaded is None


# --- UNIT3D freeleech status --------------------------------------------------
# Opt-in and best-effort: the "my uploads" filter param and the freeleech
# field name both vary across UNIT3D forks and are unverified against a live
# instance, hence the off-by-default gate and the overridable field name.


def _freeleech_plugin(**options):
    return Unit3dPlugin(
        TrackerConfig(
            enabled=True,
            url="https://x.test",
            api_key="tok",
            options={"freeleech_enabled": True, "freeleech_username": "me", **options},
        )
    )


async def _freeleech_from(payload, **options):
    plugin = _freeleech_plugin(**options)
    plugin.get = lambda *a, **k: _async(_StubResponse(payload))
    return await plugin.freeleech_status()


def test_freeleech_capability_is_off_by_default():
    plugin = Unit3dPlugin(TrackerConfig(enabled=True, api_key="tok"))
    assert Capability.FREELEECH_STATUS not in plugin.capabilities


def test_freeleech_capability_requires_opting_in():
    plugin = _freeleech_plugin()
    assert Capability.FREELEECH_STATUS in plugin.capabilities


async def test_freeleech_status_needs_a_username():
    """No username configured means no lookup, not a request with an empty
    filter — that would return every torrent on the site, not just ours."""
    plugin = Unit3dPlugin(
        TrackerConfig(enabled=True, api_key="tok", options={"freeleech_enabled": True})
    )
    plugin.get = lambda *a, **k: _async(_StubResponse({"data": []}))
    assert await plugin.freeleech_status() == []


async def test_freeleech_status_is_parsed_from_the_default_field():
    payload = {
        "data": [
            {"id": "1", "name": "Some.Release-GRP", "free": True},
            {"id": "2", "name": "Other.Release-GRP", "free": False},
        ]
    }
    entries = await _freeleech_from(payload)

    assert {e.name: e.freeleech for e in entries} == {
        "Some.Release-GRP": True,
        "Other.Release-GRP": False,
    }


async def test_freeleech_field_name_is_overridable():
    """Forks spell this differently; getting the field wrong must read as
    unknown, not silently as False (see `_as_bool`)."""
    payload = {"data": [{"id": "1", "name": "Some.Release-GRP", "download_multiplier": 0}]}

    entries = await _freeleech_from(payload, freeleech_field="download_multiplier")

    assert entries[0].freeleech is False


async def test_an_unrecognised_freeleech_field_reports_unknown_not_false():
    payload = {"data": [{"id": "1", "name": "Some.Release-GRP"}]}

    entries = await _freeleech_from(payload)

    assert entries[0].freeleech is None


# --- cookie assembly ---------------------------------------------------------
# The UI collects uid and pass separately because that is how a browser's
# storage inspector shows them. Getting the header wrong here fails silently:
# the site just serves a logged-out page.


def test_split_cookie_fields_are_assembled_into_a_header():
    plugin = IPTorrents(TrackerConfig(cookie_uid="123456", cookie_pass="deadbeef"))
    assert plugin.cookie_header == "uid=123456; pass=deadbeef"
    assert plugin.default_headers()["Cookie"] == "uid=123456; pass=deadbeef"


def test_a_raw_cookie_header_overrides_the_split_fields():
    """The raw box is the escape hatch for sites needing more than uid/pass."""
    plugin = IPTorrents(
        TrackerConfig(cookie="uid=1; pass=2; cf_clearance=3", cookie_uid="9", cookie_pass="9")
    )
    assert plugin.cookie_header == "uid=1; pass=2; cf_clearance=3"


def test_pasted_name_prefixes_are_tolerated():
    """Copying a cookie table cell can bring the name along with the value."""
    plugin = IPTorrents(TrackerConfig(cookie_uid="uid=123456", cookie_pass="pass=deadbeef"))
    assert plugin.cookie_header == "uid=123456; pass=deadbeef"


def test_half_a_cookie_is_no_cookie():
    """uid alone is not a session; claiming it is would look configured and
    then fail every request."""
    plugin = IPTorrents(TrackerConfig(cookie_uid="123456"))
    assert plugin.cookie_header == ""
    assert "Cookie" not in plugin.default_headers()
    assert Capability.TORRENT_INFO not in plugin.capabilities


def test_split_cookie_unlocks_the_same_capabilities_as_a_raw_one():
    split = IPTorrents(TrackerConfig(cookie_uid="1", cookie_pass="2"))
    raw = IPTorrents(TrackerConfig(cookie="uid=1; pass=2"))
    assert split.capabilities == raw.capabilities
    assert Capability.USER_STATS in split.capabilities


def test_iptorrents_declares_no_api_token_field():
    """The settings form is built from this; IPT has no API token to offer."""
    assert "api_key" not in IPTorrents.credential_fields
    assert "cookie_uid" in IPTorrents.credential_fields
    assert Unit3dPlugin.credential_fields == ("api_key", "passkey")
