# ADR 0006: Local-first, cloud-eventually results durability

**Status:** Partially superseded by [ADR 0008](0008-remove-cloud-sync.md)
**Note:** the local-first half stands. The cloud-eventually half was removed on
2026-09-13 — there is no outbox and no remote endpoint. `persist/sync.py` is now
`persist/backup.py` and does local snapshots only.
**Date:** 2024-01-15

## Context

The scoreboard must never be lost. The activation may run with intermittent or absent internet
(4G hotspot, venue Wi-Fi, or nothing at all). Gameplay must never block on the network — a 500 ms
cloud timeout before a "BUSTED" animation is unacceptable.

## Decision

**SQLite in WAL mode is the source of truth during operation.** Every completed run is
simultaneously inserted into `runs` and `outbox`. A continuously-running background service
(`scanmania-sync.service`) drains the outbox to the cloud endpoint with idempotent POSTs.

- `Idempotency-Key: run_uuid` (UUIDv7 generated at run start) — retries are safe, duplicates
  are impossible server-side.
- Exponential backoff with jitter per row, capped at 60 s, retrying indefinitely. Days offline
  is a normal condition, not an error state.
- Rows deleted from outbox only on a 2xx response. Nothing is ever dropped, expired, or
  garbage-collected.
- Heartbeat push every 60 s: system state, outbox depth, and health telemetry.
- Nothing in the game path ever `await`s the network. The FSM returns immediately after SQLite
  writes; `persist/sync.py` is never called from the game path.

Cloud side is deliberately simple: FastAPI + Postgres on a VPS, or Supabase, or Cloudflare
Worker + D1. One endpoint: `POST /v1/runs` with an idempotency key.

## Alternatives considered

**Cloud-primary, local cache:** Gameplay blocks on network latency. Unacceptable at hard-cutoff
scoring stakes — any network call in the game path is a reliability risk.

**Scheduled sync (cron job):** A nightly job means up to 12 hours of data lives locally only.
NUC failure at 17:00 + sync at 02:00 = a full afternoon of runs at risk. The continuously
running drain has no such window.

**Event sourcing to a real-time cloud stream:** More complex, requires a persistent message
queue on the NUC, and makes the cloud a read dependency for the leaderboard. The outbox pattern
gives the same guarantee with a single SQLite table and an HTTP POST.

**Local-only, no cloud backup:** Acceptable as a last resort but not as a design. The scoreboard
surviving a hardware failure is a stated client requirement.

## Consequences

- Gameplay is never affected by network state. The game path touches SQLite only.
- Outbox depth is always visible on the admin portal and the GM console. Paused backup is a loud
  persistent amber banner on both surfaces.
- Rolling local snapshots every 60 min into `/var/backups/scanmania/`, keeping the last 48.
  Belt-and-suspenders: even without internet, a USB drive is always enough.
- `Export snapshot` in the admin portal: `VACUUM INTO` a dated `.db` file plus a leaderboard
  CSV, downloadable from the browser. On demand, and automatically on service stop.
- The outdoor leaderboard renders from local data. The cloud leaderboard, if used, is an
  optional enhancement that degrades to local on any failure.
- Cloud credentials live in `/etc/scanmania/secrets.env` (`chmod 600`). Never in the repo.
