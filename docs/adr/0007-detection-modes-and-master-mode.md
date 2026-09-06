# ADR 0007: Three detection modes and master mode

**Status:** Accepted
**Date:** 2024-01-15

## Context

Hard-cutoff scoring (ADR 0003) inverts the risk profile: a missed break costs nothing, but a
phantom break ends someone's run in front of a queue. The vision system needs to earn trust
before being left in full-auto, and the gamemaster needs authority over the machine at all times.
The system must also degrade gracefully when the camera faults.

## Decision

**Three detection modes, switchable live from the GM console with one tap:**

| Mode | Vision behaviour | Gamemaster |
|------|-----------------|------------|
| `auto` | Confirmed break immediately busts the run. | Can `VOID` afterwards. |
| `assisted` | Confirmed break halts the stopwatch and raises a full-screen CONFIRM / VETO prompt with the evidence thumbnail. VETO → stopwatch resumes, duration credited back. | Adjudicates every call. |
| `manual` | Vision is advisory only — lights an indicator, does not bust. | Presses BUST to end a run. |

**Automatic degradation** (system drops to a safer mode itself; never escalates back automatically):

| Trigger | Action |
|---------|--------|
| Vision STALLED (frame gap > 300 ms) | → `manual`, red banner |
| Camera unreachable after 3 reconnects | → `manual`, offer PoE port power-cycle |
| Boot drift check fails (`CAMERA_MOVED`) | → `manual`, "recheck beam ROIs" |

**Master mode** — a separate FSM state for demos, VIP runs, fault-finding, and "the show must
go on": fire any preset, toggle individual relay channels, control the stopwatch manually, force
any FSM state. Runs recorded with `mode: master`, excluded from leaderboard by default.

## Alternatives considered

**Auto-only from day one:** Requires the detection system to be fully trusted before measuring
its false-positive rate. Threshold tuning happens on site with real haze densities and real
players. `assisted` as the default for the first days provides the measured path to `auto`.

**Manual-only:** Safe, but requires a gamemaster call on every bust. Fatiguing, inconsistent,
and defeats the purpose of computer vision.

**Auto-escalation back to `auto` when vision recovers:** This surprises the gamemaster. The
system becoming silently stricter mid-session is unacceptable. Returning to `auto` is always a
deliberate human tap.

**No master mode:** Leaves the team without a path when hardware is partially broken but the
show must continue. A VIP run with no active lasers and a hand-operated stopwatch is a real
scenario; master mode makes it unambiguous.

## Consequences

- Every mode change is logged and appears in the run record, so results are always interpretable
  after the fact.
- `assisted` is the recommended default for the first days on site. The veto rate in assisted
  mode is the evidence needed to decide whether `auto` is trustworthy.
- The detection mode badge is always visible at the top of the GM console.
- Master mode is accessible via long-press on `/gm` and from `/admin`. Exiting returns to
  `SELF_TEST` so the system re-verifies hardware before returning to play.
- Per-beam masking is orthogonal to detection mode: masked beams are excluded from detection in
  all three modes and from the pre-flight gate.
