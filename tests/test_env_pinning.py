"""An env var beats the config file, which makes the matching UI field a lie
unless we say so. These cover the reporting that keeps the two honest."""

import os

from stallarr import config as config_module
from stallarr.web.routes import _pinned_in_payload


def test_nothing_is_pinned_without_env_vars(monkeypatch):
    for key in list(os.environ):
        if key.startswith("STALLARR_"):
            monkeypatch.delenv(key)
    assert config_module.env_pinned_paths() == {}


def test_a_pinned_path_names_the_variable_responsible(monkeypatch):
    monkeypatch.setenv("STALLARR_QBIT_URL", "http://pinned:8080")
    assert config_module.env_pinned_paths()["qbittorrent.url"] == "STALLARR_QBIT_URL"


def test_per_tracker_secrets_are_pinned_too(monkeypatch):
    monkeypatch.setenv("STALLARR_TRACKER_DARKPEERS_API_KEY", "tok")
    pinned = config_module.env_pinned_paths()
    assert pinned["trackers.darkpeers.api_key"] == "STALLARR_TRACKER_DARKPEERS_API_KEY"


def test_pinning_report_matches_what_load_actually_applies(monkeypatch):
    """The two must agree, or the UI locks the wrong fields."""
    monkeypatch.setenv("STALLARR_QBIT_URL", "http://pinned:8080")
    monkeypatch.setenv("STALLARR_SCAN_INTERVAL_MINUTES", "3")

    applied = config_module._apply_env({})
    pinned = config_module.env_pinned_paths()

    assert applied["qbittorrent"]["url"] == "http://pinned:8080"
    assert applied["scan_interval_minutes"] == 3
    assert set(pinned) == {"qbittorrent.url", "scan_interval_minutes"}


def test_saving_a_pinned_field_is_reported_back(monkeypatch):
    monkeypatch.setenv("STALLARR_QBIT_URL", "http://pinned:8080")

    ignored = _pinned_in_payload({"qbittorrent": {"url": "http://typed-by-user:8080"}})

    assert ignored == [{"path": "qbittorrent.url", "env": "STALLARR_QBIT_URL"}]


def test_saving_an_unpinned_field_reports_nothing(monkeypatch):
    monkeypatch.setenv("STALLARR_QBIT_URL", "http://pinned:8080")

    assert _pinned_in_payload({"qbittorrent": {"username": "powers"}}) == []
