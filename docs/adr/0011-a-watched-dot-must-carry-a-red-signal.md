# ADR 0011 — A watched dot must carry a red signal

Status: Accepted (2026-09-18)
Relates to: ADR 0002 (ceiling dot detection), ADR 0009 (per-maze dot capture)

## Context

Detection enrols dots in one signal domain and measures them in another.

- `vision/dots.py::find_dots` finds blobs with a white top-hat on
  `cv2.COLOR_BGR2GRAY`, thresholded at `thr` (25 on every camera).
- `vision/detect.py::sample_circle` measures the mean of the top 20% of
  `clip(R - (G+B)/2, 0, 255)` inside the ROI.

`R - (G+B)/2` is a **pure chroma** measure: luma cancels out of it exactly. A
grey patch reads 0.000 at every brightness from 40 to 255 — verified
numerically, and asserted by `tests/test_capture.py` for a white frame. The
response is also flat above clipping: rendered dots at peak 400, 600, 1000 and
2000 all measure ~202-204, so a 5x spread in real laser power is
indistinguishable once the core saturates.

The consequence is that a **bright but colourless** blob — container corner
glare, an edge light fitting, a lens ghost — passes the grayscale finder and is
then handed a baseline near zero by the chroma measurement. Nothing rejected it.

The shipped calibration contained 15 such entries out of 594. The distribution
is sharply bimodal, with no dot anywhere inside the gap:

| population | n | range |
|---|---|---|
| colourless artefacts | 15 | 0.68 – 16.05 |
| real laser dots | 579 | 85.50 – 209.0 |

`process_frame` skipped only `baseline <= 0`, so all 15 were live. Detection is
`ratio = value / baseline` against `break_ratio` 0.5, which means the worst of
them — `SM-CAM-21:d0` in maze_3, baseline **0.68** — ended a player's run on a
drop of **0.34 of one 8-bit count**. `stats()` counted them as healthy
`watched`, so the console showed a green, fully-watched maze.

Both failure directions are real and neither is visible:

- the artefact sits **chronically dark**, phantom-busting players — and, via the
  report-once latch, shadowing every genuine break behind it;
- or it sits **permanently bright**, a beam nothing watches, reported as watched.

13 of the 15 sat within 70 px of a frame border, which is consistent with the
glare-and-fittings explanation rather than 15 independently bad lasers.

## Decision

**A dot is watched only if its calibrated baseline is at least
`detection.min_baseline` (default 40).** Below that it is not sampled, and it is
reported in `stats()` as `blind` rather than `watched`.

40 is taken from the data, not from feel: it sits inside the empty 5.3x gap
between the two populations, so it removes exactly the 15 artefacts and touches
no real dot. Sane range 20-80.

This is invariant 5 applied one level earlier than usual. Invariant 5 says
vision suppresses when it is unsure; this says vision should not pretend to
measure something whose measurement cannot carry information in the first place.

## Consequences

- ~2.5% of enrolled dots stop being watched, all of them at frame edges, none of
  them beams. Real coverage is unchanged.
- The console stops reporting them as watched, so the blind spot is visible.
- Per-camera bust statistics become meaningful. The observation that "7 of 8
  breaks came from SM-CAM-23" was an artifact of these dots: `_decide` reports
  the darkest dot of the largest cluster, and a near-zero-baseline dot wins that
  `min(ratio)` whether or not a body is anywhere near it. SM-CAM-23 held two
  sub-20 dots in all three mazes; SM-CAM-24, the apparently "near-blind" camera,
  held none and was the control.

## The better fix, not taken here

The root cause is the domain crossing: find in grayscale, measure in chroma. The
durable fix is to compute the top-hat on the red-isolated image in
`vision/dots.py` so geometry and photometry live in the same domain, and a
colourless blob is never enrolled at all.

That changes what every threshold means and requires a full `tools/capture.py`
retune with the container available. The floor above is the guard that makes the
current calibration safe in the meantime, and it stays useful afterwards as a
cheap assertion that a recapture produced measurable dots.
