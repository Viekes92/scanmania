"""
persist/db.py — SQLite schema, migrations, and query helpers.

Inputs:  db_path (str), async operations via aiosqlite.
Outputs: initialized database with all tables; query helpers for runs, events, leaderboard.
Invariant: WAL mode always enabled. Never rotate 'runs' table. Events rotate at 30 days.
           All run IDs are UUIDv7 (generated externally, passed in).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS players (
    id          TEXT PRIMARY KEY,
    nickname    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    extra_json  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id               TEXT PRIMARY KEY,
    player_id        TEXT REFERENCES players(id),
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    elapsed_ms       INTEGER,
    outcome          TEXT NOT NULL,
    detection_mode   TEXT NOT NULL,
    busting_beam_id  TEXT,
    segment_reached  INTEGER,
    voided_reason    TEXT,
    -- outcome as it was immediately before void_run() overwrote it, so unvoid
    -- can restore the truth instead of promoting a busted run to 'clean'
    pre_void_outcome TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT REFERENCES runs(id),
    ts_mono_ns   INTEGER NOT NULL,
    ts_wall      TEXT NOT NULL,
    type         TEXT NOT NULL,
    source       TEXT NOT NULL,
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS beam_hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT REFERENCES runs(id),
    beam_id     TEXT NOT NULL,
    ts_mono_ns  INTEGER NOT NULL,
    ratio       REAL,
    thumb_path  TEXT
);

CREATE TABLE IF NOT EXISTS health (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    component TEXT NOT NULL,
    status    TEXT NOT NULL,
    detail    TEXT
);

CREATE TABLE IF NOT EXISTS config_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    path        TEXT NOT NULL,
    before_json TEXT,
    after_json  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at   ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_outcome       ON runs(outcome);
CREATE INDEX IF NOT EXISTS idx_events_run_id      ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_ts_wall     ON events(ts_wall);
CREATE INDEX IF NOT EXISTS idx_beam_hits_run_id   ON beam_hits(run_id);
CREATE INDEX IF NOT EXISTS idx_health_ts          ON health(ts);
"""

