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
import os
from collections import deque
from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np

from config.loader import BeamROI

log = logging.getLogger(__name__)

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
        self._frame_buffer: deque[_Frame] = deque(maxlen=_RING_BUFFER_SIZE)

    # ------------------------------------------------------------------
    # Frame ingestion
    # ------------------------------------------------------------------

    def push_frame(self, frame: np.ndarray, timestamp_ns: int) -> None:
        """Add a decoded frame to the ring buffer. Called for every frame."""
        self._frame_buffer.append(_Frame(frame=frame, timestamp_ns=timestamp_ns))

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
            filename = f"{beam_id}_{fr.timestamp_ns}_{label}.jpg"
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
