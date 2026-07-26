import hashlib

from stallarr.utils import (
    bdecode,
    bencode,
    human_bytes,
    infohash_from_magnet,
    infohash_from_torrent,
    looks_like_torrent,
    normalise_host,
)


def test_bencode_roundtrip():
    value = {b"a": 1, b"b": [b"x", 2], b"c": {b"nested": b"yes"}}
    assert bdecode(bencode(value)) == value


def test_bencode_sorts_keys():
    # Key order is part of the spec; getting it wrong changes the infohash.
    assert bencode({b"b": 1, b"a": 2}) == b"d1:ai2e1:bi1ee"


def test_infohash_from_torrent_matches_manual_sha1():
    info = {b"name": b"test", b"piece length": 16384, b"length": 100}
    torrent = bencode({b"announce": b"http://tracker/announce", b"info": info})
    assert infohash_from_torrent(torrent) == hashlib.sha1(bencode(info)).hexdigest()


def test_infohash_from_torrent_rejects_garbage():
    assert infohash_from_torrent(b"not a torrent") is None


def test_looks_like_torrent():
    assert looks_like_torrent(bencode({b"info": {b"name": b"x"}}))
    assert not looks_like_torrent(b"<html>login page</html>")


def test_infohash_from_magnet_hex_and_base32():
    hex_hash = "0123456789abcdef0123456789abcdef01234567"
    assert infohash_from_magnet(f"magnet:?xt=urn:btih:{hex_hash}&dn=x") == hex_hash
    assert infohash_from_magnet("magnet:?xt=urn:btih:AEBAGBAFAYDQQCIKBMGA2DQPCAIREEYU") is not None


def test_normalise_host_strips_scheme_port_path():
    assert normalise_host("https://Tracker.Example.org:8080/announce") == "tracker.example.org"


def test_human_bytes():
    assert human_bytes(1536) == "1.5 KiB"
    assert human_bytes(512) == "512 B"
