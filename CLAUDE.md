# CLAUDE.md — ScanMania

## What this is
ScanMania is a laser-maze game in a shipping container. A camera watches the ceiling where each laser beam terminates as a bright dot; a dot disappearing means a beam was broken. A player runs from a start plate to a stop button — break a beam and the run is over, fastest clean run wins.

## How to run on a laptop (no hardware)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scanmania --fake-all        # io/ + inputs/ + vision/ + audio/ fakes active
```

Open http://localhost:8000 — frontends are live. Drive a full run from another terminal:

```bash
python tools/fake_run.py --scenario clean    # player walks through clean
python tools/fake_run.py --scenario busted   # player breaks beam b03 mid-run
python tools/fake_run.py --scenario aborted  # max_run_ms exceeded
python tools/fake_run.py --scenario voided   # gamemaster voids after the fact
```

`fake_run.py` builds its **own** runner in its own process — it does not drive a
`python -m scanmania` you have running, so watch its output, not the server's.

**Hearing the soundtrack.** Audio is real under `--fake-all` (it is the one thing
a laptop already has hardware for), and on in `fake_run.py`. Add `--pace` or the
run is over before you can hear it:

```bash
python tools/fake_run.py --scenario clean --pace 3   # hear the whole arc
python tools/fake_run.py --scenario busted --pace 3  # ...and a GM bust
python tools/fake_run.py --scenario clean --silent   # no audio
```

To audition one file, or to check what actually loaded: **Admin → Hardware →
Audio**. To drive the *server* through a run instead, force-reset out of MASTER
first (the box boots into it), then use the GM console or
`POST /api/admin/dev/trigger`.

Frontends:
- `/gm`           — gamemaster console (iPad, includes player sign-in)
- `/admin`        — admin portal (laptop)
- `/display/in`   — in-container stopwatch display (HDMI 1)
- `/display/out`  — outdoor display: stopwatch + leaderboard (HDMI 2). No camera
                    feed: `vision/mjpeg.py` was removed and BACK_CAM_URL is
                    hardcoded null, so the black background is by design.

## How to run the tests

```bash
pytest tests/ -v
```

Green means the pure-logic core is covered: every FSM transition (including false starts,
out-of-order checkpoints, all detection modes), scoring, stopwatch and
the reconciler. It does **not** mean the I/O layers are covered — `inputs/`,
`iobackend/modbus.py`, `config/loader.py` and `persist/db.py` have no tests, and `vision/`
and `web/` are only partly covered (`test_maze_dots.py`, `test_vision_service.py`,
`test_audit_fixes.py`), and
the fakes are handed to the runner as inert stubs rather than driven through their own
event pipelines. See `docs/testing.md`.

## Invariants — do not break these

1. **`core/fsm.py` is a pure function.** No I/O, no `await`, no clock reads, no side effects. `transition(state, event, ctx)` returns `(new_state, [SideEffect, ...])`. Metrics are returned as `EmitMetric` effects, never emitted inline. Tests run 10 000 transitions with no mocks.
2. **The server owns the stopwatch.** `time.monotonic_ns()` in `core/stopwatch.py` only. Never `datetime.now()`. A browser may call `Date.now()` for wall-clock display ("finished at 14:32") but must never compute a DURATION from it — the stopwatch interpolates from `performance.now()` and hard-corrects on every server broadcast. Browsers interpolate from server broadcasts and hard-correct on each message.
3. **`config/beams.json` is the only source of truth for ROIs and thresholds.** `tools/capture.py` and the admin portal write to it; nothing else creates or modifies beam geometry. ROIs live under `mazes.<name>.cameras.<cam>.dots` — one capture per maze, because each shape lights about half the floor and a dot's baseline depends on which of its neighbours are lit. The 45 entries under `beams` are the relay wiring record and are **not** read at runtime. ROIs are frame pixels, so the capture resolution is stored beside them: a resolution change on the MAIN stream (which is what we decode) silently invalidates every saved ROI. See ADR 0009.
4. **Never write coils outside `iobackend/presets.py`.** Every coil write goes through `apply_preset()`, `apply_all_off()`, `apply_channels()` or `apply_direct()`. The one sanctioned exception is `iobackend/reconcile.py`, which re-asserts desired state and documents itself as such. A direct `write_coils()` leaves `_desired` stale and the reconciler undoes the write within 500 ms.
5. **Vision suppresses events when unsure.** A dot whose baseline is below
   `detection.min_baseline` is not watched at all and is reported as `blind`,
   because `ratio = value/baseline` on a near-zero baseline is noise divided by
   noise (ADR 0011). Frame gap > 300 ms → no break events emitted, auto-drop to `manual` detection mode. An uncalibrated preset watches nothing at all. More than `max_simultaneous_breaks` dots dark at once is a hardware fault, not a player, and is suppressed. A false positive ends someone's run in front of a queue; silence is always safer.
6. **Gameplay never awaits the network.** Cloud sync was removed (ADR 0008); there is no outbox and no remote endpoint. The rule still binds every remaining network path — Modbus to the relay boards, Art-Net to the hazer, RTSP to the cameras, WebSocket to the frontends: the game path writes to SQLite and returns. `persist/backup.py` does local snapshots only and is never awaited from the game path.
7. **Never resume a run after a restart.** There is no checkpoint file and nothing is resumed, so `runner.py` starts clean every time. The record-keeping half is now covered too: the run row is written **pessimistically at GO** with `outcome: in_progress`, and `close_orphaned_runs()` settles any such row as `aborted` at the next boot. A crash mid-run leaves a truthful record; it never leaves a resumable one.

## Where things live

```
core/       Pure logic: FSM, stopwatch, scoring, events dataclasses, metrics façade. Zero I/O.
iobackend/  Modbus master, preset resolution, reconciliation loop, Art-Net DMX
            (hazer + room lights), FSM-driven light cues. fake.py is mandatory.
