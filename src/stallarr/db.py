"""SQLite state: recovery attempt bookkeeping, action history, notification dedupe.

All writes go through asyncio.to_thread — the volumes here are tiny (a few
hundred rows per scan at most), so a connection-per-call keeps it simple and
avoids cross-thread cursor sharing.

The database lives in DATA_DIR by default, but `STALLARR_DB_DIR` can move it.
Do that when /config is a network share: SQLite needs POSIX byte-range locks,
and CIFS/SMB and NFS don't supply working ones — every write fails with
"database is locked" no matter which journal mode you pick. Config JSON is a
plain file and stays happily on the share; only the database has to be local.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import DATA_DIR

log = logging.getLogger(__name__)

DB_DIR = Path(os.environ.get("STALLARR_DB_DIR") or DATA_DIR)
DB_PATH = DB_DIR / "stallarr.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    infohash    TEXT NOT NULL,
    ailment     TEXT NOT NULL,
    action      TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    last_at     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (infohash, ailment, action)
);

CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    infohash    TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    tracker     TEXT NOT NULL DEFAULT '',
    ailment     TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 1,
    dry_run     INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_ts ON history (ts DESC);
CREATE INDEX IF NOT EXISTS idx_history_infohash ON history (infohash);

CREATE TABLE IF NOT EXISTS notified (
    key         TEXT PRIMARY KEY,
    last_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ignores (
    infohash    TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);

-- One row per seeding obligation a tracker has published, alive or settled.
-- Keyed by the normalised release name rather than an infohash: an obligation
-- can exist with no local torrent at all, which is the case that most needs
-- tracking. `actions` counts the repairs stallarr landed while it was
-- outstanding, and is what separates "the debt cleared" from "we cleared it".
CREATE TABLE IF NOT EXISTS seeding_obligations (
    tracker     TEXT NOT NULL,
    key         TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    cleared_at  INTEGER NOT NULL DEFAULT 0,
    actions     INTEGER NOT NULL DEFAULT 0,
    last_action TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tracker, key)
);
CREATE INDEX IF NOT EXISTS idx_obligations_cleared ON seeding_obligations (cleared_at);

-- When the tracker last gave us a straight answer about a torrent. Only a real
-- reply counts, so a run of timeouts can never masquerade as "checked
-- recently" and suppress the re-check that would have caught a deletion.
CREATE TABLE IF NOT EXISTS tracker_checks (
    infohash    TEXT PRIMARY KEY,
    checked_at  INTEGER NOT NULL
);

-- Torrents currently diagnosed as deleted-at-the-tracker, and since when.
-- Deletion reads `first_at` to enforce a soak period, so the verdict has to
-- survive restarts and history pruning; a row is dropped the moment a torrent
-- stops being unregistered, which is what makes the age "continuously so"
-- rather than "at some point".
CREATE TABLE IF NOT EXISTS condemned (
    infohash    TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    tracker     TEXT NOT NULL DEFAULT '',
    first_at    INTEGER NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0
);

-- Every torrent stallarr has actually deleted. Deliberately *not* covered by
-- `prune_history`: the history table answers "what happened lately" and is
-- trimmed to `history_retention_days`, but the one irreversible thing this
-- program does has to stay answerable long after that window closes. A row is
-- written only when a delete really happened, so the table grows by the
-- handful, not by the scan.
CREATE TABLE IF NOT EXISTS deletions (
    infohash      TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    tracker       TEXT NOT NULL DEFAULT '',
    size          INTEGER NOT NULL DEFAULT 0,
    files_deleted INTEGER NOT NULL DEFAULT 0,
    reason        TEXT NOT NULL DEFAULT '',
    condemned_at  INTEGER NOT NULL DEFAULT 0,
    deleted_at    INTEGER NOT NULL,
    export_path   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_deletions_at ON deletions (deleted_at DESC);

-- One row per completed scan: the numbers the dashboard shows, kept over time.
-- Everything else here is "right now", which cannot answer whether a tracker's
-- unregistered share is a step change or a slow drift — the exact question the
-- deletion circuit breaker's threshold turns on.
CREATE TABLE IF NOT EXISTS scan_snapshots (
    ts        INTEGER PRIMARY KEY,
    total     INTEGER NOT NULL DEFAULT 0,
    healthy   INTEGER NOT NULL DEFAULT 0,
    sick      INTEGER NOT NULL DEFAULT 0,
    skipped   INTEGER NOT NULL DEFAULT 0,
    actions   INTEGER NOT NULL DEFAULT 0,
    condemned INTEGER NOT NULL DEFAULT 0,
    owed      INTEGER NOT NULL DEFAULT 0,
    duration  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON scan_snapshots (ts DESC);
"""

