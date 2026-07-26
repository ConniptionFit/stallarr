"""Small shared helpers: bencode, infohash extraction, formatting."""

from __future__ import annotations

import hashlib
import re
from typing import Any


class BencodeError(ValueError):
    pass


def bdecode(data: bytes) -> Any:
    """Minimal bencode decoder. Returns bytes for strings (no implicit decode)."""
    value, _ = _decode_at(data, 0)
    return value


def _decode_at(data: bytes, i: int) -> tuple[Any, int]:
    if i >= len(data):
        raise BencodeError("unexpected end of data")
    char = data[i : i + 1]

    if char == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1 : end]), end + 1

    if char == b"l":
        items: list[Any] = []
        i += 1
        while data[i : i + 1] != b"e":
            item, i = _decode_at(data, i)
            items.append(item)
        return items, i + 1

    if char == b"d":
        mapping: dict[bytes, Any] = {}
        i += 1
        while data[i : i + 1] != b"e":
            key, i = _decode_at(data, i)
            value, i = _decode_at(data, i)
            mapping[key] = value
        return mapping, i + 1

    if char.isdigit():
        colon = data.index(b":", i)
        length = int(data[i:colon])
        start = colon + 1
        return data[start : start + length], start + length

    raise BencodeError(f"invalid bencode marker {char!r} at {i}")


def bencode(value: Any) -> bytes:
    if isinstance(value, bool):
        raise BencodeError("bool is not bencodable")
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        return bencode(value.encode())
    if isinstance(value, (list, tuple)):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        out = [b"d"]
        for key in sorted(value, key=lambda k: k if isinstance(k, bytes) else str(k).encode()):
            raw_key = key if isinstance(key, bytes) else str(key).encode()
            out.append(bencode(raw_key))
            out.append(bencode(value[key]))
        out.append(b"e")
        return b"".join(out)
    raise BencodeError(f"cannot bencode {type(value).__name__}")


def infohash_from_torrent(data: bytes) -> str | None:
    """SHA-1 infohash (v1) of a .torrent file's info dict, lowercase hex."""
    try:
        meta = bdecode(data)
        if not isinstance(meta, dict) or b"info" not in meta:
            return None
        return hashlib.sha1(bencode(meta[b"info"])).hexdigest()
    except (BencodeError, ValueError, KeyError):
        return None


def looks_like_torrent(data: bytes) -> bool:
    return data[:1] == b"d" and b"info" in data[:4096]


_MAGNET_HASH = re.compile(r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})")


def infohash_from_magnet(magnet: str) -> str | None:
    match = _MAGNET_HASH.search(magnet)
    if not match:
        return None
    value = match.group(1)
    if len(value) == 40:
        return value.lower()
    import base64

    try:
        return base64.b32decode(value.upper()).hex()
    except Exception:  # noqa: BLE001 - malformed magnet is not worth a custom error
        return None


def human_bytes(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(num) < 1024 or unit == "PiB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} PiB"


def human_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def normalise_host(value: str) -> str:
    """Strip scheme/port/path so config and announce URLs compare equal."""
    value = value.strip().lower()
    value = re.sub(r"^[a-z]+://", "", value)
    value = value.split("/")[0].split(":")[0]
    return value


def normalise_release(name: str) -> str:
    """Reduce a release name to a comparison key.

    Trackers list a torrent by its release name; the client may have renamed it,
    changed separators, or kept a different extension. Stripping everything but
    alphanumerics survives all three. This is only ever used to match a tracker's
    own listing against our client — never to decide two torrents are the same
    content, which needs an infohash.
    """
    stem = re.sub(r"\.(torrent|mkv|mp4|avi|flac|mp3|iso)$", "", name.strip(), flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]", "", stem.lower())


def truncate(text: str, limit: int = 90) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- secret redaction --------------------------------------------------------
# Announce URLs, download links and API errors all carry credentials, and all
# three end up in log lines, history rows and webhook payloads. Redaction has to
# cover every shape those secrets take, because the ones it misses are invisible
# until someone reads a log.

#: `?passkey=…`, `&rsskey=…`, `?auth=…` and friends.
_SECRET_PARAM = re.compile(
    r"\b(passkey|torrent_pass|api_?key|api_token|rsskey|auth|token|session)=[^&\s\"']+",
    re.IGNORECASE,
)

#: `https://user:password@host` — the whole userinfo section.
_USERINFO = re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")

#: Notification endpoints carry their credential in the URL itself, and an httpx
#: connection error quotes the URL it failed on. Both need naming explicitly:
#: their tokens contain `:`, `_` and `-`, so the generic alphanumeric-segment
#: rule below does not see them.
_TELEGRAM_TOKEN = re.compile(r"/bot\d+:[A-Za-z0-9_-]+", re.IGNORECASE)
_DISCORD_WEBHOOK = re.compile(r"(/api/webhooks/\d+)/[A-Za-z0-9_-]+", re.IGNORECASE)

#: A key sitting alone in a path segment. Covers both announce layouts:
#: IPTorrents puts it before (`/<key>/announce`), UNIT3D after
#: (`/announce/<key>`), so anchoring on "announce" catches only one of them.
#: Matching any long alphanumeric-only segment catches both. Torrent slugs
#: contain dashes or dots, so they are not swept up by this.
#: The trailing lookahead is "no more alphanumerics", not "a URL delimiter":
#: these strings are usually embedded in a sentence, so the key is just as often
#: followed by a space or a full stop as by a slash.
_KEYLIKE_SEGMENT = re.compile(r"(?<=/)[A-Za-z0-9]{20,64}(?![A-Za-z0-9])")

#: UNIT3D download links are `/torrent/download/<id>.<rsskey>`.
_KEYLIKE_SUFFIX = re.compile(r"(?<=\.)[A-Za-z0-9]{20,64}(?![A-Za-z0-9])")


def redact_secrets(text: str) -> str:
    """Strip credentials out of anything headed for a log, history row or alert.

    Deliberately over-redacts: a needlessly masked string in a log costs nothing,
    a leaked passkey costs an account. Applied at the choke points rather than at
    each call site, so a new caller cannot forget it.
    """
    if not text:
        return text
    text = _SECRET_PARAM.sub(lambda m: f"{m.group(1)}=***", text)
    text = _USERINFO.sub("***:***", text)
    text = _TELEGRAM_TOKEN.sub("/bot***", text)
    text = _DISCORD_WEBHOOK.sub(r"\1/***", text)
    text = _KEYLIKE_SEGMENT.sub("***", text)
    return _KEYLIKE_SUFFIX.sub("***", text)
