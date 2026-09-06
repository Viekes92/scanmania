# ADR 0003: Hard-cutoff scoring

**Status:** Accepted
**Date:** 2024-01-15

## Context

We needed a scoring model for a laser maze where a player runs from a start plate to a stop
button. Options ranged from penalty-time accumulation to multiple lives to a single-chance model.
The game is public-facing with a queue, so the model must be legible to spectators and
unambiguous to the gamemaster.

## Decision

**Stopwatch counting up. A confirmed beam break is a hard cutoff — the run is over immediately.**
Fastest clean run wins. Busted runs are recorded but excluded from the leaderboard.

- Score = elapsed milliseconds from GO to stop button press. Lower is better.
- A confirmed break: stopwatch stops, maze snaps to `bust` preset, buzzer fires, both displays
  show "BUSTED".
- `max_run_ms` exceeded without reaching the stop button → outcome `aborted`.
- Gamemaster can `VOID` any run: recorded, excluded from leaderboard, reason stored.

## Alternatives considered

**Penalty time accumulation:** Break a beam, add N seconds to your score. Feels fairer, but
"you broke one beam and added 10 seconds" is harder to understand and watch than "you're out."
The game is theatre — a hard cutoff creates a clearer crowd reaction and a cleaner display.

**Multiple lives:** More forgiving. Complicates the FSM significantly, the display logic, and the
scoring model. Raises two new questions: how many lives, and what happens when you exhaust them?
Not needed for v1 and deferred explicitly.

**Time limit with no penalty:** Play until time runs out, score is distance reached. Does not
work for a maze with a fixed exit point (the stop button).

**Count-up with no cutoff (free-run):** Players can break beams freely. Requires spectators to
watch closely to know who "won." Loses the dramatic moment.

## Consequences

- The FSM has a clean binary outcome per run: `FINISHED` (clean) or `BUSTED` or `ABORTED`. No
  intermediate "still running but penalised" state.
- Detection must bias toward false negatives. A missed break costs a player nothing in score; a
  phantom break ends their run in front of a queue. This drives the N=3 consecutive-frame
  confirmation requirement, the `assisted` mode (ADR 0007), and the global rate limit.
- The `assisted` mode — break halts stopwatch, gamemaster confirms or vetoes with evidence
  thumbnail — is a direct consequence of hard-cutoff stakes. It makes every bust a human
  decision until the false-positive rate is measured and trusted.
- Leaderboard sorted ascending by `elapsed_ms`, clean runs only. Simple and unambiguous.
