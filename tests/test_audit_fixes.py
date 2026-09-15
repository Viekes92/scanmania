"""
tests/test_audit_fixes.py — regressions found by the 2-month-tour longevity audit.

Inputs:  the pure FSM, the config loader, the day-boundary helper, the limiter
Outputs: assertions that each fixed bug stays fixed
Invariant: every test here corresponds to a real defect, not a hypothetical —
           each docstring says what used to happen.
"""

from __future__ import annotations

import datetime as _dt

import pytest

import config.loader as loader
from core.events import (
    GmForceReset, MasterModeEngage, StopPressed, SaveRun,
    RunOutcome, RUN_SEG_3, BUSTED, FINISHED, MASTER, ATTRACT, RESET, FAULT,
)
from core.fsm import FSMContext, transition
from persist.db import day_bounds
from web.ratelimit import allow, reset as rl_reset


def _effects(fx, kind):
    return [e for e in fx if isinstance(e, kind)]


# ---------------------------------------------------------------------------
# Run integrity
# ---------------------------------------------------------------------------

def test_stop_with_a_pending_break_is_a_bust_not_a_clean_run():
    """
    Used to record `clean` with the HALT time, because Stopwatch.stop() is
    idempotent — a beam-breaking run topping the leaderboard with a time
    shorter than reality.
    """
    ctx = FSMContext()
    ctx.run_id = "r1"
    ctx.pending_break = "SM-CAM-13:d17"
    state, fx = transition(RUN_SEG_3, StopPressed(), ctx)

    assert state == BUSTED
    saves = _effects(fx, SaveRun)
    assert len(saves) == 1
    assert saves[0].outcome == RunOutcome.busted
    assert saves[0].busting_beam_id == "SM-CAM-13:d17"
    assert ctx.pending_break is None


def test_stop_without_a_pending_break_is_still_clean():
    ctx = FSMContext()
    ctx.run_id = "r1"
    state, fx = transition(RUN_SEG_3, StopPressed(), ctx)
    assert state == FINISHED
    assert _effects(fx, SaveRun)[0].outcome == RunOutcome.clean


def test_master_mode_mid_run_saves_the_run_instead_of_erasing_it():
    """The runner clears run_id on entering MASTER, so with no SaveRun the run
    vanished entirely — no row, no outcome. One mis-tap erased a player's run."""
    ctx = FSMContext()
    ctx.run_id = "r1"
    state, fx = transition(RUN_SEG_3, MasterModeEngage(), ctx)
    assert state == MASTER
    assert _effects(fx, SaveRun)[0].outcome == RunOutcome.aborted


def test_master_mode_outside_a_run_saves_nothing():
    ctx = FSMContext()
    state, fx = transition(ATTRACT, MasterModeEngage(), ctx)
    assert state == MASTER
    assert _effects(fx, SaveRun) == []


def test_an_abort_from_a_run_state_saves_the_run_as_aborted():
    """What an undecided assisted break resolves to on timeout. Not bust — that
    convicts a player nobody looked at — and not veto, which re-arms detection
    on someone still standing in the beam and loops forever."""
    from core.events import GmAbort, ABORTED
    ctx = FSMContext()
    ctx.run_id = "r1"
    ctx.pending_break = "SM-CAM-13:d17"
    state, fx = transition(RUN_SEG_3, GmAbort(), ctx)
    assert state == ABORTED
    assert _effects(fx, SaveRun)[0].outcome == RunOutcome.aborted


def test_force_reset_escapes_master_mode():
    """The FSM was always right here; the GM console could not deliver the tap."""
    ctx = FSMContext()
    assert transition(MASTER, GmForceReset(), ctx)[0] == RESET


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_out_of_range_values_are_clamped_not_accepted():
    """`max_simultaneous_breaks: 0` loaded clean and made len(dark) > 0 always
    true — every break suppressed, forever, with the config reading as fine."""
    assert loader._ranged(0, 1, 100, 10, "t") == 1
    assert loader._ranged(500, 1, 100, 10, "t") == 100
    assert loader._ranged(50, 1, 100, 10, "t") == 50


def test_a_non_numeric_value_falls_back_to_the_default():
    assert loader._ranged("banana", 1, 100, 10, "t") == 10


def test_detection_thresholds_come_from_the_detection_block():
    """They used to be read from beams[0] — one entry of the 45-channel wiring
    record that invariant 3 says is not read at runtime."""
    cfg = loader.load_all()
    assert 0 < cfg.beams.detection.break_ratio < cfg.beams.detection.clear_ratio < 1


# ---------------------------------------------------------------------------
# The operating day
# ---------------------------------------------------------------------------

def test_the_day_is_bounded_on_both_sides():
    """With only a lower bound, runs recorded while the clock was wrong pinned
    junk to the daily leaderboard permanently."""
    start, end = day_bounds()
    assert start < end


def test_a_late_night_session_stays_on_one_day():
    """UTC midnight wiped the public board at 02:00 local, mid-session."""
    late = _dt.datetime(2026, 9, 14, 3, 30).astimezone()   # 03:30 local
    evening = _dt.datetime(2026, 9, 13, 23, 30).astimezone()
    assert day_bounds(late) == day_bounds(evening)