#: Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` does
#: nothing to a database that already has the table, so additive changes need
#: their own idempotent step or they only ever exist on fresh installs.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("condemned", "size", "INTEGER NOT NULL DEFAULT 0"),
)


#: Resolved once on first connect: "wal" locally, "delete" on network shares.
_journal_mode: str | None = None


def _resolve_journal_mode(conn: sqlite3.Connection) -> str:
    """WAL where possible, rollback journal where it isn't.

    WAL needs a shared-memory file alongside the database. CIFS/SMB and NFS
    don't provide one, and SQLite reports that as "database is locked" — which
    is exactly where a homelab /config volume tends to live. Try WAL, verify
    the server actually honoured it, and fall back rather than refusing to run.
    """
    try:
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if row and str(row[0]).lower() == "wal":
            return "wal"
    except sqlite3.DatabaseError:
        pass

    conn.execute("PRAGMA journal_mode=DELETE")
    return "delete"


def _connect() -> sqlite3.Connection:
    global _journal_mode

    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")

    if _journal_mode is None:
        _journal_mode = _resolve_journal_mode(conn)
        if _journal_mode != "wal":
            logging.getLogger(__name__).warning(
                "sqlite WAL unavailable at %s — using rollback journal. If writes fail with "
                "'database is locked', this path is a network share; set STALLARR_DB_DIR to "
                "local disk.",
                DB_DIR,
            )
    else:
        conn.execute(f"PRAGMA journal_mode={_journal_mode}")

    return conn


def _init_sync() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        for table, column, spec in _MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
                log.info("migrated: added %s.%s", table, column)
        conn.commit()


async def init() -> None:
    await asyncio.to_thread(_init_sync)


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _exec(sql: str, params: tuple = ()) -> None:
    with _connect() as conn:
        conn.execute(sql, params)
        conn.commit()


# --- attempts -----------------------------------------------------------------


def _get_attempts_sync(infohash: str, ailment: str) -> dict[str, dict[str, int]]:
    rows = _rows(
        "SELECT action, count, last_at FROM attempts WHERE infohash = ? AND ailment = ?",
        (infohash, ailment),
    )
    return {r["action"]: {"count": r["count"], "last_at": r["last_at"]} for r in rows}


async def get_attempts(infohash: str, ailment: str) -> dict[str, dict[str, int]]:
    return await asyncio.to_thread(_get_attempts_sync, infohash, ailment)


def _record_attempt_sync(infohash: str, ailment: str, action: str) -> None:
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            """INSERT INTO attempts (infohash, ailment, action, count, last_at)
               VALUES (?, ?, ?, 1, ?)
               ON CONFLICT(infohash, ailment, action)
               DO UPDATE SET count = count + 1, last_at = excluded.last_at""",
            (infohash, ailment, action, now),
        )
        conn.commit()


async def record_attempt(infohash: str, ailment: str, action: str) -> None:
    await asyncio.to_thread(_record_attempt_sync, infohash, ailment, action)


async def clear_attempts(infohash: str, ailment: str | None = None) -> None:
    """Called when a torrent recovers, so the ladder starts fresh next time."""
    if ailment:
        await asyncio.to_thread(
            _exec, "DELETE FROM attempts WHERE infohash = ? AND ailment = ?", (infohash, ailment)
        )
    else:
        await asyncio.to_thread(_exec, "DELETE FROM attempts WHERE infohash = ?", (infohash,))


# --- history ------------------------------------------------------------------


