"""
tests/test_last_run.py — the "last run" line on the outdoor display.

Inputs:  an in-memory Database, and a GameRunner driven through whole runs
Outputs: assertions on db.get_last_run() and on _get_state_message()["last_run"]
Invariant: the line must outlive the reset back to ATTRACT (the queue outside
           is still looking at the screen), must not survive a void of that
           same run, and is ALWAYS scoped to the operating day — deliberately
           not to game.leaderboard.scope, which a multi-day venue may widen.
"""
from datetime import datetime, timedelta

import pytest

from core.events import (
    BootComplete, SelfTestPass, PlayerRegistered, PlateHigh,
    CountInRequested, RampComplete, Cp1Pressed, Cp2Pressed, StopPressed,
    GmForceReset, GmVoid,
)
from core.runner import GameRunner
from persist.db import day_bounds

from tests.test_runner import _drain


async def _row(db, rid, pid, when, ms, outcome="clean"):
    await db.insert_run({
        "id": rid, "player_id": pid, "started_at": when.isoformat(),
        "ended_at": (when + timedelta(seconds=ms / 1000)).isoformat(),
        "elapsed_ms": ms, "outcome": outcome, "detection_mode": "auto",
        "busting_beam_id": None, "segment_reached": 3,
        "voided_reason": None, "created_at": when.isoformat(),
    })


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_last_run_is_the_most_recently_finished(db):
    start, _ = day_bounds()
    t0 = datetime.fromisoformat(start) + timedelta(hours=2)
    await db.upsert_player("p1", "Ada")
    await db.upsert_player("p2", "Grace")
    await _row(db, "r1", "p1", t0, 20_000)
    await _row(db, "r2", "p2", t0 + timedelta(minutes=5), 25_000)

    lr = await db.get_last_run()
    assert lr["player_nickname"] == "Grace"
    assert lr["elapsed_ms"] == 25_000


@pytest.mark.asyncio
async def test_a_busted_run_still_counts_as_the_last_one(db):
    """Breaks cost time now; a GM bust is still a run somebody just did."""
    start, _ = day_bounds()
    t0 = datetime.fromisoformat(start) + timedelta(hours=2)
    await db.upsert_player("p1", "Ada")
    await _row(db, "r1", "p1", t0, 20_000, outcome="busted")

    lr = await db.get_last_run()
    assert lr is not None and lr["outcome"] == "busted"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["aborted", "voided", "in_progress"])
async def test_unfinished_and_struck_runs_are_never_the_last_run(db, outcome):
    """
    A time nobody completed, or one the GM struck, must not go on the street.
    """
    start, _ = day_bounds()
    t0 = datetime.fromisoformat(start) + timedelta(hours=2)
    await db.upsert_player("p1", "Ada")
    await db.upsert_player("p2", "Grace")
    await _row(db, "r1", "p1", t0, 20_000)                       # good
    await _row(db, "r2", "p2", t0 + timedelta(minutes=5), 9_999,
               outcome=outcome)                                   # later, bad

    lr = await db.get_last_run()
    assert lr["player_nickname"] == "Ada", lr


@pytest.mark.asyncio
async def test_yesterdays_run_does_not_show_this_morning(db):
    """
    The same bug the leaderboard had: an unattended night leaving yesterday's
    name under this morning's board, on a screen the public photographs.
    """
    start, _ = day_bounds()
    today = datetime.fromisoformat(start) + timedelta(hours=2)
    await db.upsert_player("p1", "Ada")
    await _row(db, "r1", "p1", today - timedelta(days=1), 20_000)

    assert await db.get_last_run() is None, "yesterday's runner must not show"


