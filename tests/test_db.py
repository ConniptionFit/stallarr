import asyncio
import sqlite3

from stallarr import db


class FakeConn:
    """Stands in for a connection on a filesystem that can't do WAL."""

    def __init__(self, wal_result):
        self.wal_result = wal_result
        self.executed: list[str] = []

    def execute(self, sql):
        self.executed.append(sql)
        if "WAL" in sql:
            if isinstance(self.wal_result, Exception):
                raise self.wal_result
            return FakeCursor(self.wal_result)
        return FakeCursor(None)


class FakeCursor:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return (self.value,) if self.value is not None else None


def test_wal_is_used_when_the_filesystem_supports_it():
    assert db._resolve_journal_mode(FakeConn("wal")) == "wal"


def test_falls_back_when_the_server_silently_ignores_wal():
    """CIFS/NFS answer the pragma but stay in rollback-journal mode."""
    conn = FakeConn("delete")
    assert db._resolve_journal_mode(conn) == "delete"
    assert any("DELETE" in sql for sql in conn.executed)


def test_falls_back_when_setting_wal_raises():
    """SMB shares surface this as 'database is locked'."""
    conn = FakeConn(sqlite3.OperationalError("database is locked"))
    assert db._resolve_journal_mode(conn) == "delete"
    assert any("DELETE" in sql for sql in conn.executed)


async def test_real_database_round_trips_after_mode_resolution():
    await db.init()
    await db.add_history(infohash="f" * 40, name="x", action="reannounce", ok=True)
    rows = await db.get_history(infohash="f" * 40)
    assert rows and rows[0]["action"] == "reannounce"


# --- history filters ----------------------------------------------------------


async def _seed_history():
    await db.init()
    await asyncio.to_thread(db._exec, "DELETE FROM history", ())
    # Infohashes are namespaced away from the single-letter ones other test
    # modules use: this table is shared for the whole session, and a collision
    # would break their lookups rather than ours.
    for index, (action, ok) in enumerate([
        ("reannounce", True),
        ("recheck", True),
        ("redownload", True),
        ("redownload", False),
        ("escalate", True),
    ]):
        await db.add_history(
            infohash=f"histfilter{index:030d}", name=action, action=action, ok=ok
        )


async def test_history_filters_by_action():
    """The filter has to run in SQL: `limit` is applied last, so filtering in
    the browser would search only the newest 200 rows and report "no
    re-downloads" for a library doing hundreds of reannounces a day."""
    await _seed_history()

    rows = await db.get_history(action="redownload")

    assert len(rows) == 2
    assert {r["action"] for r in rows} == {"redownload"}


async def test_history_filters_by_outcome():
    await _seed_history()

    assert len(await db.get_history(ok=False)) == 1
    assert len(await db.get_history(ok=True)) == 4


async def test_history_filters_combine():
    await _seed_history()

    rows = await db.get_history(action="redownload", ok=False)

    assert len(rows) == 1
    assert rows[0]["ok"] == 0


async def test_ok_false_is_not_confused_with_no_filter():
    """`if ok:` instead of `if ok is not None:` would silently ignore a request
    for failures — the filter people reach for first."""
    await _seed_history()

    assert len(await db.get_history(ok=False)) != len(await db.get_history())


async def test_no_filters_returns_everything():
    await _seed_history()

    assert len(await db.get_history()) == 5


async def test_an_unknown_action_matches_nothing_rather_than_everything():
    await _seed_history()

    assert await db.get_history(action="not_a_real_action") == []
