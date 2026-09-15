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
    # Run outcomes
    RunOutcome,
    # Side effects
    ApplyPreset, PlayShow, StopShow, StartStopwatch, StopStopwatch, ResetStopwatch,
    ArmDetection, DisarmDetection, StartCountIn, BeamPreflightCheck,
    ReadyBlink, SaveRun, VoidRun, BroadcastState, EmitMetric,
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
    VisionStalled,
)
from core.fsm import FSMContext, transition
from core.stopwatch import Stopwatch, server_clock_message
from iobackend.reconcile import ReconcileLoop
import core.metrics as metrics

log = logging.getLogger(__name__)

# The NUC boots faster than the PoE switch, so the relay boards are routinely
# unreachable for the first few seconds of a venue power-up.
_SELF_TEST_ATTEMPTS = 6
_SELF_TEST_RETRY_S = 5.0


class MasterModeRequired(RuntimeError):
    """Raised when a master-mode-only operation is attempted outside MASTER."""


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

        self.started_at_mono: float = time.monotonic()
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
        # Name of the show _show_task is playing, so reload_config() can re-arm it.
        self._show_name: str | None = None
        self._self_test_task: asyncio.Task | None = None
        self._countdown_step: int = 0
        self._countdown_total: int = 0
        # monotonic_ns at which the ramp reaches GO, or None outside COUNTDOWN.
        self._countdown_go_ns: int | None = None
        self._max_run_deadline_ns: int | None = None
        self._current_preset: str | None = None
        self._event_writes: set[asyncio.Task] = set()
        # Set by __main__ once the DMX controller exists. The lights are show
        # content driven by FSM state, not a static room setting.
        self.lights = None
        self._db_fault: str | None = None
        # Nothing ever called metrics.configure(), so _sink stayed None and
        # every emit() returned immediately — relay.mismatch, vision.stall,
        # vision.mass_dark, break.detected have never recorded a value. Log
        # them: on an unattended tour this is the difference between "we saw
        # the relays degrading in week 2" and "it stopped working on a Saturday".
        metrics.configure(self._metric_sink)
        self._assisted_task: asyncio.Task | None = None
        self._registered_timeout_task: asyncio.Task | None = None
        self._countdown_start_ns: int = 0
        self._last_outcome: str | None = None
        # Invariant 4's safety backstop. Started in run(); exposed so the admin
        # hardware page can read mismatch_counts.
        self.reconciler: Any = None
        # Cached hardware readback (updated every ~2s by _hardware_poller)
        self._board_states: dict[str, dict] = {}  # board_id → {status, coils, rtt_ms}
        self._cached_leaderboard: list[dict] = []
        self._last_rank: int | None = None

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
        3. Emit SelfTestPass/Fail → MASTER or FAULT

        Boot lands in MASTER, not ATTRACT (game.boot_to_master). The lasers
        stay dark and the house lights stay up until the GM has walked the
        container and pressed FORCE RESET.

        Then drain the event queue, executing side effects for each event.
        Also starts background tasks: inputs listener, vision listener, clock broadcaster.
        """
        log.info("GameRunner starting")

        # Seed this BEFORE the self test, which is what consumes it. Setting it
        # afterwards is setting it too late — the pass handler has already sent
        # the box to ATTRACT.
        self.context.boot_to_master = bool(
            getattr(getattr(self.config, "game", None), "boot_to_master", True)
        ) if self.config else True
        if self.context.boot_to_master:
            log.info("Boot will land in MASTER MODE — the GM must walk the "
                     "container and press FORCE RESET to enter game mode")

        # Boot
        await self.dispatch(BootComplete())
        await self._run_self_test()

        # Start the inputs backend poll loop if it has one (ModbusInputs.run())
        # Seed the leaderboard before the first broadcast. It started as [] and
        # was only ever refreshed on save/void, and the outdoor display's
        # `if (msg.leaderboard)` is true for an empty array — so the public board
        # went blank after every restart until someone finished a run.
        await self._refresh_leaderboard()

        extra_tasks: list[asyncio.Task] = []
        if hasattr(self.inputs, "run"):
            extra_tasks.append(asyncio.create_task(self.inputs.run(), name="inputs_poll"))

        # Same for vision. Without this nothing ever opened a camera: the
        # backend was constructed, handed to the runner, and then left idle,
        # so _vision_listener sat on a queue no one was filling.
        if hasattr(self.vision, "run"):
            extra_tasks.append(asyncio.create_task(self.vision.run(), name="vision_run"))

        # Start background tasks.
        tasks = [
            asyncio.create_task(self._inputs_listener(), name="inputs_listener"),
            asyncio.create_task(self._vision_listener(), name="vision_listener"),
            asyncio.create_task(self._clock_broadcaster(), name="clock_broadcaster"),
            asyncio.create_task(self._event_drain(), name="event_drain"),
            asyncio.create_task(self._hardware_poller(), name="hardware_poller"),
            # Supervised, not fire-and-forget. These used to be created with no
            # reference kept and left out of the wait() set below, so a dead
            # vision pipeline or a dead input poller left the process running
            # and looking healthy — and VisionService.run() RETURNS (no
            # exception) when no cameras are configured.
            *extra_tasks,
        ]

        # Invariant 4: re-assert coils that drift from the desired state. Reads
        # the resolver through a callable so a config reload doesn't strand it on
        # the old one.
        if self.io is not None:
            self.reconciler = ReconcileLoop(
                get_resolver=lambda: self._resolver,
                backend=self.io,
                metrics_emit=metrics.emit,
            )
            tasks.append(asyncio.create_task(self.reconciler.run(), name="reconcile"))

        # Wait for the FIRST task to finish. A plain gather() re-raises on the
        # first failure and skips the cancel loop below, which orphans the
        # siblings: the clock kept broadcasting, the displays looked healthy,
        # and the start plate was dead. The process stayed alive, so systemd
        # Restart=always never fired.
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
        except asyncio.CancelledError:
            log.info("GameRunner cancelled")
            for t in tasks:
                t.cancel()
            await self.blackout()
            raise

        failed = [t for t in done if not t.cancelled() and t.exception() is not None]
        for t in failed:
            log.critical(
                "Subsystem %s died: %r", t.get_name(), t.exception(), exc_info=t.exception()
            )
        for t in pending:
            t.cancel()
        await self.blackout()

        # Re-raise so __main__ ends the process and systemd restarts it clean.
        # Invariant 7 makes that safe: the runner never resumes a run.
        if failed:
            raise failed[0].exception()  # type: ignore[misc]
        log.warning("GameRunner: all subsystem tasks exited without error")

    @staticmethod
    def _metric_sink(name: str, value: float, tags: dict) -> None:
        """Default sink: structured lines in the journal, greppable per metric."""
        if tags:
            bits = " ".join(f"{k}={v}" for k, v in sorted(tags.items()))
            log.info("METRIC %s=%.4g %s", name, value, bits)
        else:
            log.info("METRIC %s=%.4g", name, value)

    def _leaderboard_scope(self) -> str:
        """The configured scope. It was hardcoded 'daily' at both call sites, so
        setting `scope: activation` for a multi-day venue did nothing."""
        lb = getattr(getattr(self.config, "game", None), "leaderboard", None)
        return getattr(lb, "scope", "daily") or "daily"

    def _leaderboard_limit(self) -> int:
        lb = getattr(getattr(self.config, "game", None), "leaderboard", None)
        return int(getattr(lb, "max_entries", 20) or 20)

    async def _refresh_leaderboard(self) -> None:
        """Refresh the cached leaderboard. Never raises."""
        if self.db is None:
            return
        try:
            self._cached_leaderboard = await self.db.get_leaderboard(
                scope=self._leaderboard_scope(), limit=self._leaderboard_limit()
            )
        except Exception as exc:
            log.warning("leaderboard refresh failed: %s", exc)

    def _record_event(self, event: Any, old_state: str, new_state: str) -> None:
        """Queue one event row. Never raises, never blocks the FSM."""
        try:
            payload = {"from": old_state, "to": new_state}
            for field in ("beam_id", "reason", "mode", "nickname"):
                v = getattr(event, field, None)
                if v is not None:
                    payload[field] = str(v)[:200]
            task = asyncio.create_task(
                self.db.insert_event(
                    run_id=self.context.run_id,
                    ts_mono_ns=time.monotonic_ns(),
                    type=type(event).__name__,
                    source="fsm",
                    payload=payload,
                ),
                name="record_event",
            )
            # Keep a reference so the loop does not garbage-collect it, and
            # retrieve the exception so a DB failure is logged rather than
            # surfacing as "Task exception was never retrieved" at GC time.
            self._event_writes.add(task)
            task.add_done_callback(self._event_write_done)
        except Exception as exc:
            log.debug("event recording skipped: %s", exc)

    def _event_write_done(self, task: asyncio.Task) -> None:
        self._event_writes.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("event write failed: %r", task.exception())

    async def _open_run_row(self) -> None:
        """Insert the in-progress run row. Best effort; never blocks the game."""
        if self.db is None or not self.context.run_id:
            return
        try:
            await self.db.insert_run({
                "id": self.context.run_id,
                "player_id": self.context.player_id,
                "started_at": self._run_started_at_iso,
                "ended_at": None,
                "elapsed_ms": None,
                # A run that never reaches SaveRun keeps this outcome, which is
                # exactly what "the process died mid-run" should look like.
                "outcome": "in_progress",
                "detection_mode": self.context.detection_mode,
                "busting_beam_id": None,
                "segment_reached": self.context.segment,
                "voided_reason": None,
                "pre_void_outcome": None,
            })
        except Exception as exc:
            self._db_fault = f"could not open run row: {exc}"
            log.error("could not write the in-progress run row: %s", exc)

    async def blackout(self) -> None:
        """
        Everything off: lasers dark, hazer zeroed.

        Relay coils LATCH, and the boards are separately powered. Stopping the
        service used to leave whatever preset was last written energised in an
        unattended container with the reconciler dead — including via the
        recalibration runbook, which tells the operator to stop the service.

        Best effort and never raises: this runs on the shutdown path, and a
        board that has already gone away must not stop the process from exiting.
        Bounded, so an unreachable board cannot hold shutdown past
        TimeoutStopSec.

        When house-light control lands, raise them here too.
        """
        if self._resolver is not None and self.io is not None:
            try:
                await asyncio.wait_for(
                    self._resolver.apply_all_off(self.io), timeout=3.0
                )
                log.info("blackout: all coils off")
            except Exception as exc:
                log.error("blackout: could not drive coils off (%s) — "
                          "LASERS MAY STILL BE LIT", exc)
        if self.lights is not None:
            try:
                self.lights.stop()
            except Exception as exc:
                log.warning("stopping light cues failed: %s", exc)
        hazer = getattr(self, "hazer", None)
        if hazer is not None and hasattr(hazer, "blackout"):
            # Zeroes haze and the maze lights but leaves the ENTRANCE lit —
            # the node holds the last frame, so this is the state the container
            # is left in when the process exits.
            hazer.blackout()

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

        # Room lights follow the state: dim pulse in ATTRACT, dark for the run,
        # a flash on a clean finish. COUNTDOWN and the RUN states are forced
        # dark inside the player regardless of cue or GM override — ambient
        # light raises the reading inside every dot's ROI, and a broken beam
        # that still reads above break_ratio is a MISSED break.
        if self.lights is not None and new_state != old_state:
            try:
                self.lights.set_state(new_state)
            except Exception as exc:
                log.error("light cue for %s failed: %s", new_state, exc)

        # Flight recorder. insert_event() had zero callers, so every disputed
        # bust opened a run detail showing an empty timeline — which reads as
        # "nothing happened during this run", not "logging was never wired up".
        # Fire and forget: the game path must never wait on the DB (invariant 6).
        if self.db is not None and self.context.run_id:
            self._record_event(event, old_state, new_state)

        await self._execute_side_effects(side_effects, old_state, new_state)

        # Entering SELF_TEST (e.g. after exiting MASTER): re-run the probe
        if new_state == "SELF_TEST" and old_state != "SELF_TEST" and old_state != "BOOT":
            self._cancel_task("_self_test_task")
            self._self_test_task = asyncio.create_task(self._run_self_test(), name="self_test")

        # Entering REGISTERED: start an inactivity timeout. Without one, a
        # player who signs in and wanders off leaves the container dark (the
        # attract show was stopped) with their name on the outdoor display,
        # until a GM notices and taps cancel.
        if new_state == "REGISTERED" and old_state != "REGISTERED":
            self._cancel_task("_registered_timeout_task")
            reg_ms = getattr(getattr(self.config, "game", None),
                             "registered_timeout_ms", 180_000) if self.config else 180_000
            self._registered_timeout_task = asyncio.create_task(
                self._timeout_after(reg_ms, GmCancel()), name="registered_timeout"
            )
        if new_state != "REGISTERED":
            self._cancel_task("_registered_timeout_task")

        # Clear the assisted decision deadline once the decision landed.
        if new_state not in RUN_STATES or not self.context.pending_break:
            self._cancel_task("_assisted_task")

        # Entering ARM: start inactivity timeout
        if new_state == "ARM" and old_state != "ARM":
            self._cancel_task("_arm_timeout_task")
            arm_timeout_ms = getattr(getattr(self.config, "game", None), "arm_timeout_ms", 180_000) if self.config else 180_000
            self._arm_timeout_task = asyncio.create_task(
                self._timeout_after(arm_timeout_ms, ArmTimeout()), name="arm_timeout"
            )

        # Entering MASTER: kill all background tasks and clear run context
        if new_state == "MASTER" and old_state != "MASTER":
            for attr in ("_show_task", "_count_in_task", "_arm_timeout_task",
                         "_result_timeout_task", "_max_run_task"):
                self._cancel_task(attr)
            # Clear stale run context so next game session starts fresh
            self.context.run_id = None
            self.context.player_id = None
            self.context.player_nickname = None
            self.context.segment = 1
            self.context.pending_break = None
            self._last_outcome = None
            self._last_rank = None

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
        for attr in ("_arm_timeout_task", "_result_timeout_task", "_max_run_task",
                     "_count_in_task", "_show_task", "_self_test_task",
                     "_assisted_task", "_registered_timeout_task"):
            self._cancel_task(attr)
        self.context.run_id = None
        self.context.player_id = None
        self.context.player_nickname = None
        self.context.segment = 1
        self.context.pending_break = None
        self.context.assisted_halt_elapsed_ms = None
        self._run_started_at_iso = None
        self._last_outcome = None
        self._last_rank = None
        self._countdown_step = 0
        self._countdown_total = 0
        self._countdown_go_ns = None
        self._max_run_deadline_ns = None

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
        self._show_name = None
        self._cancel_task("_count_in_task")
        # Before the write, not after. The settle window starts when set_maze is
        # called, so calling it afterwards left the new shape physically lit
        # while the detector still held the old dot set — and a slow board could
        # stretch that past the hysteresis window and bust a player for the maze
        # changing shape at a checkpoint.
        self._apply_maze(effect.preset_name)
        if self._resolver and self.io:
            try:
                await self._resolver.apply_preset(effect.preset_name, self.io)
            except KeyError:
                log.warning("ApplyPreset: unknown preset '%s' — skipping", effect.preset_name)

    def _apply_maze(self, preset_name: str) -> None:
        self._current_preset = preset_name
        """
        Point vision at the dots captured while this preset was lit.

        This is the whole answer to "is that a broken beam or a maze change".
        The dot set moves with the maze, so a dot going dark because its relay
        opened is simply not being looked at any more.

        A preset with no capture watches nothing. That is deliberate: without a
        capture there is no baseline to compare against, so every reading would
        be a guess, and a guess ends someone's run (invariant 5).
        """
        if self.vision is None or not hasattr(self.vision, "set_maze"):
            return
        settle_ms = getattr(self.config.game, "preset_settle_ms", 250) if self.config else 250
        self.vision.set_maze(preset_name, settle_ms)

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

        self._show_name = effect.show_name
        self._show_task = asyncio.create_task(
            self._run_show(show, effect.show_name), name=f"show_{effect.show_name}"
        )

    async def _handle_stop_show(self, effect: StopShow) -> None:
        """
        Stop the playing show. Leave the coils where the show left them.

        ReadyBlink or the count-in ramp sets the next coil state. Writing here
        would race them.
        """
        log.info("[SideEffect] StopShow")
        self._cancel_task("_show_task")
        self._show_name = None

    async def _run_show(self, show: Any, name: str) -> None:
        """Execute a show's step sequence. Loops if show.loop is True."""
        try:
            while True:
                for step in show.steps:
                    preset_name = step.get("preset") if isinstance(step, dict) else getattr(step, "preset", None)
                    hold_ms = step.get("hold_ms", 300) if isinstance(step, dict) else getattr(step, "hold_ms", 300)
                    if preset_name and self._resolver and self.io:
                        # Shows drive presets straight through the resolver, so
                        # without this the detector keeps watching whatever maze
                        # was last APPLIED while the show flashes something else.
                        # With the rolling EMA live, every blackout frame of the
                        # attract show was being folded into that maze's
                        # baselines — editing a show's hold_ms silently retuned
                        # detection sensitivity.
                        self._apply_maze(preset_name)
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
        """Start or resume the stopwatch. Resume if already has a run_id (veto case)."""
        if not self.context.run_id:
            self.context.run_id = str(uuid.uuid4())
            log.info("Run started: run_id=%s", self.context.run_id)
            self.stopwatch.start()
            self._run_started_at_iso = datetime.now(timezone.utc).isoformat()
            # Write the row pessimistically at GO — CLAUDE.md invariant 7.
            # Nothing existed until SaveRun at the END, so a crash or a power
            # cut mid-run left no trace of the run at all, and the flight
            # recorder could not attach events to a run that had no row yet.
            # SaveRun upserts over this.
            await self._open_run_row()
        else:
            # Veto case: resume from halted elapsed
            self.stopwatch.resume()
            log.info("Stopwatch resumed from %d ms", self.stopwatch.elapsed_ms())
        log.info("[SideEffect] StartStopwatch")
        # Max-run is an ABSOLUTE deadline anchored at the first start, not a
        # fresh countdown per call. Every assisted veto used to re-arm the full
        # budget, so three false positives bought a player 9 minutes and the
        # queue stalled behind them.
        self._cancel_task("_max_run_task")
        max_run_ms = getattr(getattr(self.config, "game", None), "max_run_ms", 120_000) if self.config else 120_000
        if self._max_run_deadline_ns is None:
            self._max_run_deadline_ns = time.monotonic_ns() + max_run_ms * 1_000_000
        remaining_ms = max(
            0, (self._max_run_deadline_ns - time.monotonic_ns()) // 1_000_000
        )
        self._max_run_task = asyncio.create_task(
            self._timeout_after(remaining_ms, MaxRunExceeded()), name="max_run_timer"
        )

    async def _handle_stop_stopwatch(self, effect: StopStopwatch) -> None:
        """Stop the stopwatch and freeze elapsed."""
        log.info("[SideEffect] StopStopwatch → %d ms", self.stopwatch.elapsed_ms())
        self.stopwatch.stop()
        # An assisted halt stays in a RUN state with the stopwatch frozen and
        # every timer cancelled, so if the GM never taps CONFIRM or VETO the
        # game hangs with nothing to recover it. Give the decision a deadline.
        if self.context.pending_break and self.state in RUN_STATES:
            self._cancel_task("_assisted_task")
            timeout_ms = getattr(getattr(self.config, "game", None),
                                 "assisted_timeout_ms", 60_000) if self.config else 60_000
            # ABORT, not bust and not veto.
            #
            # Auto-veto can loop: it clears pending_break and re-arms detection,
            # so a player still standing in the beam re-triggers within ~120 ms
            # and the game never escapes. Auto-bust terminates cleanly but
            # convicts a player nobody actually looked at, which is exactly what
            # invariant 5 exists to prevent — and detection is not calibrated.
            #
            # Abort says what really happened: nobody adjudicated, so the run
            # does not count. No leaderboard entry, no verdict, and the player
            # runs again.
            self._assisted_task = asyncio.create_task(
                self._timeout_after(timeout_ms, GmAbort()),
                name="assisted_timeout",
            )
            log.info("Assisted decision deadline: %d ms → run will ABORT "
                     "if the GM does not decide", timeout_ms)
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
        # Anchor the ramp here rather than inside the task. StartCountIn is
        # followed immediately by BroadcastState, which would otherwise go out
        # with no deadline set and flash the wrong digit for one frame.
        self._countdown_start_ns = time.monotonic_ns()
        if self.config is not None:
            total_ms = sum(
                p.on_ms + p.off_ms for p in self.config.game.count_in.pulses
            )
            self._countdown_go_ns = self._countdown_start_ns + total_ms * 1_000_000
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
        # Same anchor _handle_start_count_in used for the GO deadline, so the
        # last pulse edge and the broadcast countdown reach zero together.
        next_edge_ns = self._countdown_start_ns

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
            # Invariant 4: go through the resolver. A direct write_coils leaves
            # _desired lit, and the reconciler re-lights the maze mid-gap.
            if self._resolver and self.io:
                await self._resolver.apply_all_off(self.io)

        # solid_at_end: keep the preset on at GO
        if self.config.game.count_in.solid_at_end and self._resolver and self.io:
            try:
                await self._resolver.apply_preset(count_in_preset, self.io)
            except KeyError:
                pass

        # Ramp complete: emit event to FSM.
        await self.put_event(RampComplete())

    async def _handle_beam_preflight_check(self, effect: BeamPreflightCheck) -> None:
        """
        The gate between "the maze is actually working" and GO.

        This used to emit PreflightPass unconditionally, so PreflightFail could
        never be reached from anywhere and the system would happily arm a run
        with unreachable relay boards, stalled cameras and zero calibrated dots.

        It checks what it can cheaply: boards answering, vision not stalled, and
        the lit maze actually having dots to watch. In manual detection mode the
        vision checks are skipped — the operator is the detector.
        """
        log.info("[SideEffect] BeamPreflightCheck")
        problems: list[str] = []

        if hasattr(self.io, "all_boards"):
            dead = [b.board_id for b in self.io.all_boards() if b.status != "OK"]
            if dead:
                problems.append(f"relay board(s) {', '.join(dead)} not OK")

        if self.context.detection_mode != "manual":
            stats = {}
            if self.vision is not None and hasattr(self.vision, "detector_stats"):
                try:
                    stats = self.vision.detector_stats() or {}
                except Exception as exc:
                    problems.append(f"vision stats unavailable ({exc})")
            if stats:
                if not stats.get("total"):
                    problems.append(
                        f"maze '{stats.get('maze')}' has no calibrated dots — "
                        f"run tools/capture.py")
                if stats.get("blind"):
                    problems.append(f"{stats['blind']} dot(s) have no baseline")
                if stats.get("stalled"):
                    problems.append("a camera is stalled")
                if stats.get("fault"):
                    problems.append(str(stats["fault"]))

        if problems:
            reason = "; ".join(problems)
            log.error("Preflight FAILED: %s", reason)
            metrics.emit("preflight.failed", 1.0, {"reason": reason})
            await self.put_event(PreflightFail(reason=reason))
            return
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
            # Invariant 4: ARM has no ApplyPreset, so nothing repairs _desired.
            # A direct write here leaves the maze lit for the whole ARM state.
            await self._resolver.apply_all_off(self.io)
        except Exception as exc:
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
            # Refresh leaderboard cache and compute rank
            try:
                self._cached_leaderboard = await self.db.get_leaderboard(
                    scope=self._leaderboard_scope(), limit=self._leaderboard_limit())
                # Compute rank for this run
                self._last_rank = None
                if effect.outcome == "clean":
                    for i, entry in enumerate(self._cached_leaderboard, 1):
                        if entry.get("id") == effect.run_id:
                            self._last_rank = i
                            break
            except Exception:
                pass
        except Exception as exc:
            # A lost run used to be completely silent: _last_outcome was already
            # set, so the GM console and both displays still showed the result,
            # and faults() had no DB entry at all. It was discovered days later
            # when someone asked why the record was not on the board.
            log.exception("SaveRun: DB insert failed")
            self._db_fault = f"run not saved: {exc}"

    async def _handle_void_run(self, effect: VoidRun) -> None:
        """
        Void an already-saved run.

        Uses db.void_run(), an idempotent UPDATE. insert_run() would raise
        IntegrityError here, because the row was written when the run ended.
        """
        log.info("[SideEffect] VoidRun(run_id=%r, reason=%r)", effect.run_id, effect.reason)
        if not self.db or not hasattr(self.db, "void_run"):
            log.warning("VoidRun: no valid DB — skipping")
            return
        try:
            await self.db.void_run(effect.run_id, effect.reason)
            self._last_outcome = RunOutcome.voided
            log.info("VoidRun: voided run %s (reason=%r)", effect.run_id, effect.reason)
        except Exception:
            log.exception("VoidRun: DB update failed")
            return
        # Drop the voided run from the cached leaderboard.
        try:
            self._cached_leaderboard = await self.db.get_leaderboard(
                    scope=self._leaderboard_scope(), limit=self._leaderboard_limit())
            self._last_rank = None
        except Exception:
            log.exception("VoidRun: leaderboard refresh failed")

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
        """
        Translate an admin action string to an FSM event.

        Only fire-and-forget actions live here. The master-mode relay and
        stopwatch operations are awaited directly by the admin routes so that
        failures surface as HTTP errors instead of a log line.
        """
        if action == "master_engage":
            self._event_queue.put_nowait(MasterModeEngage())
        elif action == "master_exit":
            self._event_queue.put_nowait(MasterModeExit())
        elif action == "dev_trigger":
            self._handle_dev_trigger(payload)
        else:
            log.debug("Admin action %r (no handler)", action)

    # ------------------------------------------------------------------
    # Config reload
    # ------------------------------------------------------------------

    async def reload_config(self, new_config: Any) -> None:
        """
        Swap in a freshly loaded AppConfig and rebuild the preset resolver.

        Called by the admin routes after a config file is written so that edits
        take effect without a service restart. Desired coil state is reset to
        all-off by the new resolver; the reconciler re-asserts it on its next
        pass. Never called during a run — the routes refuse to reload unless the
        FSM is in a quiescent state.

        A running show holds a reference to its old step list, so swapping the
        config alone leaves it playing the pre-edit sequence until a restart.
        Re-arm it against the new config so editing the show you are watching
        actually does something.
        """
        from iobackend.presets import PresetResolver
        self.config = new_config
        self._resolver = PresetResolver(new_config.mazes, new_config.hardware)
        log.info("Config reloaded: %d boards, %d beams, %d presets",
                 len(new_config.hardware.relay_boards),
                 len(new_config.beams.beams),
                 len(new_config.mazes.presets))

        # Vision caches thresholds, baselines and the whole dot set at
        # construction, so without this a reloaded beams.json — including a
        # fresh calibration — reported success and changed nothing until the
        # service restarted.
        if self.vision is not None and hasattr(self.vision, "reload"):
            try:
                self.vision.reload(new_config)
                # Re-point at the maze that is actually lit right now.
                if self._current_preset:
                    self._apply_maze(self._current_preset)
            except Exception as exc:
                log.error("vision reload failed: %s", exc)

        if self._show_name and self._show_task and not self._show_task.done():
            name = self._show_name
            log.info("Re-arming show '%s' against the reloaded config", name)
            await self._handle_play_show(PlayShow(name))

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def faults(self) -> list[dict]:
        """Return the current active faults for the admin dashboard."""
        out: list[dict] = []
        for board_id, st in self._board_states.items():
            status = st.get("status")
            if status and status != "OK":
                out.append({"subsystem": board_id, "message": f"relay board {status}"})
        # Default False, not True. With True, renaming or dropping is_connected
        # would silently report the Opta as fine — and two lines away the WS
        # message defaults the same attribute to False, so they disagreed.
        if not getattr(self.inputs, "is_connected", False):
            out.append({"subsystem": "inputs", "message": "Opta not connected"})

        # Only an INVOLUNTARY downgrade is a fault. 'assisted' is the shipping
        # default, so flagging it meant the fault list was never empty and the
        # deploy health check printed a fault on every single deploy — which
        # trains operators to ignore the list.
        if self.context.detection_mode == "manual":
            out.append({
                "subsystem": "vision",
                "message": "detection dropped to manual — vision is not policing runs",
            })

        # The conditions that actually end an event, none of which were reported.
        vision = self.vision
        if vision is not None and hasattr(vision, "detector_stats"):
            try:
                vs = vision.detector_stats() or {}
            except Exception as exc:
                out.append({"subsystem": "vision",
                            "message": f"vision stats unavailable: {exc}"})
                vs = {}
            if vs:
                if not vs.get("total"):
                    out.append({"subsystem": "vision",
                                "message": f"maze '{vs.get('maze')}' has no "
                                           f"calibrated dots — detection is blind"})
                if vs.get("blind"):
                    out.append({"subsystem": "vision",
                                "message": f"{vs['blind']} dot(s) have no baseline"})
                total_cams = vs.get("cameras_total") or 0
                live = vs.get("cameras_live") or 0
                if total_cams and live < total_cams:
                    out.append({"subsystem": "vision",
                                "message": f"{total_cams - live} of {total_cams} "
                                           f"camera(s) not delivering frames"})
                if vs.get("fault"):
                    out.append({"subsystem": "vision", "message": str(vs["fault"])})

        if self._db_fault:
            out.append({"subsystem": "db", "message": self._db_fault})
        if self.context.beams_masked:
            out.append({
                "subsystem": "beams",
                "message": f"{len(self.context.beams_masked)} beam(s) masked",
            })
        if self.state == "FAULT":
            out.append({"subsystem": "fsm", "message": "FSM in FAULT state"})
        return out

    def _require_master(self) -> None:
        """Raise MasterModeRequired unless the FSM is in MASTER."""
        if self.state != "MASTER":
            raise MasterModeRequired(f"not in MASTER mode (state={self.state})")

    async def _master_apply_preset(self, preset_name: str) -> None:
        """
        Apply a preset directly in master mode.

        Raises MasterModeRequired if not in MASTER, KeyError for an unknown
        preset, and RuntimeError if a relay board write fails.
        """
        self._require_master()
        if not self._resolver or not self.io:
            raise RuntimeError("relay backend not available")
        ok = await self._resolver.apply_preset(preset_name, self.io)
        if not ok:
            raise RuntimeError(f"one or more boards rejected preset {preset_name!r}")
        log.info("Master: applied preset %r", preset_name)

    async def _master_apply_channels(self, channels: list[int]) -> list[int]:
        """
        Apply a raw channel list directly in master mode (no preset name needed).

        Goes through PresetResolver so desired state stays in sync and the
        reconciler does not immediately overwrite the write (invariant 4).
        Returns the channels that were out of range and skipped.
        """
        self._require_master()
        if not self._resolver or not self.io:
            raise RuntimeError("relay backend not available")
        rejected = await self._resolver.apply_channels(channels, self.io)
        log.info("Master: applied %d channels directly (%d rejected)",
                 len(channels) - len(rejected), len(rejected))
        return rejected

    async def _master_toggle_channel(self, board_id: str, channel: int, state: bool) -> None:
        """
        Toggle a single relay channel on a specific board in master mode.

        Raises MasterModeRequired, ValueError (unknown board / bad channel) or
        RuntimeError (write failed).
        """
        self._require_master()
        if not self._resolver or not self.io:
            raise RuntimeError("relay backend not available")
        await self._resolver.apply_direct(board_id, channel, state, self.io)
        log.info("Master: %s ch %d → %s", board_id, channel, state)

    def _master_stopwatch(self, action: str) -> None:
        """
        Control the stopwatch in master mode.

        Gated on MASTER: without this guard an admin request could reset a live
        player's clock, violating invariant 2 (the server owns the stopwatch).
        """
        self._require_master()
        if action == "start":
            self.stopwatch.start()
        elif action == "stop":
            self.stopwatch.stop()
        elif action == "reset":
            self.stopwatch.reset()
        else:
            raise ValueError(f"unknown stopwatch action {action!r}")
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

    async def _run_self_test(self, attempts: int = _SELF_TEST_ATTEMPTS) -> None:
        """
        Probe relay boards, retrying before giving up.

        A single miss used to latch FAULT, and only GmForceReset escapes FAULT.
        The NUC boots faster than the PoE switch, so every morning the boards
        were briefly unreachable and the box sat wedged with a green systemd
        unit until someone found the iPad.
        """
        failed: list[str] = []
        for attempt in range(1, attempts + 1):
            failed = []
            if hasattr(self.io, "all_boards"):
                for board in self.io.all_boards():
                    coils = await board.read_coils()
                    if coils is not None:
                        log.info("Self-test: %s OK", board.board_id)
                    else:
                        log.error("Self-test: %s FAILED", board.board_id)
                        failed.append(board.board_id)
            if not failed:
                await self.dispatch(SelfTestPass())
                return
            if attempt < attempts:
                log.warning("Self-test: %s unreachable (attempt %d/%d) — "
                            "retrying in %.0f s",
                            ", ".join(failed), attempt, attempts, _SELF_TEST_RETRY_S)
                await asyncio.sleep(_SELF_TEST_RETRY_S)

        from core.events import SelfTestFail
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
            kind = event_tuple[0]
            if kind == "break":
                _, beam_id, ratio, ts_ns = event_tuple
                await self.put_event(BreakConfirmed(
                    beam_id=beam_id,
                    ratio=ratio,
                    run_id=self.context.run_id or "",
                ))
            elif kind == "clear":
                # Informational — the detector handles hysteresis itself.
                pass
            elif kind == "stall":
                # Invariant 5. The FSM drops to manual detection so a camera
                # outage cannot end someone's run. This branch is why the
                # invariant is reachable at all: the listener used to
                # understand "break" only, so a stall tuple was discarded.
                _, stalled, camera_ids = event_tuple
                if stalled:
                    log.warning("vision stalled on %s — dropping to manual", camera_ids)
                    await self.put_event(VisionStalled())
            else:
                log.warning("vision_listener: unknown event kind %r", kind)

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
            # Watched / dark / masked per camera. This replaced a strip of 45
            # channel pills: detection watches ~175 individual dots now, and 175
            # pills is not something anyone reads on an iPad mid-run. A count
            # per camera answers the only live question — is vision seeing what
            # it should.
            "vision": (self.vision.detector_stats()
                       if self.vision is not None
                       and hasattr(self.vision, "detector_stats") else None),
            "pending_break": self.context.pending_break,
            "countdown_step": self._countdown_step,
            "countdown_total": self._countdown_total,
            # Milliseconds until GO. The pulse ramp accelerates, so the pulse
            # index is not a seconds countdown — the display needs the deadline
            # to render 3-2-1. Server-side per invariant 2.
            "countdown_remaining_ms": (
                max(0, (self._countdown_go_ns - time.monotonic_ns()) // 1_000_000)
                if self._countdown_go_ns is not None else None
            ),
            "outcome": self._last_outcome,
            "rank": self._last_rank,
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
    StopShow:         GameRunner._handle_stop_show,
    StartStopwatch:   GameRunner._handle_start_stopwatch,
    StopStopwatch:    GameRunner._handle_stop_stopwatch,
    ResetStopwatch:   GameRunner._handle_reset_stopwatch,
    ArmDetection:     GameRunner._handle_arm_detection,
    DisarmDetection:  GameRunner._handle_disarm_detection,
    StartCountIn:     GameRunner._handle_start_count_in,
    BeamPreflightCheck: GameRunner._handle_beam_preflight_check,
    ReadyBlink:       GameRunner._handle_ready_blink,
    SaveRun:          GameRunner._handle_save_run,
    VoidRun:          GameRunner._handle_void_run,
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
