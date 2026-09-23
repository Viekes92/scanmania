"""
tests/test_fsm.py — comprehensive FSM transition tests for ScanMania.

No mocks required. The FSM is a pure function; every test calls transition()
directly. Coverage includes every path described in plan.md §5.3 and core/fsm.py.
"""

from __future__ import annotations

import pytest

from core.events import (
    # States
    BOOT, SELF_TEST, ATTRACT, REGISTERED, ARM, COUNTDOWN,
    RUN_SEG_1, RUN_SEG_2, RUN_SEG_3,
    FINISHED, BUSTED, ABORTED, RESULT, RESET, FAULT, MASTER,
    # Run outcomes and detection modes
    RunOutcome, DetectionMode,
    # Input events
    BootComplete, SelfTestPass, SelfTestFail, PlayerRegistered,
    PlateHigh, PlateLow, PreflightPass, PreflightFail,
    CountInRequested, RampComplete, BaselineCaptureFail,
    BreakConfirmed, Cp1Pressed, Cp2Pressed, StopPressed,
    MaxRunExceeded, GmBust, GmAbort, GmVoid, GmForceReset,
    GmCancel, GmConfirmBreak, GmVetoBreak,
    MasterModeEngage, MasterModeExit,
    VisionStalled, DetectionModeChanged, ProcessRestart,
    ArmTimeout, ResultDisplayTimeout,
    # Side effects
    ApplyPreset, PlayShow, StopShow, StartStopwatch, StopStopwatch, ResetStopwatch,
    ArmDetection, DisarmDetection, StartCountIn, BeamPreflightCheck,
    ReadyBlink, SaveRun, VoidRun, BroadcastState, EmitMetric,
    SaveBreakEvidence, DropDetectionMode,
    ApplyTimePenalty,
    RevokeTimePenalty,)
from core.fsm import FSMContext, transition, make_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ctx(**kwargs) -> FSMContext:
    """Create an FSMContext with optional field overrides."""
    return make_context(**kwargs)


def step(state: str, event, ctx: FSMContext | None = None) -> tuple[str, list]:
    """Single transition helper."""
    if ctx is None:
        ctx = make_ctx()
    return transition(state, event, ctx)


def new_state(state: str, event, ctx: FSMContext | None = None) -> str:
    """Return only the new state from a single transition."""
    s, _ = step(state, event, ctx)
    return s


def effects(state: str, event, ctx: FSMContext | None = None) -> list:
    """Return only the side effects from a single transition."""
    _, e = step(state, event, ctx)
    return e


def effect_types(state: str, event, ctx: FSMContext | None = None) -> list[type]:
    """Return the types of side effects from a single transition."""
    return [type(e) for e in effects(state, event, ctx)]


def run_transitions(events_list: list, initial_state: str = BOOT,
                    ctx: FSMContext | None = None) -> list[str]:
    """
    Drive the FSM through a list of events and return every state visited
    (including the initial state).

    Automatically handles the RESET → ATTRACT transient: whenever the FSM
    enters RESET, an ATTRACT state is appended (mirroring runner.py behaviour).
    """
    if ctx is None:
        ctx = make_ctx()
    states = [initial_state]
    state = initial_state
    for event in events_list:
        state, _ = transition(state, event, ctx)
        states.append(state)
        # Mirror runner.py: RESET is transient, always followed by ATTRACT.
        if state == RESET:
            state = ATTRACT
            states.append(state)
    return states


def _registered_ctx(**kwargs) -> FSMContext:
    """Context with a player already registered (common setup)."""
    ctx = make_ctx(**kwargs)
    ctx.player_id = "player-1"
    ctx.player_nickname = "Alice"
    ctx.run_id = "run-1"
    return ctx


def _run_ctx(segment: int = 1, mode: str = DetectionMode.auto, **kwargs) -> FSMContext:
    """Context appropriate for a RUN state."""
    ctx = _registered_ctx(**kwargs)
    ctx.detection_mode = mode
    ctx.segment = segment
    return ctx


# ---------------------------------------------------------------------------
# Boot and self-test
# ---------------------------------------------------------------------------

class TestBoot:
    def test_boot_complete_to_self_test(self):
        assert new_state(BOOT, BootComplete()) == SELF_TEST

    def test_self_test_pass_to_attract(self):
        s, fx = step(SELF_TEST, SelfTestPass())
        assert s == ATTRACT
        assert PlayShow in effect_types(SELF_TEST, SelfTestPass())

    def test_self_test_fail_to_fault(self):
        s, fx = step(SELF_TEST, SelfTestFail(reason="board_1 unreachable"))
        assert s == FAULT

    def test_unknown_event_in_boot_is_ignored(self):
        assert new_state(BOOT, StopPressed()) == BOOT


# ---------------------------------------------------------------------------
# Full clean run: BOOT → ATTRACT → RUN_SEG_3 → FINISHED → RESULT → RESET → ATTRACT
# ---------------------------------------------------------------------------

