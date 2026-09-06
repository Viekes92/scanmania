"""
core/runner.py — wires the pure FSM to I/O backends and owns the async event loop.

Input:  FSM events from inputs/, vision/, web/, and internal timers.
Output: executes FSM side effects via io/, persist/, web/ backends.
Invariant: this is the ONLY module that bridges pure FSM logic to I/O. The FSM
           itself has no I/O; runner.py has no game logic. Keep them separate.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from core.events import (
    # States
    RESET, ATTRACT, SELF_TEST, RUN_STATES,
    # Side effects
    ApplyPreset, PlayShow, StartStopwatch, StopStopwatch, ResetStopwatch,
    ArmDetection, DisarmDetection, StartCountIn, BeamPreflightCheck,
    ReadyBlink, SaveRun, QueueSync, BroadcastState, EmitMetric,
    SaveBreakEvidence, AutoMaskBeam, DropDetectionMode,
    # Input events for internal production
    BootComplete, SelfTestPass, ProcessRestart, RampComplete,
    ResultDisplayTimeout, ArmTimeout, MaxRunExceeded,
    PreflightPass, PreflightFail,
    # Input events from hardware / external sources
    PlayerRegistered, PlateHigh, PlateLow, Cp1Pressed, Cp2Pressed,
    StopPressed, BreakConfirmed,
    # GM / admin events
    CountInRequested, GmBust, GmAbort, GmVoid, GmForceReset, GmCancel,
    GmConfirmBreak, GmVetoBreak, MasterModeEngage, MasterModeExit,
    DetectionModeChanged,
)
from core.fsm import FSMContext, transition
from core.stopwatch import Stopwatch, server_clock_message
import core.metrics as metrics

log = logging.getLogger(__name__)

# Input ID → FSM event mapping (matches Pico firmware wire contract)
_INPUT_EVENT_MAP = {
    ("plate", 1): PlateHigh,
    ("plate", 0): PlateLow,
    ("cp1",   1): Cp1Pressed,
    ("cp2",   1): Cp2Pressed,
    ("stop",  1): StopPressed,
}


class GameRunner:
    """
    Owns the asyncio event loop for the game. Receives events from all sources,
    feeds them to the FSM, and executes the returned side effects.

    Parameters
    ----------
    config:          AppConfig instance (from config/loader.py)
    io_backend:      io.modbus.ModbusMaster or io.fake.FakeIO
    inputs_backend:  inputs.pico_link.PicoLink or inputs.fake.FakeInputs
    vision_backend:  vision.camera.CameraManager or vision.fake.FakeVision
    db:              persist.db.Database instance
    hub:             web.server.WebSocketHub (optional, set later via set_hub)
    """

    def __init__(self, config: Any, io_backend: Any, inputs_backend: Any,
                 vision_backend: Any, db: Any, hub: Any = None) -> None:
        self.config = config
        self.io = io_backend
        self.inputs = inputs_backend
        self.vision = vision_backend
        self.db = db
        self._hub = hub

        self.state: str = "BOOT"
        self.context: FSMContext = FSMContext(
            detection_mode=getattr(config.game, "detection_mode", "auto")
            if config is not None else "auto"
        )
        self.stopwatch: Stopwatch = Stopwatch()
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._count_in_task: asyncio.Task | None = None
        self._arm_timeout_task: asyncio.Task | None = None
        self._result_timeout_task: asyncio.Task | None = None
        self._max_run_task: asyncio.Task | None = None
        self._run_started_at_iso: str | None = None
        self._show_task: asyncio.Task | None = None
        self._self_test_task: asyncio.Task | None = None
        self._countdown_step: int = 0
        self._countdown_total: int = 0
        self._last_outcome: str | None = None
        # Cached hardware readback (updated every ~2s by _hardware_poller)
        self._board_states: dict[str, dict] = {}  # board_id → {status, coils, rtt_ms}
        self._cached_leaderboard: list[dict] = []

        # Preset resolver — bridges preset names to relay board coil writes
        self._resolver = None
        if config is not None:
            try:
                from iobackend.presets import PresetResolver
                self._resolver = PresetResolver(config.mazes, config.hardware)
            except Exception as exc:
                log.warning("PresetResolver unavailable: %s", exc)

    def set_hub(self, hub: Any) -> None:
        """Inject the WebSocketHub after construction (avoids circular imports)."""
        self._hub = hub

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Main async event loop. Runs forever; systemd Restart=always handles crashes.

        Boot sequence:
        1. Emit BootComplete → SELF_TEST
        2. Run self-tests (relay boards, Pico, cameras)
        3. Emit SelfTestPass/Fail → ATTRACT or FAULT

        Then drain the event queue, executing side effects for each event.
        Also starts background tasks: inputs listener, vision listener, clock broadcaster.
        """
        log.info("GameRunner starting")

        # Boot
        await self.dispatch(BootComplete())
        await self._run_self_test()

        # Start the inputs backend poll loop if it has one (ModbusInputs.run())
        if hasattr(self.inputs, "run"):
            asyncio.create_task(self.inputs.run(), name="inputs_poll")

        # Start background tasks.
        tasks = [
            asyncio.create_task(self._inputs_listener(), name="inputs_listener"),
            asyncio.create_task(self._vision_listener(), name="vision_listener"),
            asyncio.create_task(self._clock_broadcaster(), name="clock_broadcaster"),
            asyncio.create_task(self._event_drain(), name="event_drain"),
            asyncio.create_task(self._hardware_poller(), name="hardware_poller"),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            log.info("GameRunner cancelled")
            for t in tasks:
                t.cancel()

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, event: Any) -> None:
        """
        Feed an event to the FSM, execute resulting side effects, and handle
        automatic RESET → ATTRACT transition.

        Thread-safe: external producers should call put_event() instead.
        """
        old_state = self.state
        new_state, side_effects = transition(self.state, event, self.context)
        self.state = new_state

        log.debug("FSM %s --[%s]--> %s", old_state, type(event).__name__, new_state)

        await self._execute_side_effects(side_effects, old_state, new_state)

        # Entering SELF_TEST (e.g. after exiting MASTER): re-run the probe
        if new_state == "SELF_TEST" and old_state != "SELF_TEST" and old_state != "BOOT":
            self._cancel_task("_self_test_task")
            self._self_test_task = asyncio.create_task(self._run_self_test(), name="self_test")

        # Entering ARM: start inactivity timeout
        if new_state == "ARM" and old_state != "ARM":
            self._cancel_task("_arm_timeout_task")
            arm_timeout_ms = getattr(getattr(self.config, "game", None), "arm_timeout_ms", 180_000) if self.config else 180_000
            self._arm_timeout_task = asyncio.create_task(
                self._timeout_after(arm_timeout_ms, ArmTimeout()), name="arm_timeout"
            )

        # Entering MASTER: kill all background tasks for full manual control
        if new_state == "MASTER" and old_state != "MASTER":
            for attr in ("_show_task", "_count_in_task", "_arm_timeout_task",
                         "_result_timeout_task", "_max_run_task"):
                self._cancel_task(attr)

        # When entering RESULT, start timeout for RESULT → RESET
        # (replaces the FINISHED/BUSTED→RESULT timer that just fired)
        if new_state == "RESULT" and old_state != "RESULT":
            self._cancel_task("_result_timeout_task")
            result_ms = getattr(getattr(self.config, "game", None), "result_display_ms", 8000) if self.config else 8000
            self._result_timeout_task = asyncio.create_task(
                self._timeout_after(result_ms, ResultDisplayTimeout()), name="result_to_reset"
            )

        # RESET is transient: immediately transition to ATTRACT.
        if new_state == RESET and old_state != RESET:
            await self._do_reset_to_attract()

    async def put_event(self, event: Any) -> None:
        """Thread-safe event submission from external producers."""
        await self._event_queue.put(event)

    async def _event_drain(self) -> None:
        """Drain the event queue and dispatch events sequentially."""
        while True:
            event = await self._event_queue.get()
            await self.dispatch(event)
            self._event_queue.task_done()

    async def _do_reset_to_attract(self) -> None:
        """
        Complete the RESET → ATTRACT transition immediately.
        Clears run context so the next player starts fresh.
        """
        # Cancel all outstanding timers
        for attr in ("_arm_timeout_task", "_result_timeout_task", "_max_run_task", "_count_in_task"):
            self._cancel_task(attr)
        self.context.run_id = None
        self.context.player_id = None
        self.context.player_nickname = None
        self.context.segment = 1
        self.context.pending_break = None
        self.context.assisted_halt_elapsed_ms = None
        self._run_started_at_iso = None
        self._last_outcome = None
        self._countdown_step = 0
        self._countdown_total = 0

        old_state = self.state
        new_state, side_effects = transition(self.state, _AttractTick(), self.context)
        # If the FSM doesn't handle _AttractTick (it won't in default tables),
        # force the state directly.
        self.state = ATTRACT
        await self._execute_side_effects(
            [PlayShow("attract"), ResetStopwatch(), BroadcastState()],
            old_state, ATTRACT,
        )

    # ------------------------------------------------------------------
    # Side effect executor
    # ------------------------------------------------------------------

    async def _execute_side_effects(
        self,
        side_effects: list,
        old_state: str,
        new_state: str,
    ) -> None:
        """
        Execute each side effect returned by the FSM in order.

        Each side effect type has a corresponding _handle_* method below.
        Unknown types are logged and skipped — never raise.
        """
        for effect in side_effects:
            etype = type(effect)
            handler = _EFFECT_HANDLERS.get(etype)
            if handler is None:
                log.warning("Unknown side effect: %r", effect)
                continue
            try:
                await handler(self, effect)
            except Exception:
                log.exception("Error executing side effect %r", effect)

    # ------------------------------------------------------------------
    # Side effect handlers
    # ------------------------------------------------------------------

    async def _handle_apply_preset(self, effect: ApplyPreset) -> None:
        """Apply a named preset via the io backend. Cancels any running show or count-in."""
        log.info("[SideEffect] ApplyPreset(%r)", effect.preset_name)
        self._cancel_task("_show_task")
        self._cancel_task("_count_in_task")
        if self._resolver and self.io:
            try:
                await self._resolver.apply_preset(effect.preset_name, self.io)
            except KeyError:
                log.warning("ApplyPreset: unknown preset '%s' — skipping", effect.preset_name)

    async def _handle_play_show(self, effect: PlayShow) -> None:
        """Start an animated show (sequence of presets with timing)."""
        log.info("[SideEffect] PlayShow(%r)", effect.show_name)
        # Cancel any currently playing show
        self._cancel_task("_show_task")

        if not self.config or not self._resolver or not self.io:
            # Fallback: try as a static preset
            await self._handle_apply_preset(ApplyPreset(effect.show_name))
            return

        show = self.config.mazes.shows.get(effect.show_name)
        if not show:
            # Not a show — try as a static preset fallback
            log.debug("PlayShow: '%s' not found as show, trying as preset", effect.show_name)
            await self._handle_apply_preset(ApplyPreset(effect.show_name))
            return

        self._show_task = asyncio.create_task(
            self._run_show(show, effect.show_name), name=f"show_{effect.show_name}"
        )

    async def _run_show(self, show: Any, name: str) -> None:
        """Execute a show's step sequence. Loops if show.loop is True."""
        try:
            while True:
                for step in show.steps:
                    preset_name = step.get("preset") if isinstance(step, dict) else getattr(step, "preset", None)
                    hold_ms = step.get("hold_ms", 300) if isinstance(step, dict) else getattr(step, "hold_ms", 300)
                    if preset_name and self._resolver and self.io:
                        try:
                            await self._resolver.apply_preset(preset_name, self.io)
                        except KeyError:
                            log.warning("Show '%s': unknown preset '%s'", name, preset_name)
                    await asyncio.sleep(hold_ms / 1000.0)
                if not getattr(show, "loop", False):
                    break
        except asyncio.CancelledError:
            pass

    async def _handle_start_stopwatch(self, effect: StartStopwatch) -> None:
        """Start the monotonic stopwatch and assign a run_id if none exists yet."""
        if not self.context.run_id:
            self.context.run_id = str(uuid.uuid4())
            log.info("Run started: run_id=%s", self.context.run_id)
        self.stopwatch.start()
        self._run_started_at_iso = datetime.now(timezone.utc).isoformat()
        log.info("[SideEffect] StartStopwatch")
        # Start/restart max-run timer (cancel existing to avoid doubles after veto)
        self._cancel_task("_max_run_task")
        max_run_ms = getattr(getattr(self.config, "game", None), "max_run_ms", 120_000) if self.config else 120_000
        self._max_run_task = asyncio.create_task(
            self._timeout_after(max_run_ms, MaxRunExceeded()), name="max_run_timer"
        )

    async def _handle_stop_stopwatch(self, effect: StopStopwatch) -> None:
        """Stop the stopwatch and freeze elapsed."""
        log.info("[SideEffect] StopStopwatch → %d ms", self.stopwatch.elapsed_ms())
        self.stopwatch.stop()
        # Cancel run timers
        self._cancel_task("_max_run_task")
        self._cancel_task("_arm_timeout_task")
        # Only start result display timer if we're in a post-run state
        # (NOT during assisted-mode halt where the run is still active)
        if self.state in ("FINISHED", "BUSTED", "ABORTED"):
            self._cancel_task("_result_timeout_task")
            result_display_ms = getattr(getattr(self.config, "game", None), "result_display_ms", 8000) if self.config else 8000
            self._result_timeout_task = asyncio.create_task(
                self._timeout_after(result_display_ms, ResultDisplayTimeout()), name="result_timeout"
            )

    async def _handle_reset_stopwatch(self, effect: ResetStopwatch) -> None:
        """Reset stopwatch to idle."""
        log.info("[SideEffect] ResetStopwatch")
        self.stopwatch.reset()

    async def _handle_arm_detection(self, effect: ArmDetection) -> None:
        """Tell the vision backend to start emitting break events."""
        log.info("[SideEffect] ArmDetection")
        if self.vision and hasattr(self.vision, "arm"):
            grace_ms = (
                self.config.beams.detection.arm_grace_ms
                if self.config else 150
            )
            self.vision.arm(self.context.run_id or "", grace_ms)
        # Arm timeout is handled by the FSM ARM state entry, not here

    async def _handle_disarm_detection(self, effect: DisarmDetection) -> None:
        """Tell the vision backend to stop emitting break events."""
        log.info("[SideEffect] DisarmDetection")
        if self.vision and hasattr(self.vision, "disarm"):
            self.vision.disarm()

    async def _handle_start_count_in(self, effect: StartCountIn) -> None:
        """
        Schedule the count-in ramp.

        Uses monotonic_ns with absolute next-edge computation — no sleep accumulation.
        Late pulse edges are SKIPPED rather than delayed so GO always lands on time.
        """
        log.info("[SideEffect] StartCountIn")
        if self._count_in_task and not self._count_in_task.done():
            self._count_in_task.cancel()
        self._count_in_task = asyncio.create_task(
            self._run_count_in_ramp(), name="count_in_ramp"
        )

    async def _run_count_in_ramp(self) -> None:
        """
        Execute the accelerating flash ramp defined in config.game.count_in.pulses.

        Each pulse: on for on_ms, off for off_ms.
        Timing is absolute: next_edge_ns is set once and each sleep targets that
        absolute time, skipping late edges rather than accumulating delay.
        After all pulses, emit RampComplete.
        """
        if self.config is None:
            await self.put_event(RampComplete())
            return

        pulses = self.config.game.count_in.pulses
        count_in_preset = self.config.game.count_in.preset
        self._countdown_total = len(pulses)
        self._countdown_step = 0
        now_ns = time.monotonic_ns()
        next_edge_ns = now_ns

        for i, pulse in enumerate(pulses):
            self._countdown_step = i
            # ON edge
            next_edge_ns += pulse.on_ms * 1_000_000
            sleep_s = (next_edge_ns - time.monotonic_ns()) / 1e9
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)
            if self._resolver and self.io:
                try:
                    await self._resolver.apply_preset(count_in_preset, self.io)
                except KeyError:
                    pass

            # OFF edge
            next_edge_ns += pulse.off_ms * 1_000_000
            sleep_s = (next_edge_ns - time.monotonic_ns()) / 1e9
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)
            if self.io and hasattr(self.io, "all_boards"):
                for board in self.io.all_boards():
                    await board.write_coils([False] * 16)

        # solid_at_end: keep the preset on at GO
        if self.config.game.count_in.solid_at_end and self._resolver and self.io:
            try:
                await self._resolver.apply_preset(count_in_preset, self.io)
            except KeyError:
                pass

        # Ramp complete: emit event to FSM.
        await self.put_event(RampComplete())

    async def _handle_beam_preflight_check(self, effect: BeamPreflightCheck) -> None:
        """Trigger the silent preflight check; emit PreflightPass immediately for fake mode."""
        log.info("[SideEffect] BeamPreflightCheck")
        # Phase 2 (real vision): capture baseline blink here.
        # For now, immediately signal pass so the game can proceed.
        await self.put_event(PreflightPass())

    async def _handle_ready_blink(self, effect: ReadyBlink) -> None:
        """Execute the ready blink of the count_in preset."""
        log.info("[SideEffect] ReadyBlink")
        if not self._resolver or not self.config or not self.io:
            return
        preset = self.config.game.count_in.preset
        blink_ms = self.config.game.count_in.ready_blink_ms
        try:
            await self._resolver.apply_preset(preset, self.io)
            await asyncio.sleep(blink_ms / 1000.0)
            for board in self.io.all_boards():
                await board.write_coils([False] * 16)
        except (KeyError, Exception) as exc:
            log.warning("ReadyBlink failed: %s", exc)

    async def _handle_save_run(self, effect: SaveRun) -> None:
        """Persist the run to SQLite."""
        log.info("[SideEffect] SaveRun(outcome=%r, run_id=%r)", effect.outcome, effect.run_id)
        self._last_outcome = effect.outcome
        if not self.db or not hasattr(self.db, "insert_run"):
            log.warning("SaveRun: no valid DB — skipping")
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        run = {
            "id": effect.run_id,
            "player_id": self.context.player_id,
            "started_at": self._run_started_at_iso or now_iso,
            "ended_at": now_iso,
            "elapsed_ms": self.stopwatch.elapsed_ms(),
            "outcome": effect.outcome,
            "detection_mode": self.context.detection_mode,
            "busting_beam_id": getattr(effect, "busting_beam_id", None) or self.context.pending_break,
            "segment_reached": self.context.segment,
            "voided_reason": None,
            "created_at": now_iso,
        }
        try:
            await self.db.insert_run(run)
            log.info(
                "SaveRun: inserted run %s (outcome=%s, elapsed=%d ms)",
                effect.run_id, effect.outcome, run["elapsed_ms"],
            )
            # Refresh leaderboard cache for WS broadcast
            try:
                self._cached_leaderboard = await self.db.get_leaderboard(scope="daily", limit=20)
            except Exception:
                pass
        except Exception:
            log.exception("SaveRun: DB insert failed")

    async def _handle_queue_sync(self, effect: QueueSync) -> None:
        """Insert the run into the cloud-sync outbox."""
        log.info("[SideEffect] QueueSync(run_id=%r)", effect.run_id)
        if not self.db or not hasattr(self.db, "insert_outbox"):
            return
        try:
            run = await self.db.get_run(effect.run_id)
            if run:
                await self.db.insert_outbox(effect.run_id, run)
        except Exception:
            log.exception("QueueSync: outbox insert failed")

    async def _handle_broadcast_state(self, effect: BroadcastState) -> None:
        """Broadcast current FSM state + stopwatch clock over WebSocket."""
        log.debug("[SideEffect] BroadcastState(state=%r)", self.state)
        if self._hub:
            await self._hub.broadcast(self._get_state_message())

    async def _handle_emit_metric(self, effect: EmitMetric) -> None:
        """Emit a metric via core/metrics.py."""
        metrics.emit(effect.name, effect.value, effect.tags)

    async def _handle_save_break_evidence(self, effect: SaveBreakEvidence) -> None:
        """Tell vision to save break evidence JPEG."""
        log.info("[SideEffect] SaveBreakEvidence(beam=%r, run=%r)", effect.beam_id, effect.run_id)
        # Phase 2 (real vision): await self.vision.save_evidence(effect.beam_id, effect.run_id)

    async def _handle_auto_mask_beam(self, effect: AutoMaskBeam) -> None:
        """Mask a flapping beam in the vision backend and config."""
        log.warning("[SideEffect] AutoMaskBeam(beam=%r, reason=%r)", effect.beam_id, effect.reason)
        self.context.beams_masked.add(effect.beam_id)
        # Phase 2 (real vision): await self.vision.mask_beam(effect.beam_id, effect.reason)
        metrics.emit(metrics.BEAM_MASKED, tags={"beam_id": effect.beam_id, "reason": effect.reason})

    async def _handle_drop_detection_mode(self, effect: DropDetectionMode) -> None:
        """Downgrade detection mode and log the reason."""
        log.warning("[SideEffect] DropDetectionMode(mode=%r, reason=%r)", effect.mode, effect.reason)
        self.context.detection_mode = effect.mode
        metrics.emit(
            metrics.DETECTION_MODE_CHANGED,
            tags={"mode": effect.mode, "reason": effect.reason, "auto": True},
        )

    # ------------------------------------------------------------------
    # External action handlers (called from web routes via sync callbacks)
    # ------------------------------------------------------------------

    def on_player_registered(self, player_id: str, nickname: str) -> None:
        """Called from the web layer when a player signs in."""
        self._event_queue.put_nowait(PlayerRegistered(
            player_id=player_id,
            nickname=nickname,
        ))

    def on_gm_action(self, action: str, payload: dict) -> None:
        """Translate a GM console action string to an FSM event and enqueue it."""
        if action == "count_in":
            self._event_queue.put_nowait(CountInRequested())
        elif action == "bust":
            self._event_queue.put_nowait(GmBust())
        elif action == "abort":
            self._event_queue.put_nowait(GmAbort())
        elif action == "void":
            self._event_queue.put_nowait(GmVoid(reason=payload.get("reason", "")))
        elif action == "force_reset":
            self._event_queue.put_nowait(GmForceReset())
        elif action == "cancel":
            self._event_queue.put_nowait(GmCancel())
        elif action == "confirm_break":
            self._event_queue.put_nowait(GmConfirmBreak())
        elif action == "veto_break":
            self._event_queue.put_nowait(GmVetoBreak())
        elif action == "detection_mode":
            self._event_queue.put_nowait(DetectionModeChanged(mode=payload.get("mode", "auto")))
        elif action == "master_mode":
            ev = MasterModeEngage() if payload.get("engage") else MasterModeExit()
            self._event_queue.put_nowait(ev)
        elif action == "mask_beam":
            # Phase 2: persist to beams.json and update vision backend
            log.info("mask_beam: beam_id=%r masked=%r", payload.get("beam_id"), payload.get("masked"))
        else:
            log.warning("Unknown GM action: %r", action)

    def on_admin_action(self, action: str, payload: dict) -> None:
        """Translate an admin action string to an FSM event or direct operation."""
        if action == "master_engage":
            self._event_queue.put_nowait(MasterModeEngage())
        elif action == "master_exit":
            self._event_queue.put_nowait(MasterModeExit())
        elif action == "master_apply_preset":
            asyncio.ensure_future(self._master_apply_preset(payload.get("preset", "")))
        elif action == "master_toggle_channel":
            asyncio.ensure_future(self._master_toggle_channel(
                payload.get("board_id", ""), payload.get("channel", 1), payload.get("state", False),
            ))
        elif action == "master_apply_channels":
            asyncio.ensure_future(self._master_apply_channels(payload.get("channels", [])))
        elif action == "master_stopwatch":
            self._master_stopwatch(payload.get("action", ""))
        elif action == "dev_trigger":
            self._handle_dev_trigger(payload)
        else:
            log.debug("Admin action %r (no handler)", action)

    async def _master_apply_preset(self, preset_name: str) -> None:
        """Apply a preset directly in master mode."""
        if self.state != "MASTER" or not self._resolver or not self.io:
            log.warning("master_apply_preset: not in MASTER mode or no resolver")
            return
        try:
            await self._resolver.apply_preset(preset_name, self.io)
            log.info("Master: applied preset %r", preset_name)
        except Exception as exc:
            log.error("Master: apply_preset failed: %s", exc)

    async def _master_apply_channels(self, channels: list[int]) -> None:
        """Apply a raw channel list directly in master mode (no preset name needed)."""
        if self.state != "MASTER" or not self._resolver or not self.io:
            log.warning("master_apply_channels: not in MASTER mode")
            return
        try:
            # Build per-board coil arrays from the channel list
            state = self._resolver._empty_board_state()
            for ch in channels:
                try:
                    board_id, local_idx = self._resolver._channel_to_board(ch)
                    state[board_id][local_idx] = True
                except ValueError:
                    pass
            for board_id, coils in state.items():
                board = self.io.get_board(board_id)
                await board.write_coils(coils)
            log.info("Master: applied %d channels directly", len(channels))
        except Exception as exc:
            log.error("Master: apply_channels failed: %s", exc)

    async def _master_toggle_channel(self, board_id: str, channel: int, state: bool) -> None:
        """Toggle a single relay channel on a specific board in master mode."""
        if self.state != "MASTER" or not self.io:
            log.warning("master_toggle_channel: not in MASTER mode")
            return
        try:
            board = self.io.get_board(board_id)
            coil_idx = channel - 1  # channel is 1-indexed per board
            coils = list(getattr(board, "last_coils", [False] * 16))
            if 0 <= coil_idx < len(coils):
                coils[coil_idx] = state
                await board.write_coils(coils)
                log.info("Master: %s ch %d → %s", board_id, channel, state)
            else:
                log.warning("Master: channel %d out of range for %s", channel, board_id)
        except KeyError:
            log.error("Master: unknown board_id %r", board_id)
        except Exception as exc:
            log.error("Master: toggle_channel failed: %s", exc)

    def _master_stopwatch(self, action: str) -> None:
        """Control stopwatch in master mode."""
        if action == "start":
            self.stopwatch.start()
        elif action == "stop":
            self.stopwatch.stop()
        elif action == "reset":
            self.stopwatch.reset()
        log.info("Master: stopwatch %s", action)

    def _handle_dev_trigger(self, payload: dict) -> None:
        """Inject a raw FSM event for dev/testing purposes. --fake-all mode only."""
        event_name = payload.get("event", "")
        try:
            if event_name == "PlateHigh":
                self._event_queue.put_nowait(PlateHigh())
            elif event_name == "PlateLow":
                self._event_queue.put_nowait(PlateLow())
            elif event_name == "CountInRequested":
                self._event_queue.put_nowait(CountInRequested())
            elif event_name == "RampComplete":
                self._event_queue.put_nowait(RampComplete())
            elif event_name == "Cp1Pressed":
                self._event_queue.put_nowait(Cp1Pressed())
            elif event_name == "Cp2Pressed":
                self._event_queue.put_nowait(Cp2Pressed())
            elif event_name == "StopPressed":
                self._event_queue.put_nowait(StopPressed())
            elif event_name == "BreakConfirmed":
                self._event_queue.put_nowait(BreakConfirmed(
                    beam_id=payload.get("beam_id", "b001"),
                    ratio=float(payload.get("ratio", 0.1)),
                    run_id=self.context.run_id or "",
                ))
            elif event_name == "GmBust":
                self._event_queue.put_nowait(GmBust())
            elif event_name == "GmAbort":
                self._event_queue.put_nowait(GmAbort())
            elif event_name == "GmForceReset":
                self._event_queue.put_nowait(GmForceReset())
            elif event_name == "GmConfirmBreak":
                self._event_queue.put_nowait(GmConfirmBreak())
            elif event_name == "GmVetoBreak":
                self._event_queue.put_nowait(GmVetoBreak())
            elif event_name == "PlayerRegistered":
                self._event_queue.put_nowait(PlayerRegistered(
                    player_id=payload.get("player_id", "dev-player"),
                    nickname=payload.get("nickname", "Dev Player"),
                ))
            elif event_name == "MasterModeEngage":
                self._event_queue.put_nowait(MasterModeEngage())
            elif event_name == "MasterModeExit":
                self._event_queue.put_nowait(MasterModeExit())
            else:
                log.warning("Dev trigger: unknown event %r", event_name)
                return
            log.info("Dev trigger: injected %s", event_name)
        except Exception:
            log.exception("Dev trigger failed for event %r", event_name)

    # ------------------------------------------------------------------
    # Self-test
    # ------------------------------------------------------------------

    async def _run_self_test(self) -> None:
        """Probe relay boards and emit SelfTestPass or SelfTestFail."""
        passed = True
        if hasattr(self.io, "all_boards"):
            for board in self.io.all_boards():
                coils = await board.read_coils()
                if coils is not None:
                    log.info("Self-test: %s OK", board.board_id)
                else:
                    log.error("Self-test: %s FAILED", board.board_id)
                    passed = False
        if passed:
            await self.dispatch(SelfTestPass())
        else:
            from core.events import SelfTestFail
            failed = [b.board_id for b in self.io.all_boards() if b.status != "OK"]
            await self.dispatch(SelfTestFail(reason=f"Boards failed: {', '.join(failed)}"))

    # ------------------------------------------------------------------
    # Timer helpers
    # ------------------------------------------------------------------

    def _cancel_task(self, attr: str) -> None:
        """Cancel a named task attribute if it exists and is running."""
        task = getattr(self, attr, None)
        if task and not task.done():
            task.cancel()
        setattr(self, attr, None)

    async def _timeout_after(self, ms: int, event: Any) -> None:
        """Sleep for ms milliseconds, then inject event into the FSM queue."""
        try:
            await asyncio.sleep(ms / 1000.0)
            await self.put_event(event)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    async def _inputs_listener(self) -> None:
        """
        Listen for events from the Pico inputs backend and enqueue FSM events.
        Maps (input_id, state) pairs to FSM event classes via _INPUT_EVENT_MAP.
        """
        log.info("inputs_listener started")
        if not hasattr(self.inputs, "events"):
            log.warning("inputs backend has no events() — listener is idle")
            while True:
                await asyncio.sleep(3600)
            return
        async for input_id, state, host_ns in self.inputs.events():
            event_cls = _INPUT_EVENT_MAP.get((input_id, state))
            if event_cls is not None:
                await self.put_event(event_cls())
            else:
                log.debug("inputs_listener: unhandled input %r state=%d", input_id, state)

    async def _vision_listener(self) -> None:
        """
        Listen for beam break/clear events from the vision backend and enqueue them.
        """
        log.info("vision_listener started")
        if not hasattr(self.vision, "events"):
            log.warning("vision backend has no events() — listener is idle")
            while True:
                await asyncio.sleep(3600)
            return
        async for event_tuple in self.vision.events():
            if event_tuple[0] == "break":
                _, beam_id, ratio, ts_ns = event_tuple
                await self.put_event(BreakConfirmed(
                    beam_id=beam_id,
                    ratio=ratio,
                    run_id=self.context.run_id or "",
                ))
            # "clear" events are informational — no FSM event needed

    async def _clock_broadcaster(self) -> None:
        """
        Broadcast the game state + stopwatch clock over WebSocket at ~10 Hz.
        """
        log.info("clock_broadcaster started")
        while True:
            t0 = time.monotonic()
            if self._hub:
                try:
                    await self._hub.broadcast(self._get_state_message())
                except Exception as exc:
                    log.debug("clock_broadcaster broadcast error: %s", exc)
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, 0.1 - elapsed))

    async def _hardware_poller(self) -> None:
        """
        Read actual coil states from all boards every ~500ms.
        Caches results in self._board_states for the WS broadcast.
        """
        log.info("hardware_poller started")
        while True:
            if hasattr(self.io, "all_boards"):
                for board in self.io.all_boards():
                    try:
                        t0 = time.monotonic()
                        coils = await board.read_coils()
                        rtt = (time.monotonic() - t0) * 1000
                        self._board_states[board.board_id] = {
                            "status": board.status,
                            "coils": coils or [False] * 16,
                            "rtt_ms": round(rtt, 1),
                        }
                    except Exception as exc:
                        self._board_states[board.board_id] = {
                            "status": "ERR",
                            "coils": [False] * 16,
                            "rtt_ms": None,
                        }
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # State message builder (shared by broadcast and BroadcastState)
    # ------------------------------------------------------------------

    def _get_state_message(self) -> dict:
        """Build the WebSocket state dict broadcast to all connected clients."""
        sw = server_clock_message(self.stopwatch)
        return {
            "state": self.state,
            "detection_mode": self.context.detection_mode,
            "run_id": self.context.run_id,
            "player_nickname": self.context.player_nickname,
            "elapsed_ms": sw["elapsed_ms"],
            "started_at_mono_ns": sw["started_at_mono_ns"],
            "server_mono_now_ns": sw["server_mono_now_ns"],
            "segment": self.context.segment,
            "beams_masked": list(self.context.beams_masked),
            "pending_break": self.context.pending_break,
            "countdown_step": self._countdown_step,
            "countdown_total": self._countdown_total,
            "outcome": self._last_outcome,
            "boards": self._board_states,
            "leaderboard": self._cached_leaderboard,
            "inputs": {
                "connected": getattr(self.inputs, "is_connected", False),
                "states": dict(zip(
                    getattr(self.inputs, "_input_map", {}).values(),
                    getattr(self.inputs, "_prev_states", []),
                )) if hasattr(self.inputs, "_prev_states") else {},
            },
            "timestamp": time.time(),
        }


