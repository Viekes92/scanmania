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
import re
import shutil
import subprocess
import time
from typing import Callable

import cv2
import numpy as np

from config.loader import CameraConfig

log = logging.getLogger(__name__)

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

    Boot-time drift check: call check_drift() after connect() to compare the live
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
        self._last_frame_ns: int | None = None
        self._last_frame: np.ndarray | None = None
        self._stalled: bool = False
        self._fps_accumulator: list[float] = []  # inter-frame intervals (s)

    # ------------------------------------------------------------------
    # Main async loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Decode RTSP stream, deliver each frame to on_frame, and detect stalls.

        Runs until cancelled. Reconnects automatically on failure.
        """
        log.info("CameraStream '%s': starting, url=%s", self._config.id, self._config.url)
        while True:
            try:
                await self._open_and_read()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("CameraStream '%s': stream error (%s), reconnecting in 2 s", self._config.id, exc)
            if self._cap is not None:
                self._cap.release()
                self._cap = None
            await asyncio.sleep(2.0)

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

        cap = await loop.run_in_executor(None, self._open_capture)
        if not cap.isOpened():
            raise RuntimeError(f"failed to open '{self._config.url}'")

        self._cap = cap
        self._stalled = False
        log.info("CameraStream '%s': connected", self._config.id)

        while True:
            ret, frame = await loop.run_in_executor(None, cap.read)
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

            self._last_frame_ns = now_ns
            self._last_frame = frame

            self._on_frame(frame, now_ns, self._config.id)
            await asyncio.sleep(_FRAME_LOOP_SLEEP_S)

    # ------------------------------------------------------------------
    # Boot-time drift check
    # ------------------------------------------------------------------

    def check_drift(self, reference_path: str) -> float:
        """
        Compare the current live frame to the stored reference using phase correlation
        on the red channel. Returns the Euclidean shift in pixels.

        Raises RuntimeError if no frame has been decoded yet.
        Calls on_drift if shift > _DRIFT_THRESHOLD_PX.
        """
        if self._last_frame is None:
            raise RuntimeError(
                f"CameraStream '{self._config.id}': no frame available for drift check"
            )

        ref = cv2.imread(reference_path)
        if ref is None:
            raise RuntimeError(f"Could not load reference frame from '{reference_path}'")

        live = self._last_frame

        # Resize reference to match live if needed
        if ref.shape[:2] != live.shape[:2]:
            ref = cv2.resize(ref, (live.shape[1], live.shape[0]))

        # Red channel isolation
        ref_red = ref[:, :, 2].astype(np.float32)
        live_red = live[:, :, 2].astype(np.float32)

        shift, _ = cv2.phaseCorrelate(ref_red, live_red)
        drift_px = float(np.hypot(shift[0], shift[1]))

        log.info(
            "CameraStream '%s': drift check shift=(%.2f, %.2f) drift=%.2f px",
            self._config.id, shift[0], shift[1], drift_px,
        )

        if drift_px > _DRIFT_THRESHOLD_PX:
            log.warning(
                "CameraStream '%s': CAMERA_MOVED — drift=%.2f px (threshold=%.1f px)",
                self._config.id, drift_px, _DRIFT_THRESHOLD_PX,
            )
            self._on_drift(self._config.id, drift_px)

        return drift_px

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def fps(self) -> float:
        """Current decoded FPS (rolling average of last 60 inter-frame intervals)."""
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