inputs/     Pico USB serial link + MicroPython firmware. fake.py is mandatory.
vision/     RTSP decode, dot detection, baseline, evidence thumbnails. fake.py is mandatory. (No MJPEG — removed.)
audio/      The soundtrack: player.py owns the device, cues.py maps FSM states
            onto it, fake.py is mandatory. Sounds live in sounds/ as .wav.
persist/    SQLite schema + migrations, local snapshots, CSV exports. No cloud sync.
web/        FastAPI + WebSocket broadcast, route handlers, static frontends (no build step).
config/     YAML/JSON files — the only place to change hardware topology or game settings.
tools/      Dev utilities: fake_run.py, capture.py (calibration), camshow.py and
            cam_probe.py (viewers), ramp.py, click_relays.py, deploy.sh.
            laser.py is the operator's own reference file — never modify it.
            shutdown.py is the end-of-day close: save, then darken.
            gen_placeholder_sounds.py writes stand-in .wav files into sounds/;
            push_sounds.sh copies the real soundtrack to the box.
sounds/     The audio the container plays. GITIGNORED except the README —
            push it with tools/push_sounds.sh, not deploy.sh. A fresh checkout
            has none and runs silent; see sounds/README.md.
tests/      Pure-logic modules are covered; every I/O boundary is not (see docs/testing.md).
            FSM tests are the most important — run them first.
