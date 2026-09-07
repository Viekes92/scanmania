# Changelog

All notable changes to the ScanMania system are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Fixed

- **Editing the show that is currently playing did nothing until a restart.** `_run_show()` holds a
  reference to the step list it was handed, so `GameRunner.reload_config()` swapping `self.config`
  left the old sequence looping. Saving `attract` from the admin panel reported a successful reload
  while the maze kept running the pre-edit steps. The runner now tracks the playing show's name and
  re-arms it against the reloaded config. `reload_config()` is `async` as a result — its only
  caller, `_reload_config()` in `web/routes_admin.py`, now awaits it.

### Changed

- Show step `hold_ms` ceiling raised from 10 s to 60 s in `ShowStepBody` and in the two admin show
  editor inputs. The old limit rejected legitimate slow attract sequences with a 422; the loader and
  show player never had a cap.

### Admin panel audit — backend and frontend

#### Fixed (correctness / data loss)

- **Logs tab was completely unreachable.** `/api/admin/logs/{unit}` was registered before
  `/api/admin/logs/stream`; Starlette matches in registration order, so the path parameter
  swallowed `stream`. The stream route is now registered first, with a comment pinning the order.

- **Admin edited the wrong config directory in production.** `web/server.py` hardcoded
  `<repo>/config` while `__main__.py` resolves `/etc/scanmania/config`. Every admin config write
  on the NUC targeted files the process never loaded, silently breaking invariant 3. The resolved
  directory is now threaded from `__main__.py` through `ScanManiaApp` into `register_routes`.

- **Unvoid guessed the original outcome.** Voiding overwrote `runs.outcome`, so unvoiding had to
  invent a replacement and could resurrect a busted run onto the leaderboard. Schema v2 adds
  `runs.pre_void_outcome`; unvoid now restores the real value and returns 409 when it is unknown.
  Voiding with an empty reason is rejected with 422.

- **Config writes could brick the next boot.** A saved file is now validated by running the real
  loader over a temp directory containing the full candidate config set before anything is written.
  Writes are serialised under a lock, written atomically with `fsync` on both file and directory,
  and the audit row records the true before/after content.

- **Master mode wrote coils directly**, bypassing `PresetResolver` and leaving `_desired` stale
  (invariant 4). `apply_channels()` and `apply_direct()` in `iobackend/presets.py` now own these
  writes and keep desired state in sync, so the reconciler cannot fight an admin write.

- **The admin API could reset a live player's stopwatch** (invariant 2). All master-mode
  operations are now gated behind `_require_master()` and raise `MasterModeRequired`.

- **Master-mode failures were invisible.** Routes awaited nothing and always returned `ok: true`.
  They now await the runner and translate failures into 404 / 409 / 422 / 502.

#### Fixed (security)

- **Removed the default admin password** that was committed in the repo. Auth now fails closed on
  an unset `SCANMANIA_ADMIN_PASSWORD` (503). To avoid locking out an operator, `__main__.py`
  generates a per-boot password and logs it at WARNING, recoverable via `journalctl`.

- **Gated every unauthenticated admin read** that exposed operational data, hardware topology,
  config audit history (including full file contents), or sync error text. `/api/admin/runs`,
  `/beams` and `/leaderboard` stay open by design — the GM console and outdoor display consume
  them and have no password.

- **Downloads and the log stream are authenticated.** `window.location` and `EventSource` cannot
  send headers, so they now use short-lived, single-use download tokens issued over the
  header-authenticated channel.

- **CSV formula injection** — exported fields beginning with `=`, `+`, `-`, `@`, tab or CR are
  prefixed with a quote.

- **Non-ASCII password header returned 500** instead of failing auth, because Starlette decodes
  headers as latin-1 and `hmac.compare_digest` rejects non-ASCII `str`.

- **Dev triggers are restricted to `--fake-all`** and return 404 otherwise;
  `GET /api/admin/dev/available` lets the UI hide the controls.

- **Journal unit names are validated against an allowlist**, and request bodies are typed and
  bounded via Pydantic models (channel counts, show steps, name patterns, string lengths).

#### Fixed (responsiveness)

