"""
vision/service.py — owns the real vision pipeline and presents it to the runner.

Input:  AppConfig (hardware.cameras + beams), RTSP frames from CameraStream
Output: async event tuples ("break"|"clear"|"stall", ...) consumed by
        core/runner.py::_vision_listener; MJPEG frames on :8081
Invariant: mirrors vision/fake.py's interface exactly — run(), events(), arm(),
           disarm(), is_armed(), fps(), is_stalled(), last_frame_ns(). The
           runner must not care which one it holds.
Invariant: detection is suppressed whenever any camera is stalled (invariant 5).
           A missed break is recoverable; a phantom bust in front of a queue is not.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from config.loader import AppConfig
from vision.baseline import BaselineManager
from vision.camera import CameraStream
from vision.detect import DotDetector
from vision.evidence import EvidenceCapture

# vision/mjpeg.py imports aiohttp, which is declared in neither requirements.txt
# nor pyproject.toml. Nothing ever noticed, because MjpegServer was never
# instantiated. Keep it optional so detection does not depend on a decision that
# has not been made yet: either add aiohttp, or serve MJPEG from the FastAPI app
# that is already running and drop both the dependency and the second port.
try:
    from vision.mjpeg import MjpegServer
    _MJPEG_AVAILABLE = True
except ImportError as _exc:  # pragma: no cover - depends on the environment
    MjpegServer = None  # type: ignore[assignment]
    _MJPEG_AVAILABLE = False
    _MJPEG_IMPORT_ERROR = _exc

log = logging.getLogger(__name__)

# How often the watchdog checks for a frame gap. Must be well under
# detection.stall_threshold_ms so the crossing is noticed promptly.
_WATCHDOG_INTERVAL_S = 0.1


class VisionService:
    """
    Constructs and connects CameraStream, DotDetector, BaselineManager,
    EvidenceCapture and MjpegServer.

    Nothing did this before. Each piece existed and was individually plausible,
    but no object owned them, so `from vision.camera import VisionService` in
    __main__.py raised ImportError and the whole real-camera path was dead.

    One decode, two consumers: each frame goes to the detector and to the MJPEG
    server. See docs/adr/0002.
    """

    def __init__(self, config: AppConfig, metrics_emit: Any = None) -> None:
        self._config = config
        self._queue: asyncio.Queue = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None

        if metrics_emit is None:
            import core.metrics as _metrics
            metrics_emit = _metrics.emit
        self._metrics_emit = metrics_emit

        self._baselines = BaselineManager(config.beams)
        self._detector = DotDetector(
            beams_config=config.beams,
            on_break=self._on_break,
            on_clear=self._on_clear,
            metrics_emit=metrics_emit,
        )
        for beam_id, value in self._baselines.all_baselines().items():
            self._detector.set_baseline(beam_id, value)

        self._evidence = EvidenceCapture()
        self._mjpeg = MjpegServer() if _MJPEG_AVAILABLE else None
        if self._mjpeg is None:
            log.warning(
                "MJPEG server unavailable (%s) — /display/out keeps its black "
                "background. Detection is unaffected.", _MJPEG_IMPORT_ERROR,
            )

        self._streams: dict[str, CameraStream] = {}
        for cam in config.hardware.cameras:
            self._streams[cam.id] = CameraStream(
                camera_config=cam,
                on_frame=self._on_frame,
                on_stall=self._on_camera_stall,
                on_drift=self._on_drift,
            )

        # Stall is tracked per camera. Any stalled camera suppresses detection.
        self._stalled_cameras: set[str] = set()
        # Cumulative per camera, so the admin page can show a flapping
        # feed that currently happens to be up.
        self._stall_counts: dict[str, int] = {}
        self._stall_threshold_ms = config.beams.detection.stall_threshold_ms
        self._last_emitted_stall: bool | None = None

    # ------------------------------------------------------------------
    # Camera callbacks — these run on the event loop thread
    # ------------------------------------------------------------------

    def _on_frame(self, frame, timestamp_ns: int, camera_id: str) -> None:
        """One decode, two consumers. Detection first, so evidence matches it."""
        if camera_id in self._stalled_cameras:
            # Frames are flowing again. The watchdog clears the flag.
            self._clear_stall(camera_id)

        self._evidence.push_frame(frame, timestamp_ns)
        # Route by camera: a beam's ROI is only meaningful in its own
        # camera's frame. Without this, four cameras sample every beam four
        # times, three of them against coordinates that mean nothing.
        self._detector.process_frame(frame, timestamp_ns, camera_id)
        # Ceiling frames deliberately do NOT go to MJPEG. cam_1-4 point straight
        # up at the dots — that view is meaningless to the crowd outside.
        # /display/out gets its own camera (the back cam at 172.16.0.205, aimed
        # at the play area), which is not a detection camera and must not be
        # fed to the detector either. See plan.md section 18.

    def _on_camera_stall(self, camera_id: str) -> None:
        """CameraStream noticed a gap between two frames it did receive."""
        self._mark_stall(camera_id)

    def _on_drift(self, camera_id: str, drift_px: float) -> None:
        log.warning("Camera '%s' drifted %.1f px from its reference frame",
                    camera_id, drift_px)
        self._metrics_emit("vision.camera_drift", drift_px, {"camera_id": camera_id})

    # ------------------------------------------------------------------
    # Stall handling
    #
    # CameraStream only ever notices a stall on the ARRIVAL of a frame, so the
    # two genuine no-frame cases bypass it: a blocking read that never returns,
    # and a read that fails and goes down the reconnect path (which also used to
    # clear the flag). The watchdog below is what actually catches those.
    # ------------------------------------------------------------------

    def _mark_stall(self, camera_id: str) -> None:
        if camera_id not in self._stalled_cameras:
            self._stalled_cameras.add(camera_id)
            self._stall_counts[camera_id] = self._stall_counts.get(camera_id, 0) + 1
            log.warning("Camera '%s' STALLED — suppressing detection", camera_id)
            self._metrics_emit("vision.stall", 1.0, {"camera_id": camera_id})
            self._sync_detector_stall()

    def _clear_stall(self, camera_id: str) -> None:
        if camera_id in self._stalled_cameras:
            self._stalled_cameras.discard(camera_id)
            log.info("Camera '%s' recovered", camera_id)
            self._metrics_emit("vision.stall", 0.0, {"camera_id": camera_id})
            self._sync_detector_stall()

    def _sync_detector_stall(self) -> None:
        """Any stalled camera suppresses the whole detector (invariant 5)."""
        stalled = bool(self._stalled_cameras)
        self._detector.set_stalled(stalled)
        if stalled != self._last_emitted_stall:
            self._last_emitted_stall = stalled
            self._queue.put_nowait(("stall", stalled, sorted(self._stalled_cameras)))

    async def _watchdog(self) -> None:
        """
        Catch the stalls that never produce a frame.

        Runs outside the frame path on purpose. A camera that freezes with the
        TCP connection open parks the read forever, so nothing inside the read
        loop can ever fire.
        """
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL_S)
            now_ns = time.monotonic_ns()
            for cam_id, stream in self._streams.items():
                last = stream.last_frame_ns
                if last is None:
                    continue  # never delivered a frame yet; startup, not a stall
                gap_ms = (now_ns - last) / 1_000_000
                if gap_ms > self._stall_threshold_ms:
                    self._mark_stall(cam_id)

    # ------------------------------------------------------------------
    # Detector callbacks
    # ------------------------------------------------------------------

    def _on_break(self, beam_id: str, ratio: float, ts_ns: int) -> None:
        self._queue.put_nowait(("break", beam_id, ratio, ts_ns))

    def _on_clear(self, beam_id: str, ts_ns: int) -> None:
        self._queue.put_nowait(("clear", beam_id, ts_ns))

    # ------------------------------------------------------------------
    # Lifecycle — mirrors vision/fake.py
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run every camera, the MJPEG server and the stall watchdog."""
        self._loop = asyncio.get_running_loop()
        if not self._streams:
            log.error("VisionService: no cameras configured — nothing to do")
            return

        tasks = [
            asyncio.create_task(s.run(), name=f"camera_{cid}")
            for cid, s in self._streams.items()
        ]
        if self._mjpeg is not None:
            tasks.append(asyncio.create_task(self._mjpeg.run(), name="mjpeg"))
        tasks.append(asyncio.create_task(self._watchdog(), name="vision_watchdog"))

        log.info("VisionService started: %d camera(s), MJPEG %s",
                 len(self._streams), "on :8081" if self._mjpeg else "disabled")

        # Same shape as GameRunner.run(): a dead camera must surface, not be
        # silently orphaned while the rest keeps pretending to work.
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

        failed = [t for t in done if not t.cancelled() and t.exception() is not None]
        for t in failed:
            log.critical("Vision task %s died: %r", t.get_name(), t.exception(),
                         exc_info=t.exception())
        for t in pending:
            t.cancel()
        if failed:
            raise failed[0].exception()  # type: ignore[misc]

    async def events(self):
        """
        Async generator of vision events.

        Break: ("break", beam_id, ratio, ts_ns)
        Clear: ("clear", beam_id, ts_ns)
        Stall: ("stall", stalled: bool, camera_ids: list[str])
        """
        while True:
            yield await self._queue.get()

    # ------------------------------------------------------------------
    # Arm / disarm — mirrors DotDetector and FakeVision
    # ------------------------------------------------------------------

    def arm(self, run_id: str, grace_ms: int) -> None:
        # Invariant: the baseline is frozen for the whole run. Adapting mid-run
        # slowly accepts a broken beam as normal.
        self._baselines.freeze()
        self._detector.arm(run_id, grace_ms)

    def disarm(self) -> None:
        self._detector.disarm()
        self._baselines.unfreeze()

    def is_armed(self) -> bool:
        return self._detector.is_armed

    def set_watchlist(self, channel_ids, settle_ms: int = 250) -> None:
        """
        Point detection at the channels the current maze shape has lit.

        Called on every preset change. See DotDetector.set_watchlist — this is
        what stops a shape change from reading as a mass beam break.
        """
        self._detector.set_watchlist(channel_ids, settle_ms)

    # ------------------------------------------------------------------
    # Telemetry — read by the admin hardware page
    # ------------------------------------------------------------------

    def fps(self) -> float:
        """Mean fps across cameras, so a single number matches FakeVision."""
        values = [s.fps for s in self._streams.values() if s.fps > 0]
        return sum(values) / len(values) if values else 0.0

    def is_stalled(self) -> bool:
        return bool(self._stalled_cameras)

    def last_frame_ns(self) -> int | None:
        values = [s.last_frame_ns for s in self._streams.values()
                  if s.last_frame_ns is not None]
        return max(values) if values else None

    def camera_stats(self, camera_id: str | None = None):
        """
        Per-camera telemetry for the admin hardware page.

        With a camera_id, returns that camera's dict — the shape
        web/routes_admin.py expects. Without one, returns every camera keyed by
        id, which is what the fakes and the tests use.
        """
        def one(cid: str) -> dict:
            st = self._streams[cid]
            return {
                "fps": round(st.fps, 1),
                "stalled": cid in self._stalled_cameras,
                "stall_count": self._stall_counts.get(cid, 0),
                "last_frame_ns": st.last_frame_ns,
            }
        if camera_id is not None:
            return one(camera_id) if camera_id in self._streams else {}
        return {cid: one(cid) for cid in self._streams}

    def save_evidence(self, beam_id: str, run_id: str) -> str | None:
        """Save a thumbnail of the beam's ROI at the moment it broke."""
        beam = next((b for b in self._config.beams.beams if b.id == beam_id), None)
        if beam is None:
            log.warning("save_evidence: unknown beam '%s'", beam_id)
            return None
        return self._evidence.save(beam_id, run_id, beam.roi)
