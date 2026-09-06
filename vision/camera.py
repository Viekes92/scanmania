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
import time
from typing import Callable

import cv2
import numpy as np

from config.loader import CameraConfig

log = logging.getLogger(__name__)

_STALL_THRESHOLD_MS = 300   # frame gap above this → STALLED
_DRIFT_THRESHOLD_PX = 2.0  # phase correlation shift above this → CAMERA_MOVED
_FRAME_LOOP_SLEEP_S = 0.001  # asyncio yield between blocking reads


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

    async def _open_and_read(self) -> None:
        loop = asyncio.get_running_loop()
        log.info("CameraStream '%s': opening %s", self._config.id, self._config.url)

        cap = await loop.run_in_executor(
            None, lambda: cv2.VideoCapture(self._config.url)
        )
        if not cap.isOpened():
            raise RuntimeError(f"cv2.VideoCapture failed to open '{self._config.url}'")

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