def _add_history_sync(entry: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO history (ts, infohash, name, tracker, ailment, action, ok, dry_run, message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(time.time()),
                entry.get("infohash", ""),
                entry.get("name", ""),
                entry.get("tracker", ""),
                entry.get("ailment", ""),
                entry.get("action", ""),
                1 if entry.get("ok", True) else 0,
                1 if entry.get("dry_run") else 0,
                entry.get("message", ""),
            ),
        )
        conn.commit()


async def add_history(**entry: Any) -> None:
    await asyncio.to_thread(_add_history_sync, entry)


async def get_history(
    limit: int = 200,
    infohash: str | None = None,
    action: str | None = None,
    ok: bool | None = None,
) -> list[dict[str, Any]]:
    """Recent actions, newest first. Every filter is optional and ANDed.

    Filtering in SQL rather than in the browser matters once history is long:
    `limit` is applied last, so a client-side filter would be searching only the
    most recent 200 rows and quietly reporting "no re-downloads" for a library
    that does hundreds of reannounces a day.
    """
    where: list[str] = []
    params: list[Any] = []
    if infohash:
        where.append("infohash = ?")
        params.append(infohash)
    if action:
        where.append("action = ?")
        params.append(action)
    if ok is not None:
        where.append("ok = ?")
        params.append(1 if ok else 0)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)
    return await asyncio.to_thread(
        _rows, f"SELECT * FROM history {clause} ORDER BY ts DESC LIMIT ?", tuple(params)
    )


async def recently_recovered_hashes(days: int = 7) -> set[str]:
    """Infohashes with a successful action in the last `days`.

    One query rather than `get_history` per torrent — this is meant to be
    called once per dashboard request and checked against a whole library.
    """
    cutoff = int(time.time()) - max(days, 0) * 86400
    rows = await asyncio.to_thread(
        _rows,
        "SELECT DISTINCT infohash FROM history WHERE ok = 1 AND ts >= ?",
        (cutoff,),
    )
    return {row["infohash"] for row in rows}


async def prune_history(retention_days: int) -> None:
    cutoff = int(time.time()) - retention_days * 86400
    await asyncio.to_thread(_exec, "DELETE FROM history WHERE ts < ?", (cutoff,))


# --- tracker verification -----------------------------------------------------


async def mark_verified(infohash: str) -> None:
    """Record that the tracker answered a lookup for this torrent just now."""
    await asyncio.to_thread(
        _exec,
        "INSERT INTO tracker_checks (infohash, checked_at) VALUES (?, ?) "
        "ON CONFLICT(infohash) DO UPDATE SET checked_at = excluded.checked_at",
        (infohash, int(time.time())),
    )


async def verified_map() -> dict[str, int]:
    """infohash -> when the tracker last answered about it.

    Read once per scan and consulted in memory: the alternative is a query per
    torrent, and the whole table is smaller than one scan's worth of history.
    """
    rows = await asyncio.to_thread(_rows, "SELECT infohash, checked_at FROM tracker_checks", ())
    return {r["infohash"]: r["checked_at"] for r in rows}


async def prune_tracker_checks(retention_days: int) -> None:
    """Forget stale check times. These belong to torrents no longer in the
    client; the cost of being wrong is one extra lookup."""
    cutoff = int(time.time()) - retention_days * 86400
    await asyncio.to_thread(_exec, "DELETE FROM tracker_checks WHERE checked_at < ?", (cutoff,))


# --- condemned torrents -------------------------------------------------------


def _condemn_sync(infohash: str, name: str, tracker: str, size: int) -> int:
    """Record this torrent as unregistered, returning when that first became so.

    A torrent already known to have been unregistered gets its original
    timestamp back, not a fresh one, or the soak period would restart on every
    scan and nothing would ever age past it.
    """
    now = int(time.time())
    with _connect() as conn:
        row = conn.execute(
            "SELECT first_at FROM condemned WHERE infohash = ?", (infohash,)
        ).fetchone()
        if row:
            # Size can arrive late (an older row predates the column), but the
            # clock must not move.
            conn.execute(
                "UPDATE condemned SET name = ?, tracker = ?, size = ? WHERE infohash = ?",
                (name, tracker, size, infohash),
            )
            conn.commit()
            return int(row["first_at"])

        # No live record. History may still show when this verdict was first
        # reached — real evidence of how long it has stood, and worth more than
        # restarting the clock because stallarr was upgraded or restarted.
        seen = conn.execute(
            "SELECT MIN(ts) AS first FROM history WHERE infohash = ? AND ailment = ?",
            (infohash, "unregistered"),
        ).fetchone()
        first_at = int(seen["first"]) if seen and seen["first"] else now
        conn.execute(
            "INSERT INTO condemned (infohash, name, tracker, first_at, size) VALUES (?, ?, ?, ?, ?)",
            (infohash, name, tracker, first_at, size),
        )
        conn.commit()
    return first_at