@pytest.mark.asyncio
async def test_the_line_is_day_scoped_even_when_the_board_is_not(db):
    """
    game.leaderboard.scope must not reach this query.

    An all-time board is a reasonable choice for a multi-day activation. An
    all-time "LAST RUN" is not: it would put yesterday's last player under
    this morning's queue on a screen that faces the street.
    """
    start, _ = day_bounds()
    today = datetime.fromisoformat(start) + timedelta(hours=2)
    await db.upsert_player("p1", "Ada")
    await _row(db, "r1", "p1", today - timedelta(days=1), 20_000)

    # The all-time BOARD still carries the run...
    board = await db.get_leaderboard(scope="all", limit=10)
    assert [r["player_nickname"] for r in board] == ["Ada"]
    # ...while the line does not.
    assert await db.get_last_run() is None


# ---------------------------------------------------------------------------
# The wiring
# ---------------------------------------------------------------------------

def _runner(fake_config, fake_io, fake_inputs, fake_vision, db):
    return GameRunner(config=fake_config, io_backend=fake_io,
                      inputs_backend=fake_inputs, vision_backend=fake_vision,
                      db=db)


async def _clean_run(r, player_id, nickname):
    await r.dispatch(PlayerRegistered(player_id=player_id, nickname=nickname))
    await r.dispatch(PlateHigh())
    await _drain(r, iterations=5, pause=0)
    await r.dispatch(CountInRequested())
    await r.dispatch(RampComplete())
    await r.dispatch(Cp1Pressed())
    await r.dispatch(Cp2Pressed())
    run_id = r.context.run_id
    await r.dispatch(StopPressed())
    return run_id


def _cleanup(r):
    for attr in ("_arm_timeout_task", "_result_timeout_task",
                 "_max_run_task", "_count_in_task"):
        r._cancel_task(attr)


@pytest.mark.asyncio
async def test_last_run_outlives_the_reset_to_attract(
        fake_config, fake_io, fake_inputs, fake_vision, db):
    """
    The whole point of the line. _last_outcome and _last_rank are cleared on
    the way back to ATTRACT; this one must not be, or the queue outside never
    sees who just ran.
    """
    r = _runner(fake_config, fake_io, fake_inputs, fake_vision, db)
    await db.upsert_player("p-1", "Ada")
    await r.dispatch(BootComplete())
    await r.dispatch(SelfTestPass())

    await _clean_run(r, "p-1", "Ada")

    msg = r._get_state_message()
    assert msg["last_run"]["player_nickname"] == "Ada"

    await r.dispatch(GmForceReset())
    msg = r._get_state_message()
    assert msg["state"] == "ATTRACT"
    assert msg["outcome"] is None, "the result itself should have been cleared"
    assert msg["last_run"]["player_nickname"] == "Ada", \
        "the last-run line must survive the reset"
    _cleanup(r)


@pytest.mark.asyncio
async def test_a_second_run_replaces_the_first(
        fake_config, fake_io, fake_inputs, fake_vision, db):
    r = _runner(fake_config, fake_io, fake_inputs, fake_vision, db)
    await db.upsert_player("p-1", "Ada")
    await db.upsert_player("p-2", "Grace")
    await r.dispatch(BootComplete())
    await r.dispatch(SelfTestPass())

    await _clean_run(r, "p-1", "Ada")
    await r.dispatch(GmForceReset())
    await _clean_run(r, "p-2", "Grace")

    assert r._get_state_message()["last_run"]["player_nickname"] == "Grace"
    _cleanup(r)


@pytest.mark.asyncio
async def test_voiding_that_run_takes_it_off_the_display(
        fake_config, fake_io, fake_inputs, fake_vision, db):
    """
    A void is a deliberate operator decision — the run must leave the street
    display too, not just the leaderboard.
    """
    r = _runner(fake_config, fake_io, fake_inputs, fake_vision, db)
    await db.upsert_player("p-1", "Ada")
    await r.dispatch(BootComplete())
    await r.dispatch(SelfTestPass())

    await _clean_run(r, "p-1", "Ada")
    assert r._get_state_message()["last_run"]["player_nickname"] == "Ada"

    await r.dispatch(GmVoid(reason="climbed"))
    await _drain(r, iterations=5, pause=0)

    assert r._get_state_message()["last_run"] is None, \
        "a voided run must not stay on the outdoor display"
    _cleanup(r)