docs/       Architecture, protocols, runbooks, ADRs. A PR without doc update is not done.
```

## Conventions

- **Event types:** PascalCase dataclasses (`PlateHigh`, `Cp1Pressed`), defined exclusively in `core/events.py`. The `SCREAMING_SNAKE_CASE` names in that file are FSM *states*, not events. Every field documented, every emitter and consumer noted.
- **Metric names:** `<domain>.<thing>.<verb|state>` snake_case — `relay.mismatch`, `run.completed`, `vision.stall`. Constants live in `core/metrics.py`; add new names there, not inline.
- **Config keys:** add to the YAML/JSON file + a one-line comment with purpose and sane range + validation in `config/loader.py`.
- **Commit format:** `<scope>: <what changed>` e.g. `fsm: handle false-start during COUNTDOWN`.
- **DMX:** one universe, one owner — `iobackend/dmx.py`. Every Art-Net frame carries all 512 channels, so a second sender would zero the first one's work twice a second. Patch: ch1 hazer blower, ch2 haze, ch3 left, ch4 right, ch5 entrance. Run `python3 tools/dmxpatch.py` for the live sheet; it is generated from config, never hand-maintained. Haze is **duty-cycled** (`haze_burst_s` / `haze_interval_s`) because continuous output at any usable level is too much. Light levels come from `light_cues` in `mazes.yaml`, keyed by FSM state. The entrance is `always_on`, which now means **only a light cue may dim it** (`allow_always_on=True`, passed by `lightshow._apply()` alone) — see ADR 0010. It is dark from sign-in to the result and lit in attract/master/aborted/fault, the states where somebody is walking in or out. Two absolutes remain: `blackout()`, which is what a dying process calls, RESTORES it, and `DmxController.power_down()` is the only path that leaves it dark — end of day, operator at the breaker. COUNTDOWN and every RUN state are forced dark in code, over any cue and over the GM's work-light switch.
- **Breaks cost TIME, not the run.** A confirmed beam break emits
  `ApplyTimePenalty` and the player keeps running; `game.penalty_ms` is the
  cost and `game.penalty_cooldown_ms` is the minimum RUN-time gap between two
  penalties. The cooldown is load-bearing: without it a player parked in a beam
  collects one penalty per detection. `BUSTED` still exists but is reachable
  ONLY by the gamemaster pressing BUST — for cheating, climbing, or leaving and
  re-entering the maze, which no camera can judge. `Stopwatch.elapsed_ms()`
  includes penalties; `raw_elapsed_ms()` is what the clock measured, and the run
  row keeps both.
- **Audio:** decoration, and it must never be able to end a run — a missing
  file, a dead sound card or a mixer that will not start are all silence plus a
  log line, never an exception on the game path. `audio.music` in `game.yaml` is
  keyed by FSM state and a state that is **not listed keeps whatever is
  playing**, which is what carries one track across `RUN_SEG_1/2/3` instead of
  restarting it at each checkpoint; ask for quiet by name with `silence`.
  `audio.cues` fires one-shots on entering a state. A bed normally LOOPS; list it
  under `audio.music_once` (`{track: what follows it}`) to play it once and
  hand back — `end.mp3` is an eight-second sting, and looping it left the
  fanfare repeating under the score for the rest of the result. The hand-back
  is a polling thread guarded by a generation counter, so a sting superseded by
  a state change can never come back and stamp on the new bed. **Cues are `.wav`** — decoded
  into RAM at startup, because the game path may only call `play()`. **The bed
  is `.mp3`/`.ogg`** — it streams, and an 8-hour ambient track as `.wav` is
  5 GB. Both are checked at startup (`verify_music`), so a codec the box cannot
  decode is a boot-log line rather than silence discovered mid-show.
- **Recalibrating — two different jobs.** *Geometry* (the container was moved,
  the dots drifted): **Admin → Calibration → Check**, then Save. It runs inside
  the game, lights each maze, re-finds every dot and re-measures its baseline,
  reusing the per-camera thresholds untouched. Needs MASTER MODE and nobody
  inside; it takes the room lights **and the entrance** dark itself for the
  capture and restores them afterwards, so the ambient gate is not failed by
  the one fixture the DMX layer otherwise refuses to dim. It is a dry run until
  you press Save, backs up `beams.json`, and refuses a result that loses more
  than a quarter of the dots.
  *Tuning* (a camera's exposure changed, dots are not being found at all) still
  needs the full tool below — there is no substitute for watching each stage.
- **Retuning:** stop the game (`systemctl stop scanmania-kiosk scanmania` — kiosk first, or its `Wants=` drags the game back up), run `tools/capture.py`, open port 8090. Light a maze, tune each camera, capture, repeat, write. Verify on `/admin/beams`.
- **Module docstrings:** every module opens with its one job, inputs, outputs, and invariants (3–6 lines). No exceptions.

## Things that look wrong but aren't

- **Baseline frozen during a run.** Correct. Adapting mid-run slowly accepts a
  broken beam as normal. The EMA is frozen from **ARM entry** (not GO) to
  DISARM — ARM lights `arm_box` while the detector still watches the maze, so
  every sample in between is of an *unlit* dot, and feeding those to the EMA
  walked baselines down to the dark level with a 4 s time constant. They were
  then frozen in at GO and the maze was silently blind for the rest of the
  session. `update_ema` additionally refuses any sample outside 0.5x-2x of the
  calibrated value.
- **The rolling EMA barely runs at all, and that is fine.** It was documented as
  running in `ATTRACT`. It cannot: the only presets with ROI captures are
  `maze_1/2/3`, and the attract show lights `all_on`, which has none — so there
  are no dots to sample. Baselines are calibration-time constants in all but
  name. Do not "fix" this by giving the attract show a maze step without also
  thinking about what an unattended EMA can do to a run.
- **`monotonic_ns()` everywhere, never wall clock.** Correct. Wall clock can jump (NTP, DST). `events.ts_wall` stores wall time for human readability only; nothing computes durations from it.
- **FSM returns side effects as data, doesn't execute them.** Correct. This is what makes `test_fsm.py` fast and deterministic without any mocks.
- **The count-in starts 4 s after the GM taps COUNT IN.** Deliberate, and not
  a hang. The spoken "3-2-1" is baked into `game.mp3` — the words land at
  4.0 s, 5.15 s and 6.05 s with GO at 7.0 s — so the bed starts on the tap and
  the visual ramp waits `count_in.audio_lead_ms` (4000) before beginning. GO is
  `audio_lead_ms` + the 3000 ms pulse total. The in-container display shows
  GET READY during the lead, because `countdown_remaining_ms` is deliberately
  null until the ramp anchors: a deadline set at the tap would make the display
  count down from 7, and it must read 3-2-1. Tune the lead by ear against the
  track — mixer start-up latency is part of the offset.

  The clock is still the authority, never the audio: the lead is a monotonic
  wait, nothing listens to the mixer, and a missing file or a dead sound card
  costs the voice-over and nothing else. The ramp takes exactly as long and GO
  lands on time, silently. Audio must never be able to change when a run
  starts.

- **Count-in FLASHES `all_on`, not the maze about to be played.** Deliberate: players read a flash of the real shape as "go now" and start early, so the ramp shows the whole grid, which carries no route information. Safe because nothing measures a baseline during the ramp — runtime baselines come from `beams.json` and `count_in.baseline_pulse_index` is parsed but unused — and the FSM applies the real maze at GO before detection arms. `count_in.preset` still names the maze lit at GO; `count_in.flash_preset` is what the ramp shows.
- **Detection cannot say which segment broke.** Correct, and deliberate. Calibration lights a whole maze and records what the cameras see; no dot is tied to a relay. A bust names the dot and the camera that saw it (`SM-CAM-13:d17`), which is what an operator needs to know where to look. See ADR 0009.
- **`write_coils` writes the full 16-channel board every time.** Correct. One atomic transaction is what makes the maze snap rather than morph. Never loop single-coil writes.

## Displays

Both are single-file HTML with no build step, styled from
`web/static/shared/brand.css` — Proxima Nova loaded from disk (no CDN: a venue
with no internet must still render), red `#EC1C24`, blue `#005AA9`, both sampled
from the supplied artwork. The laser lines are artwork, not drawn, so they match
the printed collateral: `lasers-h.png` on `/display/in` (16:9), `lasers-v.png` on
`/display/out` (9:16).