async def condemn(infohash: str, name: str = "", tracker: str = "", size: int = 0) -> int:
    return await asyncio.to_thread(_condemn_sync, infohash, name, tracker, size)


def _sync_condemned_sync(current: set[str]) -> int:
    """Drop every condemned row not in `current`, returning how many."""
    with _connect() as conn:
        rows = conn.execute("SELECT infohash FROM condemned").fetchall()
        gone = [r["infohash"] for r in rows if r["infohash"] not in current]
        if gone:
            conn.executemany("DELETE FROM condemned WHERE infohash = ?", [(h,) for h in gone])
            conn.commit()
    return len(gone)


async def sync_condemned(current: set[str]) -> int:
    """Keep the ledger to exactly the torrents unregistered right now.

    A torrent that recovers — or that was only unregistered because the tracker
    was having a bad minute — loses its accrued age immediately, which is the
    whole point of the soak period.
    """
    return await asyncio.to_thread(_sync_condemned_sync, current)


async def reprieve(infohash: str) -> None:
    """Forget one condemned torrent, without touching anyone else's clock."""
    await asyncio.to_thread(_exec, "DELETE FROM condemned WHERE infohash = ?", (infohash,))


async def list_condemned() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_rows, "SELECT * FROM condemned ORDER BY first_at", ())


# --- completed deletions (permanent) ------------------------------------------


async def record_deletion(
    *,
    infohash: str,
    name: str,
    tracker: str,
    size: int,
    files_deleted: bool,
    reason: str,
    condemned_at: int,
    export_path: str = "",
) -> None:
    """Write the permanent record of a torrent stallarr removed.

    `INSERT OR REPLACE` rather than plain insert: re-adding and re-deleting the
    same infohash should leave one row describing the latest removal, not fail.
    """
    await asyncio.to_thread(
        _exec,
        """INSERT OR REPLACE INTO deletions
           (infohash, name, tracker, size, files_deleted, reason,
            condemned_at, deleted_at, export_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            infohash,
            name,
            tracker,
            int(size),
            1 if files_deleted else 0,
            reason,
            int(condemned_at),
            int(time.time()),
            export_path,
        ),
    )


async def list_deletions(limit: int = 200) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        _rows, "SELECT * FROM deletions ORDER BY deleted_at DESC LIMIT ?", (limit,)
    )


async def deletion_totals(days: int = 7) -> dict[str, Any]:
    """How much has been removed, all time and in the recent window.

    The all-time figures are why the table is exempt from pruning: they are the
    only answer to "what has this thing actually erased", and a 30-day history
    window cannot give it.
    """
    cutoff = int(time.time()) - max(days, 0) * 86400
    rows = await asyncio.to_thread(
        _rows,
        """SELECT
               COUNT(*)                                        AS all_count,
               COALESCE(SUM(size), 0)                          AS all_bytes,
               COALESCE(SUM(size * files_deleted), 0)           AS all_reclaimed,
               COALESCE(SUM(deleted_at >= ?), 0)               AS recent_count,
               COALESCE(SUM(CASE WHEN deleted_at >= ? THEN size * files_deleted ELSE 0 END), 0)
                                                               AS recent_reclaimed
           FROM deletions""",
        (cutoff, cutoff),
    )
    row = rows[0] if rows else {}
    return {
        "window_days": days,
        "count": int(row.get("all_count") or 0),
        "bytes": int(row.get("all_bytes") or 0),
        # Only deletes that took the data with them freed anything.
        "reclaimed": int(row.get("all_reclaimed") or 0),
        "recent_count": int(row.get("recent_count") or 0),
        "recent_reclaimed": int(row.get("recent_reclaimed") or 0),
    }


# --- scan snapshots -----------------------------------------------------------


async def record_snapshot(**fields: Any) -> None:
    """One row per finished scan. Keyed by second, so a re-run replaces."""
    await asyncio.to_thread(
        _exec,
        """INSERT OR REPLACE INTO scan_snapshots
           (ts, total, healthy, sick, skipped, actions, condemned, owed, duration)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            int(fields.get("ts") or time.time()),
            int(fields.get("total") or 0),
            int(fields.get("healthy") or 0),
            int(fields.get("sick") or 0),
            int(fields.get("skipped") or 0),
            int(fields.get("actions") or 0),
            int(fields.get("condemned") or 0),
            int(fields.get("owed") or 0),
            float(fields.get("duration") or 0.0),
        ),
    )


