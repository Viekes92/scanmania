"""
persist/outbox.py — continuously drains the outbox to the cloud endpoint.

Inputs:  Database instance, cloud endpoint URL + bearer token, metrics callable.
Outputs: POST /v1/runs with Idempotency-Key: run_id; row deleted on 2xx.
Invariant: retries indefinitely with exponential backoff + jitter (cap 60 s).
           Rows only deleted on 2xx. Never drops a row on any error.
           Gameplay never awaits this module.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Callable

import httpx

from persist.db import Database

log = logging.getLogger(__name__)

# Backoff constants
_BASE_DELAY_S = 1.0
_MAX_DELAY_S = 60.0
_SWEEP_INTERVAL_S = 10.0
_HEARTBEAT_INTERVAL_S = 60.0
_POST_TIMEOUT_S = 15.0


def _backoff_delay(attempts: int) -> float:
    """Exponential backoff with ±25 % jitter, capped at _MAX_DELAY_S."""
    raw = _BASE_DELAY_S * (2 ** min(attempts, 10))
    capped = min(raw, _MAX_DELAY_S)
    jitter = capped * 0.25 * (2 * random.random() - 1)
    return max(0.0, capped + jitter)


class OutboxWorker:
    """
    Drains the outbox table to the cloud endpoint.

    Woken on every new insert; also sweeps every _SWEEP_INTERVAL_S.
    Exponential backoff per row, capped at 60 s.
    Pause/resume controlled by the admin portal.
    """

    def __init__(
        self,
        db: Database,
        endpoint_url: str,
        token: str,
        metrics_emit: Callable,
    ) -> None:
        self._db = db
        self._endpoint_url = endpoint_url.rstrip("/")
        self._token = token
        self._metrics_emit = metrics_emit
        self._paused = False
        self._wake_event = asyncio.Event()
        self._last_push_ok_at: float | None = None
        self._last_error: str | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main drain loop. Woken on insert; also sweeps every 10 s."""
        self._running = True
        log.info("OutboxWorker started, endpoint=%s", self._endpoint_url)
        last_heartbeat = time.monotonic()

        while self._running:
            # Wait for a wake signal or the sweep interval, whichever comes first.
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=_SWEEP_INTERVAL_S
                )
            except asyncio.TimeoutError:
                pass
            self._wake_event.clear()

            if self._paused:
                continue

            await self._drain()

            # Send a heartbeat to the cloud every _HEARTBEAT_INTERVAL_S.
            now = time.monotonic()
            if now - last_heartbeat >= _HEARTBEAT_INTERVAL_S:
                await self._send_heartbeat()
                last_heartbeat = now

        log.info("OutboxWorker stopped")

    async def stop(self) -> None:
        """Signal the run loop to exit cleanly."""
        self._running = False
        self._wake_event.set()

    async def wake(self) -> None:
        """Signal a new row was inserted — drain immediately."""
        self._wake_event.set()

    async def pause(self) -> None:
        """Pause draining. Outbox accumulates. Logs and emits metric."""
        if not self._paused:
            self._paused = True
            log.warning("OutboxWorker PAUSED — outbox accumulating")
            self._metrics_emit("sync.paused", value=1, tags={})

    async def resume(self) -> None:
        """Resume draining and drain immediately."""
        if self._paused:
            self._paused = False
            log.info("OutboxWorker RESUMED")
            self._metrics_emit("sync.paused", value=0, tags={})
            await self.wake()

    async def force_push(self) -> int:
        """Ignore backoff, drain all rows now. Returns rows pushed."""
        if self._paused:
            log.warning("force_push called while paused — running anyway")
        await self._db.reset_outbox_backoff()
        rows = await self._drain(force=True)
        return rows

    async def reset_backoff(self) -> None:
        """Reset backoff clock on all rows with exhausted intervals."""
        await self._db.reset_outbox_backoff()
        log.info("Outbox backoff reset for all rows")
        await self.wake()

    async def test_endpoint(self) -> dict:
        """GET /v1/health. Returns {ok: bool, status_code: int, latency_ms: int}."""
        url = f"{self._endpoint_url}/v1/health"
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_POST_TIMEOUT_S) as client:
                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {self._token}"}
                )
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {
                "ok": resp.is_success,
                "status_code": resp.status_code,
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {"ok": False, "status_code": 0, "latency_ms": latency_ms, "error": str(exc)}

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def last_push_ok_at(self) -> float | None:
        return self._last_push_ok_at

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _drain(self, force: bool = False) -> int:
        """
        Process pending outbox rows.

        force=True skips backoff gating AND the pause check (used by force_push,
        which is an explicit operator action).

        The pause check lives here rather than only in run(), so pausing is
        enforced at the point the work happens. With the guard only in the loop,
        the test for it had to re-implement the check in the test body and could
        never fail.

        Returns number of rows successfully pushed this pass.
        """
        if self._paused and not force:
            return 0
        pushed = 0
        rows = await self._db.get_pending_outbox(limit=50)
        for row in rows:
            if not force:
                delay = _backoff_delay(row["attempts"])
                if row["last_attempt_at"] is not None:
                    # If we haven't waited long enough since last attempt, skip.
                    last_str = row["last_attempt_at"]
                    try:
                        last_dt = datetime.fromisoformat(last_str)
                        elapsed = (
                            datetime.now(timezone.utc) - last_dt
                        ).total_seconds()
                        if elapsed < delay:
                            continue
                    except ValueError:
                        pass

            ok = await self._post_run(row)
            if ok:
                await self._db.mark_outbox_success(row["run_id"])
                self._last_push_ok_at = time.monotonic()
                self._last_error = None
                pushed += 1
                self._metrics_emit(
                    "sync.push_ok", value=1, tags={"run_id": row["run_id"]}
                )
            else:
                self._metrics_emit(
                    "sync.push_failed", value=1, tags={"run_id": row["run_id"]}
                )

        depth = await self._db.outbox_depth()
        self._metrics_emit("sync.outbox_depth", value=depth, tags={})
        return pushed

    async def _post_run(self, row: dict) -> bool:
        """POST one row to cloud. Returns True on 2xx. Uses httpx."""
        run_id = row["run_id"]
        url = f"{self._endpoint_url}/v1/runs"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Idempotency-Key": run_id,
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=_POST_TIMEOUT_S) as client:
                resp = await client.post(
                    url, content=row["payload_json"], headers=headers
                )
            if resp.is_success:
                log.debug("Outbox pushed run_id=%s status=%d", run_id, resp.status_code)
                return True
            else:
                error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                log.warning("Outbox push failed run_id=%s %s", run_id, error)
                await self._db.mark_outbox_attempt(run_id, error=error)
                self._last_error = error
                return False
        except Exception as exc:
            error = str(exc)
            log.warning("Outbox push exception run_id=%s %s", run_id, error)
            await self._db.mark_outbox_attempt(run_id, error=error)
            self._last_error = error
            return False

    async def _send_heartbeat(self) -> None:
        """POST a heartbeat payload to /v1/heartbeat (best-effort, no retry)."""
        url = f"{self._endpoint_url}/v1/heartbeat"
        depth = await self._db.outbox_depth()
        payload = {
            "source": "scanmania-sync",
            "outbox_depth": depth,
            "paused": self._paused,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json=payload, headers=headers)
        except Exception as exc:
            log.debug("Heartbeat failed (non-fatal): %s", exc)
