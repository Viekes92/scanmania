# ADR 0010: The entrance light is cue-controlled, not unconditionally on

**Status:** Accepted
**Date:** 2026-09-17

## Context

The entrance light was specified as an absolute: `always_on` in
`hardware.yaml`, and `DmxController.set_light()` refused to take it below 1.
The reasoning is in the code and in four documents — a dark box with people in
it and no lit exit is the one state worth hard-coding against — and the only
sanctioned way past the guard was `DmxController.power_down()`, reached from an
explicit end-of-day shutdown with the operator at the breaker.

Three things then made that absolute unworkable in the container:

1. **It leaks straight down the box.** From the start plate, the entrance glow
   is the brightest thing a player can see, and everything from sign-in to the
   result is meant to be read by laser light.
2. **It defeats calibration.** It is the brightest fixture in the container and
   it points along it, so its reflections count against the ambient gate — the
   check that refuses a capture when the cameras see anything with every laser
   off. A recalibration could not run with it lit.
3. **It washes out detection.** Ambient light raises the reading inside every
   dot's ROI, and a broken beam that still reads above `break_ratio` is a
   MISSED break — the failure direction nobody sees.

## Decision

The entrance is controlled by the light cue table, state by state. The guard
stays, but `set_light(..., allow_always_on=True)` is the key, and **only a cue
holds it** — because only the cue table knows whether somebody is walking in or
out right now.

It is dark in `registered`, `arm`, `countdown`, the run states, `finished`,
`busted` and `result`. It is lit in `attract`, `master`, `aborted` and `fault`
— every state where a person is entering or leaving, plus the two that mean
something has gone wrong.

Two absolutes remain, and they are the ones that matter:

- **`blackout()` restores it.** That is what a dying process calls, so if the
  software falls over the way out is lit again regardless of the last cue.
- **`power_down()` is still the only path that leaves it dark**, and only from
  an explicit end-of-day shutdown.

Recalibration takes it dark for the capture and restores it in a `finally`, on
every path out including a refusal or an exception.

## Consequences

The egress guarantee is now "lit whenever a person is walking in or out, and
restored if the process dies" rather than "never switched off". That is a
weaker guarantee, deliberately, and it is written down here because four
documents asserted the stronger one and a reader in an emergency would have
believed them.

The container is never dark *and* occupied under normal operation: a run ends
within `max_run_ms`, and `aborted` and `fault` both relight the entrance. The
residual risk is a run state entered with somebody inside who is not the
player — which MASTER MODE and the GM walk-round exist to prevent.

## Amendment, 2026-09-27 — the RUN states no longer rely on a cue

The decision above is unchanged. How it is enforced is.

"Only a cue holds the key" turned out to make a correctness property depend on
a cue having run *earlier*, in a different state, which in turn depended on
`runner._release_work_lights()` having cleared the GM's work-light override in
time for that cue to play at all. With the override still set,
`LightCuePlayer._restart()` returns at `_all_on()` for REGISTERED and ARM, no
cue runs, and `set_maze_lights()` skips `always_on` fixtures by design — so the
entrance stayed at 255 through COUNTDOWN and all three segments. Measured
directly against `LightCuePlayer`; driving the real `GameRunner` showed no live
exposure, because the sign-in release always won the race in practice.

A safety property resting on the ordering of two calls in another module is not
one you can read off the code. `_DARK_STATES` now takes the `always_on`
fixtures dark itself (`LightCuePlayer._always_on_dark`), so the RUN states hold
whatever happened before them. Cues still own the entrance in every other
state, `blackout()` still restores it, and `power_down()` is still the only
path that leaves it dark.

The test that should have caught it asserted the opposite — `left == 0` only,
plus a `test_the_entrance_stays_lit_through_a_run` written 2026-09-15, two days
before this ADR, and never revisited when the decision changed. Both are now
correct and the run states are checked per-state.

Supersedes the absolute stated in ADR 0002's consequences and in
`config/hardware.yaml`.
