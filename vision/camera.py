"""
vision/camera.py — RTSP stream decoder with stall detection and boot-time drift check.

Inputs:  CameraConfig (url, reference_frame path), callbacks for frame/stall/drift events
Outputs: decoded numpy frames via on_frame callback; on_stall when frame gap > 300 ms;
         on_drift when phase correlation against reference frame exceeds 2 px
Invariant: decode happens exactly once here; frame gap > 300 ms = STALLED, no break
           events may be emitted. Boot-time drift check raises CAMERA_MOVED if > 2 px.
           Never opens the RTSP stream more than once (one decode, two consumers rule).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
import re
import shutil
import subprocess
import time
from typing import Callable

import cv2
import numpy as np

from config.loader import CameraConfig

log = logging.getLogger(__name__)

# Cameras get their own threads. See _open_and_read for why this must never be
# the default executor.
_CAMERA_POOL: ThreadPoolExecutor | None = None
_CAMERA_POOL_LOCK = threading.Lock()

_OPEN_TIMEOUT_S = 20.0      # RTSP connect + ffprobe; generous, but bounded
_READ_TIMEOUT_S = 5.0       # well past a 25 fps frame interval
_RECONNECT_MIN_S = 2.0
_RECONNECT_MAX_S = 30.0


# A SHARED pool was the wrong shape here, and dangerously so.
#
# asyncio.wait_for cancels the FUTURE, not the thread: a worker parked in a
# blocking cap.read() against a camera whose TCP session is up but silent stays
# parked. With one shared pool of 24 and a repeating failure loop — a PoE or
# VLAN outage, which is routine over a tour — every camera burned a worker
# every few seconds until the pool was exhausted in about two minutes. After
# that run_in_executor only QUEUED, so the HEALTHY cameras' reads never started
# either, timed out, reconnected, and queued more work. Vision died completely
# and never recovered when the network came back; only a restart fixed it.
#
# Now each camera owns a single-worker executor. A parked thread can starve
# only its own camera, never its neighbours, and the executor is RETIRED on
# timeout so the camera immediately gets a fresh working thread.
_RETIRE_LIMIT = 40          # abandoned threads per camera before we call it broken


class _CameraExecutor:
    """A single worker for one camera, replaceable when its thread is lost."""

    def __init__(self, cam_id: str) -> None:
        self._cam_id = cam_id
        self._pool = self._new_pool()
        self.retired = 0

    def _new_pool(self) -> ThreadPoolExecutor:
        return ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"cam-{self._cam_id}")

    @property
    def pool(self) -> ThreadPoolExecutor:
        return self._pool

    def retire(self, cap=None) -> None:
        """
        Abandon the current worker and start a fresh one.

        The old thread is still inside cap.read(); it will end when OpenCV's own
        socket timeout fires. Releasing the capture from THIS thread while that
        read is in flight is not thread-safe, so the release is queued onto the
        retiring executor instead: its single worker runs it after the parked
        read returns, which is exactly the ordering we need.
        """
        self.retired += 1
        old = self._pool
        if cap is not None:
            try:
                old.submit(cap.release)
            except Exception:
                pass
        old.shutdown(wait=False)
        self._pool = self._new_pool()
        if self.retired == _RETIRE_LIMIT:
            log.error("CameraStream '%s': %d abandoned reader threads — this "
                      "camera is not recovering; check the link or restart",
                      self._cam_id, self.retired)


def _camera_pool() -> ThreadPoolExecutor:
    """Kept for callers outside the stream; cameras use their own executors."""
    global _CAMERA_POOL
    with _CAMERA_POOL_LOCK:
        if _CAMERA_POOL is None:
            _CAMERA_POOL = ThreadPoolExecutor(
                max_workers=_CAMERA_POOL_SIZE, thread_name_prefix="cam"
            )
        return _CAMERA_POOL


_CAMERA_POOL_SIZE = 8

_STALL_THRESHOLD_MS = 300   # frame gap above this → STALLED
_DRIFT_THRESHOLD_PX = 2.0  # phase correlation shift above this → CAMERA_MOVED
_FRAME_LOOP_SLEEP_S = 0.001  # asyncio yield between blocking reads


# ---------------------------------------------------------------------------
# Frame source selection
#
# cv2.VideoCapture can only open an RTSP URL when the OpenCV wheel was built
# with FFmpeg. That is a build-time coin flip and it differs between the two
# machines this runs on: the Mac wheel reports FFMPEG: NO and fails in 0.0 s
# with isOpened() False, which looks exactly like a network fault. The Linux
# wheel on the NUC bundles FFmpeg, but the NUC has no ffmpeg binary installed.
#
# Supporting both covers both machines, and neither needs to know which.
# ---------------------------------------------------------------------------

def _cv2_has_ffmpeg() -> bool:
    m = re.search(r"FFMPEG:\s*(\w+)", cv2.getBuildInformation())
    return bool(m) and m.group(1).upper() == "YES"


class _FfmpegReader:
    """
    Decode RTSP through an ffmpeg subprocess into raw BGR frames.

    Same output contract as cv2.VideoCapture: read() -> (ok, frame). Used when
    the OpenCV build cannot open RTSP itself.
    """

    def __init__(self, url: str) -> None:
        self._w, self._h = self._probe_size(url)
        self._frame_bytes = self._w * self._h * 3
        self._proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-rtsp_transport", "tcp",
             "-fflags", "nobuffer", "-flags", "low_delay", "-avioflags", "direct",
             "-i", url, "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _probe_size(url: str) -> tuple[int, int]:
        """The raw pipe carries no header, so ffprobe has to supply W/H."""
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", url],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0 or "x" not in out.stdout:
            raise RuntimeError(f"ffprobe could not read '{url}': {out.stderr.strip()}")
        w, h = out.stdout.strip().split("x")[:2]
        return int(w), int(h)

    def isOpened(self) -> bool:          # noqa: N802 - mirrors the cv2 API
        return self._proc.poll() is None

    def read(self):
        buf = self._proc.stdout.read(self._frame_bytes)
        if not buf or len(buf) < self._frame_bytes:
            return False, None
        return True, np.frombuffer(buf, np.uint8).reshape(self._h, self._w, 3)

    def release(self) -> None:
        try:
            self._proc.kill()
            self._proc.wait(timeout=2)
        except Exception:
            pass


class CameraStream:
    """
    Decodes an RTSP stream using OpenCV and delivers frames to registered callbacks.

    Stall detection: if the gap between decoded frames exceeds _STALL_THRESHOLD_MS
    the on_stall callback fires and is_stalled becomes True until frames resume.

    No drift check. check_drift() was removed: it had ZERO callers, the ref/
    frames it compared against never existed, and the metric was wrong for the
    job anyway — a single global phase correlation is dominated by the static
    ceiling, while dot ROIs are 0.2-0.3% of the frame, so the per-dot drift
    ADR 0009 describes was invisible to it. Its 2 px threshold was also ~2x
    tighter than any real dot's tolerance (min 4.25 px, median 7.75 px).
    Geometry drift is checked by Admin -> Calibration -> Check, which compares
    found dots against the stored ROIs per dot.
    image against a stored reference frame. A shift > 2 px means the camera moved.
    """

    def __init__(
        self,
        camera_config: CameraConfig,
        on_frame: Callable,
        on_stall: Callable,
        on_drift: Callable,
    ) -> None:
        self._config = camera_config
        self._on_frame = on_frame    # (frame: np.ndarray, timestamp_ns: int, camera_id: str) → None
        self._on_stall = on_stall    # (camera_id: str) → None
        self._on_drift = on_drift    # (camera_id: str, drift_px: float) → None

        self._cap: cv2.VideoCapture | None = None
        self._exec = _CameraExecutor(camera_config.id)
        self._last_frame_ns: int | None = None
        self._last_frame: np.ndarray | None = None
        self._stalled: bool = False
        self._backoff_s: float = _RECONNECT_MIN_S
        self._fps_accumulator: list[float] = []  # inter-frame intervals (s)

    # ------------------------------------------------------------------
    # Main async loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Decode RTSP stream, deliver each frame to on_frame, and detect stalls.

        Runs until cancelled. Reconnects with capped backoff on failure.
        """
        log.info("CameraStream '%s': starting, url=%s", self._config.id, self._config.url)
        # Reset lives on the instance and is done by _open_and_read the moment
        # a frame actually arrives.
        #
        # It used to sit on the line after `await self._open_and_read()`, which
        # is unreachable: that method contains no return statement at all, only
        # raises, so it can never fall through. The backoff therefore doubled
        # 2 -> 4 -> 8 -> 16 -> 30 and NEVER reset for the life of the process.
        # By late afternoon a single blip cost ~36 s of reconnect wait — and
        # because one stalled camera suppresses the whole detector, that is the
        # entire fleet blind for longer than a run.
        self._backoff_s = _RECONNECT_MIN_S
        try:
            while True:
                try:
                    await self._open_and_read()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("CameraStream '%s': stream error (%s), "
                                "reconnecting in %.0f s",
                                self._config.id, exc, self._backoff_s)
                self._release()
                await asyncio.sleep(self._backoff_s)
                # Capped backoff. Eight cameras retrying flat-out every 2 s
                # after a switch reboot is a connect storm that also occupies a
                # reader thread per attempt.
                self._backoff_s = min(self._backoff_s * 2, _RECONNECT_MAX_S)
        finally:
            # A cancelled task used to jump straight past the release, orphaning
            # an ffmpeg subprocess that kept its RTSP session — which is what
            # made the NEXT run fail to connect.
            self._release()

    @property
    def last_frame(self):
        """The most recent decoded frame, or None. Read-only view for callers
        that need to look at what this camera is seeing right now — the
        in-process recalibration, which is why it no longer needs the game
        stopped."""
        return self._last_frame

    @property
    def abandoned_threads(self) -> int:
        """Reader threads lost to timeouts. Non-zero means a flaky link."""
        return self._exec.retired

    def _release(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception as exc:
                log.debug("CameraStream '%s': release failed: %s",
                          self._config.id, exc)

    def _open_capture(self):
        """Open the stream with whichever backend this machine can actually use."""
        if _cv2_has_ffmpeg():
            return cv2.VideoCapture(self._config.url)
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError(
                "this OpenCV build has FFMPEG: NO and no ffmpeg/ffprobe binary was "
                "found, so there is no way to open an RTSP stream. Install ffmpeg, "
                "or install an OpenCV wheel built with FFmpeg."
            )
        log.info("CameraStream '%s': cv2 has no FFmpeg — using an ffmpeg subprocess",
                 self._config.id)
        return _FfmpegReader(self._config.url)

    async def _open_and_read(self) -> None:
        loop = asyncio.get_running_loop()
        log.info("CameraStream '%s': opening %s", self._config.id, self._config.url)

        # Own executor, never the default one. Eight cameras each holding a
        # worker in a blocking read exactly fills the default pool on a 4-core
        # box, and everything else in the process shares it: every Modbus coil
        # write, and the 20 Hz Opta poll that carries the stop button. Modbus
        # times out while merely QUEUED and marks boards DEGRADED, so camera
        # traffic could take the maze and the stop button down without a single
        # network fault.
        pool = self._exec.pool

        try:
            cap = await asyncio.wait_for(
                loop.run_in_executor(pool, self._open_capture),
                timeout=_OPEN_TIMEOUT_S)
        except asyncio.TimeoutError:
            # The worker is still inside the connect. Abandon it and take a
            # fresh thread, or this camera can never retry.
            self._exec.retire()
            raise
        if not cap.isOpened():
            # Assign before the check, or this capture leaks: run()'s release
            # only ever saw the PREVIOUS value.
            self._cap = cap
            raise RuntimeError(f"failed to open '{self._config.url}'")

        self._cap = cap
        self._stalled = False
        log.info("CameraStream '%s': connected", self._config.id)

        while True:
            # A camera that freezes with its TCP session open blocks cap.read()
            # forever: no exception, so the reconnect below is never reached and
            # the worker is gone for the life of the process. The deadline turns
            # that into an ordinary reconnect.
            try:
                ret, frame = await asyncio.wait_for(
                    loop.run_in_executor(pool, cap.read),
                    timeout=_READ_TIMEOUT_S)
            except asyncio.TimeoutError:
                # Hand the capture to the retiring worker to release, so the
                # release runs after the parked read returns rather than racing
                # it, and drop our reference so _release() cannot double-free.
                self._cap = None
                self._exec.retire(cap)
                pool = self._exec.pool
                raise
            if not ret or frame is None:
                raise RuntimeError("cap.read() returned False — stream ended or dropped")

            now_ns = time.monotonic_ns()

            # Stall detection — check gap since last frame
            if self._last_frame_ns is not None:
                gap_ms = (now_ns - self._last_frame_ns) / 1_000_000
                if gap_ms > _STALL_THRESHOLD_MS and not self._stalled:
                    log.warning(
                        "CameraStream '%s': STALLED (gap=%.0f ms)",
                        self._config.id, gap_ms,
                    )
                    self._stalled = True
                    self._on_stall(self._config.id)
                elif gap_ms <= _STALL_THRESHOLD_MS and self._stalled:
                    log.info("CameraStream '%s': stall cleared", self._config.id)
                    self._stalled = False

                # Rolling FPS (capped at last 60 samples)
                self._fps_accumulator.append(gap_ms / 1000.0)
                if len(self._fps_accumulator) > 60:
                    self._fps_accumulator.pop(0)

            # A frame arrived, so this connection works: forget the backoff
            # that got us here. Anything else leaves it ratcheted up forever.
            self._backoff_s = _RECONNECT_MIN_S
            self._last_frame_ns = now_ns
            self._last_frame = frame

            self._on_frame(frame, now_ns, self._config.id)
            await asyncio.sleep(_FRAME_LOOP_SLEEP_S)

    # ------------------------------------------------------------------
    # Boot-time drift check
    # ------------------------------------------------------------------

    @property
    def fps(self) -> float:
        """
        Current decoded FPS (rolling average of last 60 inter-frame intervals).

        A PROPERTY, like is_stalled and last_frame_ns below it. It was a plain
        method while every one of its three consumers in vision/service.py read
        it as an attribute, so detector_stats() raised TypeError on
        round(stream.fps, 1) — inside _get_state_message(), which meant EVERY
        BroadcastState failed and the GM console and both displays went stale
        while the game itself ran on normally.
        """
        if not self._fps_accumulator:
            return 0.0
        avg_interval = sum(self._fps_accumulator) / len(self._fps_accumulator)
        return 1.0 / avg_interval if avg_interval > 0 else 0.0

    @property
    def is_stalled(self) -> bool:
        return self._stalled

    @property
    def last_frame_ns(self) -> int | None:
        return self._last_frame_ns