class TestFullCleanRun:
    def test_full_clean_run_state_sequence(self):
        ctx = make_ctx()
        states = run_transitions([
            BootComplete(),
            SelfTestPass(),
            PlayerRegistered(player_id="p1", nickname="Alice"),
            PlateHigh(),
            PreflightPass(),
            CountInRequested(),
            RampComplete(),
            Cp1Pressed(),
            Cp2Pressed(),
            StopPressed(),
            ResultDisplayTimeout(),
            ResultDisplayTimeout(),   # RESULT → RESET → ATTRACT
        ], ctx=ctx)

        assert BOOT       in states
        assert SELF_TEST  in states
        assert ATTRACT    in states
        assert REGISTERED in states
        assert ARM        in states
        assert COUNTDOWN  in states
        assert RUN_SEG_1  in states
        assert RUN_SEG_2  in states
        assert RUN_SEG_3  in states
        assert FINISHED   in states
        assert RESULT     in states
        assert RESET      in states

    def test_stop_pressed_emits_save_run_clean(self):
        ctx = _run_ctx(segment=3)
        s, fx = step(RUN_SEG_3, StopPressed(), ctx)
        assert s == FINISHED
        save_effects = [e for e in fx if isinstance(e, SaveRun)]
        assert len(save_effects) == 1
        assert save_effects[0].outcome == RunOutcome.clean

    def test_stop_pressed_emits_stop_stopwatch(self):
        ctx = _run_ctx(segment=3)
        assert StopStopwatch in effect_types(RUN_SEG_3, StopPressed(), ctx)


    def test_finished_to_result_on_timeout(self):
        assert new_state(FINISHED, ResultDisplayTimeout()) == RESULT

    def test_result_to_reset_on_timeout(self):
        assert new_state(RESULT, ResultDisplayTimeout()) == RESET

    def test_ramp_complete_applies_segment1_preset(self):
        assert ApplyPreset("maze_1") in effects(COUNTDOWN, RampComplete())

    def test_ramp_complete_starts_stopwatch(self):
        assert StartStopwatch in effect_types(COUNTDOWN, RampComplete())

    def test_ramp_complete_arms_detection(self):
        assert ArmDetection in effect_types(COUNTDOWN, RampComplete())

    def test_plate_high_from_registered_triggers_blink_and_preflight(self):
        fx_types = effect_types(REGISTERED, PlateHigh())
        assert ReadyBlink in fx_types
        assert BeamPreflightCheck in fx_types

    def test_count_in_requested_starts_count_in(self):
        assert StartCountIn in effect_types(ARM, CountInRequested())


# ---------------------------------------------------------------------------
# Bust in each segment — auto mode
# ---------------------------------------------------------------------------

