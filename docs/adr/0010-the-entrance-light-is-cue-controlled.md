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

Supersedes the absolute stated in ADR 0002's consequences and in
`config/hardware.yaml`.
