# CLAUDE.md — ScanMania

## What this is
ScanMania is a laser-maze game in a shipping container. A camera watches the ceiling where each laser beam terminates as a bright dot; a dot disappearing means a beam was broken. A player runs from a start plate to a stop button — break a beam and the run is over, fastest clean run wins.

## How to run on a laptop (no hardware)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scanmania --fake-all        # io/fake.py + inputs/fake.py + vision/fake.py active
```

Open http://localhost:8000 — frontends are live. Drive a full run from another terminal:

```bash
python tools/fake_run.py --scenario clean    # player walks through clean
python tools/fake_run.py --scenario busted   # player breaks beam b03 mid-run
python tools/fake_run.py --scenario aborted  # max_run_ms exceeded
python tools/fake_run.py --scenario voided   # gamemaster voids after the fact
```

Frontends:
- `/gm`           — gamemaster console (iPad, includes player sign-in)
- `/admin`        — admin portal (laptop)
- `/display/in`   — in-container stopwatch display (HDMI 1)
- `/display/out`  — outdoor display: MJPEG feed + stopwatch + leaderboard (HDMI 2)

## How to run the tests

```bash
pytest tests/ -v
```

Green means: every FSM transition covered (including false starts, out-of-order checkpoints, all detection modes, restart mid-run), outbox idempotency proven, fake backends exercised end-to-end.

## Invariants — do not break these

1. **`core/fsm.py` is a pure function.** No I/O, no `await`, no clock reads, no side effects. Takes `(state, event)`, returns `(new_state, [SideEffect, ...])`. Tests run 10 000 transitions with no mocks.
2. **The server owns the stopwatch.** `time.monotonic_ns()` in `core/stopwatch.py` only. Never `datetime.now()`, never `Date.now()` in a browser. Browsers interpolate from server broadcasts and hard-correct on each message.
3. **`config/beams.json` is the only source of truth for ROIs and thresholds.** The admin portal and `tools/pick_rois.py` write to it. Nothing else creates or modifies beam geometry.
4. **Never write coils outside `io/presets.py`.** Every coil write goes through `apply_preset()` or the master-mode direct toggle, both in that module. No exceptions.
5. **Vision suppresses events when unsure.** Frame gap > 300 ms → no break events emitted, auto-drop to `manual` detection mode. A false positive ends someone's run in front of a queue; silence is always safer.
6. **Gameplay never awaits the network.** Cloud sync runs in `scanmania-sync.service`. The game path returns immediately after writing to SQLite + outbox. Never `await` anything in `persist/sync.py` from the game path.
7. **Never resume a run after a restart.** Service restart → emit `ABORTED` → `RESET`. There is no checkpoint file. `runner.py` starts clean every time.

## Where things live

```
core/       Pure logic: FSM, stopwatch, scoring, events dataclasses, metrics façade. Zero I/O.
io/         Modbus master, preset resolution, reconciliation loop. fake.py is mandatory.
inputs/     Pico USB serial link + MicroPython firmware. fake.py is mandatory.
vision/     RTSP decode, dot detection, baseline, evidence thumbnails, MJPEG out. fake.py is mandatory.
persist/    SQLite schema + migrations, outbox drain, cloud sync, snapshots, exports.
web/        FastAPI + WebSocket broadcast, route handlers, static frontends (no build step).
config/     YAML/JSON files — the only place to change hardware topology or game settings.
tools/      Dev utilities: fake_run.py, pick_rois.py, ramp.py, replay.py.
tests/      One file per module. FSM tests are the most important — run them first.
docs/       Architecture, protocols, runbooks, ADRs. A PR without doc update is not done.
```

## Conventions

- **Event types:** `SCREAMING_SNAKE_CASE`, defined exclusively in `core/events.py`. Every field documented, every emitter and consumer noted.
- **Metric names:** `<domain>.<thing>.<verb|state>` snake_case — `relay.mismatch`, `run.completed`, `vision.stall`. See `docs/metrics.md`.
- **Config keys:** add to the YAML/JSON file + a one-line comment with purpose and sane range + validation in `config/loader.py`.
- **Commit format:** `<scope>: <what changed>` e.g. `fsm: handle false-start during COUNTDOWN`.
- **Adding a beam:** edit `config/beams.json`, use `tools/pick_rois.py` to get the ROI stanza, verify on `/admin/beams` overlay.
- **Module docstrings:** every module opens with its one job, inputs, outputs, and invariants (3–6 lines). No exceptions.

## Things that look wrong but aren't

- **Baseline frozen during a run.** Correct. Adapting mid-run slowly accepts a broken beam as normal. Rolling EMA only runs in `ATTRACT`.
- **`monotonic_ns()` everywhere, never wall clock.** Correct. Wall clock can jump (NTP, DST). `events.ts_wall` stores wall time for human readability only; nothing computes durations from it.
- **Outbox rows never dropped, even on repeated 4xx.** Correct. A misconfigured endpoint must not silently lose data. Errors surface on the admin portal; the row retries forever.
- **FSM returns side effects as data, doesn't execute them.** Correct. This is what makes `test_fsm.py` fast and deterministic without any mocks.
- **Count-in uses `segment_1`, not `all_on`.** Correct. The baseline is captured against exactly the preset that will be lit at GO. Flashing `all_on` would give wrong references for beams not in play.
- **`write_coils` writes the full 16-channel board every time.** Correct. One atomic transaction is what makes the maze snap rather than morph. Never loop single-coil writes.

## Definition of done

1. `pytest` green.
2. Docs updated — at minimum the relevant `docs/` file or a note in `plan.md`.
3. ADR added (in `docs/adr/`) if a locked decision changed; old ADR marked `Superseded by 00NN`.
4. Entry in `CHANGELOG.md`.