# ---------------------------------------------------------------------------
# Side effect dispatch table
# Maps SideEffect type → bound method on GameRunner.
# ---------------------------------------------------------------------------

_EFFECT_HANDLERS: dict = {
    ApplyPreset:      GameRunner._handle_apply_preset,
    PlayShow:         GameRunner._handle_play_show,
    StartStopwatch:   GameRunner._handle_start_stopwatch,
    StopStopwatch:    GameRunner._handle_stop_stopwatch,
    ResetStopwatch:   GameRunner._handle_reset_stopwatch,
    ArmDetection:     GameRunner._handle_arm_detection,
    DisarmDetection:  GameRunner._handle_disarm_detection,
    StartCountIn:     GameRunner._handle_start_count_in,
    BeamPreflightCheck: GameRunner._handle_beam_preflight_check,
    ReadyBlink:       GameRunner._handle_ready_blink,
    SaveRun:          GameRunner._handle_save_run,
    QueueSync:        GameRunner._handle_queue_sync,
    BroadcastState:   GameRunner._handle_broadcast_state,
    EmitMetric:       GameRunner._handle_emit_metric,
    SaveBreakEvidence: GameRunner._handle_save_break_evidence,
    AutoMaskBeam:     GameRunner._handle_auto_mask_beam,
    DropDetectionMode: GameRunner._handle_drop_detection_mode,
}


# ---------------------------------------------------------------------------
# Internal sentinel event (not part of the public wire contract)
# ---------------------------------------------------------------------------

class _AttractTick:
    """Internal sentinel used only by runner.py for the RESET→ATTRACT transition."""
    type = "_AttractTick"
