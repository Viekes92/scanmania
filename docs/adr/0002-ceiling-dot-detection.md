# ADR 0002: Ceiling-dot detection, not beam-line sampling

**Status:** Accepted — partially superseded by [ADR 0009](0009-per-maze-dot-capture.md)
**Date:** 2024-01-15

> The detection method below stands unchanged. What ADR 0009 replaced is the
> bookkeeping around it: ROIs are captured per maze per camera, not per relay
> channel, and the per-channel fault rule is now a count.

## Context

We need to detect when a player breaks a laser beam. Each laser travels horizontally through the
container and terminates as a bright dot on the ceiling. Two options were evaluated: sampling
brightness along the beam line through haze, or watching the ceiling dot where the beam lands.

The detection outcome is high-stakes: with hard-cutoff scoring (ADR 0003), a false positive ends
someone's run in front of a queue. The chosen approach must be reliable enough to earn trust.

## Decision

Watch the **ceiling dot**. Each beam's dot is a small, fixed, high-contrast target on a stable
surface. Dot present = beam intact. Dot gone = beam broken.

Per frame, per unmasked beam:
1. Sample the mean of the top 20% brightest pixels in the beam's circular ROI (defined in
   `config/beams.json`) on the red-isolated image (`R - (G+B)/2`).
2. Compute `ratio = value / baseline`.
3. Apply hysteresis: `broken` after `ratio < break_ratio` for N consecutive frames; `clear`
   after `ratio > clear_ratio` for N frames. Asymmetric thresholds prevent boundary chatter.
4. `N = 3` at 30 fps gives approximately 100 ms detection latency.

> **Superseded in part (2026-09-18).** The box ships `consecutive_frames = 1`
> at 25 fps and 1920x1080, not N=3 at 30 fps and 1024x576. N was lowered
> deliberately: at 25 fps, N=3 is 120 ms, and an arm crossing a beam is gone
> inside that. Do not budget the noise immunity this line implies — there is
> none on the temporal axis. What replaced it is refusing to watch dots whose
> measurement cannot mean anything (ADR 0011), which costs no latency.
>
> Guaranteed detection is therefore ~75-80 ms of continuous occlusion, not the
> ~45 ms a full-extinction model predicts: a blocked dot retains 23-45% of its
> lit value (measured from real busts), so a block must cover most of one
> exposure before the ratio crosses 0.5. Halving that means halving the frame
> period, which is a camera setting and is gated on moving `process_frame` off
> the event-loop thread first.
>
> The exposure/AWB lock this ADR mandates is NOT enforced anywhere in code.
> `check_drift()` was removed in 2026-09 — it had no callers and the `ref/`
> frames it needed never existed.

The camera is locked: exposure, gain, white balance, and focus fixed. Auto-adjustment is
disabled. A boot-time drift check compares a stored reference frame; if shift > 2 px, raise
`CAMERA_MOVED` and drop to `manual` detection mode.

## Alternatives considered

**Sampling the beam line:** The beam is a diffuse glow through haze — wide, variable, and
sensitive to haze density changes throughout a session. The ceiling dot is a specular spot on a
fixed surface with far greater contrast. The dot is the better signal.

**Photoelectric receivers at the far end of each beam:** Reliable, but requires signal cable to
every emitter/receiver pair and a DAQ board or serial adapter for every channel. Expensive,
complex to wire in a container. The camera approach reuses hardware already needed for the
outdoor display feed (ADR 0002 consequence: one decode, two consumers).

**Laser power sensors:** Same wiring complexity as photoelectric receivers, plus per-laser
calibration. No advantage over photoelectric.

## Consequences

- Camera must not move. Lock it down. Thread-lock the mount. Boot-time drift check is mandatory.
- Work on the red channel isolated — suppresses white highlights from work lights, phone flashes,
  and displays while preserving the laser dots.
- Under 720p is sufficient; the cameras run 1024×576 @ 25 fps. One decode, two consumers: detection and the MJPEG stream
  for the outdoor display (`vision/mjpeg.py`). Never open the same RTSP stream twice.
- `config/beams.json` is the only source of truth for ROI positions and thresholds. The 45 beam
  ROIs are captured with `tools/capture.py` (ADR 0009) and verified on the
  `/admin/beams` live overlay page.
- Evidence thumbnails (JPEG crop of the triggering ROI plus two preceding frames) saved per
  break. When a player disputes a bust, you look at the picture.
- Vision suppresses break events when unsure: frame gap > 300 ms → no events emitted, auto-drop
  to `manual` mode. Silence is always safer than a phantom bust.
