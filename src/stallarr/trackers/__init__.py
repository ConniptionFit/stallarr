"""Tracker plugin registry.

Every module in this package is imported at startup; any TrackerPlugin subclass
decorated with @register becomes available. Adding a tracker is therefore a
single new file — no wiring anywhere else.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import TypeVar

from ..config import Config, TrackerConfig
from ..utils import normalise_host
from .base import Capability, TrackerError, TrackerPlugin

log = logging.getLogger(__name__)

__all__ = [
    "Capability",
    "TrackerError",
    "TrackerPlugin",
    "TrackerRouter",
    "base_plugins",
    "build_plugin",
    "configured_tracker_ids",
    "load_plugins",
    "register",
    "registry",
]

#: plugin id -> class
registry: dict[str, type[TrackerPlugin]] = {}

P = TypeVar("P", bound=type[TrackerPlugin])


def register(cls: P) -> P:
    """Class decorator that adds a plugin to the registry."""
    if not cls.id:
        raise ValueError(f"{cls.__name__} must define a non-empty `id`")
    if cls.id in registry and registry[cls.id] is not cls:
        raise ValueError(f"duplicate tracker plugin id {cls.id!r}")
    registry[cls.id] = cls
    return cls


def discover() -> None:
    """Import every sibling module so their @register decorators run."""
    for module in pkgutil.iter_modules(__path__):
        if module.name.startswith("_") or module.name == "base":
            continue
        importlib.import_module(f"{__name__}.{module.name}")


def base_plugins() -> dict[str, type[TrackerPlugin]]:
    """Generic plugins a config-defined tracker can be built on.

    These are deliberately not in `registry`: they describe no site of their own,
    so they must never show up as a tracker you can enable. They exist so that a
    stock UNIT3D site can work with a config entry and no plugin file.
    """
    from .unit3d import Unit3dPlugin

    return {"unit3d": Unit3dPlugin, "generic": TrackerPlugin}


def build_plugin(plugin_id: str, tracker_cfg: TrackerConfig) -> TrackerPlugin:
    """Instantiate one tracker, from a plugin file if there is one.

    A plugin file always wins: if someone later writes `seedpool.py`, the config
    entry that used to be `plugin: "unit3d"` silently upgrades to the real thing.
    """
    # Must discover first, or that guarantee holds only when something else
    # happened to populate the registry — and a real tracker would quietly get
    # the generic driver instead, losing its default URL and behaviour.
    discover()
    plugin_cls = registry.get(plugin_id)
    if plugin_cls is not None:
        return plugin_cls(tracker_cfg)

    base_name = tracker_cfg.plugin or "unit3d"
    base_cls = base_plugins().get(base_name)
    if base_cls is None:
        raise ValueError(f"unknown base plugin {base_name!r} for tracker {plugin_id!r}")

    plugin = base_cls(tracker_cfg)
    # Identity comes from config rather than class attributes for these. Set it
    # on the instance so everything downstream (routing, logs, history rows)
    # cannot tell the difference between this and a real plugin file.
    plugin.id = plugin_id
    plugin.name = tracker_cfg.name or plugin_id
    plugin.hosts = tuple(normalise_host(h) for h in tracker_cfg.hosts if h.strip())
    return plugin


def configured_tracker_ids(cfg: Config) -> list[str]:
    """Every tracker the UI should offer: plugin files plus config-defined ones."""
    discover()
    return sorted(set(registry) | set(cfg.trackers))


def load_plugins(cfg: Config) -> dict[str, TrackerPlugin]:
    """Instantiate every enabled tracker."""
    discover()
    plugins: dict[str, TrackerPlugin] = {}
    for plugin_id in configured_tracker_ids(cfg):
        tracker_cfg = cfg.trackers.get(plugin_id, TrackerConfig())
        if not tracker_cfg.enabled:
            continue
        try:
            plugins[plugin_id] = build_plugin(plugin_id, tracker_cfg)
        except Exception:
            log.exception("failed to initialise tracker plugin %s", plugin_id)
    return plugins


class TrackerRouter:
    """Maps a torrent's announce hosts to the plugin that owns them."""

    def __init__(self, plugins: dict[str, TrackerPlugin]) -> None:
        self.plugins = plugins

    def for_hosts(self, hosts: list[str]) -> TrackerPlugin | None:
        for host in hosts:
            for plugin in self.plugins.values():
                if plugin.matches_host(host):
                    return plugin
        return None

    def get(self, plugin_id: str) -> TrackerPlugin | None:
        return self.plugins.get(plugin_id)

    async def close(self) -> None:
        for plugin in self.plugins.values():
            try:
                await plugin.close()
            except Exception:
                log.debug("error closing plugin %s", plugin.id, exc_info=True)