# Current schema version — bump when adding migrations.
_SCHEMA_VERSION = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Async SQLite wrapper with schema init, migrations, and query helpers."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Open DB, enable WAL, create tables if not exist, run migrations."""
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        # WAL is set in DDL but we also assert it here for clarity.
        await self._conn.executescript(_DDL)
        await self._conn.commit()
        await self._run_migrations()
        log.info("Database initialised at %s", self._db_path)

    async def close(self) -> None:
        """Flush and close the connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.init() has not been called")
        return self._conn

    async def _run_migrations(self) -> None:
        """Apply any schema migrations that haven't been applied yet."""
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER NOT NULL)"
        )
        await self._db.commit()
        async with self._db.execute("SELECT version FROM _schema_version") as cur:
            row = await cur.fetchone()
        current = row[0] if row else 0
        if current < _SCHEMA_VERSION:
            if current < 2:
                # v2: remember the pre-void outcome so unvoid can restore it.
                # The DDL already declares the column for fresh databases, so
                # ALTER on an existing one may report a duplicate — ignore that.
                try:
                    await self._db.execute(
                        "ALTER TABLE runs ADD COLUMN pre_void_outcome TEXT"
                    )
                except Exception as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
            if current < 3:
                # v3: cloud sync removed. The outbox only ever buffered rows
                # for an endpoint that is no longer part of the system.
                await self._db.execute("DROP TABLE IF EXISTS outbox")
            await self._db.execute("DELETE FROM _schema_version")
            await self._db.execute(
                "INSERT INTO _schema_version VALUES (?)", (_SCHEMA_VERSION,)
            )
            await self._db.commit()
            log.info("Schema migrated from v%d to v%d", current, _SCHEMA_VERSION)

    async def _prune_events(self) -> None:
        """Delete events older than 30 days. Called opportunistically."""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        # Simple ISO8601 string comparison works because format is fixed.
        cutoff_str = cutoff.isoformat()
        await self._db.execute(
            "DELETE FROM events WHERE ts_wall < ?", (cutoff_str,)
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Players
    # ------------------------------------------------------------------

    async def upsert_player(
        self, id: str, nickname: str, extra: dict | None = None
    ) -> None:
        extra_json = json.dumps(extra) if extra is not None else None
        await self._db.execute(
            """
            INSERT INTO players (id, nickname, created_at, extra_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                nickname   = excluded.nickname,
                extra_json = excluded.extra_json
            """,
            (id, nickname, _now_iso(), extra_json),
        )
        await self._db.commit()

    async def get_player(self, id: str) -> dict | None:
        async with self._db.execute(
            "SELECT * FROM players WHERE id = ?", (id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    async def insert_run(self, run: dict) -> None:
        """
        Insert a run row, or update it if the id already exists.

        Upsert rather than plain INSERT so a second save for the same run can
        never vanish. A plain INSERT raised IntegrityError, the runner logged it
        and carried on, and the run kept its stale outcome while the UI showed
        the new one. A void already has its own path — see void_run().
        """
        await self._db.execute(
            """
            INSERT INTO runs
                (id, player_id, started_at, ended_at, elapsed_ms, outcome,
                 detection_mode, busting_beam_id, segment_reached, voided_reason,
                 created_at)
            VALUES
                (:id, :player_id, :started_at, :ended_at, :elapsed_ms, :outcome,
                 :detection_mode, :busting_beam_id, :segment_reached, :voided_reason,
                 :created_at)
            ON CONFLICT(id) DO UPDATE SET
                ended_at        = excluded.ended_at,
                elapsed_ms      = excluded.elapsed_ms,
                -- A void is a deliberate operator decision. A late save must
                -- not undo it. voided_reason is left alone for the same reason.
                outcome         = CASE WHEN runs.outcome = 'voided'
                                       THEN 'voided' ELSE excluded.outcome END,
                detection_mode  = excluded.detection_mode,
                busting_beam_id = excluded.busting_beam_id,
                segment_reached = excluded.segment_reached
            """,
            {
                "id": run["id"],
                "player_id": run.get("player_id"),
                "started_at": run["started_at"],
                "ended_at": run.get("ended_at"),
                "elapsed_ms": run.get("elapsed_ms"),
                "outcome": run["outcome"],
                "detection_mode": run["detection_mode"],
                "busting_beam_id": run.get("busting_beam_id"),
                "segment_reached": run.get("segment_reached"),
                "voided_reason": run.get("voided_reason"),
                "created_at": run.get("created_at", _now_iso()),
            },
        )
        await self._db.commit()

    _ALLOWED_RUN_COLUMNS = frozenset({
        "outcome", "voided_reason", "ended_at", "elapsed_ms",
        "detection_mode", "busting_beam_id", "segment_reached",
        "pre_void_outcome",
    })

    async def update_run(self, run_id: str, **kwargs: Any) -> None:
        """Update allowed columns on a run row by keyword arguments."""
        if not kwargs:
            return
        bad = set(kwargs) - self._ALLOWED_RUN_COLUMNS
        if bad:
            raise ValueError(f"update_run: disallowed columns {bad}")
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [run_id]
        await self._db.execute(
            f"UPDATE runs SET {set_clause} WHERE id = ?", values
        )
        await self._db.commit()

    async def get_run(self, run_id: str) -> dict | None:
        async with self._db.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    @staticmethod
    def _run_search_clause(q: str) -> tuple[str, list[Any]]:
        """
        Build the WHERE fragment for a run search.

        Matching is done in SQL (not after pagination) so that LIMIT/OFFSET
        page through the filtered result set rather than the full table.
        """
        if not q:
            return "", []
        like = f"%{q.lower()}%"
        clause = (
            " WHERE lower(COALESCE(p.nickname, '')) LIKE ?"
            " OR lower(r.id) LIKE ?"
            " OR lower(r.outcome) LIKE ?"
        )
        return clause, [like, like, like]

    async def list_runs(
        self, limit: int = 50, offset: int = 0, q: str = ""
    ) -> list[dict]:
        where_sql, params = self._run_search_clause(q)
        async with self._db.execute(
            f"""SELECT r.*, p.nickname AS player_nickname
                FROM runs r LEFT JOIN players p ON p.id = r.player_id
                {where_sql}
                ORDER BY r.started_at DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def count_runs(self, q: str = "") -> int:
        """Total number of runs matching the same filter as list_runs()."""
        where_sql, params = self._run_search_clause(q)
        async with self._db.execute(
            f"""SELECT COUNT(*) FROM runs r
                LEFT JOIN players p ON p.id = r.player_id
                {where_sql}""",
            params,
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_runs_by_outcome(self, since_date: str | None = None) -> dict[str, int]:
        """
        Return {outcome: count} for all runs, optionally scoped to started_at >= since_date.

        Used by the admin dashboard so its counters share one time scope instead
        of mixing "today" with "the last N rows".
        """
        if since_date:
            query = "SELECT outcome, COUNT(*) FROM runs WHERE started_at >= ? GROUP BY outcome"
            params: tuple = (since_date,)
        else:
            query = "SELECT outcome, COUNT(*) FROM runs GROUP BY outcome"
            params = ()
        async with self._db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return {r[0]: int(r[1]) for r in rows}

    async def void_run(self, run_id: str, reason: str) -> None:
        """
        Mark a run as voided with a reason. Idempotent.

        The previous outcome is preserved in pre_void_outcome so unvoid restores
        it; without this a voided bust would come back as 'clean' and re-enter
        the leaderboard with its elapsed time.
        """
        await self._db.execute(
            """UPDATE runs
               SET pre_void_outcome = CASE
                       WHEN outcome = 'voided' THEN pre_void_outcome
                       ELSE outcome
                   END,
                   outcome = 'voided',
                   voided_reason = ?
               WHERE id = ?""",
            (reason, run_id),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Leaderboard
    # ------------------------------------------------------------------

    async def get_leaderboard(
        self, scope: str = "daily", limit: int = 20
    ) -> list[dict]:
        """
        Return clean runs sorted ascending by elapsed_ms.

        scope values:
          daily      — started_at >= today UTC midnight
          activation — all time (alias for 'all')
          all        — all time
        """
        where_clauses = ["r.outcome = 'clean'", "r.elapsed_ms IS NOT NULL"]
        params: list[Any] = []

        if scope == "daily":
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            where_clauses.append("r.started_at >= ?")
            params.append(today)

        where_sql = " AND ".join(where_clauses)
        query = f"""
            SELECT r.id, r.player_id, p.nickname AS player_nickname,
                   r.started_at, r.elapsed_ms,
                   r.detection_mode, r.segment_reached
            FROM runs r
            LEFT JOIN players p ON p.id = r.player_id
            WHERE {where_sql}
            ORDER BY r.elapsed_ms ASC
            LIMIT ?
        """
        params.append(limit)

        async with self._db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Events (flight recorder)
    # ------------------------------------------------------------------

    async def insert_event(
        self,
        run_id: str | None,
        ts_mono_ns: int,
        type: str,
        source: str,
        payload: dict | None = None,
    ) -> None:
        payload_json = json.dumps(payload) if payload is not None else None
        await self._db.execute(
            """
            INSERT INTO events (run_id, ts_mono_ns, ts_wall, type, source, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, ts_mono_ns, _now_iso(), type, source, payload_json),
        )
        await self._db.commit()

    async def list_events(self, run_id: str, limit: int = 200) -> list[dict]:
        async with self._db.execute(
            """
            SELECT * FROM events WHERE run_id = ?
            ORDER BY ts_mono_ns ASC LIMIT ?
            """,
            (run_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def rotate_old_events(self) -> int:
        """Delete events older than 30 days. Returns count deleted."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        async with self._db.execute(
            "DELETE FROM events WHERE ts_wall < ?", (cutoff,)
        ) as cur:
            count = cur.rowcount
        await self._db.commit()
        if count:
            log.info("Rotated %d old events (>30 days)", count)
        return count

    # ------------------------------------------------------------------
    # Beam hits
    # ------------------------------------------------------------------

    async def insert_beam_hit(
        self,
        run_id: str,
        beam_id: str,
        ts_mono_ns: int,
        ratio: float,
        thumb_path: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO beam_hits (run_id, beam_id, ts_mono_ns, ratio, thumb_path)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, beam_id, ts_mono_ns, ratio, thumb_path),
        )
        await self._db.commit()

    async def list_beam_hits(self, run_id: str) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM beam_hits WHERE run_id = ? ORDER BY ts_mono_ns ASC",
            (run_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def insert_health(
        self, component: str, status: str, detail: str = ""
    ) -> None:
        await self._db.execute(
            "INSERT INTO health (ts, component, status, detail) VALUES (?, ?, ?, ?)",
            (_now_iso(), component, status, detail),
        )
        await self._db.commit()

    async def get_latest_health(self) -> list[dict]:
        """Return the most recent health row per component."""
        async with self._db.execute(
            """
            SELECT h.*
            FROM health h
            INNER JOIN (
                SELECT component, MAX(ts) AS max_ts
                FROM health
                GROUP BY component
            ) latest ON h.component = latest.component AND h.ts = latest.max_ts
            ORDER BY h.component
            """
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Config audit
    # ------------------------------------------------------------------

    async def insert_config_audit(
        self,
        actor: str,
        path: str,
        before: dict | None,
        after: dict,
    ) -> None:
        before_json = json.dumps(before) if before is not None else None
        after_json = json.dumps(after)
        await self._db.execute(
            """
            INSERT INTO config_audit (ts, actor, path, before_json, after_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_now_iso(), actor, path, before_json, after_json),
        )
        await self._db.commit()

    async def list_config_audit(
        self, limit: int = 50, path_filter: str | None = None
    ) -> list[dict]:
        if path_filter:
            query = (
                "SELECT * FROM config_audit WHERE path LIKE ? "
                "ORDER BY ts DESC LIMIT ?"
            )
            params = (f"%{path_filter}%", limit)
        else:
            query = "SELECT * FROM config_audit ORDER BY ts DESC LIMIT ?"
            params = (limit,)
        async with self._db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