- **The event loop no longer blocks.** `subprocess.run` for journalctl became
  `asyncio.create_subprocess_exec` with a timeout, and config file reads/writes moved to
  `asyncio.to_thread`.

- **Run search and pagination moved server-side** (`list_runs(q=...)`, `count_runs`), instead of
  fetching every run and filtering in the browser.

#### Added

- `GET /api/admin/shows`, real uptime and a `faults` list on `/api/admin/status`, per-board Modbus
  error counts and reconcile mismatch counters, and `GameRunner.reload_config()` so config saves
  apply without a restart (deferred, with a note, while a run is in progress).

- `/api/gm/hazer` (unauthenticated, matching the rest of `/api/gm/*`) so the passwordless GM
  console keeps working now that `/api/admin/hazer` requires auth.

#### Changed

- Admin frontend: fixed the maze editor saving the wrong preset, the show editor's play loop,
  403 handling with forced re-login, HTML escaping, delegated run-row listeners, a capped log
  buffer, updates gated to the visible tab, and in-page modals replacing `confirm`/`prompt`.

---

## Phase 2 — Runner wired, hardware configured, 72 tests green

### Added

- **Runner side effects wired** — `core/runner.py` fully wired: FSM side effects executed against
  real or fake backends (io, inputs, vision). Countdown pulse loop driven from `monotonic_ns` with
  absolute scheduling (no accumulated sleep drift). Timeout tasks (`max_run_ms`, `arm_timeout_ms`)
  scheduled and cancelled correctly on state transitions.

- **`python -m scanmania` entry point** — `__main__.py` wires argparse (`--fake-all`, `--fake-io`,
  `--fake-vision`, `--fake-inputs`), constructs backends, and launches all async tasks via a single
  `asyncio.run()`. SIGINT/SIGTERM trigger a graceful shutdown: snapshot written, outbox drained,
  services stopped in reverse order.

- **OutboxWorker env-var driven** — `SCANMANIA_CLOUD_URL` and `SCANMANIA_CLOUD_TOKEN` read from
  environment (sourced from `/etc/scanmania/secrets.env` in production). Worker skips cloud sync
  if `SCANMANIA_CLOUD_URL` is unset, accumulating outbox rows silently until configured. No
  credentials in the repo.

- **Admin status with live runner state** — `GET /api/admin/status` returns current FSM state,
  active player, detection mode, beam health summary, outbox depth, and last sync timestamp.
  Admin dashboard auto-refreshes every 2 s via fetch().

- **23 audit issues fixed** — issues found during Phase 1 review: monotonic clock not propagated
  correctly into WebSocket broadcast, FSM `VOID` transition missing from `RESULT` state, outbox
  worker not woken on insert, assisted-mode veto not crediting elapsed time back, count-in pulse
  timing drifting on slow systems, arm-grace window off by one frame, several missing event
  emissions, and incorrect beam-state reset on `FORCE_RESET`.

- **Runner tests (72 tests green)** — `tests/test_runner.py`: every FSM transition exercised via
  fake backends end-to-end. Includes false starts, out-of-order checkpoint events, break during
  each segment, `max_run_ms` abort, service restart mid-run (→ `ABORTED` → `RESET`), detection
  mode switches mid-run, `VOID` after `FINISHED`, `VOID` after `BUSTED`, and outbox idempotency
  across simulated retries.

### Changed

- **Sign-in redesign — full contact fields** — `players` table and sign-in form updated with:
  `first_name`, `surname`, `email`, `dob` (date of birth), `gender`. The display name shown on
  the GM console and leaderboard is derived as `first_name + surname[0]` (e.g. "Sarah K.").
  All fields validated server-side. Email is stored hashed for GDPR-friendliness; raw email
  retained in `extra_json` under a separate consent flag.

- **GM page inline sign-in** — `/gm` now embeds a compact sign-in panel (collapsible) so the
  gamemaster can register a walk-up player without handing the iPad to a steward. Full `/signin`
  iPad flow remains the primary path for attended queues.

- **Display welcome screens** — `/display/in` and `/display/out` show a welcome/idle screen in
  `ATTRACT` state: animated logo, "STEP UP TO PLAY", and live leaderboard. These replace the
  previous blank attract state on both displays.

### Hardware configuration

