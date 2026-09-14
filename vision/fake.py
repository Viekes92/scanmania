"""
vision/fake.py — fake camera and detection backend for development without hardware.

Inputs:  programmatic trigger calls from fake_run.py or test code
Outputs: same on_break / on_clear callbacks as DotDetector; arm/disarm state
Invariant: MANDATORY. Allows the entire game to run on a laptop with no camera or
           lasers attached. Implements the DotDetector interface so runner.py accepts
           either. Also supports replay mode: play a recorded video file through the
           real DotDetector pipeline (see tools/replay.py).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from config.loader import BeamsConfig

log = logging.getLogger(__name__)

# Plausible dot count so preflight sees a calibrated maze.
_FAKE_DOTS = 174


class FakeVision:
    """
    Drop-in replacement for the real CameraStream + DotDetector combination.

    No frames are decoded. Break and clear events are injected programmatically
    via trigger_break() and trigger_clear(), which call the same callbacks that
    the real pipeline would call.

    is_stalled is always False; fps is reported as 30.0.
    """

    def __init__(
        self,
        beams_config: BeamsConfig,
        on_break: Callable | None = None,
        on_clear: Callable | None = None,
        metrics_emit: Callable | None = None,
    ) -> None:
        self._beams_config = beams_config
        self._on_break = on_break       # (beam_id: str, ratio: float, ts_ns: int) → None
        self._on_clear = on_clear       # (beam_id: str, ts_ns: int) → None
        self._metrics_emit = metrics_emit

        self._armed: bool = False
        self._maze: str | None = None
        self._run_id: str | None = None
        self._arm_time_ns: int | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=256)

        # Build a quick lookup of known beam IDs for validation
        self._beam_ids: frozenset[str] = frozenset(b.id for b in beams_config.beams)

    # ------------------------------------------------------------------
    # Main loop — no-op
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        No-op loop — keeps the coroutine alive for runner.py.
        Events are injected via trigger_break() / trigger_clear().
        """
        log.info("FakeVision: running (no hardware; events injected via trigger_break/clear)")
        while True:
            await asyncio.sleep(3600)

    # ------------------------------------------------------------------
    # Injection API (fake_run.py and tests)
    # ------------------------------------------------------------------

    async def trigger_break(self, beam_id: str, ratio: float = 0.2) -> None:
        """
        Simulate a confirmed beam break.

        Only fires the callback if detection is armed and the beam_id is known.
        ratio defaults to 0.2 (well below any typical break_ratio of 0.4).
        """
        if beam_id not in self._beam_ids:
            log.warning("FakeVision.trigger_break: unknown beam_id '%s'", beam_id)
            return
        if not self._armed:
            log.debug("FakeVision.trigger_break: not armed — ignoring beam '%s'", beam_id)
            return

        ts_ns = time.monotonic_ns()
        log.info(
            "FakeVision: trigger_break beam='%s' ratio=%.2f run='%s'",
            beam_id, ratio, self._run_id,
        )
        if self._metrics_emit:
            self._metrics_emit(
                "break.detected",
                value=ratio,
                tags={"beam_id": beam_id, "run_id": self._run_id or "", "source": "fake"},
            )
        self._queue.put_nowait(("break", beam_id, ratio, ts_ns))
        if self._on_break:
            self._on_break(beam_id, ratio, ts_ns)

    async def trigger_clear(self, beam_id: str) -> None:
        """Simulate a beam clearing (dot reappears)."""
        if beam_id not in self._beam_ids:
            log.warning("FakeVision.trigger_clear: unknown beam_id '%s'", beam_id)
            return
        ts_ns = time.monotonic_ns()
        log.info("FakeVision: trigger_clear beam='%s'", beam_id)
        self._queue.put_nowait(("clear", beam_id, ts_ns))
        if self._on_clear:
            self._on_clear(beam_id, ts_ns)

    async def events(self):
        """Async generator yielding vision events as tuples.

        Break: ("break", beam_id, ratio, ts_ns)
        Clear: ("clear", beam_id, ts_ns)
        """
        while True:
            yield await self._queue.get()

    # ------------------------------------------------------------------
    # Arm / disarm — mirror DotDetector interface
    # ------------------------------------------------------------------

    def arm(self, run_id: str, grace_ms: int) -> None:
        self._armed = True
        self._run_id = run_id
        self._arm_time_ns = time.monotonic_ns()
        log.info("FakeVision: armed for run='%s' grace_ms=%d", run_id, grace_ms)

    def disarm(self) -> None:
        self._armed = False
        self._run_id = None
        self._arm_time_ns = None
        log.info("FakeVision: disarmed")

    # ------------------------------------------------------------------
    # Properties — mirror DotDetector / CameraStream interface
    # ------------------------------------------------------------------

    def set_maze(self, preset: str | None, settle_ms: int = 250) -> None:
        """Recorded so the runner can call it unconditionally. No dots to watch."""
        self._maze = preset

    def set_masked(self, dot_id: str, masked: bool = True) -> bool:
        """No real dots to mask; report success so admin calls do not error."""
        return True

    def detector_stats(self) -> dict:
        """
        Report a healthy, calibrated detector.

        The fake exists so the whole game can be driven with no hardware, and
        preflight now refuses to arm a run when the lit maze has no calibrated
        dots. A fake that reported zero would block the --fake-all workflow for
        a condition that cannot exist without cameras.
        """
        return {
            "maze": self._maze,
            "total": _FAKE_DOTS,
            "blind": 0,
            "stalled": False,
            "fault": None,
            "cameras_live": 1,
            "cameras_total": 1,
            "cameras": {
                "fake_cam": {
                    "watched": _FAKE_DOTS, "blind": 0, "dark": 0, "masked": 0,
                    "stalled": False, "fps": 30.0, "age_ms": 0,
                }
            },
        }

    def clear_masks(self) -> int:
        """No real dots to unmask."""
        return 0

    @property
    def is_armed(self) -> bool:
        return self._armed

    @property
    def fps(self) -> float:
        """Always 30.0 — fake streams don't decode real frames."""
        return 30.0

    @property
    def is_stalled(self) -> bool:
        """Always False — fake streams never stall."""
        return False

    @property
    def last_frame_ns(self) -> int | None:
        """Returns current monotonic time — fake stream is always 'live'."""
        return time.monotonic_ns()
