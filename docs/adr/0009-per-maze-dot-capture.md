# ADR 0009 — Calibrate per maze, not per relay channel

**Status:** Accepted, 2026-09-14
**Supersedes:** parts of [ADR 0002](0002-ceiling-dot-detection.md) — the detection
method is unchanged; what a "beam" is, and how ROIs are obtained, is not.

## Context

ADR 0002 fixed the detection method: a camera watches the ceiling, each laser
terminates as a bright dot, and a dot going dark means a beam was broken. That
still holds.

What did not hold was the bookkeeping around it. `config/beams.json` had 45
entries, one per relay channel, each carrying up to 5 dots. Calibration
(`tools/sweep.py`) existed to build that mapping: light one channel from dark,
see which 5 dots appear, record them. Two passes, 45 relay switches, an
ambiguous labelling step in the middle, and a fault rule ("all 5 dots of this
channel are dark, so it is hardware, not a player") that depended on the mapping
being right.

Three things pushed against it.

1. **The mapping buys nothing.** Ending a run needs to know that *a* beam broke.
   Which relay drives the broken laser changes nothing the game does — not the
   outcome, not the stopwatch, not the display. It is useful with a soldering
   iron in your hand and at no other moment.

2. **One set of detection parameters cannot serve every camera.** The white
   top-hat kernel has to be larger than a dot and smaller than the spacing
   between dots, and both scale with how far the camera is from the ceiling. A
   real sweep found 5 dots on one camera and 11 on another, looking at the same
   five lasers: the near camera's dots were bigger than the kernel, so each one
   became a ring that fragmented into arcs.

3. **Eight cameras, not four.** `SM-CAM-11`-`14` down the left side,
   `SM-CAM-21`-`24` down the right. With full ceiling coverage every dot is
   close to *some* camera. Tuning becomes per camera and local, which is tractable;
   labelling 225 dots by relay channel across 8 overlapping views is not.

## Decision

Calibration lights a **whole maze**, tunes **each camera** until its dot count
looks right, and saves **what it saw** as that maze's ROIs.

```
beams.json
  mazes:
    maze_1:
      captured_at: ...
      cameras:
        SM-CAM-11: { w, h, params: {thr, tophat, min_area, max_area},
                 dots: [ {id: "SM-CAM-11:d0", cx, cy, r, baseline, masked}, ... ] }
```

Consequences of the shape:

- **A dot id is `<camera>:<index>` (`SM-CAM-13:d17`).** Stable within a maze capture, meaningless
  across recaptures. That is fine: nothing outside one capture refers to it.
- **Captures are per maze.** Each shape lights roughly half the floor, so a dot
  with two lit neighbours in one shape and none in another reads a different
  baseline. One number cannot serve all three.
- **Detection params are stored with the dots they produced.** A capture is
  reproducible, and the admin page can show why a camera found what it found.
- **The 45 channel entries stay**, documented as the relay wiring record. They
  are not read at runtime.

## The fault rule

The old rule was per channel: five colinear dots all dark at once is not a body,
so it must be hardware. Without a channel mapping that rule has nothing to key
on. It is replaced by a **count**:

```
0                               nothing
1 .. max_simultaneous_breaks    a body is in the beams  -> break
> max_simultaneous_breaks       not a person            -> suppress, report a fault
```

`max_simultaneous_breaks` defaults to 10. A body blocks a handful of dots; a
relay that did not fire, a preset change the detector was not told about, or a
camera glitch kills dozens. The rule is coarser than the old one but rests on
nothing that can be miscalibrated.

## What we give up

**Which segment broke is no longer knowable.** A bust names a dot and the camera
that saw it — `SM-CAM-13:d17` — not a laser array. Accepted deliberately: the
operator needs to know *that* a beam broke and roughly where to look, and the
camera id answers the second part well enough.

## Consequences

- `tools/sweep.py` and its LABEL/MEASURE passes are gone, replaced by
  `tools/capture.py`. Same web UI pattern, same port, no relay switching beyond
  lighting the maze being captured.
- The GM console's 45-pill beam strip is a per-camera summary. 175 pills is not
  something anyone reads on an iPad mid-run.
- Baselines are keyed `<maze>/<dot_id>`, and the rolling EMA that
  [invariant 6 of ADR 0002] always described is now actually wired: it was dead
  code, with `update_ema()` having no callers at all.
- An uncalibrated preset watches **nothing**, and that is the correct failure.
  Without a baseline every reading is a guess, and a guess ends someone's run
  (invariant 5).
