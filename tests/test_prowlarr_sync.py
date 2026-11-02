"""The recurring Prowlarr sync, as the service runs it."""

from stallarr import config as config_module
from stallarr.config import Config
from stallarr.integrations import prowlarr
from stallarr.service import StallarrService


def _service(**prowlarr_cfg) -> StallarrService:
    cfg = Config.model_validate(
        {"prowlarr": {"enabled": True, "url": "http://prowlarr:9696", "api_key": "k", **prowlarr_cfg}}
    )
    return StallarrService(cfg)


async def test_auto_sync_saves_and_records_what_it_added(monkeypatch):
    saved = []
    monkeypatch.setattr(config_module, "save", saved.append)

    async def fake_sync(cfg, **kwargs):
        assert kwargs["overwrite"] is True  # rotated keys must land
        return {
            "imported": [{"tracker_id": "seedpool", "changed": ["created"]}],
            "skipped": [],
            "added": ["seedpool"],
            "updated": [],
            "proposals": [{}],
        }

    monkeypatch.setattr(prowlarr, "sync", fake_sync)
    svc = _service(auto_sync=True)

    assert await svc._auto_sync_prowlarr() is True
    assert saved == [svc.cfg]
    assert svc.prowlarr_sync["ok"]
    assert svc.prowlarr_sync["added"] == ["seedpool"]
    assert svc.prowlarr_sync["trigger"] == "auto"


async def test_auto_sync_with_nothing_new_does_not_save(monkeypatch):
    saved = []
    monkeypatch.setattr(config_module, "save", saved.append)

    async def fake_sync(cfg, **kwargs):
        return {"imported": [], "skipped": [], "added": [], "updated": [], "proposals": []}

    monkeypatch.setattr(prowlarr, "sync", fake_sync)

    assert await _service(auto_sync=True)._auto_sync_prowlarr() is False
    assert saved == []


async def test_prowlarr_down_is_recorded_not_raised(monkeypatch):
    async def fake_sync(cfg, **kwargs):
        raise prowlarr.ProwlarrError("could not reach Prowlarr")

    monkeypatch.setattr(prowlarr, "sync", fake_sync)
    svc = _service(auto_sync=True)

    assert await svc._auto_sync_prowlarr() is False
    assert not svc.prowlarr_sync["ok"]
    assert "could not reach" in svc.prowlarr_sync["error"]


async def test_auto_sync_off_makes_no_call(monkeypatch):
    async def fake_sync(cfg, **kwargs):
        raise AssertionError("synced with auto_sync off")

    monkeypatch.setattr(prowlarr, "sync", fake_sync)

    assert await _service(auto_sync=False)._auto_sync_prowlarr() is False


def test_status_floors_the_interval():
    svc = _service(auto_sync=True, sync_interval_minutes=1)
    info = svc.snapshot()["prowlarr"]

    assert info["auto_sync"]
    assert info["interval_minutes"] == config_module.MIN_PROWLARR_SYNC_MINUTES
