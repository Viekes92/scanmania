"""
vision/dots.py — find laser dots in a frame.

Inputs:  a BGR frame, and the per-camera tuning params stored in beams.json
Outputs: [(cx, cy, r)] per dot, plus every intermediate image for diagnosis
Invariant: pure image processing — no I/O, no config reads, no device access.

Lived in tools/capture.py, which made it unreachable from the running game:
tools/ is not a package, so nothing in vision/ or web/ could import it. The
alignment check needs exactly this, and there must be ONE implementation — a
second copy would drift from the one the calibration was made with, and then
the check and the capture would disagree about where the dots are.
"""

from __future__ import annotations

import cv2
import numpy as np


# ---------------------------------------------------------------------------

# A STARTING POINT for a 1920x1080 main stream, not an answer. Every one of these
# is a slider on the page, and the right value differs per camera — that is the
# whole reason params are stored per camera.
#
# Both bounds err LARGE, deliberately, because the two failure directions are
# not symmetric:
#   tophat too large  -> slightly weaker background subtraction, dots still found
#   tophat too small  -> the dot exceeds the kernel, becomes a RING, fragments
#                        into arcs, and one dot is counted three times or lost
#   max_area too large -> a reflection sneaks in, visible on the preview
#   max_area too small -> the brightest near dots are silently discarded
# Tighten them on the page against a lit maze. Do not tighten them blind — and
# prefer the per-camera "apply measured" button, which derives the kernel from
# the dot size and spacing this camera actually sees. Both scale with resolution
# and with distance to the ceiling, so no single default fits eight cameras.
#
# max_area does NOT reject a light fitting. Anything larger than the kernel has
# its interior removed by the top-hat, so a bright rectangle survives as four
# small CORNER blobs — dot-sized by area, and indistinguishable from dots by any
# test this function applies. min_area is what rejects them, and the real
# defence is the ambient guard: house lights off.
DEFAULT_PARAMS = {
    "thr": 25,          # absolute threshold on the top-hat signal, 0-255
    "tophat": 21,       # structuring element, px. Bigger than a dot, smaller
                        # than the gap between two dots. Measured at 1920x1080:
                        # dots are 10-16 px across, spacing 30-65 px
    "min_area": 40,     # px. Measured at 1080p: a dot is ~200 px of area and a
                        # light-fitting corner artefact ~12, so 40 clears both
                        # by 5x. Below this it is sensor noise — or the CORNER of
                        # something large. See the note below
    "max_area": 900,    # px. Above this it is a reflection or a light fitting
}

# A STARTING POINT for a 1920x1080 main stream, not an answer. Every one of these
# is a slider on the page, and the right value differs per camera — that is the
# whole reason params are stored per camera.
#
# Both bounds err LARGE, deliberately, because the two failure directions are
# not symmetric:
#   tophat too large  -> slightly weaker background subtraction, dots still found
#   tophat too small  -> the dot exceeds the kernel, becomes a RING, fragments
#                        into arcs, and one dot is counted three times or lost
#   max_area too large -> a reflection sneaks in, visible on the preview
#   max_area too small -> the brightest near dots are silently discarded
# Tighten them on the page against a lit maze. Do not tighten them blind — and
# prefer the per-camera "apply measured" button, which derives the kernel from
# the dot size and spacing this camera actually sees. Both scale with resolution
# and with distance to the ceiling, so no single default fits eight cameras.
#
# max_area does NOT reject a light fitting. Anything larger than the kernel has
# its interior removed by the top-hat, so a bright rectangle survives as four
# small CORNER blobs — dot-sized by area, and indistinguishable from dots by any
# test this function applies. min_area is what rejects them, and the real
# defence is the ambient guard: house lights off.
DEFAULT_PARAMS = {
    "thr": 25,          # absolute threshold on the top-hat signal, 0-255
    "tophat": 21,       # structuring element, px. Bigger than a dot, smaller
                        # than the gap between two dots. Measured at 1920x1080:
                        # dots are 10-16 px across, spacing 30-65 px
    "min_area": 40,     # px. Measured at 1080p: a dot is ~200 px of area and a
                        # light-fitting corner artefact ~12, so 40 clears both
                        # by 5x. Below this it is sensor noise — or the CORNER of
                        # something large. See the note below
    "max_area": 900,    # px. Above this it is a reflection or a light fitting
}

_MEDIAN_FRAMES = 7      # frames to median per capture; kills sensor noise
_PREVIEW_MEDIAN = 5     # frames to median per preview pass; ~200 ms at 25 fps
_PREVIEW_HZ = 2.0
# How many preview passes to report the dot-count spread over. A count that
# swings across this window is not tuned, however good the middle value looks.
_COUNT_WINDOW = 10