class TestBreakAppliesATimePenalty:
    """
    A confirmed break costs TIME. It does not end the run any more.

    Busting is a gamemaster decision now — see TestGmBustStillEndsARun. These
    tests pin the replacement: penalty applied, run continues, and a cooldown
    that stops a player parked in a beam collecting one penalty per frame.
    """

    def _evt(self, elapsed_ms: int = 0) -> BreakConfirmed:
        return BreakConfirmed(beam_id="b01", ratio=0.2, run_id="run-1",
                              run_elapsed_ms=elapsed_ms)

    def _auto_ctx(self, segment: int = 1) -> FSMContext:
        return _run_ctx(segment=segment, mode=DetectionMode.auto)

    def test_break_does_not_end_the_run(self):
        for state, seg in ((RUN_SEG_1, 1), (RUN_SEG_2, 2), (RUN_SEG_3, 3)):
            ctx = self._auto_ctx(seg)
            s, _ = step(state, self._evt(), ctx)
            assert s == state, f"a break ended the run in {state}"

    def test_break_emits_a_time_penalty(self):
        ctx = self._auto_ctx(1)
        fx = effects(RUN_SEG_1, self._evt(), ctx)
        pen = [e for e in fx if isinstance(e, ApplyTimePenalty)]
        assert len(pen) == 1
        assert pen[0].ms == ctx.penalty_ms
        assert pen[0].beam_id == "b01"

    def test_break_does_not_stop_the_stopwatch(self):
        ctx = self._auto_ctx(1)
        assert StopStopwatch not in effect_types(RUN_SEG_1, self._evt(), ctx)

    def test_break_saves_no_run_and_plays_no_bust_show(self):
        ctx = self._auto_ctx(1)
        fx = effects(RUN_SEG_1, self._evt(), ctx)
        assert not [e for e in fx if isinstance(e, SaveRun)]
        assert PlayShow("bust") not in fx

    def test_break_still_saves_evidence(self):
        ctx = self._auto_ctx(1)
        fx = effects(RUN_SEG_1, self._evt(), ctx)
        assert any(isinstance(e, SaveBreakEvidence) for e in fx)

    def test_context_counts_the_penalty(self):
        ctx = self._auto_ctx(1)
        step(RUN_SEG_1, self._evt(), ctx)
        assert ctx.penalty_count == 1
        assert ctx.penalty_total_ms == ctx.penalty_ms
        assert ctx.last_penalty_beam == "b01"

    # --- the cooldown, which is the whole point -----------------------------

    def test_a_second_break_inside_the_cooldown_is_suppressed(self):
        """
        Without this a player standing in a beam collects a penalty per
        detection — ~6 a second at the shipped rate limit — and finishes
        several minutes down.
        """
        ctx = self._auto_ctx(1)
        step(RUN_SEG_1, self._evt(elapsed_ms=1_000), ctx)
        fx = effects(RUN_SEG_1, self._evt(elapsed_ms=1_200), ctx)
        assert not [e for e in fx if isinstance(e, ApplyTimePenalty)]
        assert ctx.penalty_count == 1, "a stacked penalty got through"

    def test_a_break_after_the_cooldown_applies_again(self):
        ctx = self._auto_ctx(1)
        step(RUN_SEG_1, self._evt(elapsed_ms=1_000), ctx)
        after = 1_000 + ctx.penalty_cooldown_ms
        fx = effects(RUN_SEG_1, self._evt(elapsed_ms=after), ctx)
        assert [e for e in fx if isinstance(e, ApplyTimePenalty)]
        assert ctx.penalty_count == 2

    def test_a_zero_cooldown_lets_every_break_through(self):
        ctx = self._auto_ctx(1)
        ctx.penalty_cooldown_ms = 0
        for i in range(4):
            step(RUN_SEG_1, self._evt(elapsed_ms=i), ctx)
        assert ctx.penalty_count == 4

    def test_the_cooldown_is_measured_from_the_last_APPLIED_penalty(self):
        """A suppressed break must not push the window along behind it."""
        ctx = self._auto_ctx(1)
        step(RUN_SEG_1, self._evt(elapsed_ms=0), ctx)          # applies
        step(RUN_SEG_1, self._evt(elapsed_ms=4_000), ctx)      # suppressed
        fx = effects(RUN_SEG_1, self._evt(elapsed_ms=5_000), ctx)
        assert [e for e in fx if isinstance(e, ApplyTimePenalty)], (
            "a suppressed break extended the cooldown")


class TestPenaltyInAssistedMode:
    """
    Assisted raises the penalty with the GM, who may take it back.

    It does NOT halt the clock any more. Halting it was right when the decision
    was "end this run or not"; with a penalty the game keeps flowing, and
    halting also handed the player however long the GM took to decide, because
    nothing stopped them running.
    """

    def _evt(self, elapsed_ms: int = 0) -> BreakConfirmed:
        return BreakConfirmed(beam_id="b02", ratio=0.25, run_id="run-2",
                              run_elapsed_ms=elapsed_ms)

    def _ctx(self) -> FSMContext:
        return _run_ctx(segment=1, mode=DetectionMode.assisted)

    def test_penalty_applies_immediately_and_flags_the_gm(self):
        ctx = self._ctx()
        s, fx = step(RUN_SEG_1, self._evt(), ctx)
        assert s == RUN_SEG_1
        assert [e for e in fx if isinstance(e, ApplyTimePenalty)]
        assert ctx.pending_break == "b02"

    def test_the_clock_is_not_halted(self):
        ctx = self._ctx()
        assert StopStopwatch not in effect_types(RUN_SEG_1, self._evt(), ctx)

    def test_confirm_dismisses_the_notice_and_keeps_the_penalty(self):
        ctx = self._ctx()
        step(RUN_SEG_1, self._evt(), ctx)
        s, fx = step(RUN_SEG_1, GmConfirmBreak(), ctx)
        assert s == RUN_SEG_1, "confirming a penalty must not end the run"
        assert ctx.pending_break is None
        assert ctx.penalty_count == 1
        assert not [e for e in fx if isinstance(e, RevokeTimePenalty)]
        assert not [e for e in fx if isinstance(e, SaveRun)]

    def test_veto_gives_the_time_back(self):
        ctx = self._ctx()
        step(RUN_SEG_1, self._evt(), ctx)
        s, fx = step(RUN_SEG_1, GmVetoBreak(), ctx)
        assert s == RUN_SEG_1
        rev = [e for e in fx if isinstance(e, RevokeTimePenalty)]
        assert len(rev) == 1 and rev[0].ms == ctx.penalty_ms
        assert ctx.pending_break is None
        assert ctx.penalty_count == 0
        assert ctx.penalty_total_ms == 0

    def test_veto_does_not_restart_the_stopwatch(self):
        """It was never stopped; restarting it re-anchored and gave free time."""
        ctx = self._ctx()
        step(RUN_SEG_1, self._evt(), ctx)
        assert StartStopwatch not in effect_types(RUN_SEG_1, GmVetoBreak(), ctx)

    def test_veto_reopens_the_cooldown(self):
        """
        The GM has just said that break was not real. Holding the cooldown
        against it would suppress a genuine break right behind it.
        """
        ctx = self._ctx()
        step(RUN_SEG_1, self._evt(elapsed_ms=1_000), ctx)
        step(RUN_SEG_1, GmVetoBreak(), ctx)
        fx = effects(RUN_SEG_1, self._evt(elapsed_ms=1_100), ctx)
        assert [e for e in fx if isinstance(e, ApplyTimePenalty)]

    def test_confirm_without_a_pending_notice_is_ignored(self):
        ctx = _run_ctx(mode=DetectionMode.assisted)
        ctx.pending_break = None
        s, _ = step(RUN_SEG_1, GmConfirmBreak(), ctx)
        assert s == RUN_SEG_1

    def test_penalties_apply_in_every_segment(self):
        for state, seg in ((RUN_SEG_1, 1), (RUN_SEG_2, 2), (RUN_SEG_3, 3)):
            ctx = _run_ctx(segment=seg, mode=DetectionMode.assisted)
            s, fx = step(state, self._evt(), ctx)
            assert s == state
            assert [e for e in fx if isinstance(e, ApplyTimePenalty)]


