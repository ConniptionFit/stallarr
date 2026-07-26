"""Torrent client drivers."""

from __future__ import annotations

from ..config import Config
from .base import ClientError, TorrentClient
from .qbittorrent import QbittorrentClient

__all__ = ["ClientError", "QbittorrentClient", "TorrentClient", "build_client"]


def build_client(cfg: Config) -> TorrentClient:
    """Return the configured client driver.

    Add new clients here: give them a config section and dispatch on it.
    """
    return QbittorrentClient(cfg.qbittorrent)
