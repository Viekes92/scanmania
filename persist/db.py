"""
persist/db.py — SQLite schema, migrations, and query helpers.

Inputs:  db_path (str), async operations via aiosqlite.
Outputs: initialized database with all tables; query helpers for runs, events, leaderboard.
Invariant: WAL mode always enabled. Never rotate 'runs' table. Events rotate at 30 days.
           All run IDs are UUIDv7 (generated externally, passed in).
"""

from __future__ import annotations

import json
import os
import logging
from datetime import datetime, timedelta, timezone
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
    created_at       TEXT NOT NULL,
    -- Time penalties. A beam break costs seconds instead of ending the run, so
    -- elapsed_ms is the penalised time and raw_elapsed_ms is what the clock
    -- actually measured. Both are kept so a result stays checkable.
    penalty_count    INTEGER DEFAULT 0,
    penalty_total_ms INTEGER DEFAULT 0,
    raw_elapsed_ms   INTEGER
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
-- Covers the leaderboard: filter by outcome, order by time, without a temp
-- B-tree sort of every clean run ever on the one shared connection.
CREATE INDEX IF NOT EXISTS idx_runs_leaderboard   ON runs(outcome, elapsed_ms);
CREATE INDEX IF NOT EXISTS idx_events_run_id      ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_ts_wall     ON events(ts_wall);
CREATE INDEX IF NOT EXISTS idx_beam_hits_run_id   ON beam_hits(run_id);
CREATE INDEX IF NOT EXISTS idx_health_ts          ON health(ts);
"""

# Current schema version — bump when adding migrations.
_SCHEMA_VERSION = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# The operating day. "Daily" used to mean UTC midnight, which in CEST wiped the
# public board at 02:00 local — mid-session on a late slot — and moves to 01:00
# after the 25 Oct 2026 DST change, inside the tour window.
DAY_START_HOUR = int(os.environ.get("SCANMANIA_DAY_START_HOUR", "9"))


def day_bounds(now: datetime | None = None) -> tuple[str, str]:
    """
    ISO bounds of the current operating day, in the same UTC form runs are
    stamped with. The day rolls over at DAY_START_HOUR local time, so a session
    running past midnight stays on one leaderboard.
    """
    now = now or datetime.now().astimezone()
    start_local = now.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    if now < start_local:
        start_local -= timedelta(days=1)
    end_local = start_local + timedelta(days=1)
    return (start_local.astimezone(timezone.utc).isoformat(),
            end_local.astimezone(timezone.utc).isoformat())


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

        # One quick check at open. Corruption otherwise surfaces deep inside a
        # query at the worst moment, and systemd just restart-loops on it with
        # no indication of the cause.
        try:
            async with self._conn.execute("PRAGMA quick_check") as cur:
                row = await cur.fetchone()
            if row and row[0] != "ok":
                log.error("DATABASE INTEGRITY CHECK FAILED (%s) — restore a "
                          "snapshot from /var/backups/scanmania", row[0])
        except Exception as exc:
            log.error("Database integrity check could not run: %s", exc)

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

    async def _purge_dropped_pii(self) -> None:
        """
        Strip email and gender from every stored player.

        Only touches rows that actually carry them, so a re-run is free, and
        leaves malformed extra_json alone rather than discarding a row's other
        fields trying to clean it.
        """
        import json as _json

        async with self._db.execute(
                "SELECT id, extra_json FROM players "
                "WHERE extra_json IS NOT NULL AND extra_json != ''") as cur:
            rows = await cur.fetchall()

        changed = 0
        for row in rows:
            raw = row[1]
            try:
                extra = _json.loads(raw)
            except (ValueError, TypeError):
                log.warning("v4 purge: player %s has unparseable extra_json — "
                            "left untouched", row[0])
                continue
            if not isinstance(extra, dict):
                continue
            if not any(k in extra for k in ("email", "gender")):
                continue
            extra.pop("email", None)
            extra.pop("gender", None)
            await self._db.execute(
                "UPDATE players SET extra_json = ? WHERE id = ?",
                (_json.dumps(extra), row[0]))
            changed += 1

        if changed:
            log.warning("v4: removed email/gender from %d player row(s)", changed)
        else:
            log.info("v4: no stored email/gender to remove")

    async def _run_migrations(self) -> None:
        """Apply any schema migrations that haven't been applied yet."""
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER NOT NULL)"
        )
        await self._db.commit()
        async with self._db.execute("SELECT version FROM _schema_version") as cur:
            row = await cur.fetchone()
        current = row[0] if row else 0
        if current > _SCHEMA_VERSION:
            # An old binary against a newer DB — e.g. after a rollback deploy.
            # Refuse rather than run against a schema this code does not know.
            raise RuntimeError(
                f"database schema is v{current} but this build understands "
                f"v{_SCHEMA_VERSION}. Refusing to start — restore a matching "
                f"snapshot, or deploy the newer build."
            )
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
            if current < 4:
                # v4: forget email and gender.
                #
                # Sign-in stopped collecting them, but rows written before that
                # still carried them in extra_json — and in every hourly
                # snapshot taken since. Data we have decided not to hold should
                # not survive in the file just because it was written earlier.
                #
                # Rewritten in Python rather than with json_remove(): JSON1 is
                # near-universal but this has to run unattended on a box in a
                # shipping container, and a migration that fails there fails at
                # boot. A few thousand rows is instantaneous either way.
                await self._purge_dropped_pii()
            if current < 5:
                # v5: time penalties. A beam break costs seconds instead of
                # ending the run, so elapsed_ms is no longer the whole story —
                # a result is "42.1s plus two penalties". Stored alongside so
                # the raw time stays checkable and the CSV can show both.
                for ddl in (
                    "ALTER TABLE runs ADD COLUMN penalty_count INTEGER DEFAULT 0",
                    "ALTER TABLE runs ADD COLUMN penalty_total_ms INTEGER DEFAULT 0",
                    "ALTER TABLE runs ADD COLUMN raw_elapsed_ms INTEGER",
                ):
                    try:
                        await self._db.execute(ddl)
                    except Exception as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
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
                 created_at, penalty_count, penalty_total_ms, raw_elapsed_ms)
            VALUES
                (:id, :player_id, :started_at, :ended_at, :elapsed_ms, :outcome,
                 :detection_mode, :busting_beam_id, :segment_reached, :voided_reason,
                 :created_at, :penalty_count, :penalty_total_ms, :raw_elapsed_ms)
            ON CONFLICT(id) DO UPDATE SET
                ended_at         = excluded.ended_at,
                elapsed_ms       = excluded.elapsed_ms,
                penalty_count    = excluded.penalty_count,
                penalty_total_ms = excluded.penalty_total_ms,
                raw_elapsed_ms   = excluded.raw_elapsed_ms,
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
                # Defaulted with .get, not required: insert_run takes a plain
                # dict from the runner, the orphan-run settler, tools and
                # tests. A new named parameter with no default here is a
                # binding error at runtime for every caller that has not been
                # updated — which is exactly what adding these columns did.
                "penalty_count": run.get("penalty_count", 0) or 0,
                "penalty_total_ms": run.get("penalty_total_ms", 0) or 0,
                # Falls back to elapsed_ms so a row written by an older caller
                # still reports a sensible raw time rather than NULL.
                "raw_elapsed_ms": run.get("raw_elapsed_ms",
                                          run.get("elapsed_ms")),
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
          daily      — the current operating day (see day_bounds)
          activation — all time (alias for 'all')
          all        — all time

        One row per PLAYER, not per run: without the GROUP BY, one keen punter
        doing ten clean runs filled every row of the public outdoor display.
        """
        where_clauses = ["r.outcome = 'clean'", "r.elapsed_ms IS NOT NULL"]
        params: list[Any] = []
        # The per-player "best run" subquery has to be scoped the SAME way as
        # the outer query, or the two disagree — see below.
        best_scope_sql = ""
        best_scope_params: list[Any] = []

        if scope == "daily":
            start, end = day_bounds()
            best_scope_sql = " AND r2.started_at >= ? AND r2.started_at < ?"
            best_scope_params = [start, end]
            # Bounded on BOTH sides. With only a lower bound, a session recorded
            # while the clock was wrong (dead CMOS battery, blocked NTP) pinned
            # junk times to the daily board permanently.
            where_clauses.append("r.started_at >= ? AND r.started_at < ?")
            params.extend([start, end])

        where_sql = " AND ".join(where_clauses)
        query = f"""
            SELECT r.id, r.player_id, p.nickname AS player_nickname,
                   r.started_at, r.elapsed_ms,
                   r.detection_mode, r.segment_reached
            FROM runs r
            LEFT JOIN players p ON p.id = r.player_id
            WHERE {where_sql}
              AND r.elapsed_ms = (
                    SELECT MIN(r2.elapsed_ms) FROM runs r2
                    WHERE r2.player_id = r.player_id
                      AND r2.outcome = 'clean' AND r2.elapsed_ms IS NOT NULL
                      {best_scope_sql}
                  )
            GROUP BY r.player_id
            ORDER BY r.elapsed_ms ASC, r.started_at ASC
            LIMIT ?
        """
        params.extend(best_scope_params)
        params.append(limit)

        async with self._db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def close_orphaned_runs(self) -> int:
        """
        Mark runs left 'in_progress' by a crash or power cut as aborted.

        Invariant 7 says never RESUME a run, and that still holds — this only
        settles the record. Called once at startup, so a row written at GO that
        never reached SaveRun tells the truth instead of sitting in-progress
        forever.
        """
        async with self._db.execute(
            "UPDATE runs SET outcome = 'aborted', "
            "voided_reason = COALESCE(voided_reason, 'process restarted mid-run') "
            "WHERE outcome = 'in_progress'"
        ) as cur:
            count = cur.rowcount
        await self._db.commit()
        if count:
            log.warning("Closed %d run(s) left in progress by a restart", count)
        return count

    async def get_day_export(self, day_start: str | None = None,
                             day_end: str | None = None) -> list[dict]:
        """
        Every run of one operating day, joined to its player's details.

        This is the end-of-day export: one row per run, in the order they
        happened, including busted and aborted runs. Voided runs are included
        and flagged rather than dropped — an export that silently omits rows is
        worse than one that explains them.
        """
        if day_start is None or day_end is None:
            day_start, day_end = day_bounds()
        query = """
            SELECT r.id, r.player_id, r.started_at, r.elapsed_ms, r.outcome,
                   r.segment_reached, r.detection_mode, r.busting_beam_id,
                   r.voided_reason, r.pre_void_outcome,
                   p.nickname AS player_nickname, p.extra_json
            FROM runs r
            LEFT JOIN players p ON p.id = r.player_id
            WHERE r.started_at >= ? AND r.started_at < ?
            ORDER BY r.started_at ASC
        """
        async with self._db.execute(query, (day_start, day_end)) as cur:
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

    async def rotate_old_events(self, days: int = 30) -> int:
        """
        Delete events older than `days`. Returns count deleted.

        The module docstring has always claimed "Events rotate at 30 days";
        nothing called this, which was harmless only while nothing wrote events
        either. Now that the flight recorder is wired, this is what keeps the
        table from growing for the whole tour.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        async with self._db.execute(
            "DELETE FROM events WHERE ts_wall < ?", (cutoff,)
        ) as cur:
            count = cur.rowcount
        await self._db.commit()
        if count:
            log.info("Rotated %d old events (>%d days)", count, days)
        return count

    async def purge_old_players(self, days: int) -> int:
        """
        Forget personal details for players with no run inside `days`.

        Sign-in collects surname and date of birth from members of the
        public. Nothing ever removed them, and there was no deletion path at
        all. The player row and its nickname stay so the leaderboard still
        reads, but extra_json is emptied.
        """
        if days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        async with self._db.execute(
            """
            UPDATE players SET extra_json = NULL
            WHERE extra_json IS NOT NULL
              AND id NOT IN (SELECT DISTINCT player_id FROM runs
                             WHERE player_id IS NOT NULL AND started_at >= ?)
            """,
            (cutoff,),
        ) as cur:
            count = cur.rowcount
        await self._db.commit()
        if count:
            log.info("Purged personal details for %d player(s) older than %d days",
                     count, days)
        return count

    async def rotate_config_audit(self, keep: int = 2000) -> int:
        """Keep only the most recent `keep` audit rows."""
        async with self._db.execute(
            "DELETE FROM config_audit WHERE id NOT IN "
            "(SELECT id FROM config_audit ORDER BY id DESC LIMIT ?)", (keep,)
        ) as cur:
            count = cur.rowcount
        await self._db.commit()
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