async def scan_history(days: int = 7, limit: int = 500) -> list[dict[str, Any]]:
    cutoff = int(time.time()) - max(days, 0) * 86400
    rows = await asyncio.to_thread(
        _rows,
        "SELECT * FROM scan_snapshots WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
        (cutoff, limit),
    )
    return list(reversed(rows))


async def prune_snapshots(retention_days: int) -> None:
    cutoff = int(time.time()) - retention_days * 86400
    await asyncio.to_thread(_exec, "DELETE FROM scan_snapshots WHERE ts < ?", (cutoff,))


# --- seeding obligations ------------------------------------------------------


def _record_obligations_sync(tracker: str, items: list[tuple[str, str]]) -> None:
    now = int(time.time())
    with _connect() as conn:
        for key, name in items:
            conn.execute(
                """INSERT INTO seeding_obligations (tracker, key, name, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(tracker, key) DO UPDATE SET
                       name       = excluded.name,
                       last_seen  = excluded.last_seen,
                       -- A debt that settled and is now owed again is a new
                       -- obligation. Carrying the old attempt count over would
                       -- credit this one to a repair that fixed something else.
                       first_seen  = CASE WHEN cleared_at > 0 THEN excluded.first_seen
                                          ELSE first_seen END,
                       actions     = CASE WHEN cleared_at > 0 THEN 0 ELSE actions END,
                       last_action = CASE WHEN cleared_at > 0 THEN '' ELSE last_action END,
                       cleared_at  = 0""",
                (tracker, key, name, now, now),
            )
        conn.commit()


async def record_seeding_obligations(tracker: str, items: list[tuple[str, str]]) -> None:
    """Note that these (key, name) pairs are currently owed on `tracker`."""
    if items:
        await asyncio.to_thread(_record_obligations_sync, tracker, items)


def _clear_obligations_sync(tracker: str, keys: set[str]) -> int:
    now = int(time.time())
    with _connect() as conn:
        rows = conn.execute(
            "SELECT key FROM seeding_obligations WHERE tracker = ? AND cleared_at = 0", (tracker,)
        ).fetchall()
        gone = [r["key"] for r in rows if r["key"] not in keys]
        if gone:
            conn.executemany(
                "UPDATE seeding_obligations SET cleared_at = ? WHERE tracker = ? AND key = ?",
                [(now, tracker, key) for key in gone],
            )
            conn.commit()
    return len(gone)


async def clear_seeding_obligations(tracker: str, keys: set[str]) -> int:
    """Settle every outstanding obligation on `tracker` that is not in `keys`.

    Only ever call this for a tracker that actually answered. A site that errors
    returns no rows, and treating that as "nothing is owed" would settle every
    live debt at once — and then credit stallarr with averting them.
    """
    return await asyncio.to_thread(_clear_obligations_sync, tracker, keys)


async def note_seeding_intervention(tracker: str, key: str, action: str) -> None:
    """Record that a repair landed on an obligation while it was outstanding.

    Restricted to open obligations on purpose: an action after the debt cleared
    did not clear it.
    """
    await asyncio.to_thread(
        _exec,
        """UPDATE seeding_obligations
              SET actions = actions + 1, last_action = ?
            WHERE tracker = ? AND key = ? AND cleared_at = 0""",
        (action, tracker, key),
    )