class TestGmBustStillEndsARun:
    """
    Detection can no longer bust anyone, but the gamemaster still can.

    That is the point of keeping BUSTED: cheating, climbing, leaving and
    re-entering the maze are all things the cameras cannot judge and a human
    can.
    """

    def test_gm_bust_goes_to_busted(self):
        ctx = _run_ctx(segment=1, mode=DetectionMode.auto)
        s, _ = step(RUN_SEG_1, GmBust(), ctx)
        assert s == BUSTED

    def test_gm_bust_saves_the_run_as_busted(self):
        ctx = _run_ctx(segment=1, mode=DetectionMode.auto)
        fx = effects(RUN_SEG_1, GmBust(), ctx)
        save = [e for e in fx if isinstance(e, SaveRun)]
        assert len(save) == 1 and save[0].outcome == RunOutcome.busted

    def test_busted_to_result_on_timeout(self):
        assert new_state(BUSTED, ResultDisplayTimeout()) == RESULT


# ---------------------------------------------------------------------------
# Manual mode
# ---------------------------------------------------------------------------

class TestManualMode:
    def _evt(self) -> BreakConfirmed:
        return BreakConfirmed(beam_id="b05", ratio=0.1, run_id="run-5")

    def _ctx(self) -> FSMContext:
        return _run_ctx(mode=DetectionMode.manual)

    def test_break_in_manual_does_not_bust(self):
        ctx = self._ctx()
        s, fx = step(RUN_SEG_1, self._evt(), ctx)
        assert s == RUN_SEG_1
        assert not any(isinstance(e, SaveRun) for e in fx)

    def test_break_in_manual_seg2_stays(self):
        ctx = _run_ctx(segment=2, mode=DetectionMode.manual)
        s, _ = step(RUN_SEG_2, self._evt(), ctx)
        assert s == RUN_SEG_2

    def test_break_in_manual_seg3_stays(self):
        ctx = _run_ctx(segment=3, mode=DetectionMode.manual)
        s, _ = step(RUN_SEG_3, self._evt(), ctx)
        assert s == RUN_SEG_3

    def test_gm_bust_in_manual_causes_bust(self):
        ctx = self._ctx()
        s, fx = step(RUN_SEG_1, GmBust(), ctx)
        assert s == BUSTED

    def test_gm_bust_stops_stopwatch(self):
        ctx = self._ctx()
        assert StopStopwatch in effect_types(RUN_SEG_1, GmBust(), ctx)

    def test_gm_bust_saves_run_busted(self):
        ctx = self._ctx()
        fx = effects(RUN_SEG_1, GmBust(), ctx)
        save = [e for e in fx if isinstance(e, SaveRun)]
        assert len(save) == 1
        assert save[0].outcome == RunOutcome.busted


# ---------------------------------------------------------------------------
# False start during countdown
# ---------------------------------------------------------------------------

class TestPlateRelease:
    """PlateLow is intentionally ignored in ARM and COUNTDOWN.
    Once armed, only CANCEL or FORCE RESET can back out."""

    def test_plate_low_during_countdown_stays_in_countdown(self):
        assert new_state(COUNTDOWN, PlateLow()) == COUNTDOWN

    def test_plate_low_during_arm_stays_in_arm(self):
        assert new_state(ARM, PlateLow()) == ARM


# ---------------------------------------------------------------------------
# GM cancel during countdown
# ---------------------------------------------------------------------------

class TestGmCancel:
    def test_gm_cancel_during_countdown_goes_to_arm(self):
        assert new_state(COUNTDOWN, GmCancel()) == ARM

    def test_gm_cancel_applies_blackout(self):
        assert ApplyPreset("blackout") in effects(COUNTDOWN, GmCancel())


# ---------------------------------------------------------------------------
# Out-of-order checkpoints
# ---------------------------------------------------------------------------

