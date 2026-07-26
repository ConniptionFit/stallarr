"""qBittorrent WebUI API v2 driver.

Handles the 4.x/5.x naming split: 5.0 renamed pause/resume to stop/start and
kept the old routes as aliases for a while. We probe once and cache the verbs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..config import QbitConfig
from ..models import Torrent, TorrentState, TrackerEntry
from .base import ClientError

log = logging.getLogger(__name__)

#: qBittorrent `state` string -> our TorrentState
_STATE_MAP = {
    "error": TorrentState.ERROR,
    "missingFiles": TorrentState.MISSING_FILES,
    "uploading": TorrentState.SEEDING,
    "pausedUP": TorrentState.PAUSED,
    "stoppedUP": TorrentState.PAUSED,
    "queuedUP": TorrentState.QUEUED,
    "stalledUP": TorrentState.STALLED_UP,
    "checkingUP": TorrentState.CHECKING,
    "forcedUP": TorrentState.SEEDING,
    "allocating": TorrentState.DOWNLOADING,
    "downloading": TorrentState.DOWNLOADING,
    "metaDL": TorrentState.DOWNLOADING,
    "pausedDL": TorrentState.PAUSED,
    "stoppedDL": TorrentState.PAUSED,
    "queuedDL": TorrentState.QUEUED,
    "stalledDL": TorrentState.STALLED_DL,
    "checkingDL": TorrentState.CHECKING,
    "forcedDL": TorrentState.DOWNLOADING,
    "checkingResumeData": TorrentState.CHECKING,
    "moving": TorrentState.MOVING,
    "unknown": TorrentState.UNKNOWN,
}


class QbittorrentClient:
    name = "qbittorrent"

    def __init__(self, cfg: QbitConfig) -> None:
        self._cfg = cfg
        self._base = cfg.url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=f"{self._base}/api/v2",
            timeout=httpx.Timeout(30.0, connect=10.0),
            verify=cfg.verify_ssl,
            follow_redirects=True,
            headers={"Referer": self._base, "Origin": self._base},
        )
        self._authed = False
        self._lock = asyncio.Lock()
        self._version: str = ""
        #: v5 renamed these; resolved lazily by _resolve_verbs().
        self._start_verb = "resume"
        self._stop_verb = "pause"
        self._verbs_resolved = False

    # --- transport ------------------------------------------------------------

    async def close(self) -> None:
        await self._client.aclose()

    async def connect(self) -> None:
        async with self._lock:
            await self._login()
        await self._resolve_verbs()

    async def _login(self) -> None:
        if not self._cfg.username and not self._cfg.password:
            # Host may be on the WebUI bypass list; treat as already authorised.
            self._authed = True
            return
        try:
            resp = await self._client.post(
                "/auth/login",
                data={"username": self._cfg.username, "password": self._cfg.password},
            )
        except httpx.HTTPError as exc:
            raise ClientError(f"cannot reach qBittorrent at {self._base}: {exc}") from exc

        body = resp.text.strip()
        if resp.status_code == 403:
            raise ClientError("qBittorrent refused the login (IP temporarily banned?)")

        # What success looks like depends on the version: 4.x answers 200 "Ok.",
        # 5.2 answers 204 with no body. Both set the session cookie, and a wrong
        # password answers 200 "Fails." with no cookie — so the cookie is the
        # signal that actually holds across versions.
        if self._has_session_cookie() or body == "Ok.":
            self._authed = True
            return

        detail = body or f"HTTP {resp.status_code}"
        raise ClientError(f"qBittorrent login failed: {detail}")

    def _has_session_cookie(self) -> bool:
        return any(name.startswith("QBT_SID") for name in self._client.cookies)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self._authed:
            async with self._lock:
                if not self._authed:
                    await self._login()
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ClientError(f"qBittorrent request {path} failed: {exc}") from exc

        if resp.status_code in (401, 403):
            # Session expired mid-scan; re-auth once and retry.
            self._authed = False
            async with self._lock:
                await self._login()
            try:
                resp = await self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise ClientError(f"qBittorrent request {path} failed: {exc}") from exc

        if resp.status_code >= 400:
            raise ClientError(f"qBittorrent {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, data: dict | None = None, **kwargs) -> httpx.Response:
        return await self._request("POST", path, data=data, **kwargs)

    async def version(self) -> str:
        if not self._version:
            resp = await self._get("/app/version")
            self._version = resp.text.strip()
        return self._version

    async def _resolve_verbs(self) -> None:
        """qBittorrent 5.x renamed torrents/resume -> torrents/start."""
        if self._verbs_resolved:
            return
        raw = (await self.version()).lstrip("v")
        major = 0
        try:
            major = int(raw.split(".")[0])
        except (ValueError, IndexError):
            log.debug("could not parse qBittorrent version %r, assuming 4.x verbs", raw)
        if major >= 5:
            self._start_verb, self._stop_verb = "start", "stop"
        self._verbs_resolved = True

    # --- reads ----------------------------------------------------------------

    async def list_torrents(self) -> list[Torrent]:
        resp = await self._get("/torrents/info")
        raw_list = resp.json()
        torrents = [self._parse(raw) for raw in raw_list]
        await self._attach_trackers(torrents)
        return torrents

    async def get_torrent(self, infohash: str) -> Torrent | None:
        resp = await self._get("/torrents/info", params={"hashes": infohash})
        raw_list = resp.json()
        if not raw_list:
            return None
        torrent = self._parse(raw_list[0])
        torrent.trackers = await self.get_trackers(infohash)
        return torrent

    async def _attach_trackers(self, torrents: list[Torrent]) -> None:
        """Tracker lists need one call each; bound the concurrency."""
        semaphore = asyncio.Semaphore(8)

        async def fetch(torrent: Torrent) -> None:
            async with semaphore:
                try:
                    torrent.trackers = await self.get_trackers(torrent.infohash)
                except ClientError as exc:
                    log.warning("tracker fetch failed for %s: %s", torrent.name, exc)

        await asyncio.gather(*(fetch(t) for t in torrents))

    async def get_trackers(self, infohash: str) -> list[TrackerEntry]:
        resp = await self._get("/torrents/trackers", params={"hash": infohash})
        return [
            TrackerEntry(
                url=raw.get("url", ""),
                status=int(raw.get("status", 1)),
                message=raw.get("msg", "") or "",
                num_peers=int(raw.get("num_peers", 0) or 0),
                num_seeds=int(raw.get("num_seeds", -1) or -1),
                num_leeches=int(raw.get("num_leeches", -1) or -1),
            )
            for raw in resp.json()
        ]

    @staticmethod
    def _parse(raw: dict) -> Torrent:
        raw_state = raw.get("state", "unknown")
        tags = [t.strip() for t in (raw.get("tags") or "").split(",") if t.strip()]
        return Torrent(
            infohash=(raw.get("hash") or "").lower(),
            name=raw.get("name", ""),
            state=_STATE_MAP.get(raw_state, TorrentState.UNKNOWN),
            raw_state=raw_state,
            progress=float(raw.get("progress", 0.0)),
            size=int(raw.get("size", 0)),
            amount_left=int(raw.get("amount_left", 0)),
            downloaded=int(raw.get("downloaded", 0)),
            uploaded=int(raw.get("uploaded", 0)),
            ratio=float(raw.get("ratio", 0.0)),
            num_seeds=int(raw.get("num_seeds", 0)),
            num_leechs=int(raw.get("num_leechs", 0)),
            dlspeed=int(raw.get("dlspeed", 0)),
            upspeed=int(raw.get("upspeed", 0)),
            added_on=int(raw.get("added_on", 0)),
            completion_on=int(raw.get("completion_on", 0)),
            last_activity=int(raw.get("last_activity", 0)),
            time_active=int(raw.get("time_active", 0)),
            seeding_time=int(raw.get("seeding_time", 0)),
            ratio_limit=float(raw.get("ratio_limit", -2) or -2),
            seeding_time_limit=int(raw.get("seeding_time_limit", -2) or -2),
            inactive_seeding_time_limit=int(raw.get("inactive_seeding_time_limit", -2) or -2),
            share_limit_action=raw.get("share_limit_action", "") or "",
            category=raw.get("category", "") or "",
            tags=tags,
            save_path=raw.get("save_path", "") or "",
            eta=int(raw.get("eta", 0)),
            availability=float(raw.get("availability", 0.0) or 0.0),
        )

    # --- writes ---------------------------------------------------------------

    async def resume(self, infohash: str) -> None:
        await self._resolve_verbs()
        await self._post(f"/torrents/{self._start_verb}", {"hashes": infohash})

    async def pause(self, infohash: str) -> None:
        await self._resolve_verbs()
        await self._post(f"/torrents/{self._stop_verb}", {"hashes": infohash})

    async def reannounce(self, infohash: str) -> None:
        await self._post("/torrents/reannounce", {"hashes": infohash})

    async def recheck(self, infohash: str) -> None:
        await self._post("/torrents/recheck", {"hashes": infohash})

    #: qBittorrent's sentinels for a share limit: -1 is "no limit ever", -2 is
    #: "defer to the global setting". We use -2 — restoring default behaviour is
    #: less surprising than pinning an override the user never asked for, and it
    #: keeps a global limit they set later in force.
    USE_GLOBAL_LIMIT = -2

    async def set_share_limits(
        self, infohash: str, *, ratio: float | None = None, seeding_minutes: int | None = None
    ) -> None:
        """Set per-torrent share limits. None leaves that limit untouched.

        qBittorrent 5 requires *every* field on this endpoint — including
        `shareLimitAction`, which it rejects the whole call for omitting — so
        the ones we are not changing are read back and resent rather than left
        out of the form. `share_limit_action` in particular decides whether
        hitting a limit stops the torrent or deletes it, so it is echoed back
        exactly and never defaulted.
        """
        current = await self.get_torrent(infohash)
        payload: dict[str, Any] = {
            "hashes": infohash,
            "ratioLimit": self.USE_GLOBAL_LIMIT if ratio is None else ratio,
            "seedingTimeLimit": (
                self.USE_GLOBAL_LIMIT if seeding_minutes is None else seeding_minutes
            ),
            "inactiveSeedingTimeLimit": (
                current.inactive_seeding_time_limit if current else self.USE_GLOBAL_LIMIT
            ),
        }
        # Absent on qBittorrent 4, which also does not accept the parameter.
        if current and current.share_limit_action:
            payload["shareLimitAction"] = current.share_limit_action
        await self._post("/torrents/setShareLimits", payload)

    async def add_tracker(self, infohash: str, url: str) -> None:
        await self._post("/torrents/addTrackers", {"hash": infohash, "urls": url})

    async def replace_tracker(self, infohash: str, old_url: str, new_url: str) -> None:
        await self._post("/torrents/editTracker", {"hash": infohash, "origUrl": old_url, "newUrl": new_url})

    async def remove_trackers(self, infohash: str, urls: list[str]) -> None:
        await self._post("/torrents/removeTrackers", {"hash": infohash, "urls": "|".join(urls)})

    async def add_tags(self, infohash: str, tags: list[str]) -> None:
        if tags:
            await self._post("/torrents/addTags", {"hashes": infohash, "tags": ",".join(tags)})

    async def remove_tags(self, infohash: str, tags: list[str]) -> None:
        if tags:
            await self._post("/torrents/removeTags", {"hashes": infohash, "tags": ",".join(tags)})

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
    ) -> None:
        # An explicit path is a decision the caller already made; automatic
        # management would silently move the torrent somewhere else, and
        # qBittorrent ignores `savepath` outright when autoTMM is on.
        auto_tmm = use_auto_tmm and not save_path

        await self._resolve_verbs()
        # v5 accepts "stopped", v4 accepts "paused"; sending both is harmless.
        form: dict[str, str] = {
            "skip_checking": "true" if skip_checking else "false",
            "paused": "true" if paused else "false",
            "stopped": "true" if paused else "false",
            "autoTMM": "true" if auto_tmm else "false",
        }
        if category:
            form["category"] = category
        if tags:
            form["tags"] = ",".join(tags)
        if save_path:
            form["savepath"] = save_path
        files = {"torrents": ("stallarr.torrent", data, "application/x-bittorrent")}
        await self._request("POST", "/torrents/add", data=form, files=files)

    async def delete(self, infohash: str, delete_files: bool = False) -> None:
        await self._post(
            "/torrents/delete",
            {"hashes": infohash, "deleteFiles": "true" if delete_files else "false"},
        )

    async def free_space(self) -> int | None:
        resp = await self._get("/sync/maindata", params={"rid": "0"})
        try:
            value = resp.json().get("server_state", {}).get("free_space_on_disk")
        except ValueError:
            return None
        return int(value) if isinstance(value, (int, float)) else None

    async def export_torrent(self, infohash: str) -> bytes:
        resp = await self._get("/torrents/export", params={"hash": infohash})
        data = resp.content
        # A bencoded dict starts with 'd'. qBittorrent answers 200 with an HTML
        # error page on some failures, and silently writing that to the
        # graveyard would leave a file that looks like a backup and is not one.
        if not data.startswith(b"d"):
            raise ClientError(f"export for {infohash[:8]} was not a .torrent file")
        return data
