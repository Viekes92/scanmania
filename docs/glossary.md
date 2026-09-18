# Glossary

Terms used throughout the codebase, config files, and docs. When in doubt, use these definitions exactly.

---

**beam**
A single laser line, from emitter to ceiling. Identified by a string id (e.g. `b07`). A relay channel drives a SEGMENT of 5 colinear lasers, so one channel means 5 ceiling dots. No dot is tied to a relay — see ADR 0009. "Breaking a beam" = something enters the beam path, blocking it.

**dot**
The bright spot a laser beam makes on the ceiling. This is what the camera watches. Dot present = beam intact. Dot gone = beam broken. The camera watches dots, not the beam line.

**cluster**
A physical group of beams in one section of the container (e.g. cluster 1 = the first obstacle). Clusters map to game segments. Multiple beams per cluster; multiple clusters per board.

**channel**
A single relay output on a Waveshare board (1–16). One channel drives one SEGMENT: 5 emitters that switch together. The mapping from `beam.id` → `relay_channel` → board address is recorded in `config/beams.json` and `config/hardware.yaml`.

**preset**
A named laser configuration defined in `config/mazes.yaml`: a list of channels to energise. Examples: `blackout`, `attract`, `segment_1`, `bust`. Applied with a single `write_coils` call per board.

**show**
An ordered list of presets that compose a full game maze (e.g. `main_game: [segment_1, segment_2, segment_3]`). Each segment is active during one phase of the run.

**segment**
One phase of an active run, corresponding to one preset. The maze changes preset at each checkpoint. `RUN_SEG_1` → `RUN_SEG_2` → `RUN_SEG_3`.

**run**
One player's attempt from GO to either the stop button, a beam break, or timeout. Identified by a UUIDv7 generated at run start. Outcomes: `clean` | `busted` | `aborted` | `voided`.

**bust** (noun/verb)
A run ended by a confirmed beam break. The stopwatch stops, the maze snaps to the `bust` preset (all lasers on), "BUSTED" appears on both displays. Busted runs are recorded but excluded from the leaderboard.

**void**
A gamemaster action that marks a completed or busted run as `voided` — recorded but excluded from the leaderboard. The escape hatch for "the system was wrong." Does not alter or delete the event log.

**mask**
Setting `masked: true` on a beam in `config/beams.json`. Masked beams are excluded from detection and from the pre-flight gate. Shown as grey in the GM beam strip, with a persistent "N beams masked" banner. Persisted with a timestamp and reason.

**gamemaster**
The staff member running sessions with an iPad. Always present. Responsible for sign-in, count-in, and recovery. The recovery mechanism for every fault. The `/gm` frontend is built around their workflow.

**master mode**
A separate FSM state where the gamemaster drives hardware directly: fire any preset, toggle individual channels, control the stopwatch manually. For demos, VIP runs, and fault-finding. Runs in master mode are excluded from the leaderboard by default. Accessed via long-press on `/gm` or from `/admin`.

**ROI** (Region of Interest)
The circular area in the camera frame that corresponds to one beam's ceiling dot. Defined in `config/beams.json` under `mazes.<name>.cameras.<cam>.dots` as `{cx, cy, r, baseline, masked}`. The 45 top-level `beams` entries are the relay wiring record and are NOT read at runtime in pixels. Fixed per installation — if the camera moves, ROIs are invalid.

**baseline**
The reference brightness reading for a beam's dot under normal lit conditions. Captured by `tools/capture.py` or Admin -> Calibration and stored in `beams.json`. (`count_in.baseline_pulse_index` is parsed and unused — nothing measures a baseline during the ramp). Updated as a rolling EMA in `ATTRACT`. Frozen during a run. The denominator in `ratio = value / baseline`.

**ratio**
`mean_top20_pct(ROI pixels) / baseline`. Below `break_ratio` for N consecutive frames = broken. Above `clear_ratio` for N frames = clear. Hysteresis prevents chatter at the boundary. The shipped N is **1**, so there is no temporal filtering: one frame decides. A dot whose baseline is below `detection.min_baseline` is not watched at all (ADR 0011).

**arm / ARM state**
The FSM state where the system is waiting for the player to step onto the start plate before a count-in. The start-plate HIGH event triggers the ready blink and pre-flight beam check.

**pre-flight gate**
The check at ARM (before count-in): the relay boards answer, vision is not stalled, the dot count is sane, and the stop button is not already pressed. Failure names the beam and blocks the count-in. The highest-value diagnostic in the system.

**detection mode**
`auto` — confirmed break immediately busts.
`assisted` — break halts the stopwatch and raises a CONFIRM / VETO prompt for the gamemaster.
`manual` — vision is advisory only; the gamemaster presses BUST.
Switchable live from the GM console. The system drops to `manual` automatically on vision faults.

**outbox** *(removed 2026-09-13)*
Was an SQLite table buffering completed runs for a cloud endpoint. Removed with cloud sync — see [ADR 0008](adr/0008-remove-cloud-sync.md). Kept here because the term appears in older commits, `plan.md` §8 and ADR 0006. Durability is now local only: snapshots plus CSV export.

**stall**
Vision `STALLED` condition: frame gap > 300 ms. While stalled, no break events are emitted. If a run is in progress, the system auto-drops to `manual` mode rather than busting anyone on a phantom signal.