class TestOutOfOrderCheckpoints:
    def test_cp2_during_seg1_is_ignored(self):
        ctx = _run_ctx(segment=1)
        s, fx = step(RUN_SEG_1, Cp2Pressed(), ctx)
        assert s == RUN_SEG_1
        # Should emit a metric but not change state
        assert not any(isinstance(e, ApplyPreset) for e in fx)

    def test_cp2_during_seg1_emits_metric(self):
        ctx = _run_ctx(segment=1)
        fx = effects(RUN_SEG_1, Cp2Pressed(), ctx)
        assert any(isinstance(e, EmitMetric) for e in fx)

    def test_cp1_after_passing_seg1_is_ignored_in_seg2(self):
        ctx = _run_ctx(segment=2)
        s, fx = step(RUN_SEG_2, Cp1Pressed(), ctx)
        assert s == RUN_SEG_2

    def test_cp1_after_passing_is_ignored_in_seg3(self):
        ctx = _run_ctx(segment=3)
        s, fx = step(RUN_SEG_3, Cp1Pressed(), ctx)
        assert s == RUN_SEG_3

    def test_cp2_after_passing_is_ignored_in_seg3(self):
        ctx = _run_ctx(segment=3)
        s, fx = step(RUN_SEG_3, Cp2Pressed(), ctx)
        assert s == RUN_SEG_3

    def test_stop_in_seg1_is_ignored(self):
        ctx = _run_ctx(segment=1)
        s, _ = step(RUN_SEG_1, StopPressed(), ctx)
        assert s == RUN_SEG_1

    def test_stop_in_seg2_is_ignored(self):
        ctx = _run_ctx(segment=2)
        s, _ = step(RUN_SEG_2, StopPressed(), ctx)
        assert s == RUN_SEG_2


# ---------------------------------------------------------------------------
# Checkpoint transitions set the right segment
# ---------------------------------------------------------------------------

class TestCheckpointTransitions:
    def test_cp1_transitions_to_seg2(self):
        ctx = _run_ctx(segment=1)
        s, fx = step(RUN_SEG_1, Cp1Pressed(), ctx)
        assert s == RUN_SEG_2
        assert ctx.segment == 2
        # defer: the shape must not snap under a player still stepping
        # over the checkpoint. The runner owns the wait; the FSM only flags it.
        assert ApplyPreset("maze_2", defer=True) in fx

    def test_cp2_transitions_to_seg3(self):
        ctx = _run_ctx(segment=2)
        s, fx = step(RUN_SEG_2, Cp2Pressed(), ctx)
        assert s == RUN_SEG_3
        assert ctx.segment == 3
        assert ApplyPreset("maze_3", defer=True) in fx


# ---------------------------------------------------------------------------
# Arm timeout
# ---------------------------------------------------------------------------

class TestArmTimeout:
    def test_arm_timeout_goes_to_reset(self):
        assert new_state(ARM, ArmTimeout()) == RESET

    def test_arm_timeout_effects_include_reset_stopwatch(self):
        assert ResetStopwatch in effect_types(ARM, ArmTimeout())


# ---------------------------------------------------------------------------
# Max run exceeded
# ---------------------------------------------------------------------------

class TestMaxRunExceeded:
    def test_max_run_seg1_to_aborted(self):
        ctx = _run_ctx(segment=1)
        assert new_state(RUN_SEG_1, MaxRunExceeded(), ctx) == ABORTED

    def test_max_run_seg2_to_aborted(self):
        ctx = _run_ctx(segment=2)
        assert new_state(RUN_SEG_2, MaxRunExceeded(), ctx) == ABORTED

    def test_max_run_seg3_to_aborted(self):
        ctx = _run_ctx(segment=3)
        assert new_state(RUN_SEG_3, MaxRunExceeded(), ctx) == ABORTED

    def test_max_run_saves_aborted(self):
        ctx = _run_ctx(segment=1)
        fx = effects(RUN_SEG_1, MaxRunExceeded(), ctx)
        save = [e for e in fx if isinstance(e, SaveRun)]
        assert len(save) == 1
        assert save[0].outcome == RunOutcome.aborted

    def test_aborted_to_result_on_timeout(self):
        assert new_state(ABORTED, ResultDisplayTimeout()) == RESULT


# ---------------------------------------------------------------------------
# VisionStalled — drops to manual
# ---------------------------------------------------------------------------

