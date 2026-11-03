# ADR 0012: A run may be lit, at one configured level, for the camera

**Status:** Accepted
**Date:** 2026-11-02
**Default:** `0` — the behaviour every earlier document describes.

## Context

`iobackend/lightshow.py` held COUNTDOWN and all three RUN segments at a hard 0,
over any cue and over the gamemaster's work-light switch, and said so as an
invariant in its docstring, in `CLAUDE.md` and in `tests/test_lights.py`. The
reason is detection, and it is a real one: `vision/detect.py` samples raw
brightness inside each dot's ROI with no background subtraction, so ambient
light raises every reading. A genuinely broken beam can then still read above
`break_ratio` and the break is **missed** — a player runs clean through a beam
they broke, which is the failure direction nobody sees and nobody can appeal.

The grand-prize final wants the opposite thing. The run is being filmed, and a
pitch-dark container photographs as a black rectangle with a few dots in it.
What the camera needs is not much — enough to see a person moving — and the
fixtures are already there on channels 3 and 4.

Three facts made this worth writing down rather than just editing a cue:

1. **A cue cannot do it.** `_DARK_STATES` is checked before the cue table, so a
   `run_seg_1` cue has never had any effect. That ordering is the enforcement,
   and ADR 0010's amendment is about exactly why a correctness property must
   not be left to a cue having run earlier.
2. **Detection is not currently the constraint it will be.** Vision is unwired
   on this box (`vision/camera.py` has no `VisionService`), the shipping
   detection mode is `assisted`, and the final is run with a gamemaster calling
   penalties by hand. The guarantee being traded is one nothing is relying on
   tomorrow.
3. **The number is meaningless on paper.** The D5 DMX5125 decoders here are
   effectively full past about 10, so the whole useful band is 1-6 and the
   level has to be set by standing in the container and looking at the camera.

## Decision

`hazer.lights.run_level` in `hardware.yaml`, default `0`.

- `0` — the behaviour described everywhere else: COUNTDOWN and the RUN segments
  are dark. Nothing changes for a normal event.
- Above `0` — those four states are held at exactly that level instead.

What does **not** change is the half of the invariant that makes a run
repeatable: the level is one number for all four states, set once by an
operator, and neither a cue nor the GM's work-light switch can move it. A run
looks the same every time, and there is no step at GO or at a checkpoint — a
change in the room mid-run is something a player reads as information, and on
camera it is an exposure shift in the middle of the shot.

The **entrance is not part of the fill** and still goes dark for every RUN
state (`_always_on_dark`, ADR 0010). It leaks straight down the container and
it is the one fixture a player on the plate is looking into, so it would be in
frame and in their eyes at once.

Applied with `fade=False`, for the same reason going dark snaps: a fade into a
run is still moving during the first seconds of it, and COUNTDOWN is where the
step would land.

Range-checked to **0-16**, not the full 0-255 a DMX level could take.
`_ranged()` clamps rather than rejecting, so the ceiling *is* the failure mode:
against 0-255 a fat-fingered `run_level: 200` would clamp to 255 and put the
house lights up through every run. 16 is already past full on these decoders,
with headroom for a dimmer fixture, so a typo lands somewhere bright but
survivable and says so in the log. A non-numeric value falls back to 0.

`LightCuePlayer.reload()` re-applies the current state, so the level can be
nudged from the admin config editor while standing in the container, with no
service restart. That also fixes light *cues* reloading live, which they never
did.

## Consequences

**With `run_level` above 0, camera detection is not trustworthy and must not be
relied on.** Run the final on `manual` or `assisted` detection with a
gamemaster watching, which is how it is being run anyway. Do not read a clean
auto-detected run as evidence of anything while the room is lit.

A boot line names the level (`Run fill light: level 2 through COUNTDOWN and the
RUN segments`) so "why was that run not dark" is answerable from the log rather
than from the cue file.

The egress and work-light guarantees are untouched: `blackout()` still restores
the entrance, `power_down()` is still the only path that leaves it dark, and
the GM override still cannot reach a run.

Supersedes the absolute "RUN states are always fully dark" stated in
`iobackend/lightshow.py`, `CLAUDE.md` ("Invariants") and `tests/test_lights.py`
— all three now state the weaker property, that a run is held at one level
nothing else can move.
