"""
tests/test_outbox.py — outbox idempotency and retry logic tests.

Tests use an in-memory SQLite database and mock httpx.AsyncClient.
Verifies: 2xx deletes row, 5xx retains row, pause stops drain, resume drains,
          outbox_depth returns correct count, idempotency key is sent.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from persist.db import Database
from persist.outbox import OutboxWorker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(tmp_path):
    """In-memory (well, temp-file) SQLite database for tests."""
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.init()
    yield database
    await database.close()


def _make_metrics() -> list[tuple]:
    emitted: list[tuple] = []

    def emit(name: str, value: Any, tags: dict) -> None:
        emitted.append((name, value, tags))

    emit.emitted = emitted  # type: ignore[attr-defined]
    return emit


@pytest_asyncio.fixture
async def worker(db):
    """OutboxWorker pointed at a fake endpoint with a no-op metrics emitter."""
    metrics = _make_metrics()
    w = OutboxWorker(
        db=db,
        endpoint_url="http://fake-cloud.local",
        token="test-token",
        metrics_emit=metrics,
    )
    return w


async def _insert_run_and_outbox(db: Database, run_id: str) -> None:
    """Insert the minimum run + outbox rows needed for outbox tests."""
    run = {
        "id": run_id,
        "player_id": None,
        "started_at": "2026-09-03T10:00:00+00:00",
        "ended_at": "2026-09-03T10:01:00+00:00",
        "elapsed_ms": 60000,
        "outcome": "clean",
        "detection_mode": "auto",
        "busting_beam_id": None,
        "segment_reached": 3,
        "voided_reason": None,
        "created_at": "2026-09-03T10:00:00+00:00",
    }
    await db.insert_run(run)
    await db.insert_outbox(run_id, {"run_id": run_id, "elapsed_ms": 60000})


# ---------------------------------------------------------------------------
# HTTP response mock helper
# ---------------------------------------------------------------------------

def _mock_response(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.text = ""
    return resp


def _make_httpx_client_mock(responses: list[MagicMock]):
    """
    Return an async context manager mock for httpx.AsyncClient
    that yields successive responses from the list.
    """
    call_count = [0]

    class _MockClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, url: str, **kwargs) -> MagicMock:
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(responses):
                return responses[idx]
            return _mock_response(500)

        async def get(self, url: str, **kwargs) -> MagicMock:
            return _mock_response(200)

    return _MockClient


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_2xx_deletes_row(db, worker):
    """On a 201 response the outbox row is deleted."""
    run_id = "run-2xx-001"
    await _insert_run_and_outbox(db, run_id)

    assert await db.outbox_depth() == 1

    with patch(
        "persist.outbox.httpx.AsyncClient",
        new=_make_httpx_client_mock([_mock_response(201)]),
    ):
        pushed = await worker.force_push()

    assert pushed == 1
    assert await db.outbox_depth() == 0


@pytest.mark.asyncio
async def test_200_is_idempotent(db, worker):
    """A 200 (already stored) response also deletes the row (idempotent retry)."""
    run_id = "run-200-002"
    await _insert_run_and_outbox(db, run_id)

    with patch(
        "persist.outbox.httpx.AsyncClient",
        new=_make_httpx_client_mock([_mock_response(200)]),
    ):
        pushed = await worker.force_push()

    assert pushed == 1
    assert await db.outbox_depth() == 0


@pytest.mark.asyncio
async def test_5xx_retains_row(db, worker):
    """On a 500 response the outbox row is retained and attempt count incremented."""
    run_id = "run-5xx-003"
    await _insert_run_and_outbox(db, run_id)

    with patch(
        "persist.outbox.httpx.AsyncClient",
        new=_make_httpx_client_mock([_mock_response(500)]),
    ):
        pushed = await worker.force_push()

    assert pushed == 0
    assert await db.outbox_depth() == 1

    rows = await db.get_pending_outbox(limit=1)
    assert rows[0]["attempts"] == 1
    assert rows[0]["last_error"] is not None


@pytest.mark.asyncio
async def test_retry_after_5xx_succeeds(db, worker):
    """A row that failed with 500 is eventually pushed on a subsequent 201."""
    run_id = "run-retry-004"
    await _insert_run_and_outbox(db, run_id)

    # First call → 500, second → 201
    responses = [_mock_response(500), _mock_response(201)]

    with patch(
        "persist.outbox.httpx.AsyncClient",
        new=_make_httpx_client_mock(responses),
    ):
        # First drain: fails
        pushed = await worker.force_push()
        assert pushed == 0
        assert await db.outbox_depth() == 1

        # Reset backoff so the next force_push re-tries immediately
        await worker.reset_backoff()

        # Second drain: succeeds
        pushed = await worker.force_push()
        assert pushed == 1
        assert await db.outbox_depth() == 0


@pytest.mark.asyncio
async def test_pause_stops_drain(db, worker):
    """While paused, force_push still works (it bypasses pause), but the run loop skips draining."""
    run_id = "run-pause-005"
    await _insert_run_and_outbox(db, run_id)

    await worker.pause()
    assert worker.is_paused is True

    rows_before = await db.outbox_depth()
    assert rows_before > 0, "fixture must leave a row to drain, or this proves nothing"
    attempts_before = (await db.get_pending_outbox(limit=50))[0]["attempts"]

    # Call the loop's own path. The previous version branched on is_paused in
    # the test body, so _drain was never reached at all.
    pushed = await worker._drain()

    assert pushed == 0
    assert await db.outbox_depth() == rows_before
    # Depth alone cannot fail this test: with no endpoint the push fails and the
    # row stays either way. attempts is the discriminator — a paused worker must
    # not even try, so the counter must not move.
    attempts_after = (await db.get_pending_outbox(limit=50))[0]["attempts"]
    assert attempts_after == attempts_before, "paused worker attempted a push"


@pytest.mark.asyncio
async def test_resume_drains(db, worker):
    """Resuming after a pause triggers an immediate drain."""
    run_id = "run-resume-006"
    await _insert_run_and_outbox(db, run_id)

    await worker.pause()
    assert worker.is_paused is True

    # Resume and drain
    with patch(
        "persist.outbox.httpx.AsyncClient",
        new=_make_httpx_client_mock([_mock_response(201)]),
    ):
        await worker.resume()
        # Give the event loop a tick so the wake event is processed
        # (in the real loop resume → wake → _drain; here we call directly).
        pushed = await worker.force_push()

    assert worker.is_paused is False
    assert pushed == 1
    assert await db.outbox_depth() == 0


@pytest.mark.asyncio
async def test_outbox_depth_accuracy(db, worker):
    """outbox_depth reports the exact count of pending rows."""
    assert await db.outbox_depth() == 0

    for i in range(5):
        await _insert_run_and_outbox(db, f"run-depth-{i:03d}")

    assert await db.outbox_depth() == 5

    # Push two successfully
    with patch(
        "persist.outbox.httpx.AsyncClient",
        new=_make_httpx_client_mock([_mock_response(201)] * 5),
    ):
        await worker.force_push()

    assert await db.outbox_depth() == 0


@pytest.mark.asyncio
async def test_idempotency_key_sent(db, worker):
    """The Idempotency-Key header matches the run_id on every POST."""
    run_id = "run-idem-007"
    await _insert_run_and_outbox(db, run_id)

    captured_headers: list[dict] = []

    class _CapturingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, url: str, headers: dict = None, **kwargs):
            captured_headers.append(headers or {})
            return _mock_response(201)

    with patch("persist.outbox.httpx.AsyncClient", new=_CapturingClient):
        await worker.force_push()

    assert len(captured_headers) == 1
    assert captured_headers[0].get("Idempotency-Key") == run_id


@pytest.mark.asyncio
async def test_network_exception_retains_row(db, worker):
    """A network-level exception (not HTTP error) also retains the outbox row."""
    run_id = "run-exc-008"
    await _insert_run_and_outbox(db, run_id)

    class _ErrorClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, *args, **kwargs):
            raise ConnectionError("simulated network failure")

    with patch("persist.outbox.httpx.AsyncClient", new=_ErrorClient):
        pushed = await worker.force_push()

    assert pushed == 0
    assert await db.outbox_depth() == 1
    rows = await db.get_pending_outbox()
    assert "simulated network failure" in (rows[0]["last_error"] or "")


@pytest.mark.asyncio
async def test_insert_outbox_idempotent(db):
    """Inserting the same run_id twice into outbox is a no-op (INSERT OR IGNORE)."""
    run_id = "run-idem-insert-009"
    await _insert_run_and_outbox(db, run_id)
    # Second insert must not raise or create a duplicate
    await db.insert_outbox(run_id, {"run_id": run_id, "duplicate": True})
    assert await db.outbox_depth() == 1