class TestVisionStalled:
    def test_vision_stalled_drops_to_manual(self):
        ctx = make_ctx(detection_mode=DetectionMode.auto)
        step(RUN_SEG_1, VisionStalled(), ctx)
        assert ctx.detection_mode == DetectionMode.manual

    def test_vision_stalled_emits_drop_detection_mode(self):
        ctx = make_ctx(detection_mode=DetectionMode.auto)
        fx = effects(RUN_SEG_1, VisionStalled(), ctx)
        drop = [e for e in fx if isinstance(e, DropDetectionMode)]
        assert len(drop) == 1
        assert drop[0].mode == DetectionMode.manual

    def test_vision_stalled_does_not_change_fsm_state(self):
        ctx = make_ctx(detection_mode=DetectionMode.auto)
        s, _ = step(RUN_SEG_1, VisionStalled(), ctx)
        assert s == RUN_SEG_1

    def test_vision_stalled_in_attract(self):
        ctx = make_ctx(detection_mode=DetectionMode.auto)
        s, _ = step(ATTRACT, VisionStalled(), ctx)
        assert s == ATTRACT
        assert ctx.detection_mode == DetectionMode.manual

    def test_detection_mode_changed_event_updates_context(self):
        ctx = make_ctx(detection_mode=DetectionMode.auto)
        step(ATTRACT, DetectionModeChanged(mode=DetectionMode.assisted), ctx)
        assert ctx.detection_mode == DetectionMode.assisted


# ---------------------------------------------------------------------------
# GmForceReset from various states
# ---------------------------------------------------------------------------

class TestGmForceReset:
    @pytest.mark.parametrize("state", [
        BOOT, SELF_TEST, ATTRACT, REGISTERED, ARM, COUNTDOWN,
        RUN_SEG_1, RUN_SEG_2, RUN_SEG_3,
        FINISHED, BUSTED, ABORTED, RESULT, FAULT, MASTER,
    ])
    def test_force_reset_from_any_state(self, state):
        ctx = make_ctx()
        s, _ = step(state, GmForceReset(), ctx)
        assert s == RESET

    def test_force_reset_from_fault(self):
        ctx = make_ctx()
        s, _ = step(FAULT, GmForceReset(), ctx)
        assert s == RESET


# ---------------------------------------------------------------------------
# ProcessRestart during a run → ABORTED; otherwise → RESET
# ---------------------------------------------------------------------------

class TestProcessRestart:
    @pytest.mark.parametrize("run_state", [RUN_SEG_1, RUN_SEG_2, RUN_SEG_3])
    def test_process_restart_during_run_goes_to_aborted(self, run_state):
        ctx = _run_ctx()
        s, _ = step(run_state, ProcessRestart(), ctx)
        assert s == ABORTED

    def test_process_restart_during_run_saves_aborted(self):
        ctx = _run_ctx()
        fx = effects(RUN_SEG_1, ProcessRestart(), ctx)
        save = [e for e in fx if isinstance(e, SaveRun)]
        assert len(save) == 1
        assert save[0].outcome == RunOutcome.aborted

    @pytest.mark.parametrize("state", [ATTRACT, REGISTERED, ARM, COUNTDOWN, FAULT])
    def test_process_restart_outside_run_goes_to_reset(self, state):
        ctx = make_ctx()
        s, _ = step(state, ProcessRestart(), ctx)
        assert s == RESET


# ---------------------------------------------------------------------------
# Baseline capture fail during count-in → ARM
# ---------------------------------------------------------------------------

class TestBaselineCaptureFail:
    def test_baseline_fail_in_countdown_goes_to_arm(self):
        s, _ = step(COUNTDOWN, BaselineCaptureFail(beam_id="b07"))
        assert s == ARM

    def test_baseline_fail_applies_blackout(self):
        assert ApplyPreset("blackout") in effects(COUNTDOWN, BaselineCaptureFail(beam_id="b07"))


# ---------------------------------------------------------------------------
# PreflightFail → FAULT
# ---------------------------------------------------------------------------

class TestPreflightFail:
    def test_preflight_fail_goes_to_fault(self):
        assert new_state(ARM, PreflightFail(beam_id="b01")) == FAULT

    def test_preflight_fail_broadcasts_state(self):
        assert BroadcastState in effect_types(ARM, PreflightFail(beam_id="b01"))


# ---------------------------------------------------------------------------
# Master mode
# ---------------------------------------------------------------------------

class TestMasterMode:
    @pytest.mark.parametrize("state", [ATTRACT, ARM, RUN_SEG_1, FAULT])
    def test_master_mode_engage_from_various_states(self, state):
        ctx = make_ctx()
        s, _ = step(state, MasterModeEngage(), ctx)
        assert s == MASTER

    def test_master_mode_exit_goes_to_self_test(self):
        assert new_state(MASTER, MasterModeExit()) == SELF_TEST

    def test_master_mode_exit_broadcasts(self):
        assert BroadcastState in effect_types(MASTER, MasterModeExit())


# ---------------------------------------------------------------------------
# GmVoid
# ---------------------------------------------------------------------------

class TestAttractShowIsStopped:
    def test_registering_stops_the_attract_show(self):
        """
        The attract show writes coils every 300-500 ms. Left running it fought
        the ready blink and the count-in ramp, and it masked the reconciler
        re-lighting the maze.
        """
        fx = effect_types(ATTRACT, PlayerRegistered(player_id="p1", nickname="Kim"))
        assert StopShow in fx


