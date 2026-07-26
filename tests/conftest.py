"""DATA_DIR must be redirected before stallarr.config is imported anywhere."""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="stallarr-tests-")
os.environ["DATA_DIR"] = _TMP

import pytest

from stallarr.config import Config, RuleConfig
from stallarr.models import Torrent, TorrentState, TrackerEntry


@pytest.fixture
def rules() -> RuleConfig:
    return RuleConfig()


@pytest.fixture
def cfg() -> Config:
    return Config()


def make_torrent(**overrides) -> Torrent:
    """A healthy, long-running seed. Override fields to make it sick."""
    import time

    now = time.time()
    defaults = {
        "infohash": "a" * 40,
        "name": "Some.Release.2024.1080p",
        "state": TorrentState.SEEDING,
        "raw_state": "uploading",
        "progress": 1.0,
        "size": 1024**3,
        "ratio": 1.5,
        "added_on": int(now - 86400),
        "last_activity": int(now - 30),
        "time_active": 86400,
        "upspeed": 1024,
        "num_leechs": 2,
        "trackers": [TrackerEntry(url="https://darkpeers.org/announce/key", status=2, num_leeches=3)],
    }
    defaults.update(overrides)
    return Torrent(**defaults)