# A capture takes several medians spread over a few seconds and keeps only dots
# that persist. Somebody standing in a beam, or haze drifting through it,
# removes real dots from a single pass — and a dot whose baseline was measured
# while it was blocked reads permanently dark at runtime.
_CAPTURE_PASSES = 5
_CAPTURE_PASS_GAP_S = 0.6       # ~3 s total; long enough to outlast a person moving
# 3 of 5, not 4. Somebody walking through blocks a dot for about two passes,
# and dropping it there leaves a blind spot — a beam nothing watches — which is
# worse than keeping a marginal dot, because a bad baseline at least shows up as
# a dim or zero reading in validate(). A single-pass ghost is still rejected.
_CAPTURE_MIN_HITS = 3           # of _CAPTURE_PASSES
_MATCH_TOL = 4                  # px; the cameras do not move during a capture

# With every laser off, a correctly exposed ceiling camera sees near-nothing.
# More blobs than this means something else is lighting the scene.
_MAX_AMBIENT_BLOBS = 15


def stages(frame: np.ndarray, params: dict | None = None) -> dict:
    """
    Every intermediate image, plus the dots and the blobs that were rejected.

    Split out so the page can show each stage. Looking only at the final overlay
    tells you a camera found nothing; it does not tell you WHY, and the three
    causes want opposite corrections:

        signal dark      -> the dots are not reaching the sensor. Exposure,
                            focus, or the lasers are off. No parameter helps.
        signal fine,
        mask empty       -> thr is too high.
        mask shows RINGS -> the top-hat kernel is smaller than a dot. Raise it.
                            This is the one that silently triples a dot count.
        mask solid,
        no dots          -> min_area / max_area are excluding them. The rejected
                            list says which bound and by how much.
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    k = max(3, int(p["tophat"]) | 1)      # kernel must be odd
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    sig = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kern)
    _, mask = cv2.threshold(sig, int(p["thr"]), 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    n, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)

    lo, hi = int(p["min_area"]), int(p["max_area"])
    dots, rejected = [], []
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        cx, cy = centroids[i]
        cx, cy = int(round(cx)), int(round(cy))
        r = max(3, int(round((area / np.pi) ** 0.5)) + 2)
        if area < lo:
            rejected.append((cx, cy, r, area, "small"))
        elif area > hi:
            rejected.append((cx, cy, r, area, "large"))
        else:
            dots.append((cx, cy, r))
    return {"gray": gray, "signal": sig, "mask": mask,
            "dots": dots, "rejected": rejected,
            "signal_peak": int(sig.max()) if sig.size else 0}


def suggest_params(dots: list[tuple[int, int, int]]) -> dict:
    """
    Recommend a top-hat kernel and min_area from what the camera is seeing.

    The kernel has one hard requirement: LARGER than a dot, SMALLER than the gap
    between two dots. Both scale with resolution and with how far the camera is
    from the ceiling, so the right value differs per camera and changes whenever
    the stream resolution does — which is exactly the tuning nobody wants to do
    eight times by eye.

    Measures the median dot diameter and the median nearest-neighbour distance,
    and picks a kernel between them. Returns {} when there is not enough to go
    on, rather than guessing.
    """
    if len(dots) < 6:
        return {}
    diam = 2 * float(np.median([r for _, _, r in dots]))

    pts = np.array([(x, y) for x, y, _ in dots], dtype=np.float32)
    d2 = ((pts[:, None, :] - pts[None, :, :]) ** 2).sum(-1)
    np.fill_diagonal(d2, np.inf)
    spacing = float(np.median(np.sqrt(d2.min(axis=1))))

    if spacing <= diam:
        # Dots are touching or merged; no kernel separates them. Say so rather
        # than recommend something that cannot work.
        return {"note": "dots overlap — no kernel can separate them"}

    # Comfortably clear of the dot, comfortably inside the spacing.
    k = int(round(diam * 1.6))
    k = max(k, int(diam) + 4)
    k = min(k, int(spacing * 0.8))
    k = max(3, k | 1)                      # getStructuringElement wants odd

    area = float(np.median([np.pi * max(r - 2, 1) ** 2 for _, _, r in dots]))
    return {
        "tophat": k,
        "min_area": max(4, int(area * 0.35)),
        "max_area": int(area * 6),
        "dot_px": round(diam, 1),
        "spacing_px": round(spacing, 1),
    }


def find_dots(frame: np.ndarray, params: dict | None = None) -> list[tuple[int, int, int]]:
    """Return [(cx, cy, r)] for every dot-like blob. See the recipe above."""
    return stages(frame, params)["dots"]