class TestGmVoid:
    def test_gm_void_does_not_change_state(self):
        ctx = _run_ctx()
        s, _ = step(RESULT, GmVoid(reason="system error"), ctx)
        assert s == RESULT

    def test_gm_void_emits_void_run_not_save_run(self):
        """
        VoidRun updates the existing row. SaveRun would INSERT on a primary key
        that already exists, raise IntegrityError, and lose the void silently.
        """
        ctx = _run_ctx()
        fx = effects(RESULT, GmVoid(reason="system error"), ctx)
        void = [e for e in fx if isinstance(e, VoidRun)]
        assert len(void) == 1
        assert void[0].run_id == ctx.run_id
        assert not [e for e in fx if isinstance(e, SaveRun)]

    def test_gm_void_passes_reason_through(self):
        """The GM's reason must reach runs.voided_reason."""
        ctx = _run_ctx()
        fx = effects(RESULT, GmVoid(reason="wrong player"), ctx)
        void = [e for e in fx if isinstance(e, VoidRun)]
        assert void[0].reason == "wrong player"


    def test_gm_void_from_run_state_does_not_change_state(self):
        ctx = _run_ctx()
        s, _ = step(RUN_SEG_1, GmVoid(reason="test"), ctx)
        assert s == RUN_SEG_1

    @pytest.mark.parametrize("state", [ATTRACT, REGISTERED, ARM, COUNTDOWN, RUN_SEG_1])
    def test_gm_void_is_inert_before_the_run_row_exists(self, state):
        """
        Voiding mid-run used to write the row early. The real end-of-run SaveRun
        then collided with it and was swallowed, so the DB said 'voided' while
        the displays said 'clean'.
        """
        ctx = _run_ctx()
        fx = effects(state, GmVoid(reason="too early"), ctx)
        assert not [e for e in fx if isinstance(e, VoidRun)]
        assert not [e for e in fx if isinstance(e, SaveRun)]


# ---------------------------------------------------------------------------
# GmAbort
# ---------------------------------------------------------------------------

class TestGmAbort:
    @pytest.mark.parametrize("run_state", [RUN_SEG_1, RUN_SEG_2, RUN_SEG_3])
    def test_gm_abort_during_run_goes_to_aborted(self, run_state):
        ctx = _run_ctx()
        s, _ = step(run_state, GmAbort(), ctx)
        assert s == ABORTED

    def test_gm_abort_saves_run_aborted(self):
        ctx = _run_ctx()
        fx = effects(RUN_SEG_1, GmAbort(), ctx)
        save = [e for e in fx if isinstance(e, SaveRun)]
        assert len(save) == 1
        assert save[0].outcome == RunOutcome.aborted

    def test_gm_abort_from_attract_is_ignored(self):
        """GmAbort from ATTRACT has no effect (no run to abort)."""
        ctx = make_ctx()
        s, _ = step(ATTRACT, GmAbort(), ctx)
        assert s == ATTRACT


# ---------------------------------------------------------------------------
# GmBust in various RUN states
# ---------------------------------------------------------------------------

class TestGmBust:
    @pytest.mark.parametrize("run_state", [RUN_SEG_1, RUN_SEG_2, RUN_SEG_3])
    def test_gm_bust_in_run_state(self, run_state):
        ctx = _run_ctx()
        s, _ = step(run_state, GmBust(), ctx)
        assert s == BUSTED

    def test_gm_bust_not_valid_in_attract(self):
        # GmBust in ATTRACT falls through to global handler which only applies
        # in RUN_STATES. Should return ATTRACT (ignored).
        ctx = make_ctx()
        s, _ = step(ATTRACT, GmBust(), ctx)
        assert s == ATTRACT


# ---------------------------------------------------------------------------
# PlayerRegistered sets context
# ---------------------------------------------------------------------------

class TestPlayerRegistered:
    def test_player_registered_sets_context(self):
        ctx = make_ctx()
        step(ATTRACT, PlayerRegistered(player_id="pid1", nickname="Bob"), ctx)
        assert ctx.player_id == "pid1"
        assert ctx.player_nickname == "Bob"

    def test_player_registered_goes_to_registered(self):
        assert new_state(ATTRACT, PlayerRegistered(player_id="p1", nickname="X")) == REGISTERED


# ---------------------------------------------------------------------------
# Preflight pass stays in ARM
# ---------------------------------------------------------------------------

class TestPreflightPass:
    def test_preflight_pass_stays_in_arm(self):
        assert new_state(ARM, PreflightPass()) == ARM

    def test_preflight_pass_broadcasts_state(self):
        assert BroadcastState in effect_types(ARM, PreflightPass())


