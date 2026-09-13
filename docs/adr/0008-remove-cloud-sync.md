# ADR 0008: Remove cloud sync

**Status:** Accepted
**Date:** 2026-09-13
**Supersedes:** the cloud half of [ADR 0006](0006-local-first-cloud-backup.md)

## Context

ADR 0006 made SQLite the source of truth and added an outbox that would push
completed runs to a cloud endpoint once connectivity allowed. The local half was
built and works. The cloud half was built and never used.

It had no endpoint. `OutboxWorker` only started when `SCANMANIA_SYNC_URL` was
set, and it never was, so in practice the feature was inert. Two of plan.md's
open questions — which cloud host, and whether the site has internet at all —
were still unanswered a fortnight before the activation.

What it did cost:

- `httpx`, a dependency nothing else used
- an `outbox` table, six `Database` methods, and rows accumulating per run
- six admin API routes and a whole Cloud Sync tab in the admin portal
- a `QueueSync` side effect emitted at eight sites in the pure FSM
- `persist/sync.py`, a filename that implied cloud sync throughout the codebase
  while containing only local snapshot and CSV code

A review also found the outbox retained pre-void payloads forever: `QueueSync`
re-read the row and `insert_outbox` was `INSERT OR IGNORE`, so a voided run kept
queueing its original `clean` payload. That bug would only ever have mattered if
the feature were switched on.

## Decision

**Remove cloud sync entirely.** Local durability is the whole durability story.

Kept:

- SQLite in WAL mode as the source of truth — unchanged
- `persist/backup.py` (renamed from `sync.py`): snapshot on demand, on clean
  shutdown, and now on a rolling interval that previously had no caller
- Leaderboard CSV export from the admin portal

Removed: `persist/outbox.py`, the `outbox` table (schema v3 drops it), the
`QueueSync` side effect, `httpx`, the six `/api/admin/outbox/*` routes, the
Cloud Sync admin tab, and `SCANMANIA_SYNC_URL` / `SCANMANIA_SYNC_TOKEN`.

Invariant 6 in CLAUDE.md is rewritten rather than deleted. "Gameplay never
awaits the network" still binds Modbus, Art-Net, RTSP and WebSocket — it simply
no longer has cloud sync as its motivating case.

## Consequences

**Getting data off the NUC is now a deliberate act.** Snapshots and CSV exports
are the only route, and someone has to collect them. Rolling snapshots run
hourly by default (`snapshot_interval_min`), so an unclean shutdown loses at
most an hour — but the files still sit on the NUC until a human fetches them.
That is the real risk this ADR accepts, and it is worth naming: if the NUC is
stolen or its disk dies, the data goes with it.

**Reinstating sync means writing it again**, against a real endpoint, with the
idempotency and backoff design from ADR 0006 as the starting point. The git
history holds the previous implementation.

**Existing NUC databases lose their outbox rows** when schema v3 applies. Those
rows describe pushes to an endpoint that never existed. Back up the database
before deploying, per `deployment.md` — a dropped table is not recoverable by
reverting the commit.
