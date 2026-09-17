"""
tests/test_leaderboard_scope.py — the daily board must show TODAY's best.

Inputs:  an in-memory Database with runs spanning two operating days
Outputs: assertions on get_leaderboard(scope=...)
Invariant: the "one row per player" subquery must be scoped the same way as the
           outer query, or a player's best-ever run hides their best run today.
"""
from datetime import timedelta

import pytest

from persist.db import day_bounds
from datetime import datetime


async def _run(db, rid, pid, when, ms, outcome="clean"):
    await db.insert_run({
        "id": rid, "player_id": pid, "started_at": when.isoformat(),
        "elapsed_ms": ms, "outcome": outcome, "detection_mode": "auto",
        "busting_beam_id": None, "segment_reached": 3,
        "voided_reason": None, "created_at": when.isoformat(),
    })


@pytest.mark.asyncio
async def test_yesterdays_faster_run_does_not_hide_todays(db):
    """
    A returning player must appear on the daily board with TODAY's time.

    The per-player "best run" subquery took MIN over the player's entire
    history while the outer query was filtered to today, so the two disagreed:
    if the all-time best was set on an earlier day, no row from today could
    ever equal it and the player vanished from today's board completely. On a
    two-month tour that silently empties the daily leaderboard of regulars.
    """
    start, _end = day_bounds()
    today = datetime.fromisoformat(start) + timedelta(hours=2)
    yesterday = today - timedelta(days=1)

    await db.upsert_player("p1", "Ada")
    await db.upsert_player("p2", "Grace")
    await _run(db, "r1", "p1", yesterday, 20_000)   # Ada, faster, YESTERDAY
    await _run(db, "r2", "p1", today, 22_000)       # Ada, today
    await _run(db, "r3", "p2", today, 25_000)       # Grace, today only

    board = await db.get_leaderboard(scope="daily", limit=10)
    got = [(r["player_nickname"], r["elapsed_ms"]) for r in board]
    assert got == [("Ada", 22_000), ("Grace", 25_000)], got


@pytest.mark.asyncio
async def test_the_all_time_board_still_takes_the_best_ever(db):
    """Scoping the subquery must not break the all-time board."""
    start, _end = day_bounds()
    today = datetime.fromisoformat(start) + timedelta(hours=2)
    yesterday = today - timedelta(days=1)

    await db.upsert_player("p1", "Ada")
    await _run(db, "r1", "p1", yesterday, 20_000)
    await _run(db, "r2", "p1", today, 22_000)

    board = await db.get_leaderboard(scope="all", limit=10)
    assert [(r["player_nickname"], r["elapsed_ms"]) for r in board] == [("Ada", 20_000)]


@pytest.mark.asyncio
async def test_one_row_per_player_still_holds(db):
    """The GROUP BY exists because a keen punter filled the outdoor display."""
    start, _end = day_bounds()
    today = datetime.fromisoformat(start) + timedelta(hours=2)

    await db.upsert_player("p1", "Ada")
    for i in range(5):
        await _run(db, f"r{i}", "p1", today + timedelta(minutes=i), 30_000 - i * 100)

    board = await db.get_leaderboard(scope="daily", limit=10)
    assert len(board) == 1, "one keen punter filled the board again"
    assert board[0]["elapsed_ms"] == 29_600, "not their best run of the day"