`/display/out` is **portrait** — people photograph it with a phone. The panel is
mounted rotated and `kiosk.sh` tells X (`SCANMANIA_ROTATE_OUT`, default `left`),
swapping width and height for the Chromium window because there is no window
manager to ask.

Player nicknames are written with `textContent`, never `innerHTML`. They are
public input and `/display/out` faces the street.

## Boot behaviour

The box comes up in **MASTER MODE** with the house lights on and the lasers
dark (`game.boot_to_master`, default true). The GM walks the container, then
presses **FORCE RESET** on the console to drop into ATTRACT. Nothing is playable
until a human has been inside — a box that boots straight into its attract show
after a power cut invites someone to start a run before anyone has looked at the
maze. The flag is consumed on the first self-test pass, so exiting MASTER later
goes to ATTRACT rather than looping back.

## Operating-day boundary

`SCANMANIA_DAY_START_HOUR` (default 9) sets when the leaderboard and the
end-of-day export roll over, in **local** time. It was UTC midnight, which wiped
the public board at 02:00 local mid-session.

## Definition of done

1. `pytest` green.
2. Docs updated — at minimum the relevant `docs/` file or a note in `plan.md`.
3. ADR added (in `docs/adr/`) if a locked decision changed; old ADR marked `Superseded by 00NN`.
4. Entry in `CHANGELOG.md`.
