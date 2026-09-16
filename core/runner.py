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
import random
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
        self._deferred_preset_task: asyncio.Task | None = None
        # Latched by power_down(). Nothing may re-light the container after the
        # operator has been told it is safe to cut the breaker.
        self._powered_down: bool = False
        # Set when a vision stall forced detection to manual, so recovery can
        # put back exactly what it took and nothing else.
        self._auto_dropped_to_manual: bool = False
        self._mode_before_stall: str | None = None
        # The soundtrack. Set by __main__ after construction, like .lights.
        self.audio: Any = None
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
            asyncio.create_task(self._leaderboard_refresher(), name="leaderboard_day"),
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

    def _cue_lights(self, state: str) -> None:
        """
        Point the room lights at a state.

        Must be called from EVERY path that changes self.state, not just
        dispatch(). _do_reset_to_attract() sets the state directly and executes
        its own side effects, so routing this only through dispatch left the
        player stuck on RESET after a force reset — no cue for that state, so
        the room went dark and the attract pulse never started.

        COUNTDOWN and the RUN states are forced dark inside the player whatever
        the cue says: ambient light raises the reading inside every dot's ROI,
        and a broken beam that still reads above break_ratio is a MISSED break.
        """
        if self.lights is None or self._powered_down:
            return
        try:
            self.lights.set_state(state)
        except Exception as exc:
            log.error("light cue for %s failed: %s", state, exc)

    def _cue_audio(self, state: str) -> None:
        """
        Point the soundtrack at a state. Same contract as _cue_lights.

        Called from every path that changes self.state, for the same reason:
        _do_reset_to_attract() does not go through dispatch(), and routing this
        only through dispatch would leave the attract bed unplayed after a
        force reset.

        Audio is decoration. It is wrapped here as well as inside the cue
        player because a sound must never be able to end a run.
        """
        if self.audio is None or self._powered_down:
            return
        try:
            self.audio.set_state(state)
        except Exception as exc:
            log.error("audio cue for %s failed: %s", state, exc)

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
        if self.audio is not None:
            try:
                self.audio.stop()
            except Exception as exc:
                log.warning("stopping audio failed: %s", exc)
        hazer = getattr(self, "hazer", None)
        if hazer is not None and hasattr(hazer, "blackout"):
            # Zeroes haze and the maze lights but leaves the ENTRANCE lit —
            # the node holds the last frame, so this is the state the container
            # is left in when the process exits.
            hazer.blackout()

    async def power_down(self, poweroff: bool = False,
                         snapshot: bool = True) -> dict:
        """
        End-of-day shutdown: save everything first, then darken the container.

        The order is the point. Runs are settled and the database is snapshotted
        and closed BEFORE anything goes dark, so a shutdown can never be what
        loses a player's run. Then the lasers, then the haze and the maze
        lights, and the entrance light LAST — the operator needs to see their
        way out while the rest of the box goes down.

        Leaves the container in the state it will keep once power is cut: relay
        coils latch where they are left, and an Art-Net node holds the last
        frame it received. Both are off when this returns.

        Best effort per step and never raises: a board that has already gone
        away must not strand the operator halfway through a shutdown. Every
        step reports its own outcome so the caller can show what actually
        happened rather than claiming success.
        """
        report: list[dict] = []

        async def step(name: str, coro, detail: str = "") -> bool:
            """
            Run one step. A step fails by RAISING **or** by returning False.

            apply_all_off() reports a failed board write with a False return,
            not an exception — so treating any non-exception as success meant a
            relay board that did not answer still printed "all coils off" and
            the operator was told it was safe to cut the breaker with a board
            latched on. blackout() already got this right; this did not.
            """
            try:
                result = await coro
            except Exception as exc:
                log.error("power_down: %s failed: %s", name, exc)
                report.append({"step": name, "ok": False, "detail": str(exc)})
                return False
            ok = result is not False
            report.append({
                "step": name, "ok": ok,
                "detail": (detail or (result if isinstance(result, str) else ""))
                          if ok else "the hardware did not confirm this",
            })
            return ok

        log.warning("POWER DOWN requested — saving, then darkening the container")

        # 1. Stop every timer that can drive a transition — not just the ones
        #    that write coils. A surviving _result_timeout_task walks
        #    ABORTED -> RESULT -> RESET, and _do_reset_to_attract() replays the
        #    attract show, re-lighting the whole maze ~30 s after the operator
        #    was told the container was dark. _arm_timeout_task and
        #    _registered_timeout_task reach RESET the same way.
        for attr in ("_show_task", "_count_in_task", "_max_run_task",
                     "_result_timeout_task", "_arm_timeout_task",
                     "_registered_timeout_task", "_assisted_task",
                     "_self_test_task", "_deferred_preset_task"):
            self._cancel_task(attr)
        self._show_name = None

        # 2. Settle an in-flight run so it is recorded, not lost. Invariant 7:
        #    this makes the row truthful, it does not make it resumable.
        if self.context.run_id and self.state in RUN_STATES:
            await step("settle run in progress", self.dispatch(GmAbort()),
                       f"run {self.context.run_id} saved as aborted")
        else:
            report.append({"step": "settle run in progress", "ok": True,
                           "detail": "no run in progress"})

        # 3. Save. Before anything goes dark, so a failure here is still
        #    recoverable with the lights on.
        #    Event rows are written fire-and-forget (invariant 6: the game path
        #    never waits on the DB), so some are still in flight — including the
        #    ones the abort above just queued. Drain them, or a shutdown is
        #    exactly what loses the last rows of the day.
        if self._event_writes:
            pending = set(self._event_writes)
            await step("flush pending event rows",
                       asyncio.wait(pending, timeout=5.0),
                       f"{len(pending)} row(s)")
        if snapshot and self.db is not None:
            from persist.backup import export_snapshot, snapshot_dir
            await step("database snapshot",
                       export_snapshot(self.db, snapshot_dir()))
        # Deliberately NOT closing the database here. Stopping the service is
        # what closes it, cleanly, on its own shutdown path — and closing it
        # while the process keeps serving leaves a box that is up, answering,
        # and unable to do anything, recoverable only over ssh.

        # 4. Lasers.
        self._powered_down = True          # latch before darkening, not after
        if self._resolver is not None and self.io is not None:
            await step("lasers off",
                       asyncio.wait_for(self._resolver.apply_all_off(self.io),
                                        timeout=3.0),
                       "all coils off")

        # 5. Haze and the maze lights. The entrance stays lit for now.
        if self.lights is not None:
            try:
                self.lights.set_work_lights(False)
                self.lights.stop()
            except Exception as exc:
                log.warning("power_down: stopping light cues failed: %s", exc)
        if self.audio is not None:
            try:
                self.audio.stop()
                report.append({"step": "audio stopped", "ok": True, "detail": ""})
            except Exception as exc:
                log.warning("power_down: stopping audio failed: %s", exc)
        hazer = getattr(self, "hazer", None)
        if hazer is not None and hasattr(hazer, "blackout"):
            hazer.blackout()
            report.append({"step": "haze and maze lights off", "ok": True,
                           "detail": "entrance still lit"})

        # 6. The entrance, last. This is the only sanctioned override of the
        #    always_on guard, and it exists because the operator is standing at
        #    the breaker and wants the box actually dark.
        if hazer is not None and hasattr(hazer, "power_down"):
            hazer.power_down()
            report.append({"step": "entrance light off", "ok": True,
                           "detail": "container dark"})

        # 7. Only once the container is dark: stop the units, then halt.
        #    Never halt on a sequence that reported a problem — something may
        #    still be energised, and a halted box cannot be asked about it.
        ok = all(r["ok"] for r in report)
        halting = False
        if poweroff:
            if ok:
                detail = self._schedule_halt()
                halting = detail is not None
                report.append({"step": "stopping services, then halting",
                               "ok": halting,
                               "detail": detail or "could not schedule the halt"})
                ok = ok and halting
            else:
                report.append({
                    "step": "stopping services, then halting", "ok": False,
                    "detail": "SKIPPED — a step above failed; the box stays up "
                              "so you can see what",
                })

        if not halting:
            # The box keeps running, so it must stay usable. Hand the lights
            # back, or the only way out of a dark container is ssh.
            self._powered_down = False
            if self.lights is not None and hasattr(self.lights, "reset"):
                try:
                    self.lights.reset()
                except Exception as exc:
                    log.warning("power_down: could not re-arm light cues: %s", exc)

        log.warning("POWER DOWN complete — container is dark"
                    if ok else "POWER DOWN finished WITH PROBLEMS — check the container")
        return {"ok": ok, "steps": report, "halting": halting}

    def _schedule_halt(self) -> str | None:
        """
        Stop the kiosk, then the game, then halt — detached from this process.

        Detached on purpose: the second command kills the very process that
        issued it. systemd-run puts the sequence in its own transient unit,
        outside this service's cgroup, so stopping the service cannot take the
        halt down with it.

        Kiosk first. scanmania-kiosk has Wants=scanmania.service, so stopping
        the game on its own gets it dragged straight back up within five
        seconds.

        Stopping the service rather than halting out from under it is what
        closes the database cleanly — __main__ blacks out, snapshots and closes
        on its way down. This is why power_down() does not close it itself.

        Returns a description of what was scheduled, or None if nothing was.
        """
        import shutil
        import subprocess

        seq = "systemctl stop scanmania-kiosk; systemctl stop scanmania; systemctl poweroff"
        if shutil.which("systemd-run"):
            cmd = ["systemd-run", "--no-block", "--collect",
                   "--unit=scanmania-poweroff", "/bin/sh", "-c", seq]
            what = "kiosk, then game, then poweroff"
        elif shutil.which("systemctl"):
            # systemd stops units in reverse dependency order during a halt, and
            # the kiosk is After=scanmania, so it still goes down first.
            cmd = ["systemctl", "poweroff"]
            what = "systemctl poweroff (systemd stops the units first)"
        else:
            log.error("power_down: no systemctl on this box — cannot halt")
            return None

        try:
            subprocess.Popen(cmd, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            log.error("power_down: could not schedule the halt: %s", exc)
            return None
        log.warning("POWER DOWN: halt scheduled — %s", what)
        return what

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    def _arm_if_plate_already_down(self) -> None:
        """
        Enqueue a synthetic PlateHigh when the plate is already held down.

        Only ever called on entering REGISTERED. It is a nudge, not a new
        source of truth: if the backend cannot say (no such method, link down,
        nothing polled yet) it does nothing and the real edge still works. The
        FSM applies its own guards to the event exactly as it would a real one.
        """
        level = None
        try:
            getter = getattr(self.inputs, "input_level", None)
            if callable(getter):
                level = getter("plate")
        except Exception as exc:
            log.warning("could not read the plate level: %s", exc)
            return
        if level != 1:
            return
        log.info("plate already down at sign-in — arming without a fresh step")
        try:
            self._event_queue.put_nowait(PlateHigh())
        except Exception as exc:
            log.warning("could not enqueue the synthetic PlateHigh: %s", exc)

    async def dispatch(self, event: Any) -> None:
        """
        Feed an event to the FSM, execute resulting side effects, and handle
        automatic RESET → ATTRACT transition.

        Thread-safe: external producers should call put_event() instead.
        """
        # FORCE RESET is the documented escape hatch from every state, and that
        # has to include a container darkened by power_down() but still
        # running: otherwise the only way back is ssh, from a box whose whole
        # point is that the operator is standing in front of it.
        if isinstance(event, GmForceReset) and self._powered_down:
            self._powered_down = False
            if self.lights is not None and hasattr(self.lights, "reset"):
                try:
                    self.lights.reset()
                except Exception as exc:
                    log.warning("could not re-arm light cues: %s", exc)
            if self.audio is not None and hasattr(self.audio, "reset"):
                try:
                    self.audio.reset()
                except Exception as exc:
                    log.warning("could not re-arm audio cues: %s", exc)
            log.warning("FORCE RESET after a power down — the box is live again "
                        "(haze stays off until the GM turns it back on)")

        old_state = self.state
        new_state, side_effects = transition(self.state, event, self.context)
        self.state = new_state

        log.debug("FSM %s --[%s]--> %s", old_state, type(event).__name__, new_state)

        if new_state != old_state:
            self._cue_lights(new_state)
            self._cue_audio(new_state)

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
            # The player is usually already standing on the plate by the time
            # the GM finishes typing their name. Inputs are edge-triggered, so
            # a plate that is ALREADY down never sends another PlateHigh and
            # the FSM sat in REGISTERED waiting for one — the player had to
            # step off and back on, with a queue watching, for no reason they
            # could see.
            #
            # Ask for the level instead of waiting for an edge. Enqueued rather
            # than dispatched: we are inside dispatch() already, and the drain
            # is what serialises events.
            self._arm_if_plate_already_down()
        if new_state != "REGISTERED":
            self._cancel_task("_registered_timeout_task")

        # Any state that ends a run must drop the run budget with it.
        #
        # _max_run_deadline_ns was cleared only in _do_reset_to_attract(), and
        # MasterModeExit -> SELF_TEST -> ATTRACT never passes through RESET. So
        # after a GM used MASTER mid-run, the next player inherited a deadline
        # already in the past: remaining_ms computed to 0 and their run aborted
        # the instant it started, in front of the queue.
        if new_state not in RUN_STATES and new_state != "COUNTDOWN":
            self._max_run_deadline_ns = None

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
        self._cue_lights(ATTRACT)
        self._cue_audio(ATTRACT)
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

    async def recalibrate(self, mazes: list[str] | None = None,
                          apply: bool = False) -> dict:
        """
        Re-find every dot ROI from the live cameras, maze by maze.

        This is the "the container has been moved" operation. It does NOT
        re-tune: the per-camera thr/tophat/min_area were set by hand against
        this container's lighting and are carried forward untouched. A move
        changes where the dots ARE; it does not change what a dot looks like.
        Tuning still belongs in tools/capture.py with the game stopped.

        Dry run by default. With apply=False it reports what it WOULD write and
        changes nothing, which is the form to use after a move to answer "do I
        actually need to recalibrate?" without betting the day's calibration on
        the answer.

        Refuses unless the box is in MASTER. A recapture lights each maze in
        turn and takes several seconds per shape; doing that around a player is
        not something an accidental click should be able to cause.
        """
        from vision.recalibrate import (_MAX_AMBIENT_BLOBS, ambient_blobs,
                                        recapture_maze)

        report: dict = {"ok": False, "applied": False, "mazes": {}, "notes": []}

        if self.state != "MASTER":
            report["notes"].append(
                f"refused: the box is in {self.state}. Recalibration lights each "
                f"maze for several seconds — put it in MASTER MODE first.")
            return report

        streams = getattr(self.vision, "_streams", None) or {}
        if not streams:
            report["notes"].append("refused: no camera streams (vision is not running)")
            return report

        existing = {}
        try:
            existing = (self.config.beams.mazes or {}) if self.config else {}
        except Exception:
            existing = {}

        params = {cid: (blk or {}).get("params") or {}
                  for maze in existing.values()
                  for cid, blk in (maze.get("cameras") or {}).items()}

        targets = mazes or [m for m in ("maze_1", "maze_2", "maze_3")]

        # The room must be dark for this, and that includes the ENTRANCE.
        # It is the brightest fixture in the box and it points straight down
        # the container, so leaving it lit does not merely dim the dots — the
        # ambient gate below counts its reflections as blobs and refuses the
        # whole calibration. Nothing else can switch it off: it is marked
        # always_on and the DMX layer refuses to dim it.
        #
        # Sanctioned because MASTER is required to get here, so the only person
        # who could be inside is the GM who clicked this, and it is put back in
        # the finally below on every path out — refusal, exception or clean save.
        _prev_work = None
        _dimmed: list[tuple[str, int]] = []
        _hazer = getattr(self, "hazer", None)
        if self.lights is not None:
            try:
                _prev_work = self.lights.work_lights
                self.lights.set_work_lights(False)
            except Exception as exc:
                log.warning("recalibrate: could not suspend the work lights: %s", exc)
        if _hazer is not None and hasattr(_hazer, "lights_state"):
            try:
                for _n, _st in _hazer.lights_state().items():
                    if not _st.get("always_on"):
                        continue
                    # Remember the level it is ON, not its configured default —
                    # that is what "put it back" has to mean.
                    _dimmed.append((_n, int(_st.get("target", 255))))
                    _hazer.set_light(_n, 0, fade=False, allow_always_on=True)
                if _dimmed:
                    log.info("recalibrate: %s dark for the capture",
                             ", ".join(n for n, _ in _dimmed))
            except Exception as exc:
                log.warning("recalibrate: could not dim the entrance: %s", exc)

        try:
            # Ambient gate. Every ROI captured with the house lights on is wrong,
            # and the person who left them on is the same person clicking this.
            if self._resolver and self.io:
                await self._resolver.apply_all_off(self.io)
            await asyncio.sleep(0.8)
            amb = await ambient_blobs(streams, params)
            worst = max(amb.values()) if amb else 0
            if worst > _MAX_AMBIENT_BLOBS:
                report["ambient"] = amb
                report["notes"].append(
                    f"refused: {worst} blobs visible with every laser OFF (limit "
                    f"{_MAX_AMBIENT_BLOBS}). House lights on, or a door open — "
                    f"recalibrating now would map reflections as dots.")
                return report
            report["ambient"] = amb

            settle = getattr(getattr(self.config, "game", None), "preset_settle_ms", 800)
            for maze in targets:
                try:
                    if self._resolver and self.io:
                        await self._resolver.apply_preset(maze, self.io)
                    await asyncio.sleep(max(1.0, settle / 1000.0))
                    prev = ((existing.get(maze) or {}).get("cameras") or {})
                    cams = await recapture_maze(streams, params, previous=prev)
                except Exception as exc:
                    log.error("recalibrate: %s failed: %s", maze, exc)
                    report["mazes"][maze] = {"error": str(exc)}
                    continue

                per_cam = {}
                for cid, blk in cams.items():
                    was = len((prev.get(cid) or {}).get("dots") or [])
                    now = len(blk["dots"])
                    blind = sum(1 for d in blk["dots"] if not d["baseline"])
                    per_cam[cid] = {"was": was, "now": now, "blind": blind,
                                    "delta": now - was}
                report["mazes"][maze] = {"cameras": per_cam,
                                         "total_was": sum(c["was"] for c in per_cam.values()),
                                         "total_now": sum(c["now"] for c in per_cam.values()),
                                         "_candidate": cams}

            if self._resolver and self.io:
                await self._resolver.apply_all_off(self.io)

            # Believability gate. A recapture that loses a quarter of the dots is
            # far more likely to be someone standing in the maze, a door open, or a
            # camera that dropped out than a real change of that size — and saving
            # it would replace a working calibration with a broken one.
            lost = [f"{m}: {d['total_was']} -> {d['total_now']}"
                    for m, d in report["mazes"].items()
                    if "_candidate" in d and d["total_was"]
                    and d["total_now"] < d["total_was"] * 0.75]
            if lost:
                report["notes"].append(
                    "NOT saved: dot count fell by more than a quarter (" +
                    "; ".join(lost) + "). Check nobody is in the container and the "
                    "lasers are all on, then run it again.")
                apply = False

            report["ok"] = bool(report["mazes"]) and not lost
            if apply and report["ok"]:
                applied = await self._write_calibration(report)
                report["applied"] = applied
                if applied:
                    report["notes"].append("saved; detection reloaded")

            for d in report["mazes"].values():
                d.pop("_candidate", None)
            return report
        finally:
            # The way out gets its light back first, and on every path.
            for _n, _lvl in _dimmed:
                try:
                    _hazer.set_light(_n, _lvl, fade=False, allow_always_on=True)
                except Exception as exc:
                    log.error("recalibrate: FAILED to relight '%s': %s", _n, exc)
            if self.lights is not None and _prev_work is not None:
                try:
                    self.lights.set_work_lights(_prev_work)
                except Exception as exc:
                    log.warning("recalibrate: could not restore the work lights: %s",
                                exc)

    async def _write_calibration(self, report: dict) -> bool:
        """Back up beams.json, write the new ROIs, reload detection."""
        import json
        import shutil
        from pathlib import Path
        try:
            path = Path(self._config_dir) / "beams.json" if getattr(
                self, "_config_dir", None) else Path("config/beams.json")
            doc = json.loads(path.read_text())
            # The only copy of a working calibration is the one on disk.
            backup = path.with_suffix(f".json.bak-{int(time.time())}")
            shutil.copy2(path, backup)
            doc.setdefault("mazes", {})
            for maze, d in report["mazes"].items():
                if "_candidate" in d:
                    doc["mazes"].setdefault(maze, {})["cameras"] = d["_candidate"]
            tmp = path.with_suffix(".json.new")
            tmp.write_text(json.dumps(doc, indent=2))
            tmp.replace(path)
            log.warning("recalibrate: wrote %s (previous kept at %s)", path, backup)
        except Exception as exc:
            log.error("recalibrate: could not write beams.json: %s", exc)
            report["notes"].append(f"write failed: {exc}")
            return False
        try:
            await self.reload_config()
        except Exception as exc:
            report["notes"].append(f"saved, but reload failed ({exc}) — restart the service")
        return True

    def _refuse_after_power_down(self, what: str) -> bool:
        """
        True when the container has been darkened and must stay dark.

        power_down() latches this before it drives the coils off. Guarding only
        the light and audio cues was not enough: a timer that survived the
        shutdown could still reach PlayShow/ApplyPreset and re-energise all 45
        segments while the operator was walking away from a box they had been
        told was safe.
        """
        if self._powered_down:
            log.warning("refusing %s — the container has been powered down", what)
            return True
        return False

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
        if self._refuse_after_power_down(f"ApplyPreset({effect.preset_name!r})"):
            return

        delay_ms = 0
        if getattr(effect, "defer", False):
            delay_ms = getattr(getattr(self.config, "game", None),
                               "checkpoint_shape_delay_ms", 0) if self.config else 0
        if delay_ms > 0:
            # Scheduled, not awaited: the event drain is single-consumer, and
            # sleeping in it would stall the stop button and every beam event
            # for the duration.
            self._cancel_task("_deferred_preset_task")
            self._deferred_preset_task = asyncio.create_task(
                self._apply_preset_after(effect.preset_name, delay_ms),
                name="deferred_preset")
            return

        self._apply_maze(effect.preset_name)
        if self._resolver and self.io:
            try:
                await self._resolver.apply_preset(effect.preset_name, self.io)
            except KeyError:
                log.warning("ApplyPreset: unknown preset '%s' — skipping", effect.preset_name)

    async def _apply_preset_after(self, preset_name: str, delay_ms: int) -> None:
        """
        Wait, then switch the maze — detector and coils TOGETHER.

        The pairing is the whole correctness argument. _apply_maze points the
        detector at the dots captured for a preset, and the normal path calls it
        just before the write so the settle window covers the lasers physically
        coming on. Moving the detector early and the coils late would leave it
        watching dots that are not lit yet: they read dark, and the player is
        busted for a shape that has not appeared. So both happen here, after
        the wait, and preset_settle_ms runs from that moment as before.

        The OLD shape stays lit and watched during the wait, which is correct —
        the player is still in the container and those beams are still real.
        """
        try:
            await asyncio.sleep(delay_ms / 1000.0)
            if self._refuse_after_power_down(f"deferred ApplyPreset({preset_name!r})"):
                return
            self._apply_maze(preset_name)
            if self._resolver and self.io:
                try:
                    await self._resolver.apply_preset(preset_name, self.io)
                except KeyError:
                    log.warning("deferred ApplyPreset: unknown preset '%s'",
                                preset_name)
        except asyncio.CancelledError:
            raise

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
        if self._refuse_after_power_down(f"PlayShow({effect.show_name!r})"):
            return
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

    async def _apply_sparkle(self, preset_name: str, count: int, show: str) -> None:
        """
        Light `preset_name` with `count` random channels dropped.

        Goes through apply_channels(), which is a sanctioned writer, so the
        resolver's desired state stays truthful and the reconciler does not
        fight it back on (invariant 4).
        """
        preset = (self.config.mazes.presets or {}).get(preset_name) if self.config else None
        if preset is None:
            log.warning("Show '%s': unknown preset '%s' for sparkle", show, preset_name)
            return
        chans = preset.channels
        if isinstance(chans, str):          # '*' — every channel on every board
            width = getattr(self._resolver, "_channels_per_board", 16)
            n_boards = len(getattr(self._resolver, "_board_ids", []) or [])
            on = list(range(1, width * n_boards + 1))
        else:
            on = list(chans)
        if count and len(on) > count:
            for c in random.sample(on, count):
                on.remove(c)
        self._apply_maze(preset_name)
        try:
            await self._resolver.apply_channels(on, self.io)
        except Exception as exc:
            log.warning("Show '%s': sparkle write failed: %s", show, exc)

    async def _run_show(self, show: Any, name: str) -> None:
        """Execute a show's step sequence. Loops if show.loop is True."""
        try:
            while True:
                for step in show.steps:
                    preset_name = step.get("preset") if isinstance(step, dict) else getattr(step, "preset", None)
                    hold_ms = step.get("hold_ms", 300) if isinstance(step, dict) else getattr(step, "hold_ms", 300)
                    sparkle = (step.get("sparkle_off") if isinstance(step, dict)
                               else getattr(step, "sparkle_off", 0)) or 0
                    if sparkle and preset_name and self._resolver and self.io:
                        # Sparkle: the named preset, minus a few channels chosen
                        # afresh each step. Only those few relays move, so the
                        # wear is 2*sparkle per step rather than a full
                        # all-on/all-off cycle — which is what makes it safe to
                        # leave running all day.
                        await self._apply_sparkle(preset_name, int(sparkle), name)
                    elif preset_name and self._resolver and self.io:
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
        # What the ramp FLASHES — deliberately not the maze that will be lit at
        # GO. Players read a flash of the real shape as "go now" and start
        # early. Nothing measures a baseline during the ramp (runtime baselines
        # come from beams.json) and the FSM applies the real maze at GO before
        # detection arms, so flashing the whole grid changes nothing that
        # matters and removes the tell.
        flash_preset = getattr(self.config.game.count_in, "flash_preset",
                               count_in_preset) or count_in_preset
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
                    await self._resolver.apply_preset(flash_preset, self.io)
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

        # The stop button must not already be pressed at ARM.
        #
        # It is a NORMALLY CLOSED switch, which is the right choice — a cut
        # wire, pulled connector or dead contact reads as PRESSED, so the run
        # ends rather than becoming unstoppable. But that same property means a
        # broken stop circuit makes every run end the instant it starts, and
        # from the floor that looks like the game is simply broken with no
        # clue why. Say it here, once, before anyone queues up.
        try:
            states = dict(zip(getattr(self.inputs, "_input_map", {}).values(),
                              getattr(self.inputs, "_prev_states", [])))
        except Exception:
            states = {}
        if states.get("stop"):
            problems.append(
                "the stop button reads as PRESSED before the run started — it "
                "is normally closed, so this is usually a cut wire or an "
                "unplugged connector, not someone leaning on the button")

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
        if self._refuse_after_power_down("ReadyBlink"):
            return
        preset = self.config.game.count_in.preset
        blink_ms = self.config.game.count_in.ready_blink_ms
        # Point vision at the maze BEFORE preflight reads it. ARM's effects are
        # [ReadyBlink, BeamPreflightCheck] with no ApplyPreset between them, and
        # this handler writes coils through the resolver directly — so without
        # this line the detector was still aimed at whatever the attract show
        # last applied (all_on / blackout), neither of which has an ROI capture.
        # stats()["total"] was therefore 0 and preflight failed on every arm:
        # the game could not start a run on real hardware at all. Invisible in
        # tests because vision/fake.py reports a healthy dot count regardless.
        self._apply_maze(preset)
        try:
            await self._resolver.apply_preset(preset, self.io)
            await asyncio.sleep(blink_ms / 1000.0)
            # Leave the player boxed in rather than in the dark: house lights
            # and entrance are off by the ARM cue, so without these few beams
            # there is nothing to see at all from the plate. Falls back to
            # all-off if the preset is missing — ARM has no ApplyPreset, so
            # nothing else repairs _desired (invariant 4).
            arm_preset = getattr(self.config.game.count_in, "arm_preset", "arm_box")
            try:
                await self._resolver.apply_preset(arm_preset, self.io)
            except KeyError:
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
                # Only when a maze is actually lit. At idle — MASTER, ATTRACT —
                # no maze is armed and the detector is watching nothing BY
                # DESIGN, so reporting "detection is blind" made the fault list
                # non-empty for most of the day. Booting into MASTER put it on
                # screen at every startup. Same trap as the 'assisted' line
                # above: a permanent fault teaches operators to ignore faults.
                # An armed maze with no dots is still a real, run-ending fault.
                if vs.get("maze") and not vs.get("total"):
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
                    # Remember what we are taking, so recovery can put back
                    # exactly that and nothing else.
                    if self.context.detection_mode != "manual":
                        self._mode_before_stall = self.context.detection_mode
                        self._auto_dropped_to_manual = True
                    await self.put_event(VisionStalled())
                elif self._auto_dropped_to_manual:
                    # The recovery tuple was read and thrown away, so one
                    # 300 ms frame gap — a single keyframe hiccup on one of
                    # eight cameras — left the box in manual for the rest of
                    # the day, vision not policing runs, with a permanent fault
                    # on the console that teaches operators to ignore faults.
                    self._auto_dropped_to_manual = False
                    if self.context.detection_mode == "manual":
                        self.context.detection_mode = self._mode_before_stall or "assisted"
                        log.warning("vision recovered — detection back to %s",
                                    self.context.detection_mode)
                        metrics.emit("vision.recovered", 1.0)
            else:
                log.warning("vision_listener: unknown event kind %r", kind)

    async def _leaderboard_refresher(self) -> None:
        """
        Re-read the board when the operating day rolls over.

        The cache was written only on SaveRun/VoidRun, so after an unattended
        night the 09:00 rollover left yesterday's names and times broadcasting
        at 10 Hz to the street-facing display until somebody finished a run —
        potentially the whole morning queue, photographed.
        """
        from persist.db import day_bounds
        last_day = day_bounds()[0][:10]
        while True:
            await asyncio.sleep(60)
            try:
                today = day_bounds()[0][:10]
                if today != last_day:
                    last_day = today
                    await self._refresh_leaderboard()
                    log.info("operating day rolled to %s — leaderboard refreshed",
                             today)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("leaderboard refresh failed: %s", exc)

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