- **45 laser strips, 9 rows × 5 columns** — `config/beams.json` authored for the full geometry:
  45 beams, IDs `b01`–`b45`, arranged in a 9-row × 5-column grid. Each beam assigned to a relay
  channel and a camera ROI.

- **3 relay boards: SM-NODE-1, SM-NODE-2, SM-NODE-3** — `config/hardware.yaml` defines:
  - `SM-NODE-1`: 16 channels (beams b01–b16), static IP `10.0.0.11`
  - `SM-NODE-2`: 16 channels (beams b17–b32), static IP `10.0.0.12`
  - `SM-NODE-3`: 13 channels (beams b33–b45), static IP `10.0.0.13`

- **Modbus RTU over TCP, port 4196** — Waveshare boards configured in transparent/passthrough
  mode. The wire protocol is Modbus RTU (not standard Modbus TCP with MBAP header). `io/modbus.py`
  sends raw RTU frames over a TCP socket to port 4196. See ADR 0001 for the full explanation.

- **Admin grid visualisation with masking** — `/admin` Beams tab renders a 9×5 grid of beam pills
  showing live ratio, state (intact/broken/masked/offline), and threshold bars. Clicking a cell
  toggles the `masked` flag. The grid matches the physical layout so on-site diagnosis is
  immediate.

- **Real Modbus backend wired** — `io/modbus.py` production path active: RTU-over-TCP raw socket
  per board, 200 ms connect timeout, 3 consecutive failures → `DEGRADED` metric + GM console
  amber. Reconciliation loop (500 ms) reads actual coil state via function 0x01 and re-asserts
  if it differs from desired state.

- **RTSP camera URL configured** — `config/hardware.yaml` camera entry updated with the real
  substream URL (`rtsp://10.0.0.30:554/substream`). Resolution locked to 1280×720 @ 25 fps.
  Exposure, gain, white balance, and focus locked via ONVIF commands on first connect.

---

## Phase 1 — Skeleton and docs, zero hardware required

### Added

- `CLAUDE.md` — repo orientation, invariants, conventions, definition of done
- `plan.md` — full system plan (§1–§16), locked decisions, build phases
- `docs/glossary.md` — beam, dot, cluster, channel, preset, segment, run, bust, void, mask,
  gamemaster, master mode
- `docs/architecture.md` — system topology, service map, data flow diagram
- `docs/adr/0001-modbus-tcp-not-artnet.md` — why Modbus TCP (RTU-over-TCP) replaced Art-Net
- `docs/adr/0002-ceiling-dot-detection.md` — why ceiling dots, not beam-line sampling
- `docs/adr/0003-hard-cutoff-scoring.md` — a break ends the run immediately
- `docs/adr/0004-pico-over-usb-not-raspberry-pi.md` — Pico for physical inputs
- `docs/adr/0005-systemd-not-docker.md` — native Python + systemd units
- `docs/adr/0006-local-first-cloud-backup.md` — SQLite source of truth, outbox sync
- `docs/adr/0007-detection-modes-and-master-mode.md` — AUTO/ASSISTED/MANUAL + master mode

- `config/hardware.yaml` — relay board IPs, camera URLs, channel map
- `config/mazes.yaml` — preset channel lists, show definitions
- `config/beams.json` — dot ROIs, thresholds, masks (source of truth)
- `config/game.yaml` — timings, detection mode, count-in pulse list
- `config/metrics.yaml` — metric sink configuration
- `config/loader.py` — typed dataclass loader with cross-config validation

- `core/events.py` — full event and side-effect type definitions; the wire contract
- `core/fsm.py` — pure state machine (no I/O); all transitions, all detection modes
- `core/stopwatch.py` — monotonic elapsed timer using `time.monotonic_ns()`
- `core/scoring.py` — rank computation, leaderboard queries
- `core/metrics.py` — `emit()` façade, pluggable sinks, naming convention
- `core/runner.py` — skeleton wiring FSM to I/O backends (Phase 1 stub)

- `io/modbus.py` — async Modbus TCP master, one connection per board, 200 ms timeout
- `io/presets.py` — preset → per-board coil arrays, `apply_preset()`, master-mode toggle
- `io/reconcile.py` — desired-vs-actual reconciliation loop at 500 ms
- `io/fake.py` — in-memory relay board simulator (MANDATORY for laptop dev)

