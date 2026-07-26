"""Login handling across qBittorrent versions.

The success response changed: 4.x answers `200 "Ok."`, 5.2 answers `204` with an
empty body. Both set a session cookie, and a wrong password answers `200
"Fails."` *without* one — so the cookie is what actually distinguishes them.
Checking the body alone locks you out of every 5.2 instance.
"""

import httpx
import pytest

from stallarr.clients.base import ClientError
from stallarr.clients.qbittorrent import QbittorrentClient
from stallarr.config import QbitConfig


def client_answering(status, body, *, set_cookie):
    def handler(request):
        headers = {"set-cookie": "QBT_SID_8080=abc123; path=/"} if set_cookie else {}
        return httpx.Response(status, text=body, headers=headers)

    client = QbittorrentClient(QbitConfig(url="http://qbit:8080", username="u", password="p"))
    client._client = httpx.AsyncClient(
        base_url="http://qbit:8080/api/v2", transport=httpx.MockTransport(handler)
    )
    return client


async def test_v5_answers_204_with_no_body():
    client = client_answering(204, "", set_cookie=True)

    await client._login()

    assert client._authed


async def test_v4_answers_200_ok():
    client = client_answering(200, "Ok.", set_cookie=True)

    await client._login()

    assert client._authed


async def test_wrong_password_is_still_rejected():
    """200 'Fails.' with no cookie — the case the cookie check must not swallow."""
    client = client_answering(200, "Fails.", set_cookie=False)

    with pytest.raises(ClientError, match=r"Fails\."):
        await client._login()

    assert not client._authed


async def test_a_banned_ip_says_so():
    client = client_answering(403, "", set_cookie=False)

    with pytest.raises(ClientError, match="banned"):
        await client._login()


async def test_an_unexpected_empty_response_names_the_status():
    """No body and no cookie is a failure, and the status is the only clue."""
    client = client_answering(500, "", set_cookie=False)

    with pytest.raises(ClientError, match="HTTP 500"):
        await client._login()


async def test_blank_credentials_assume_the_webui_bypass_list():
    client = QbittorrentClient(QbitConfig(url="http://qbit:8080"))

    await client._login()

    assert client._authed
