"""Writing per-torrent share limits, across the 4.x/5.x API split.

qBittorrent 5 rejects `/torrents/setShareLimits` outright unless *every* field
is present — including `shareLimitAction`, which decides whether reaching a
limit stops the torrent or deletes it. Sending a fixed value would quietly
rewrite that policy; omitting it fails the whole call with HTTP 400. Both are
regressions someone only notices when a repair stops working, so both are
pinned here.
"""

from __future__ import annotations

import httpx
import pytest

from stallarr.clients.qbittorrent import QbittorrentClient
from stallarr.config import QbitConfig

pytestmark = pytest.mark.asyncio


def client_holding(torrent: dict, sent: list):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/torrents/info"):
            return httpx.Response(200, json=[torrent])
        if request.url.path.endswith("/torrents/setShareLimits"):
            sent.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, text="")
        return httpx.Response(200, json=[])

    client = QbittorrentClient(QbitConfig(url="http://qbit:8080", username="u", password="p"))
    client._authed = True
    client._client = httpx.AsyncClient(
        base_url="http://qbit:8080/api/v2", transport=httpx.MockTransport(handler)
    )
    return client


V5_TORRENT = {
    "hash": "a" * 40,
    "name": "Some.Release",
    "state": "stoppedUP",
    "progress": 1.0,
    "ratio": 0.7,
    "ratio_limit": 1000,
    "seeding_time": 100001 * 60,
    "seeding_time_limit": 100000,
    "inactive_seeding_time_limit": -2,
    "share_limit_action": "Default",
}


async def test_the_share_limit_action_is_echoed_back():
    """qBittorrent 5 answers HTTP 400 'Missing required parameters:
    shareLimitAction' without it, so every lift silently fails."""
    sent = []
    client = client_holding(V5_TORRENT, sent)

    await client.set_share_limits("a" * 40, seeding_minutes=None)

    assert sent[0]["shareLimitAction"] == "Default"


async def test_a_non_default_action_is_preserved_not_reset():
    """The action decides whether a limit stops the torrent or deletes it.
    Normalising it to Default here would be a data-loss policy change."""
    sent = []
    client = client_holding({**V5_TORRENT, "share_limit_action": "Remove"}, sent)

    await client.set_share_limits("a" * 40, ratio=None)

    assert sent[0]["shareLimitAction"] == "Remove"


async def test_qbittorrent_4_is_sent_no_action_at_all():
    """4.x has no such field and rejects the parameter."""
    sent = []
    v4 = {k: v for k, v in V5_TORRENT.items() if k != "share_limit_action"}
    client = client_holding(v4, sent)

    await client.set_share_limits("a" * 40, seeding_minutes=None)

    assert "shareLimitAction" not in sent[0]


async def test_the_untouched_limit_is_resent_unchanged():
    """None means "clear this one"; the other has to survive the write, which on
    an endpoint that demands all fields means resending it."""
    sent = []
    client = client_holding(V5_TORRENT, sent)

    await client.set_share_limits("a" * 40, ratio=1000.0, seeding_minutes=None)

    assert float(sent[0]["ratioLimit"]) == 1000.0
    assert int(sent[0]["seedingTimeLimit"]) == QbittorrentClient.USE_GLOBAL_LIMIT


async def test_the_inactive_limit_is_never_invented():
    sent = []
    client = client_holding({**V5_TORRENT, "inactive_seeding_time_limit": 4320}, sent)

    await client.set_share_limits("a" * 40, seeding_minutes=None)

    assert int(sent[0]["inactiveSeedingTimeLimit"]) == 4320


async def test_share_limit_action_reaches_the_torrent_model():
    client = client_holding(V5_TORRENT, [])
    torrent = await client.get_torrent("a" * 40)
    assert torrent is not None
    assert torrent.share_limit_action == "Default"
