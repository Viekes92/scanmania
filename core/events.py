"""
core/events.py — wire contract for the entire ScanMania system.

Every event type, side effect, state name, and outcome constant lives here.
No other module invents event types or side effect names. This is the map of the
whole system: who emits each event, which FSM states they are valid in, and what
side effects the FSM can return. Changes here ripple everywhere — update carefully.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# FSM States
# State names are plain string constants so they work as dict keys without
# import gymnastics, and so test assertions read as prose.
# ---------------------------------------------------------------------------

BOOT = "BOOT"
SELF_TEST = "SELF_TEST"
ATTRACT = "ATTRACT"
REGISTERED = "REGISTERED"
ARM = "ARM"
COUNTDOWN = "COUNTDOWN"
RUN_SEG_1 = "RUN_SEG_1"
RUN_SEG_2 = "RUN_SEG_2"
RUN_SEG_3 = "RUN_SEG_3"
FINISHED = "FINISHED"
BUSTED = "BUSTED"
ABORTED = "ABORTED"
RESULT = "RESULT"
RESET = "RESET"
FAULT = "FAULT"
MASTER = "MASTER"

# Convenience sets used by the FSM dispatch table.
RUN_STATES: frozenset[str] = frozenset({RUN_SEG_1, RUN_SEG_2, RUN_SEG_3})
POST_RUN_STATES: frozenset[str] = frozenset({FINISHED, BUSTED, ABORTED})
ALL_STATES: frozenset[str] = frozenset({
    BOOT, SELF_TEST, ATTRACT, REGISTERED, ARM, COUNTDOWN,
    RUN_SEG_1, RUN_SEG_2, RUN_SEG_3,
    FINISHED, BUSTED, ABORTED, RESULT, RESET, FAULT, MASTER,
})


# ---------------------------------------------------------------------------
# Run outcome constants
# Stored in the `outcome` column of the `runs` table.
# ---------------------------------------------------------------------------

class RunOutcome:
    clean   = "clean"    # player pressed stop before any beam break
    # GM-only from 2026-09. A detected beam break no longer ends a run — it adds
    # a time penalty (see ApplyTimePenalty). This outcome is now reached solely
    # by the gamemaster pressing BUST, for the things detection cannot see:
    # cheating, climbing, leaving and re-entering the maze.
    busted  = "busted"   # GM ended the run deliberately
    aborted = "aborted"  # max_run_ms exceeded, or GM abort, or process restart
    voided  = "voided"   # GM voided — recorded but excluded from leaderboard


# ---------------------------------------------------------------------------
# Detection mode constants
# Matches game.yaml detection_mode field. Switchable live from GM console.
# ---------------------------------------------------------------------------

class DetectionMode:
    auto     = "auto"      # break → immediate bust
    assisted = "assisted"  # break → halt stopwatch, GM confirms/vetoes
    manual   = "manual"    # break is advisory only; GM presses BUST to end run


# ---------------------------------------------------------------------------
# Input Events
# Emitted by: inputs/ (Pico buttons), vision/ (beam detection), web/routes_gm.py
# (gamemaster actions), runner.py (internal timers), and boot/self-test logic.
# The FSM consumes these via transition(state, event, context).
# Every event is frozen — nothing mutates an event after creation.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BootComplete:
    """
    Emitted by: runner.py immediately after process startup.
    Consumed by: FSM in BOOT state.
    Drives BOOT → SELF_TEST.
    """
    type: str = field(default="BootComplete", init=False)


@dataclass(frozen=True)
class SelfTestPass:
    """
    Emitted by: runner.py after all hardware checks pass (relay boards reachable,
    Pico heartbeat received, cameras streaming).
    Consumed by: FSM in SELF_TEST state.
    Drives SELF_TEST → ATTRACT.
    """
    type: str = field(default="SelfTestPass", init=False)


@dataclass(frozen=True)
class SelfTestFail:
    """
    Emitted by: runner.py when any hardware check fails at startup.
    Consumed by: FSM in SELF_TEST state.
    Drives SELF_TEST → FAULT with the failing component named.
    Fields:
        reason: human-readable description of what failed (e.g. "board_1 unreachable")
    """
    reason: str
    type: str = field(default="SelfTestFail", init=False)


@dataclass(frozen=True)
class PlayerRegistered:
    """
    Emitted by: web/routes_signin.py when a player submits the sign-in form.
    Consumed by: FSM in ATTRACT state.
    Drives ATTRACT → REGISTERED.
    Fields:
        player_id: UUID from the players table (generated at sign-in)
        nickname:  display name shown on both screens
    """
    player_id: str
    nickname: str
    type: str = field(default="PlayerRegistered", init=False)


@dataclass(frozen=True)
class PlateHigh:
    """
    Emitted by: inputs/pico_link.py when the start plate contact closes.
    Consumed by: FSM in REGISTERED and ARM states.
    In REGISTERED: drives REGISTERED → ARM (triggers ReadyBlink + BeamPreflightCheck).
    In ARM: ignored (player was already on the plate).
    """
    type: str = field(default="PlateHigh", init=False)


@dataclass(frozen=True)
class PlateLow:
    """
    Emitted by: inputs/pico_link.py when the start plate contact opens.
    Consumed by: FSM in ARM and COUNTDOWN states.
    NOT consumed in ARM: the FSM deliberately ignores it there (a player shifting their weight on the plate must not cancel the count-in).
    NOT consumed in COUNTDOWN either — false starts are not handled; see tests/test_fsm.py, which asserts the ignore.
    """
    type: str = field(default="PlateLow", init=False)


@dataclass(frozen=True)
class PreflightPass:
    """
    Emitted by: vision/ after the silent 150 ms ARM blink confirms all beams are live.
    Consumed by: FSM in ARM state.
    No state change — stays in ARM, but GM console can now enable COUNT IN button.
    Side effect: BroadcastState so GM console reflects "preflight passed" status.
    """
    type: str = field(default="PreflightPass", init=False)


@dataclass(frozen=True)
class PreflightFail:
    """
    Emitted by: core/runner.py's preflight check, before the count-in.
    Consumed by: FSM in ARM state.
    Drives ARM → FAULT with the reason named.
    Fields:
        beam_id: legacy field — the dot or beam that failed, when one is known.
        reason:  why preflight failed, shown to the operator. Preflight now
                 checks board health, camera stalls and whether the lit maze has
                 any calibrated dots, so the cause is usually not a single beam.
    """
    beam_id: str = ""
    reason: str = ""
    type: str = field(default="PreflightFail", init=False)


@dataclass(frozen=True)
class CountInRequested:
    """
    Emitted by: web/routes_gm.py when the gamemaster taps COUNT IN.
    Consumed by: FSM in ARM state (only valid after PreflightPass).
    Drives ARM → COUNTDOWN. Side effect: StartCountIn (runner schedules the ramp).
    """
    type: str = field(default="CountInRequested", init=False)


@dataclass(frozen=True)
class RampComplete:
    """
    Emitted by: runner.py after the last count-in pulse completes and the maze goes solid.
    Consumed by: FSM in COUNTDOWN state.
    Drives COUNTDOWN → RUN_SEG_1. Side effects: ApplyPreset("maze_1"),
    StartStopwatch, ArmDetection.
    """
    type: str = field(default="RampComplete", init=False)


@dataclass(frozen=True)
class BaselineCaptured:
    """
    Emitted by: vision/ during pulse 0 of the count-in ramp for each beam.
    Consumed by: runner.py / vision pipeline (not the FSM directly).
    Informational — the FSM only cares about BaselineCaptureFail.
    Fields:
        beam_id: beam that captured its baseline
        value:   the captured mean brightness value
    """
    beam_id: str
    value: float
    type: str = field(default="BaselineCaptured", init=False)


@dataclass(frozen=True)
class BaselineCaptureFail:
    """
    Emitted by: vision/ when pulse 0 fails to produce a valid reading for a beam.
    Consumed by: FSM in COUNTDOWN state.
    Drives COUNTDOWN → ARM with blackout. Named beam appears on GM console.
    Fields:
        beam_id: the beam whose baseline could not be captured
    """
    beam_id: str
    type: str = field(default="BaselineCaptureFail", init=False)


@dataclass(frozen=True)
class BreakConfirmed:
    """
    Emitted by: vision/detect.py after N consecutive frames below break_ratio
    (hysteresis satisfied). Only emitted when detection is armed.
    Consumed by: FSM in RUN_SEG_1, RUN_SEG_2, RUN_SEG_3.
    Behaviour depends on detection_mode in FSMContext:
        auto:     → BUSTED immediately
        assisted: → stopwatch halted, GM prompt raised (pending_break set)
        manual:   → advisory indicator only, no state change
    Fields:
        beam_id: which beam triggered
        ratio:   brightness ratio at detection time (for evidence/logging)
        run_id:  current run UUID (for SaveBreakEvidence association)
        run_elapsed_ms:
                 the run clock at the moment this break was handed to the FSM,
                 stamped by runner.py from the stopwatch. The FSM needs it to
                 enforce the penalty cooldown, and invariant 1 forbids the FSM
                 reading a clock — so the clock reading is passed IN as data.
                 0 when there is no run in progress.
    """
    beam_id: str
    ratio: float
    run_id: str
    run_elapsed_ms: int = 0
    type: str = field(default="BreakConfirmed", init=False)


@dataclass(frozen=True)
class Cp1Pressed:
    """
    Emitted by: inputs/pico_link.py when checkpoint 1 plate is pressed.
    Consumed by: FSM in RUN_SEG_1 only. Ignored with a logged metric in all other states.
    Drives RUN_SEG_1 → RUN_SEG_2. Side effects: ApplyPreset("maze_2"), segment=2.
    """
    type: str = field(default="Cp1Pressed", init=False)


@dataclass(frozen=True)
class Cp2Pressed:
    """
    Emitted by: inputs/pico_link.py when checkpoint 2 plate is pressed.
    Consumed by: FSM in RUN_SEG_2 only. Ignored with a logged metric in all other states.
    Drives RUN_SEG_2 → RUN_SEG_3. Side effects: ApplyPreset("maze_3"), segment=3.
    """
    type: str = field(default="Cp2Pressed", init=False)


@dataclass(frozen=True)
class StopPressed:
    """
    Emitted by: inputs/pico_link.py when the stop button is pressed.
    Consumed by: FSM in RUN_SEG_3 only. Ignored in other states.
    Drives RUN_SEG_3 → FINISHED. Side effects: StopStopwatch, SaveRun("clean"),
    BroadcastState.
    """
    type: str = field(default="StopPressed", init=False)


@dataclass(frozen=True)
class MaxRunExceeded:
    """
    Emitted by: runner.py when the stopwatch exceeds max_run_ms without the player
    finishing. Consumed by: FSM in any RUN state.
    Drives RUN_* → ABORTED.
    """
    type: str = field(default="MaxRunExceeded", init=False)


@dataclass(frozen=True)
class GmBust:
    """
    Emitted by: web/routes_gm.py when the gamemaster taps BUST.
    Consumed by: FSM in any RUN state (and RESULT for post-hoc, though unusual).
    Drives RUN_* → BUSTED. Side effects: StopStopwatch, ApplyPreset("bust"),
    SaveRun("busted").
    """
    type: str = field(default="GmBust", init=False)


@dataclass(frozen=True)
class GmAbort:
    """
    Emitted by: web/routes_gm.py when the gamemaster taps ABORT.
    Consumed by: FSM in any state.
    Drives current state → ABORTED. Side effects: StopStopwatch, DisarmDetection,
    SaveRun("aborted").
    """
    type: str = field(default="GmAbort", init=False)


@dataclass(frozen=True)
class GmVoid:
    """
    Emitted by: web/routes_gm.py after a completed run (RESULT or any post-run state).
    Marks the run as voided — recorded but excluded from the leaderboard.
    Does not change FSM state; updates the run record only.
    Fields:
        reason: GM-entered note explaining the void (shown in run history)
    """
    reason: str
    type: str = field(default="GmVoid", init=False)


@dataclass(frozen=True)
class GmForceReset:
    """
    Emitted by: web/routes_gm.py when the gamemaster taps FORCE RESET.
    Consumed by: FSM in any state, including FAULT.
    Drives any state → RESET. The ultimate escape hatch.
    """
    type: str = field(default="GmForceReset", init=False)


@dataclass(frozen=True)
class GmCancel:
    """
    Emitted by: web/routes_gm.py when the gamemaster cancels a count-in in progress.
    Consumed by: FSM in COUNTDOWN state only.
    Drives COUNTDOWN → ARM. Side effect: ApplyPreset("blackout").
    """
    type: str = field(default="GmCancel", init=False)


@dataclass(frozen=True)
class GmConfirmBreak:
    """
    Emitted by: web/routes_gm.py when the GM taps CONFIRM on the assisted-mode prompt.
    Only valid when FSMContext.pending_break is set (i.e. detection mode is assisted
    and a break is awaiting adjudication).
    Drives current RUN state → BUSTED.
    """
    type: str = field(default="GmConfirmBreak", init=False)


@dataclass(frozen=True)
class GmVetoBreak:
    """
    Emitted by: web/routes_gm.py when the GM taps VETO on the assisted-mode prompt.
    Only valid when FSMContext.pending_break is set.
    Clears pending_break, resumes stopwatch from halted position, stays in current RUN state.
    """
    type: str = field(default="GmVetoBreak", init=False)


@dataclass(frozen=True)
class MasterModeEngage:
    """
    Emitted by: web/routes_admin.py or a long-press on the GM console.
    Consumed by: FSM in any state.
    Drives any state → MASTER. Runs in master mode are excluded from the leaderboard.
    """
    type: str = field(default="MasterModeEngage", init=False)


@dataclass(frozen=True)
class MasterModeExit:
    """
    Emitted by: web/routes_admin.py when exiting master mode.
    Consumed by: FSM in MASTER state only.
    Drives MASTER → SELF_TEST (system re-verifies itself on exit).
    """
    type: str = field(default="MasterModeExit", init=False)


@dataclass(frozen=True)
class VisionStalled:
    """
    Emitted by: vision/service.py, via runner._vision_listener, when a camera's
    frame gap exceeds detection.stall_threshold_ms (300 ms). The gap is measured
    by VisionService's watchdog task, not inside the frame-read loop — a camera
    that freezes with its TCP connection open never delivers another frame, so
    nothing in the read path can notice.
    Consumed by: FSM in any state.
    Side effects: DropDetectionMode("manual", "vision stalled"), BroadcastState.
    Never busts a player — silence is always safer than a phantom break.
    """
    type: str = field(default="VisionStalled", init=False)


@dataclass(frozen=True)
class CameraUnreachable:
    """
    Emitted by: vision/camera.py after 3 consecutive failed reconnect attempts.
    Consumed by: FSM / runner for health reporting and mode downgrade.
    Fields:
        camera_id: which camera is unreachable
    """
    camera_id: str
    type: str = field(default="CameraUnreachable", init=False)


@dataclass(frozen=True)
class CameraMoved:
    """
    Emitted by: vision/camera.py when the boot-time drift check detects a shift
    exceeding 2 px (phase correlation against the stored reference frame).
    Consumed by: FSM / runner — triggers manual mode and admin alert.
    Fields:
        camera_id: which camera drifted
        drift_px:  measured shift in pixels
    """
    camera_id: str
    drift_px: float
    type: str = field(default="CameraMoved", init=False)


@dataclass(frozen=True)
class DetectionModeChanged:
    """
    Emitted by: web/routes_gm.py when the GM switches detection mode via the segmented
    buttons, or by runner.py during automatic degradation (VisionStalled, CameraMoved).
    Consumed by: FSM — updates context.detection_mode and broadcasts state.
    Fields:
        mode: "auto" | "assisted" | "manual"
    """
    mode: str
    type: str = field(default="DetectionModeChanged", init=False)


@dataclass(frozen=True)
class ProcessRestart:
    """
    Emitted by: runner.py at startup when it detects a prior run was in progress
    (e.g. by checking the DB for an open run). Never resume — always abort.
    Consumed by: FSM in any state.
    Drives any RUN state → ABORTED; any other state → RESET.
    """
    type: str = field(default="ProcessRestart", init=False)


@dataclass(frozen=True)
class ArmTimeout:
    """
    Emitted by: runner.py when arm_timeout_ms elapses while in ARM state with no
    count-in requested. The player presumably walked away.
    Consumed by: FSM in ARM state.
    Drives ARM → RESET.
    """
    type: str = field(default="ArmTimeout", init=False)


@dataclass(frozen=True)
class ResultDisplayTimeout:
    """
    Emitted by: runner.py after result_display_ms elapses in FINISHED, BUSTED,
    ABORTED, or RESULT state.
    Consumed by: FSM in those states.
    Drives FINISHED/BUSTED/ABORTED → RESULT, or RESULT → RESET.
    """
    type: str = field(default="ResultDisplayTimeout", init=False)


# ---------------------------------------------------------------------------
# Side Effects
# Returned by the FSM as a list; executed by runner.py in order.
# The FSM never executes them — it only names them. This is what makes
# test_fsm.py fast with no mocks.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApplyPreset:
    """
    Tell io/presets.py to apply a named preset (static, single-frame).
    Used for maze changes (maze_1/2/3) and blackout.
    preset_name must exist in mazes.yaml presets section.

    `defer` asks the runner to wait game.checkpoint_shape_delay_ms before
    applying, so the maze does not snap to its next shape under a player who is
    still mid-stride across the checkpoint. It is a FLAG, not a duration: the
    FSM stays pure and does not read config or clocks (invariant 1), so the
    runner owns the timing.
    """
    preset_name: str
    defer: bool = False


@dataclass(frozen=True)
class PlayShow:
    """
    Start an animated show (sequence of presets with timing).
    Used for attract, bust effect, count-in, clean celebration.
    show_name must exist in mazes.yaml shows section.
    Cancels any currently playing show.
    """
    show_name: str


@dataclass(frozen=True)
class StopShow:
    """
    Stop the currently playing show and leave the coils where they are.

    Emitted by: fsm.py on ATTRACT -> REGISTERED.
    Consumed by: runner.py::_handle_stop_show.

    The attract show writes coils every 300-500 ms. It must not run during
    REGISTERED, ARM or the count-in ramp, where it fights the flash sequence.
    """


@dataclass(frozen=True)
class StartStopwatch:
    """Tell core/stopwatch.py to start (or restart) the stopwatch."""


@dataclass(frozen=True)
class StopStopwatch:
    """Tell core/stopwatch.py to stop the stopwatch and freeze elapsed_ms."""


@dataclass(frozen=True)
class ResetStopwatch:
    """Tell core/stopwatch.py to reset elapsed to zero and clear started_at."""


@dataclass(frozen=True)
class ArmDetection:
    """
    Tell vision/detect.py to begin emitting BreakConfirmed events.
    Vision ignores breaks for arm_grace_ms after arming (per §4.4).
    """


@dataclass(frozen=True)
class DisarmDetection:
    """Tell vision/detect.py to stop emitting BreakConfirmed events."""


@dataclass(frozen=True)
class StartCountIn:
    """
    Tell runner.py to schedule the count-in ramp (pulse list from game.yaml).
    The ramp uses monotonic_ns with absolute next-edge computation — no sleep accumulation.
    Late pulse edges are skipped; GO always lands on time.
    """


@dataclass(frozen=True)
class BeamPreflightCheck:
    """
    Tell vision/ to run the silent 150 ms preflight blink and emit
    PreflightPass or PreflightFail(beam_id).
    Triggered at PlateHigh (ARM entry).
    """


@dataclass(frozen=True)
class ReadyBlink:
    """
    Trigger the 150 ms blink of the count_in preset at ARM entry.
    This serves as visual "system ready" acknowledgement to the player.
    Applied via io/presets.py (on → 150 ms → off).
    """


@dataclass(frozen=True)
class SaveRun:
    """
    Persist the completed run to SQLite runs table.
    Executed by runner._save_run() which calls persist/db.py.
    Fields:
        outcome: RunOutcome constant (clean/busted/aborted/voided)
        run_id:  UUIDv7 for this run (generated at RUN_SEG_1 entry)
    """
    outcome: str
    run_id: str
    busting_beam_id: str | None = None


@dataclass(frozen=True)
class VoidRun:
    """
    Mark an already-saved run as voided.

    Emitted by: fsm.py on GmVoid.
    Consumed by: runner.py::_handle_void_run, which calls db.void_run().

    Separate from SaveRun because the row already exists. SaveRun inserts, and
    a second insert on the same primary key raises IntegrityError. void_run()
    updates in place and preserves pre_void_outcome so unvoid can restore it.

    Fields:
        run_id: UUIDv7 of the run to void
        reason: GM-entered note, stored in runs.voided_reason
    """
    run_id: str
    reason: str = ""



@dataclass(frozen=True)
class BroadcastState:
    """
    Send a WebSocket state update to all connected clients.
    Payload: current FSM state, detection mode, beam states, stopwatch clock,
    pending_break indicator, player nickname.
    Executed by runner._broadcast() which calls web/server.py.
    """


@dataclass(frozen=True)
class EmitMetric:
    """
    Emit a metric via core/metrics.py.
    Used by the FSM to record transitions and ignored/out-of-order events.
    Fields:
        name:  metric name constant (from core/metrics.py)
        value: numeric value (default 1.0)
        tags:  arbitrary key/value pairs for slicing
    """
    name: str
    value: float = 1.0
    tags: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SaveBreakEvidence:
    """
    Tell vision/evidence.py to save the JPEG crop of the triggering ROI
    (plus the two preceding frames) against this run.
    Fields:
        beam_id: which beam triggered
        run_id:  run to associate the evidence with
    """
    beam_id: str
    run_id: str


@dataclass(frozen=True)
class AutoMaskBeam:
    """
    Tell vision/detect.py (and config/beams.json) to mask a flapping beam.
    Displayed as a persistent banner on the GM console.
    Fields:
        beam_id: beam to mask
        reason:  why it was auto-masked (e.g. "flap_detector: 5 breaks in 60 s")
    """
    beam_id: str
    reason: str


@dataclass(frozen=True)
class DropDetectionMode:
    """
    Downgrade the detection mode to a safer level.
    Executed by runner._drop_detection_mode() which updates context and persists the change.
    Never escalates automatically — human must tap to return to stricter mode.
    Fields:
        mode:   target mode ("manual" is the typical degradation target)
        reason: human-readable cause shown on GM console (e.g. "vision stalled")
    """
    mode: str
    reason: str


# ---------------------------------------------------------------------------
# Union type alias — for type hints in fsm.py and runner.py
# ---------------------------------------------------------------------------

# All input events
Event = (
    BootComplete | SelfTestPass | SelfTestFail | PlayerRegistered |
    PlateHigh | PlateLow | PreflightPass | PreflightFail |
    CountInRequested | RampComplete | BaselineCaptured | BaselineCaptureFail |
    BreakConfirmed | Cp1Pressed | Cp2Pressed | StopPressed |
    MaxRunExceeded | GmBust | GmAbort | GmVoid | GmForceReset |
    GmCancel | GmConfirmBreak | GmVetoBreak |
    MasterModeEngage | MasterModeExit |
    VisionStalled | CameraUnreachable | CameraMoved |
    DetectionModeChanged | ProcessRestart | ArmTimeout | ResultDisplayTimeout
)

@dataclass(frozen=True)
class ApplyTimePenalty:
    """
    Add a time penalty to the running clock.

    Emitted by: FSM when a confirmed break lands outside the penalty cooldown.
    Consumed by: runner.py -> Stopwatch.add_penalty_ms.

    This replaces busting on the detection path. A clipped beam costs the player
    seconds; only the gamemaster can end a run outright.

    Fields:
        ms:      penalty in milliseconds (game.penalty_ms)
        beam_id: which dot caused it, for the record and the console
        total_penalties: how many penalties this run has now taken, including
                 this one — so the broadcast and the log line agree without the
                 runner having to recount.
    """
    ms: int
    beam_id: str = ""
    total_penalties: int = 0
    type: str = field(default="ApplyTimePenalty", init=False)


@dataclass(frozen=True)
class RevokeTimePenalty:
    """
    Take a penalty back — the gamemaster vetoed it.

    Emitted by: FSM on GmVetoBreak.
    Consumed by: runner.py -> Stopwatch.revoke_penalty_ms.

    Fields:
        ms:      how much to give back, in milliseconds
        beam_id: the dot whose penalty is being withdrawn
    """
    ms: int
    beam_id: str = ""
    type: str = field(default="RevokeTimePenalty", init=False)


# All side effects
SideEffect = (
    ApplyPreset | PlayShow | StartStopwatch | StopStopwatch | ResetStopwatch |
    ArmDetection | DisarmDetection | StartCountIn | BeamPreflightCheck |
    ReadyBlink | SaveRun | BroadcastState | EmitMetric |
    SaveBreakEvidence | AutoMaskBeam | DropDetectionMode |
    ApplyTimePenalty | RevokeTimePenalty
)
