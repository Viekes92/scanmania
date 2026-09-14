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

Green means the pure-logic core is covered: every FSM transition (including false starts,
out-of-order checkpoints, all detection modes), scoring, stopwatch and
the reconciler. It does **not** mean the I/O layers are covered — `vision/`, `web/`,
`inputs/`, `iobackend/modbus.py`, `config/loader.py` and `persist/db.py` have no tests, and
the fakes are handed to the runner as inert stubs rather than driven through their own
event pipelines. See `docs/testing.md`.

## Invariants — do not break these

1. **`core/fsm.py` is a pure function.** No I/O, no `await`, no clock reads, no side effects. `transition(state, event, ctx)` returns `(new_state, [SideEffect, ...])`. Metrics are returned as `EmitMetric` effects, never emitted inline. Tests run 10 000 transitions with no mocks.
2. **The server owns the stopwatch.** `time.monotonic_ns()` in `core/stopwatch.py` only. Never `datetime.now()`, never `Date.now()` in a browser. Browsers interpolate from server broadcasts and hard-correct on each message.
3. **`config/beams.json` is the only source of truth for ROIs and thresholds.** `tools/capture.py` and the admin portal write to it; nothing else creates or modifies beam geometry. ROIs live under `mazes.<name>.cameras.<cam>.dots` — one capture per maze, because each shape lights about half the floor and a dot's baseline depends on which of its neighbours are lit. The 45 entries under `beams` are the relay wiring record and are **not** read at runtime. ROIs are frame pixels, so the capture resolution is stored beside them: a substream resolution change silently invalidates every saved ROI. See ADR 0009.
4. **Never write coils outside `iobackend/presets.py`.** Every coil write goes through `apply_preset()`, `apply_all_off()`, `apply_channels()` or `apply_direct()`. The one sanctioned exception is `iobackend/reconcile.py`, which re-asserts desired state and documents itself as such. A direct `write_coils()` leaves `_desired` stale and the reconciler undoes the write within 500 ms.
5. **Vision suppresses events when unsure.** Frame gap > 300 ms → no break events emitted, auto-drop to `manual` detection mode. An uncalibrated preset watches nothing at all. More than `max_simultaneous_breaks` dots dark at once is a hardware fault, not a player, and is suppressed. A false positive ends someone's run in front of a queue; silence is always safer.
6. **Gameplay never awaits the network.** Cloud sync was removed (ADR 0008); there is no outbox and no remote endpoint. The rule still binds every remaining network path — Modbus to the relay boards, Art-Net to the hazer, RTSP to the cameras, WebSocket to the frontends: the game path writes to SQLite and returns. `persist/backup.py` does local snapshots only and is never awaited from the game path.
7. **Never resume a run after a restart.** There is no checkpoint file and nothing is resumed, so `runner.py` starts clean every time. The record-keeping half is now covered too: the run row is written **pessimistically at GO** with `outcome: in_progress`, and `close_orphaned_runs()` settles any such row as `aborted` at the next boot. A crash mid-run leaves a truthful record; it never leaves a resumable one.

## Where things live

```
core/       Pure logic: FSM, stopwatch, scoring, events dataclasses, metrics façade. Zero I/O.
iobackend/  Modbus master, preset resolution, reconciliation loop. fake.py is mandatory.
inputs/     Pico USB serial link + MicroPython firmware. fake.py is mandatory.
vision/     RTSP decode, dot detection, baseline, evidence thumbnails, MJPEG out. fake.py is mandatory.
persist/    SQLite schema + migrations, local snapshots, CSV exports. No cloud sync.
web/        FastAPI + WebSocket broadcast, route handlers, static frontends (no build step).
config/     YAML/JSON files — the only place to change hardware topology or game settings.
tools/      Dev utilities: fake_run.py, capture.py (calibration), camshow.py and
            cam_probe.py (viewers), ramp.py, click_relays.py, deploy.sh.
            laser.py is the operator's own reference file — never modify it.
tests/      Pure-logic modules are covered; every I/O boundary is not (see docs/testing.md).
            FSM tests are the most important — run them first.
docs/       Architecture, protocols, runbooks, ADRs. A PR without doc update is not done.
```

## Conventions

- **Event types:** `SCREAMING_SNAKE_CASE`, defined exclusively in `core/events.py`. Every field documented, every emitter and consumer noted.
- **Metric names:** `<domain>.<thing>.<verb|state>` snake_case — `relay.mismatch`, `run.completed`, `vision.stall`. Constants live in `core/metrics.py`; add new names there, not inline.
- **Config keys:** add to the YAML/JSON file + a one-line comment with purpose and sane range + validation in `config/loader.py`.
- **Commit format:** `<scope>: <what changed>` e.g. `fsm: handle false-start during COUNTDOWN`.
- **Recalibrating:** stop the game (`systemctl stop scanmania-kiosk scanmania` — kiosk first, or its `Wants=` drags the game back up), run `tools/capture.py`, open port 8090. Light a maze, tune each camera, capture, repeat, write. Verify on `/admin/beams`.
- **Module docstrings:** every module opens with its one job, inputs, outputs, and invariants (3–6 lines). No exceptions.

## Things that look wrong but aren't

- **Baseline frozen during a run.** Correct. Adapting mid-run slowly accepts a broken beam as normal. Rolling EMA only runs in `ATTRACT`.
- **`monotonic_ns()` everywhere, never wall clock.** Correct. Wall clock can jump (NTP, DST). `events.ts_wall` stores wall time for human readability only; nothing computes durations from it.
- **FSM returns side effects as data, doesn't execute them.** Correct. This is what makes `test_fsm.py` fast and deterministic without any mocks.
- **Count-in uses `segment_1`, not `all_on`.** Correct. The baseline is captured against exactly the preset that will be lit at GO. Flashing `all_on` would give wrong references for beams not in play.
- **Detection cannot say which segment broke.** Correct, and deliberate. Calibration lights a whole maze and records what the cameras see; no dot is tied to a relay. A bust names the dot and the camera that saw it (`SM-CAM-13:d17`), which is what an operator needs to know where to look. See ADR 0009.
- **`write_coils` writes the full 16-channel board every time.** Correct. One atomic transaction is what makes the maze snap rather than morph. Never loop single-coil writes.

## Operating-day boundary

`SCANMANIA_DAY_START_HOUR` (default 9) sets when the leaderboard and the
end-of-day export roll over, in **local** time. It was UTC midnight, which wiped
the public board at 02:00 local mid-session.

## Definition of done

1. `pytest` green.
2. Docs updated — at minimum the relevant `docs/` file or a note in `plan.md`.
3. ADR added (in `docs/adr/`) if a locked decision changed; old ADR marked `Superseded by 00NN`.
4. Entry in `CHANGELOG.md`.
