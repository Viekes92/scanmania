"""
core/fsm.py — pure finite state machine for the ScanMania game.

Input:  (state: str, event: Event, context: FSMContext)
Output: (new_state: str, side_effects: list[SideEffect])
Invariant: NO I/O, NO await, NO clock reads, NO imports from io/, vision/,
           inputs/, persist/, or web/. Pure function; testable at 10 000
           transitions/second with no mocks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.events import (
    # States
    BOOT, SELF_TEST, ATTRACT, REGISTERED, ARM, COUNTDOWN,
    RUN_SEG_1, RUN_SEG_2, RUN_SEG_3,
    FINISHED, BUSTED, ABORTED, RESULT, RESET, FAULT, MASTER,
    RUN_STATES, POST_RUN_STATES,
    # Run outcomes & detection modes
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
)

import core.metrics as metrics

log = logging.getLogger(__name__)

# States where the run row already exists, so GmVoid can update it.
_VOIDABLE_STATES: frozenset[str] = POST_RUN_STATES | {RESULT}

# ---------------------------------------------------------------------------
# FSMContext
# Mutable game-session state threaded through every transition.
# The FSM reads it; runner.py owns and mutates the instance between calls.
# ---------------------------------------------------------------------------

@dataclass
class FSMContext:
    """
    Mutable context passed to every FSM transition call.

    The FSM may read any field and return mutations as instructions embedded
    in side effects; runner.py applies those mutations after executing side
    effects so the context is always consistent with the emitted side effects.

    Fields:
        detection_mode:        current detection mode ("auto" | "assisted" | "manual")
        run_id:                UUIDv7 for the current run, or None between runs
        player_id:             UUID of the registered player, or None
        player_nickname:       display name of the registered player, or None
        segment:               which segment the player is in (1, 2, or 3)
        beams_masked:          set of beam_ids currently masked
        pending_break:         beam_id awaiting GM confirm in assisted mode, or None
        assisted_halt_elapsed_ms:
                               value of elapsed_ms at the moment the stopwatch was
                               halted for a GM adjudication, or None when not halted
        boot_to_master:        True until the first self-test passes, when set.
                               Sends the box to MASTER instead of ATTRACT on
                               boot, so nothing is playable until a human has
                               walked the container and pressed FORCE RESET.
                               Consumed once, so leaving MASTER later behaves
                               normally rather than looping back into it.
    """
    detection_mode: str = DetectionMode.auto
    run_id: str | None = None
    player_id: str | None = None
    player_nickname: str | None = None
    segment: int = 1
    beams_masked: set[str] = field(default_factory=set)
    pending_break: str | None = None
    assisted_halt_elapsed_ms: int | None = None
    boot_to_master: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _no_op(state: str, event: Any, ctx: FSMContext) -> tuple[str, list]:
    """Return current state with no side effects (ignored event)."""
    log.debug("FSM ignored event %s in state %s", type(event).__name__, state)
    return state, []


def _log_ignored(state: str, event: Any, extra_metric_tags: dict | None = None) -> tuple[str, list]:
    """Return state unchanged with an EmitMetric for the ignored event."""
    tags = {"state": state, "event": type(event).__name__}
    if extra_metric_tags:
        tags.update(extra_metric_tags)
    return state, [EmitMetric(name=metrics.STATE_TRANSITION, value=0.0, tags=tags)]


def _bust_effects(ctx: FSMContext) -> list:
    """Side effects shared by all 'go to BUSTED' transitions."""
    effects: list = [
        StopStopwatch(),
        PlayShow("bust"),
        DisarmDetection(),
    ]
    if ctx.run_id:
        effects += [
            SaveRun(outcome=RunOutcome.busted, run_id=ctx.run_id),
        ]
    effects.append(BroadcastState())
    return effects


def _abort_effects(ctx: FSMContext) -> list:
    """Side effects for GmAbort or MaxRunExceeded transitions."""
    effects: list = [
        StopStopwatch(),
        DisarmDetection(),
    ]
    if ctx.run_id:
        effects += [
            SaveRun(outcome=RunOutcome.aborted, run_id=ctx.run_id),
        ]
    effects.append(BroadcastState())
    return effects


def _reset_effects() -> list:
    """Side effects for entering RESET."""
    return [
        PlayShow("attract"),
        ResetStopwatch(),
        BroadcastState(),
    ]


# ---------------------------------------------------------------------------
# Per-state handlers
# Each returns (new_state, side_effects).  They are collected into a dispatch
# table below; the main transition() function indexes into it.
# ---------------------------------------------------------------------------

# --- BOOT ---

def _boot_handlers() -> dict:
    def on_boot_complete(state, event, ctx):
        return SELF_TEST, [BroadcastState()]
    return {BootComplete: on_boot_complete}


# --- SELF_TEST ---

def _self_test_handlers() -> dict:
    def on_pass(state, event, ctx):
        # Boot lands in MASTER, not ATTRACT: the lasers stay dark, the house
        # lights stay up, and the GM has to walk the container and press
        # FORCE RESET before anything is playable. A box that came up already
        # running its attract show invites someone to start a game before
        # anyone has looked inside it.
        #
        # Consumed here, so exiting MASTER later (which re-runs the self test)
        # goes to ATTRACT rather than looping straight back into MASTER.
        if ctx.boot_to_master:
            ctx.boot_to_master = False
            return MASTER, [StopStopwatch(), DisarmDetection(), BroadcastState()]
        return ATTRACT, [PlayShow("attract"), BroadcastState()]

    def on_fail(state, event: SelfTestFail, ctx):
        return FAULT, [BroadcastState()]

    return {
        SelfTestPass: on_pass,
        SelfTestFail: on_fail,
    }


# --- ATTRACT ---

def _attract_handlers() -> dict:
    def on_registered(state, event: PlayerRegistered, ctx):
        ctx.player_id = event.player_id
        ctx.player_nickname = event.nickname
        ctx.run_id = None
        ctx.segment = 1
        ctx.pending_break = None
        ctx.assisted_halt_elapsed_ms = None
        # Stop the attract show. It would otherwise keep writing coils through
        # REGISTERED, ARM and the whole count-in ramp.
        return REGISTERED, [StopShow(), BroadcastState()]

    return {PlayerRegistered: on_registered}


# --- REGISTERED ---

def _registered_handlers() -> dict:
    def on_plate_high(state, event, ctx):
        return ARM, [ReadyBlink(), BeamPreflightCheck(), BroadcastState()]

    def on_cancel(state, event, ctx):
        # Cancel registration — go back to ATTRACT
        ctx.player_id = None
        ctx.player_nickname = None
        return ATTRACT, [PlayShow("attract"), BroadcastState()]

    return {PlateHigh: on_plate_high, GmCancel: on_cancel}


# --- ARM ---

def _arm_handlers() -> dict:
    def on_preflight_pass(state, event, ctx):
        # Stay in ARM; just broadcast so GM console enables COUNT IN button.
        return ARM, [BroadcastState()]

    def on_preflight_fail(state, event: PreflightFail, ctx):
        return FAULT, [BroadcastState()]

    def on_count_in(state, event, ctx):
        return COUNTDOWN, [StartCountIn(), BroadcastState()]

    def on_arm_timeout(state, event, ctx):
        return RESET, _reset_effects()

    return {
        PreflightPass: on_preflight_pass,
        PreflightFail: on_preflight_fail,
        CountInRequested: on_count_in,
        ArmTimeout: on_arm_timeout,
        # PlateLow intentionally NOT handled — once armed, stay armed.
        # Only CANCEL or FORCE RESET can back out.
    }


# --- COUNTDOWN ---

def _countdown_handlers() -> dict:
    def on_gm_cancel(state, event, ctx):
        # GM cancelled; immediate blackout.
        return ARM, [ApplyPreset("blackout"), BroadcastState()]

    def on_baseline_fail(state, event: BaselineCaptureFail, ctx):
        # Pulse-0 capture failed for a beam — abort the count-in.
        return ARM, [ApplyPreset("blackout"), BroadcastState()]

    def on_ramp_complete(state, event, ctx):
        return RUN_SEG_1, [
            ApplyPreset("maze_1"),
            StartStopwatch(),
            ArmDetection(),
            BroadcastState(),
        ]

    return {
        # PlateLow intentionally NOT handled — countdown continues
        # regardless of plate state. Only GM CANCEL can abort.
        GmCancel: on_gm_cancel,
        BaselineCaptureFail: on_baseline_fail,
        RampComplete: on_ramp_complete,
    }


# --- RUN helpers (shared across RUN_SEG_1/2/3) ---

def _break_effects_for_mode(ctx: FSMContext, event: BreakConfirmed, current_run_state: str) -> tuple[str, list]:
    """
    Determine the new state and side effects for a BreakConfirmed event
    based on the current detection mode.

    auto:     → BUSTED immediately
    assisted: → stay in current RUN state, halt stopwatch, set pending_break,
                save evidence, broadcast (GM gets CONFIRM/VETO prompt)
    manual:   → stay in current RUN state (advisory only), broadcast
    """
    mode = ctx.detection_mode

    if mode == DetectionMode.auto:
        effects = [
            StopStopwatch(),
            PlayShow("bust"),
            DisarmDetection(),
            SaveBreakEvidence(beam_id=event.beam_id, run_id=event.run_id),
        ]
        if ctx.run_id:
            effects += [
                SaveRun(outcome=RunOutcome.busted, run_id=ctx.run_id, busting_beam_id=event.beam_id),
            ]
        effects.append(BroadcastState())
        return BUSTED, effects

    elif mode == DetectionMode.assisted:
        # Halt the stopwatch; GM sees CONFIRM/VETO prompt.
        ctx.pending_break = event.beam_id
        effects = [
            StopStopwatch(),
            SaveBreakEvidence(beam_id=event.beam_id, run_id=event.run_id),
            BroadcastState(),
        ]
        return current_run_state, effects

    else:  # manual
        # Advisory only — indicator shown on GM console, no state change.
        return current_run_state, [BroadcastState()]


# --- RUN_SEG_1 ---

def _run_seg1_handlers() -> dict:
    def on_cp1(state, event, ctx):
        ctx.segment = 2
        return RUN_SEG_2, [ApplyPreset("maze_2"), BroadcastState()]

    def on_cp2(state, event, ctx):
        # Out of order: cp2 during seg1 is ignored.
        return _log_ignored(state, event, {"reason": "out_of_order_cp2_during_seg1"})

    def on_break(state, event: BreakConfirmed, ctx):
        return _break_effects_for_mode(ctx, event, RUN_SEG_1)

    def on_max_run(state, event, ctx):
        return ABORTED, _abort_effects(ctx)

    def on_stop(state, event, ctx):
        # Stop pressed in wrong segment — ignore.
        return _log_ignored(state, event, {"reason": "stop_in_wrong_segment"})

    return {
        Cp1Pressed: on_cp1,
        Cp2Pressed: on_cp2,
        StopPressed: on_stop,
        BreakConfirmed: on_break,
        MaxRunExceeded: on_max_run,
    }


# --- RUN_SEG_2 ---

def _run_seg2_handlers() -> dict:
    def on_cp1(state, event, ctx):
        # Repeat cp1 after already passing — ignored.
        return _log_ignored(state, event, {"reason": "repeat_cp1_in_seg2"})

    def on_cp2(state, event, ctx):
        ctx.segment = 3
        return RUN_SEG_3, [ApplyPreset("maze_3"), BroadcastState()]

    def on_break(state, event: BreakConfirmed, ctx):
        return _break_effects_for_mode(ctx, event, RUN_SEG_2)

    def on_max_run(state, event, ctx):
        return ABORTED, _abort_effects(ctx)

    def on_stop(state, event, ctx):
        return _log_ignored(state, event, {"reason": "stop_in_wrong_segment"})

    return {
        Cp1Pressed: on_cp1,
        Cp2Pressed: on_cp2,
        StopPressed: on_stop,
        BreakConfirmed: on_break,
        MaxRunExceeded: on_max_run,
    }


# --- RUN_SEG_3 ---

def _run_seg3_handlers() -> dict:
    def on_cp1(state, event, ctx):
        return _log_ignored(state, event, {"reason": "repeat_cp1_in_seg3"})

    def on_cp2(state, event, ctx):
        return _log_ignored(state, event, {"reason": "repeat_cp2_in_seg3"})

    def on_stop(state, event, ctx):
        # A break is already confirmed and waiting on the GM. The stopwatch was
        # halted when it fired, and stop() is idempotent, so calling this clean
        # recorded the HALT time — a beam-breaking run topping the leaderboard
        # with a time shorter than reality. The break wins.
        if ctx.pending_break:
            busting = ctx.pending_break
            ctx.pending_break = None
            ctx.assisted_halt_elapsed_ms = None
            effects: list = [StopStopwatch(), DisarmDetection(), PlayShow("bust")]
            if ctx.run_id:
                effects.append(SaveRun(outcome=RunOutcome.busted,
                                       run_id=ctx.run_id,
                                       busting_beam_id=busting))
            effects.append(BroadcastState())
            return BUSTED, effects

        effects = [StopStopwatch(), DisarmDetection(), PlayShow("clean")]
        if ctx.run_id:
            effects += [
                SaveRun(outcome=RunOutcome.clean, run_id=ctx.run_id),
            ]
        effects.append(BroadcastState())
        return FINISHED, effects

    def on_break(state, event: BreakConfirmed, ctx):
        return _break_effects_for_mode(ctx, event, RUN_SEG_3)

    def on_max_run(state, event, ctx):
        return ABORTED, _abort_effects(ctx)

    return {
        Cp1Pressed: on_cp1,
        Cp2Pressed: on_cp2,
        StopPressed: on_stop,
        BreakConfirmed: on_break,
        MaxRunExceeded: on_max_run,
    }


# --- FINISHED / BUSTED / ABORTED (post-run display states) ---

def _post_run_handlers() -> dict:
    def on_result_timeout(state, event, ctx):
        return RESULT, [BroadcastState()]

    return {ResultDisplayTimeout: on_result_timeout}


# --- RESULT ---

def _result_handlers() -> dict:
    def on_result_timeout(state, event, ctx):
        return RESET, _reset_effects()

    return {ResultDisplayTimeout: on_result_timeout}


# --- RESET ---

def _reset_handlers() -> dict:
    # RESET is effectively transient. Any event (or the runner's immediate
    # dispatch) pushes it to ATTRACT. We handle this by making the runner
    # immediately follow a RESET entry with an ATTRACT transition. The FSM
    # itself treats RESET as a stable state that transitions to ATTRACT on any
    # "tick" — represented here as no specific events being consumed, and the
    # runner performing the ATTRACT transition directly after executing
    # _reset_effects(). For belt-and-suspenders, a GmForceReset or a timeout
    # that arrives here is handled by global handlers below.
    return {}


# --- FAULT ---

def _fault_handlers() -> dict:
    # Only GmForceReset escapes FAULT (handled in global handlers below).
    return {}


# --- MASTER ---

def _master_handlers() -> dict:
    def on_exit(state, event, ctx):
        # Exit master mode → re-verify.
        return SELF_TEST, [BroadcastState()]

    return {MasterModeExit: on_exit}


# ---------------------------------------------------------------------------
# Global / cross-cutting event handlers
# Applied AFTER per-state handlers; per-state handlers take priority.
# ---------------------------------------------------------------------------

def _handle_global(state: str, event: Any, ctx: FSMContext) -> tuple[str, list] | None:
    """
    Handle events that are valid in many or all states.
    Returns (new_state, effects) if the event was handled, else None.
    """
    etype = type(event)

    # GmAbort — valid in RUN states, COUNTDOWN, REGISTERED, ARM only.
    if etype is GmAbort and state in (RUN_SEG_1, RUN_SEG_2, RUN_SEG_3, COUNTDOWN, REGISTERED, ARM):
        effects = [ApplyPreset("blackout"), StopStopwatch(), DisarmDetection()]
        if ctx.run_id and state in RUN_STATES:
            effects += [
                SaveRun(outcome=RunOutcome.aborted, run_id=ctx.run_id),
            ]
        effects.append(BroadcastState())
        return ABORTED, effects

    # GmForceReset — the ultimate escape hatch, works from any state including FAULT.
    if etype is GmForceReset:
        return RESET, _reset_effects()

    # GmVoid — mark run voided; stay in current state.
    # Only after the run row exists. Voiding mid-run wrote a row first, and the
    # real end-of-run SaveRun then collided with it and was swallowed.
    if etype is GmVoid:
        effects: list = []
        if ctx.run_id and state in _VOIDABLE_STATES:
            effects += [
                VoidRun(run_id=ctx.run_id, reason=event.reason),
            ]
        effects.append(BroadcastState())
        return state, effects

    # GmBust — manual bust from any RUN state.
    if etype is GmBust and state in RUN_STATES:
        effects = _bust_effects(ctx)
        return BUSTED, effects

    # GmConfirmBreak — only in assisted mode with a pending break, in any RUN state.
    if etype is GmConfirmBreak and ctx.pending_break and state in RUN_STATES:
        busting_beam = ctx.pending_break
        ctx.pending_break = None
        ctx.assisted_halt_elapsed_ms = None
        effects = [StopStopwatch(), DisarmDetection(), PlayShow("bust")]
        if ctx.run_id:
            effects += [
                SaveRun(outcome=RunOutcome.busted, run_id=ctx.run_id, busting_beam_id=busting_beam),
            ]
        effects.append(BroadcastState())
        return BUSTED, effects

    # GmVetoBreak — only in assisted mode with a pending break, in any RUN state.
    if etype is GmVetoBreak and ctx.pending_break and state in RUN_STATES:
        ctx.pending_break = None
        ctx.assisted_halt_elapsed_ms = None
        return state, [StartStopwatch(), BroadcastState()]

    # MasterModeEngage — from any state. Stop everything, hand control to the admin.
    if etype is MasterModeEngage:
        effects: list = [StopStopwatch(), DisarmDetection()]
        # The runner clears run_id on entering MASTER, so without this the run
        # simply vanishes — no clean, no busted, no aborted, no DB row at all.
        # One mis-tap should not erase a player's run.
        if ctx.run_id and state in RUN_STATES:
            effects.append(SaveRun(outcome=RunOutcome.aborted, run_id=ctx.run_id))
        effects.append(BroadcastState())
        return MASTER, effects

    # ProcessRestart — never resume a run.
    if etype is ProcessRestart:
        if state in RUN_STATES:
            effects = [StopStopwatch(), DisarmDetection()]
            if ctx.run_id:
                effects += [
                    SaveRun(outcome=RunOutcome.aborted, run_id=ctx.run_id),
                ]
            effects.append(BroadcastState())
            return ABORTED, effects
        else:
            return RESET, _reset_effects()

    # VisionStalled — drop to manual; never bust.
    if etype is VisionStalled:
        ctx.detection_mode = DetectionMode.manual
        return state, [DropDetectionMode(mode=DetectionMode.manual, reason="vision stalled"), BroadcastState()]

    # DetectionModeChanged — update context.
    if etype is DetectionModeChanged:
        ctx.detection_mode = event.mode
        return state, [BroadcastState()]

    return None


# ---------------------------------------------------------------------------
# Dispatch table
# Maps state → {EventType: handler_fn}
# ---------------------------------------------------------------------------

_DISPATCH: dict[str, dict] = {
    BOOT:       _boot_handlers(),
    SELF_TEST:  _self_test_handlers(),
    ATTRACT:    _attract_handlers(),
    REGISTERED: _registered_handlers(),
    ARM:        _arm_handlers(),
    COUNTDOWN:  _countdown_handlers(),
    RUN_SEG_1:  _run_seg1_handlers(),
    RUN_SEG_2:  _run_seg2_handlers(),
    RUN_SEG_3:  _run_seg3_handlers(),
    FINISHED:   _post_run_handlers(),
    BUSTED:     _post_run_handlers(),
    ABORTED:    _post_run_handlers(),
    RESULT:     _result_handlers(),
    RESET:      _reset_handlers(),
    FAULT:      _fault_handlers(),
    MASTER:     _master_handlers(),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def transition(
    state: str,
    event: Any,
    context: FSMContext,
) -> tuple[str, list]:
    """
    Pure FSM transition function.

    Parameters
    ----------
    state:   current FSM state (one of the STATE constants)
    event:   an event dataclass instance (from core/events.py)
    context: mutable FSMContext (the FSM may mutate it in-place)

    Returns
    -------
    (new_state, side_effects)
    new_state is the resulting FSM state (may equal state for no-op transitions).
    side_effects is an ordered list of SideEffect instances to be executed by runner.py.

    Invariants
    ----------
    - Never raises. Unknown states/events → return (state, []).
    - Never performs I/O.
    - Deterministic for a given (state, event, context) triple.
    """
    etype = type(event)

    # 1. Try per-state handler first.
    state_handlers = _DISPATCH.get(state, {})
    handler = state_handlers.get(etype)
    if handler is not None:
        new_state, effects = handler(state, event, context)
        return new_state, effects + _transition_metric(state, new_state, etype.__name__)

    # 2. Try global handler.
    result = _handle_global(state, event, context)
    if result is not None:
        new_state, effects = result
        return new_state, effects + _transition_metric(state, new_state, etype.__name__)

    # 3. Truly ignored — log and return unchanged.
    log.debug("FSM: no handler for event %s in state %s", etype.__name__, state)
    return state, []


def _transition_metric(old_state: str, new_state: str, event_name: str) -> list:
    """
    Return the state.transition metric as a side effect, or nothing.

    Invariant 1: this module performs no I/O. The earlier version called
    metrics.emit() directly from inside pure_transition(). That was harmless
    only because no sink is configured — the moment anyone wires one up, the
    pure FSM would start writing to it on every transition and test_fsm.py
    would need mocks. Emitting as data keeps it pure either way.
    """
    if old_state == new_state:
        return []
    return [
        EmitMetric(
            name=metrics.STATE_TRANSITION,
            value=1.0,
            tags={"from": old_state, "to": new_state, "event": event_name},
        )
    ]


def make_context(**kwargs: Any) -> FSMContext:
    """Convenience factory: create an FSMContext with optional overrides."""
    ctx = FSMContext()
    for k, v in kwargs.items():
        setattr(ctx, k, v)
    return ctx
