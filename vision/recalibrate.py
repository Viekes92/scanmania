"""
vision/recalibrate.py — re-find the dot ROIs from the live cameras.

Inputs:  the running CameraStreams, the per-camera params already in beams.json
Outputs: a fresh {camera_id: {w, h, params, dots}} map per maze, and a report
Invariant: re-detects GEOMETRY only. The per-camera params (thr, tophat,
           min_area) were hand-tuned against this container's lighting and are
           reused untouched — a move changes where the dots are, not what a dot
           looks like. Tuning still belongs in tools/capture.py.
Invariant: never writes anything. It returns a candidate; the caller decides.

Why this exists rather than an offset-shift: the dots are where the BEAMS land.
A container that has been trucked settles, mounts shift, and every dot moves by
its own amount in its own direction — so there is no single translation to
apply. The ROIs genuinely have to be found again.

Why it can live in the game process at all: the game already holds the RTSP
streams and the relay boards, which is exactly what a capture needs and exactly
why tools/capture.py demands the game be stopped first.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np

from vision.detect import sample_circle
from vision.dots import DEFAULT_PARAMS, find_dots

log = logging.getLogger(__name__)

# Same persistence rule tools/capture.py uses: a dot has to show up in most of
# several passes spread over a few seconds. One pass catches sensor noise and
# anything that happened to be moving; this outlasts a person walking through.
_PASSES = 5
_MIN_HITS = 3
_PASS_GAP_S = 0.6

# A dot found in two passes within this many pixels is the same dot.
_MATCH_TOL_PX = 6

# With every laser off, a correctly dark container shows almost nothing. More
# blobs than this means ambient light — house lights on, or a door open onto a
# sunlit yard — and every ROI captured under it would be wrong.
_MAX_AMBIENT_BLOBS = 15


def _cluster(passes: list[list[tuple[int, int, int]]]) -> list[tuple[int, int, int]]:
    """Keep dots seen in at least _MIN_HITS passes; average their positions."""
    clusters: list[dict] = []
    for found in passes:
        for (cx, cy, r) in found:
            for c in clusters:
                if abs(c["cx"] - cx) <= _MATCH_TOL_PX and abs(c["cy"] - cy) <= _MATCH_TOL_PX:
                    c["xs"].append(cx)
                    c["ys"].append(cy)
                    c["rs"].append(r)
                    break
            else:
                clusters.append({"cx": cx, "cy": cy, "xs": [cx], "ys": [cy], "rs": [r]})
    out = []
    for c in clusters:
        if len(c["xs"]) < _MIN_HITS:
            continue
        out.append((int(round(float(np.median(c["xs"])))),
                    int(round(float(np.median(c["ys"])))),
                    int(round(float(np.median(c["rs"]))))))
    out.sort(key=lambda d: (d[1], d[0]))       # reading order, stable ids
    return out


async def _sample(streams: dict[str, Any], params: dict[str, dict]) -> dict[str, list]:
    """Run _PASSES over every live camera and cluster what persists."""
    per_cam: dict[str, list[list]] = {cid: [] for cid in streams}
    for _ in range(_PASSES):
        for cid, stream in streams.items():
            frame = getattr(stream, "last_frame", None)
            if frame is None:
                continue
            try:
                per_cam[cid].append(find_dots(frame, params.get(cid) or DEFAULT_PARAMS))
            except Exception as exc:
                log.warning("recalibrate: %s find_dots failed: %s", cid, exc)
        await asyncio.sleep(_PASS_GAP_S)
    return {cid: _cluster(p) for cid, p in per_cam.items() if p}


async def ambient_blobs(streams: dict[str, Any], params: dict[str, dict]) -> dict[str, int]:
    """How much the cameras see with every laser off. Should be ~nothing."""
    out = {}
    for cid, stream in streams.items():
        frame = getattr(stream, "last_frame", None)
        if frame is None:
            continue
        try:
            out[cid] = len(find_dots(frame, params.get(cid) or DEFAULT_PARAMS))
        except Exception:
            out[cid] = 0
    return out


async def recapture_maze(streams: dict[str, Any], params: dict[str, dict],
                         previous: dict | None = None) -> dict:
    """
    Re-find every dot for the maze currently lit.

    `previous` is the existing per-camera block for this maze; its `params` and
    frame size are carried forward, and its dot count is what the caller
    compares against to decide whether the result is believable.
    """
    found = await _sample(streams, params)
    prev = previous or {}
    cameras: dict[str, dict] = {}
    for cid, dots in found.items():
        stream = streams[cid]
        frame = getattr(stream, "last_frame", None)
        h, w = (frame.shape[0], frame.shape[1]) if frame is not None else (0, 0)
        cameras[cid] = {
            "w": w,
            "h": h,
            # Hand-tuned; a move changes geometry, not what a dot looks like.
            "params": (prev.get(cid) or {}).get("params") or params.get(cid) or dict(DEFAULT_PARAMS),
            # Baselines are measured HERE, with the maze lit, using the same
            # sample_circle() the runtime compares against. A recapture that
            # wrote geometry without baselines would leave every dot blind
            # (baseline <= 0 is skipped by process_frame) — a green console in
            # front of a maze detecting nothing.
            "dots": [
                {"id": f"{cid}:d{i}", "cx": cx, "cy": cy, "r": r,
                 "baseline": round(float(sample_circle(frame, cx, cy, r)), 3)
                             if frame is not None else 0.0,
                 "masked": False}
                for i, (cx, cy, r) in enumerate(dots)
            ],
        }
    return cameras