#: Columns the per-tracker aggregate produces, summed for the totals.
_METRIC_COLUMNS = ("outstanding", "working", "seen", "cleared", "averted", "self_cleared")


def _seeding_metrics_sync(days: int) -> dict[str, Any]:
    cutoff = int(time.time()) - days * 86400
    with _connect() as conn:
        by_tracker = [
            dict(r)
            for r in conn.execute(
                """SELECT tracker,
                          SUM(cleared_at = 0)                                  AS outstanding,
                          SUM(cleared_at = 0 AND actions > 0)                  AS working,
                          SUM(last_seen >= ?)                                  AS seen,
                          SUM(cleared_at > 0 AND cleared_at >= ?)              AS cleared,
                          SUM(cleared_at > 0 AND cleared_at >= ? AND actions > 0) AS averted,
                          SUM(cleared_at > 0 AND cleared_at >= ? AND actions = 0) AS self_cleared
                     FROM seeding_obligations
                    GROUP BY tracker
                    ORDER BY tracker""",
                (cutoff, cutoff, cutoff, cutoff),
            ).fetchall()
        ]
        recent = [
            dict(r)
            for r in conn.execute(
                """SELECT tracker, name, cleared_at, last_action, actions
                     FROM seeding_obligations
                    WHERE cleared_at >= ? AND actions > 0
                    ORDER BY cleared_at DESC LIMIT 8""",
                (max(cutoff, 1),),
            ).fetchall()
        ]

    totals = {col: sum(row[col] for row in by_tracker) for col in _METRIC_COLUMNS}
    return {"window_days": days, **totals, "by_tracker": by_tracker, "recent": recent}


async def seeding_metrics(days: int = 7) -> dict[str, Any]:
    """Obligation counts for the dashboard, overall and per tracker.

    `averted` is the number that stopped being owed *after* stallarr repaired
    them, which is as close to "hit-and-run avoided" as anything observable
    gets — the tracker never publishes "you were about to be hit". Debts that
    cleared with no intervention are counted separately rather than folded in,
    because claiming those would make the number meaningless.
    """
    return await asyncio.to_thread(_seeding_metrics_sync, days)


async def prune_seeding_obligations(retention_days: int) -> None:
    """Drop settled obligations past the retention window. Open ones stay."""
    cutoff = int(time.time()) - retention_days * 86400
    await asyncio.to_thread(
        _exec,
        "DELETE FROM seeding_obligations WHERE cleared_at > 0 AND cleared_at < ?",
        (cutoff,),
    )


# --- notification dedupe ------------------------------------------------------


def _should_notify_sync(key: str, window_seconds: int) -> bool:
    now = int(time.time())
    with _connect() as conn:
        row = conn.execute("SELECT last_at FROM notified WHERE key = ?", (key,)).fetchone()
        if row and now - row["last_at"] < window_seconds:
            return False
        conn.execute(
            "INSERT INTO notified (key, last_at) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET last_at = excluded.last_at",
            (key, now),
        )
        conn.commit()
    return True


async def should_notify(key: str, window_seconds: int) -> bool:
    """Atomically check-and-stamp; returns True if the caller should send."""
    return await asyncio.to_thread(_should_notify_sync, key, window_seconds)


# --- manual ignores -----------------------------------------------------------


async def add_ignore(infohash: str, name: str = "", reason: str = "") -> None:
    await asyncio.to_thread(
        _exec,
        "INSERT INTO ignores (infohash, name, reason, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(infohash) DO UPDATE SET reason = excluded.reason",
        (infohash, name, reason, int(time.time())),
    )


async def remove_ignore(infohash: str) -> None:
    await asyncio.to_thread(_exec, "DELETE FROM ignores WHERE infohash = ?", (infohash,))


async def list_ignores() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_rows, "SELECT * FROM ignores ORDER BY created_at DESC", ())


async def ignored_hashes() -> set[str]:
    rows = await asyncio.to_thread(_rows, "SELECT infohash FROM ignores", ())
    return {r["infohash"] for r in rows}