- `inputs/pico_link.py` — serial protocol handler, clock offset estimation, auto-reconnect
- `inputs/fake.py` — no-op Pico link fake (MANDATORY)
- `inputs/firmware/main.py` — MicroPython firmware for the Raspberry Pi Pico

- `vision/camera.py` — RTSP decode, stall detection (300 ms gap), drift check
- `vision/detect.py` — dot ROI sampling, hysteresis, cooldowns, global rate limit
- `vision/baseline.py` — rolling EMA in ATTRACT, flash-capture at count-in pulse 0
- `vision/evidence.py` — JPEG crop thumbnails saved per beam hit
- `vision/mjpeg.py` — downscaled MJPEG re-stream at http://localhost:8081/cam_a.mjpg
- `vision/fake.py` — replay from recorded video / no-op fake (MANDATORY)

- `persist/db.py` — SQLite schema + migrations, WAL mode, all query helpers
- `persist/outbox.py` — continuously running drain worker, exponential backoff, never drops rows
- `persist/sync.py` — cloud POST with Idempotency-Key, backoff, snapshot/export utilities

- `web/server.py` — FastAPI app, WebSocket hub broadcasting at 10 Hz, static file serving
- `web/routes_signin.py` — POST /api/signin → PlayerRegistered event
- `web/routes_gm.py` — POST /api/gm/* → all gamemaster actions
- `web/routes_admin.py` — GET/POST /api/admin/* → all admin portal endpoints

- `web/static/signin/index.html` — player sign-in: contact fields (first name, surname, email,
  DOB, gender), LET'S GO button, success overlay, wait screen for game-in-progress states,
  WebSocket state mirroring
- `web/static/gm/index.html` — gamemaster console: top bar (state/stopwatch/player/detection
  badge), primary action buttons (COUNT IN/BUST/ABORT/VOID/FORCE RESET), detection mode
  segmented control, live beam strip (tap to mask), health strip, assisted-mode confirm/veto
  overlay, inline sign-in panel, recent runs list; 60 fps stopwatch interpolation
- `web/static/admin/index.html` — admin portal: Dashboard, Hardware, Beams (9×5 grid),
  Presets, Config, Runs, Leaderboard, Cloud Sync, Logs, Master Mode tabs; dark/light theme toggle
- `web/static/display_in/index.html` — in-container display (HDMI 1): accelerating countdown
  ring, enormous MM:SS.mmm stopwatch through haze, BUSTED/CLEAN! full-screen states, ATTRACT
  welcome screen; 60 fps stopwatch interpolation
- `web/static/display_out/index.html` — outdoor display (HDMI 2): MJPEG background (graceful
  fallback), stopwatch overlay, leaderboard when idle, BUSTED/FINISHED overlays; 60 fps
  stopwatch interpolation

- `tools/fake_run.py` — CLI script driving clean/busted/aborted/voided runs via fake backends;
  prints final run record as JSON
- `tools/ramp.py` — generate count-in pulse list from (total_ms, pulses, ratio, duty); outputs
  YAML block + ASCII timeline + warnings for short pulses
- `tools/pick_rois.py` — OpenCV GUI: click ceiling dots → JSON stanzas for beams.json;
  auto-sequences IDs, right-click undo, Q to finish

- `requirements.txt` — all dependencies with minimum version pins
- `__main__.py` — entry point: argparse, --fake-all/io/vision/inputs, asyncio task
  orchestration, SIGINT/SIGTERM graceful shutdown, snapshot on exit
- `tests/conftest.py` — pytest fixtures: config, fake_config, db (in-memory SQLite),
  fake_io, fake_inputs, fake_vision, fsm_context

### Phase 1 gate

- `pytest tests/ -v` green
- `python tools/fake_run.py --scenario clean` prints a valid run record
- `python tools/fake_run.py --scenario busted` prints outcome=busted
- `python tools/fake_run.py --scenario aborted` prints outcome=aborted
- `python tools/fake_run.py --scenario voided` prints outcome=voided
- `python -m scanmania --fake-all` starts without error; frontends live at http://localhost:8000
- A fresh reader can run it from `CLAUDE.md` alone (no prior context)
