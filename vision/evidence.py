"""
vision/evidence.py — saves JPEG crops of break events for dispute resolution.

Inputs:  ring buffer of recent frames pushed via push_frame(); beam ROI; run_id; beam_id
Outputs: JPEG files at data/evidence/<run_id>/<beam_id>_<ts_ns>.jpg (triggering + 2 prior)
Invariant: saves the triggering frame plus the two immediately preceding frames.
           Disk write failures are logged and swallowed — evidence saving must never
           interrupt a run or raise an exception to the caller.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
import os
from collections import deque
from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np

from config.loader import BeamROI

log = logging.getLogger(__name__)


def _stamp() -> str:
    """UTC wall clock, for filenames that sort chronologically across reboots."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

_RING_BUFFER_SIZE = 10   # keep last 10 frames
_SAVE_FRAME_COUNT = 3    # triggering frame + 2 preceding


class _Frame(NamedTuple):
    frame: np.ndarray
    timestamp_ns: int


class EvidenceCapture:
    """
    Maintains a ring buffer of recent decoded frames and saves cropped JPEG
    evidence whenever a beam break is confirmed.

    Call push_frame() for every frame decoded by CameraStream.
    Call save() when DotDetector confirms a break.
    """

    def __init__(self, output_dir: str = "data/evidence") -> None:
        self._output_dir = Path(output_dir)
        # camera_id -> its own ring. See push_frame.

        self._buffers: dict[str, deque[_Frame]] = {}

    # ------------------------------------------------------------------
    # Frame ingestion
    # ------------------------------------------------------------------

    def push_frame(self, frame: np.ndarray, timestamp_ns: int,
                   camera_id: str = "") -> None:
        """
        Add a decoded frame to that camera's ring buffer.

        One shared buffer received ~200 frames/s from 8 interleaved cameras, so
        it held about 50 ms of history and consecutive entries came from
        different cameras — the "triggering frame plus 2 preceding" contract was
        never met, and a crop would come from whichever camera happened to be
        adjacent. One ring per camera fixes both.
        """
        buf = self._buffers.get(camera_id)
        if buf is None:
            buf = self._buffers[camera_id] = deque(maxlen=_RING_BUFFER_SIZE)
        buf.append(_Frame(frame=frame, timestamp_ns=timestamp_ns))

    @property
    def _frame_buffer(self) -> deque:
        """Legacy single-buffer view: the newest camera to deliver a frame."""
        if not self._buffers:
            return deque()
        return max(self._buffers.values(),
                   key=lambda b: b[-1].timestamp_ns if b else 0)

    def frames_for(self, camera_id: str) -> list:
        """The ring for one camera, oldest first."""
        return list(self._buffers.get(camera_id) or ())

    # ------------------------------------------------------------------
    # Evidence save
    # ------------------------------------------------------------------

    def save(self, beam_id: str, run_id: str, roi: BeamROI) -> str | None:
        """
        Save the last up to 3 frames (triggering + 2 preceding), each cropped to
        a region around the beam ROI. Returns the directory path on success, None
        on failure.

        The crop is the bounding box of the circular ROI padded by the radius on
        each side so context is visible.
        """
        try:
            return self._save_inner(beam_id, run_id, roi)
        except Exception as exc:
            log.error(
                "EvidenceCapture: save failed beam='%s' run='%s': %s — continuing",
                beam_id, run_id, exc,
            )
            return None

    def _save_inner(self, beam_id: str, run_id: str, roi: BeamROI) -> str | None:
        frames = list(self._frame_buffer)
        if not frames:
            log.warning("EvidenceCapture: no frames in buffer — cannot save evidence")
            return None

        # Take the last _SAVE_FRAME_COUNT frames (most recent last)
        to_save = frames[-_SAVE_FRAME_COUNT:]

        # Build output directory: data/evidence/<run_id>/
        out_dir = self._output_dir / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

        saved_paths: list[str] = []
        for i, fr in enumerate(to_save):
            crop = self._crop_roi(fr.frame, roi)
            label = "trigger" if i == len(to_save) - 1 else f"prior_{len(to_save) - 1 - i}"
            # Dot ids carry a colon (cam_3:d17). Legal in a POSIX filename and a
            # trap everywhere else — Windows, SMB, and any URL that is not
            # percent-encoded. Flatten it here, once.
            safe = beam_id.replace(":", "_").replace("/", "_")
            # Prefix with wall-clock UTC. timestamp_ns is monotonic — time since
            # BOOT — so with the container power-cycled nightly, day 2's first
            # bust got roughly the same number as day 1's and silently
            # overwrote yesterday's proof, exactly when someone disputes it.
            filename = f"{_stamp()}_{safe}_{fr.timestamp_ns}_{label}.jpg"
            path = out_dir / filename
            ok = cv2.imwrite(str(path), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                saved_paths.append(str(path))
                log.debug("EvidenceCapture: saved %s", path)
            else:
                log.warning("EvidenceCapture: cv2.imwrite failed for %s", path)

        if saved_paths:
            log.info(
                "EvidenceCapture: saved %d frame(s) for beam='%s' run='%s' → %s",
                len(saved_paths), beam_id, run_id, out_dir,
            )
            return str(out_dir)
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _crop_roi(frame: np.ndarray, roi: BeamROI) -> np.ndarray:
        """
        Crop the frame to a square region centred on the ROI, padded by one radius.
        Clamps to frame boundaries.
        """
        h, w = frame.shape[:2]
        pad = roi.r  # one radius of padding around the circle
        x0 = max(0, roi.cx - roi.r - pad)
        y0 = max(0, roi.cy - roi.r - pad)
        x1 = min(w, roi.cx + roi.r + pad + 1)
        y1 = min(h, roi.cy + roi.r + pad + 1)
        return frame[y0:y1, x0:x1].copy()
