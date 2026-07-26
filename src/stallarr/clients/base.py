"""The interface any torrent client driver must satisfy.

Only qBittorrent ships today. To add another client, implement this protocol
and wire it up in clients/__init__.py::build_client.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Torrent


class ClientError(RuntimeError):
    """Raised for auth failures, transport errors and unexpected responses."""


@runtime_checkable
class TorrentClient(Protocol):
    name: str

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def version(self) -> str: ...

    async def list_torrents(self) -> list[Torrent]: ...

    async def get_torrent(self, infohash: str) -> Torrent | None: ...

    async def resume(self, infohash: str) -> None: ...

    async def pause(self, infohash: str) -> None: ...

    async def reannounce(self, infohash: str) -> None: ...

    async def recheck(self, infohash: str) -> None: ...

    async def set_share_limits(
        self, infohash: str, *, ratio: float | None = None, seeding_minutes: int | None = None
    ) -> None: ...

    async def add_tracker(self, infohash: str, url: str) -> None: ...

    async def replace_tracker(self, infohash: str, old_url: str, new_url: str) -> None: ...

    async def remove_trackers(self, infohash: str, urls: list[str]) -> None: ...

    async def add_tags(self, infohash: str, tags: list[str]) -> None: ...

    async def remove_tags(self, infohash: str, tags: list[str]) -> None: ...

    async def add_torrent_file(
        self,
        data: bytes,
        *,
        category: str = "",
        tags: list[str] | None = None,
        save_path: str = "",
        skip_checking: bool = False,
        paused: bool = False,
        use_auto_tmm: bool = False,
    ) -> None: ...

    async def delete(self, infohash: str, delete_files: bool = False) -> None: ...

    async def free_space(self) -> int | None:
        """Bytes free where the client saves data, or None if it won't say."""
        return None

    async def export_torrent(self, infohash: str) -> bytes:
        """The original .torrent metainfo, for keeping before a delete.

        Not abstract, and raising rather than returning empty: a client that
        cannot do this must fail loudly, because the caller's whole reason for
        asking is to make an irreversible step recoverable.
        """
        raise ClientError("this client cannot export .torrent files")