# ---------------------------------------------------------------------------
# Idempotence and edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_unknown_event_in_any_state_does_not_raise(self):
        class UnknownEvent:
            type = "Unknown"

        for state in [BOOT, SELF_TEST, ATTRACT, REGISTERED, ARM, COUNTDOWN,
                      RUN_SEG_1, RUN_SEG_2, RUN_SEG_3,
                      FINISHED, BUSTED, ABORTED, RESULT, RESET, FAULT, MASTER]:
            ctx = make_ctx()
            s, fx = transition(state, UnknownEvent(), ctx)
            assert s == state
            assert fx == []

    def test_gm_confirm_break_in_auto_mode_is_ignored(self):
        ctx = _run_ctx(mode=DetectionMode.auto)
        ctx.pending_break = None
        s, _ = step(RUN_SEG_1, GmConfirmBreak(), ctx)
        assert s == RUN_SEG_1

    def test_gm_veto_break_in_auto_mode_without_pending_is_ignored(self):
        ctx = _run_ctx(mode=DetectionMode.auto)
        ctx.pending_break = None
        s, _ = step(RUN_SEG_1, GmVetoBreak(), ctx)
        assert s == RUN_SEG_1

    def test_context_run_id_preserved_across_transitions(self):
        ctx = _run_ctx()
        ctx.run_id = "run-xyz"
        step(RUN_SEG_1, Cp1Pressed(), ctx)
        assert ctx.run_id == "run-xyz"

    def test_detection_mode_changed_in_run_state(self):
        ctx = _run_ctx(mode=DetectionMode.auto)
        step(RUN_SEG_1, DetectionModeChanged(mode=DetectionMode.manual), ctx)
        assert ctx.detection_mode == DetectionMode.manual

    def test_reset_effects_include_apply_attract_preset(self):
        assert PlayShow("attract") in effects(RESULT, ResultDisplayTimeout())

    def test_reset_effects_include_reset_stopwatch(self):
        assert ResetStopwatch in effect_types(RESULT, ResultDisplayTimeout())

    def test_boot_to_self_test_broadcasts(self):
        assert BroadcastState in effect_types(BOOT, BootComplete())


# ---------------------------------------------------------------------------
# Full bust scenario tests (segment-by-segment completeness)
# ---------------------------------------------------------------------------

class TestBustScenarios:
    """Penalty side effects must be identical in all three segments and modes."""

    @pytest.mark.parametrize("seg_state,seg_num", [
        (RUN_SEG_1, 1), (RUN_SEG_2, 2), (RUN_SEG_3, 3)
    ])
    def test_auto_break_all_segments_penalises_without_saving_a_run(self, seg_state, seg_num):
        ctx = _run_ctx(segment=seg_num, mode=DetectionMode.auto)
        fx = effects(seg_state, BreakConfirmed(beam_id="bX", ratio=0.1, run_id="r1"), ctx)
        assert [e for e in fx if isinstance(e, ApplyTimePenalty)]
        assert not [e for e in fx if isinstance(e, SaveRun)], (
            "a detected break ended the run")

    @pytest.mark.parametrize("seg_state,seg_num", [
        (RUN_SEG_1, 1), (RUN_SEG_2, 2), (RUN_SEG_3, 3)
    ])
    def test_assisted_confirm_all_segments_keeps_the_run_alive(self, seg_state, seg_num):
        ctx = _run_ctx(segment=seg_num, mode=DetectionMode.assisted)
        step(seg_state, BreakConfirmed(beam_id="bY", ratio=0.2, run_id="r2"), ctx)
        s, fx = step(seg_state, GmConfirmBreak(), ctx)
        assert s == seg_state
        assert not [e for e in fx if isinstance(e, SaveRun)]
        assert ctx.penalty_count == 1

    @pytest.mark.parametrize("seg_state,seg_num", [
        (RUN_SEG_1, 1), (RUN_SEG_2, 2), (RUN_SEG_3, 3)
    ])
    def test_assisted_veto_all_segments_stays_in_run(self, seg_state, seg_num):
        ctx = _run_ctx(segment=seg_num, mode=DetectionMode.assisted)
        step(seg_state, BreakConfirmed(beam_id="bZ", ratio=0.3, run_id="r3"), ctx)
        s, _ = step(seg_state, GmVetoBreak(), ctx)
        assert s == seg_state


# ---------------------------------------------------------------------------
# Transition count performance check
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_10000_transitions_complete_quickly(self):
        """
        FSM must handle 10 000 transitions with no mocks. This test verifies the
        pure-function constraint from CLAUDE.md invariant 1.
        """
        import time as _time
        ctx = make_ctx()
        start = _time.monotonic()
        for _ in range(2500):
            transition(RUN_SEG_1, Cp1Pressed(), ctx)
            ctx.segment = 1  # reset so the transition is valid each time
            transition(RUN_SEG_1, Cp2Pressed(), ctx)
            transition(RUN_SEG_2, Cp1Pressed(), ctx)
            ctx.segment = 2
            transition(RUN_SEG_2, Cp2Pressed(), ctx)
            ctx.segment = 1
        elapsed = _time.monotonic() - start
        assert elapsed < 2.0, f"10 000 transitions took {elapsed:.3f} s — too slow"