def test_the_day_rolls_over_after_the_start_hour():
    from persist.db import DAY_START_HOUR
    before = _dt.datetime(2026, 9, 14, DAY_START_HOUR - 1, 0).astimezone()
    after = _dt.datetime(2026, 9, 14, DAY_START_HOUR + 1, 0).astimezone()
    assert day_bounds(before) != day_bounds(after)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_the_limiter_refuses_past_the_limit_and_is_per_client():
    rl_reset()
    assert all(allow("b", "1.1.1.1", 3, 60) for _ in range(3))
    assert not allow("b", "1.1.1.1", 3, 60)
    assert allow("b", "2.2.2.2", 3, 60), "one client must not lock out another"


def test_the_limiter_cannot_grow_without_bound():
    """A scanner hitting thousands of source addresses must not be the leak."""
    rl_reset()
    from web import ratelimit
    for i in range(ratelimit._MAX_KEYS + 50):
        allow("flood", f"10.0.{i // 256}.{i % 256}", 5, 60)
    assert len(ratelimit._HITS["flood"]) <= ratelimit._MAX_KEYS


# ---------------------------------------------------------------------------
# Display-name sanitisation
# ---------------------------------------------------------------------------

def test_bidi_and_invisible_codepoints_are_stripped():
    """30 characters of RTL-override render as garbage over the public board."""
    from web.routes_signin import _clean_display_name
    assert _clean_display_name("A‮bcd") == "Abcd"
    assert _clean_display_name("a​b") == "ab"


def test_combining_mark_runs_are_capped():
    from web.routes_signin import _clean_display_name
    out = _clean_display_name("e" + "́" * 30)
    assert len(out) <= 3


def test_an_ordinary_name_is_untouched():
    from web.routes_signin import _clean_display_name
    assert _clean_display_name("  Joenne  ") == "Joenne"
    assert _clean_display_name("José") == "José"


# ---------------------------------------------------------------------------
# Boot lands in MASTER, not ATTRACT
# ---------------------------------------------------------------------------

def test_boot_lands_in_master_so_a_human_checks_the_container_first():
    """A box that comes up playable after a power cut lets someone start a run
    before anyone has looked inside the maze."""
    from core.events import SelfTestPass, SELF_TEST
    ctx = FSMContext(boot_to_master=True)
    assert transition(SELF_TEST, SelfTestPass(), ctx)[0] == MASTER


def test_the_boot_flag_is_consumed_so_leaving_master_does_not_loop():
    """Exiting MASTER re-runs the self test. Without consuming the flag the box
    would bounce straight back into MASTER and never reach game mode."""
    from core.events import SelfTestPass, SELF_TEST, ATTRACT as _ATTRACT
    ctx = FSMContext(boot_to_master=True)
    transition(SELF_TEST, SelfTestPass(), ctx)
    assert ctx.boot_to_master is False
    assert transition(SELF_TEST, SelfTestPass(), ctx)[0] == _ATTRACT


def test_boot_to_master_can_be_turned_off():
    from core.events import SelfTestPass, SELF_TEST, ATTRACT as _ATTRACT
    assert transition(SELF_TEST, SelfTestPass(),
                      FSMContext(boot_to_master=False))[0] == _ATTRACT


def test_boot_into_master_does_not_start_the_attract_show():
    """The lasers must stay dark until someone has been in the container."""
    from core.events import SelfTestPass, SELF_TEST, PlayShow
    _, fx = transition(SELF_TEST, SelfTestPass(), FSMContext(boot_to_master=True))
    assert not [e for e in fx if isinstance(e, PlayShow)]


def test_a_failed_boot_probe_also_consumes_the_boot_flag():
    """The flag means "this is the boot", not "the boot succeeded".

    Only the pass path used to clear it, so a boot whose probe failed — the PoE
    switch coming up after the NUC, which is the whole reason the self test
    retries — left it set for the rest of the session. Every later
    MasterModeExit then re-ran the probe, passed, saw the flag and returned to
    MASTER: the GM's EXIT button looked dead.
    """
    from core.events import SelfTestFail, SelfTestPass, SELF_TEST, ATTRACT as _ATTRACT
    ctx = FSMContext(boot_to_master=True)
    assert transition(SELF_TEST, SelfTestFail(reason="boards"), ctx)[0] == FAULT
    assert ctx.boot_to_master is False
    # The GM fixes the hardware and leaves MASTER: they must reach game mode.
    assert transition(SELF_TEST, SelfTestPass(), ctx)[0] == _ATTRACT


def test_exiting_master_reaches_game_mode_after_a_failed_boot():
    """End to end over the path an operator actually walks on a bad morning."""
    from core.events import (BootComplete, SelfTestFail, SelfTestPass,
                             MasterModeEngage, MasterModeExit, ATTRACT as _ATTRACT)
    ctx = FSMContext(boot_to_master=True)
    state, _ = transition("BOOT", BootComplete(), ctx)
    state, _ = transition(state, SelfTestFail(reason="boards unreachable"), ctx)
    state, _ = transition(state, GmForceReset(), ctx)          # only way out of FAULT
    state, _ = transition(_ATTRACT, MasterModeEngage(), ctx)   # GM checks the box
    state, _ = transition(state, MasterModeExit(), ctx)        # re-runs the probe
    assert transition(state, SelfTestPass(), ctx)[0] == _ATTRACT


def test_force_reset_is_the_way_out_of_the_boot_master_state():
    ctx = FSMContext(boot_to_master=True)
    state, _ = transition("SELF_TEST", __import__("core.events", fromlist=["x"]).SelfTestPass(), ctx)
    assert transition(state, GmForceReset(), ctx)[0] == RESET
