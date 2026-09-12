# Changelog

All notable changes to the ScanMania system are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Fixed

- **The reconciler re-lit the maze during count-in and throughout ARM.** `_run_count_in_ramp()`
  and `_handle_ready_blink()` called `board.write_coils([False] * 16)` directly, bypassing
  `iobackend/presets.py` and leaving the resolver's `_desired` state lit. `ReconcileLoop` saw the
  drift and re-asserted within 500 ms. Measured: lasers re-lit 354 ms into a 420 ms dark gap, about
  2.6 times per count-in, and — because the ARM handler emits no `ApplyPreset` — the full maze
  stayed lit for the entire ARM state, up to `arm_timeout_ms` (3 minutes), while the player stood
  on the start plate. Latent since the initial commit; activated by `8a8ea83` turning the
  reconciler on. Both sites now use a new `PresetResolver.apply_all_off()`, which updates
  `_desired` and does not depend on the user-editable `blackout` preset.

- **The attract show ran straight through REGISTERED, ARM and the count-in ramp.** Nothing
  cancelled `_show_task` until `ApplyPreset("maze_1")` at `RampComplete`, so the show kept writing
  coils every 300–500 ms while the ready blink and flash ramp were trying to drive them. It also
  partially masked the bug above. New `StopShow` side effect, emitted on ATTRACT → REGISTERED.

- **A subsystem crash left the game dead but the process alive.** `GameRunner.run()` used
  `asyncio.gather()` with no `return_exceptions`, so the first failure re-raised and skipped the
  sibling-cancellation loop, orphaning the other tasks. The clock kept broadcasting and both
  displays looked healthy while the start plate did nothing, with no log line until shutdown —
  and because the process never exited, `Restart=always` never fired. Now uses
  `asyncio.wait(FIRST_EXCEPTION)`, logs the dead task at CRITICAL, cancels the rest and re-raises;
  `__main__` waits on the service tasks alongside the shutdown signal and exits non-zero.

- **The GM console's VOID button was silently inert.** `GmVoid` emitted `SaveRun(outcome="voided")`,
  which ran a plain `INSERT` against a primary key that already existed. The resulting
  `IntegrityError` was logged to syslog and swallowed, the HTTP response was still 200, and the
  broadcast state flipped to "voided" while SQLite still said "clean" — so the run kept its
  leaderboard slot and the GM's reason was discarded. New `VoidRun` side effect routes to the
  existing, idempotent `db.void_run()`, which preserves `pre_void_outcome` for unvoid. `GmVoid` is
  now restricted to post-run states, closing the reverse failure where voiding mid-run wrote the
  row first and made the real end-of-run save collide. `insert_run()` is an upsert as defence in
  depth, and will not revert a void.

- **Saving config mid-run blacked out the maze.** The reload guard in `web/routes_admin.py`
  compared against `"RUN"` and `"HALTED"`, neither of which is a state, so it failed open through
  ARM and all three `RUN_SEG_*` states. `reload_config()` then built a fresh `PresetResolver` whose
  desired state is all-off and the reconciler drove every laser dark within 500 ms, while the admin
  UI reported success. Now uses `core.events.RUN_STATES`, which already existed.

- **A relay board that failed every transaction reported healthy.** `connect()` cleared
  `_consecutive_failures`, so a board that drops the connection each request reconnected
  constantly and never reached `DEGRADED`. Measured: 40 of 40 writes failed with `status="OK"` and
  an empty fault list. Since the game path ignores `apply_preset()`'s return value, the maze
  silently stopped changing with nothing on the admin page. Only a completed transaction clears the
  counter now, `error_count` is rendered in the admin board table (the API already returned it),
  non-timeout `OSError` closes the socket deliberately rather than relying on an `AttributeError`
  escaping a narrow `except`, and sockets set `SO_KEEPALIVE`.

- **Both displays kept a stopwatch running after the WebSocket dropped.** `swRunning` was never
  cleared on close, so the ticker extrapolated off `performance.now()` indefinitely — the outdoor
  display showed the crowd a run that never ended, past `max_run_ms`, with nobody on stage. The
  only indicator was a badge measured at 1.24:1, and 1.05:1 over a live camera frame. Both now
  freeze the clock on close and show `NO SIGNAL — CLOCK STOPPED` at 7.9:1.

- **The FAULT screen was blank.** `#waitingText` was 1.23:1 on the in-container display, so a
  faulted machine showed the player nothing at all. FAULT now has its own message and treatment.

- **GM endpoints accepted cross-origin POSTs.** The app had no middleware, so any page in a browser
  that could route to the NUC — including the GM's own Wi-Fi iPad — could bust or force-reset a run
  via a simple form POST. An Origin guard now rejects foreign origins on state-changing methods;
  a missing Origin still passes, so `curl` and `tools/` are unaffected.

- **`python -m scanmania` without a fake flag crashed after migrating the database.**
  `vision.camera` had no `VisionService`, so the documented "production" invocation died mid-boot
  and never bound port 8000. `vision/service.py` now provides that class — see **Added**. The
  invocation works; the remaining prerequisite is calibrating `config/beams.json`, which is still
  uncalibrated (45 entries, every ROI `0,0`, all on `cam_a`).

- **`test_pause_stops_drain` could not fail.** It re-implemented the pause check inside the test
  body, so `_drain()` was never called and the final assertion compared a number to itself. The
  pause guard moved into `_drain()` where the work happens, and the test asserts the row's
  `attempts` counter — queue depth alone is not a discriminator, because the push fails either way
  without an endpoint. Verified by deleting the guard and watching the test go red.

### Added

- **`vision/service.py` — the object that was missing.** `CameraStream`,
  `DotDetector`, `BaselineManager`, `EvidenceCapture` and `MjpegServer` all existed
  and were individually plausible, but nothing constructed or connected them.
  `__main__.py` imported a `VisionService` that was never written, and
  `core/runner.py`'s docstring called it `CameraManager` — the two call sites did
  not even agree on the name. `VisionService` mirrors `vision/fake.py`'s interface
  exactly, so the runner cannot tell which one it holds.
- **Invariant 5 is now reachable.** It had two independent breaks. `VisionStalled`
  was emitted only by `test_fsm.py`, and `runner._vision_listener` understood
  `"break"` tuples only, so a stall would have been discarded even if something
  had emitted one. Stall detection also moved out of the frame-read loop into a
  watchdog task: `CameraStream` only ever checks the gap when a frame *arrives*,
  so the two genuine no-frame cases — a blocking read that never returns, and a
  failed read that goes down the reconnect path — both bypassed it, and the
  reconnect path cleared the flag on the way through.
- `PresetResolver.apply_all_off()` — config-independent all-off that keeps `_desired` in sync.
- `StopShow` and `VoidRun` side effects.
- `deploy/scanmania.service` — the unit existed only on the NUC, so `Restart=always` was documented
  in prose that nothing enforced. `deploy/README.md` records that `scanmania-kiosk.service` and
  `tools/kiosk.sh` are still NUC-only.
- Rolling DB snapshots now actually run. `rolling_snapshot_loop()` had no callers, so an unclean
  shutdown lost every run back to the last manual snapshot. New `snapshot_interval_min` and
  `snapshot_keep` keys in `config/game.yaml`.
- `docs/testing.md` — what the suite does and does not cover, and how to write a test that can fail.
- Regression tests for the reconciler/all-off interaction, the attract-show stop, and the void path.

### Changed

- Display type scale: every `clamp()` on both HDMI screens saturated below 1500 px while the panels
  are 2560 px, so both rendered at roughly half their intended size. Caps raised — the in-container
  stopwatch from 260 px to 520 px, the countdown digit from 120 px to 560 px inside its 720 px ring.
- Text colours on `/gm` and both displays now use the palette `admin/index.html` already
  established and documented (`#5b8fd6` at 6.15:1, `#6c8cae` at 5.82:1), replacing `#264f9a` at
  2.59:1. Chrome — ring strokes, glows, gradients — is unchanged.
- `core/fsm.py` returns the state-transition metric as an `EmitMetric` effect instead of calling
  `metrics.emit()` inline. That call was harmless only because no sink is configured; wiring one up
  would have made the pure FSM perform I/O on every transition. **Move any sink wiring in after
  this change, not before.**
- `web/static/shared/test.py` moved to `tools/click_relays.py`. It was served unauthenticated from
  the web root, disclosing board IPs, port 4196 and a working relay driver.
- CLAUDE.md corrected: `io/` is `iobackend/`, `tools/replay.py` does not exist, `docs/metrics.md`
  does not exist, the FSM signature takes a context argument, `pick_rois.py` prints rather than
  writes, invariant 7's record-keeping half is documented as unimplemented, and the test-coverage
  claim now matches reality.

- **Editing the show that is currently playing did nothing until a restart.** `_run_show()` holds a
  reference to the step list it was handed, so `GameRunner.reload_config()` swapping `self.config`
  left the old sequence looping. Saving `attract` from the admin panel reported a successful reload
  while the maze kept running the pre-edit steps. The runner now tracks the playing show's name and
  re-arms it against the reloaded config. `reload_config()` is `async` as a result — its only
  caller, `_reload_config()` in `web/routes_admin.py`, now awaits it.

- **The invariant-4 safety backstop was never running.** `ReconcileLoop` was fully implemented
  but never instantiated, so nothing detected or corrected relay coils that drifted from the
  desired state. `GameRunner.run()` now starts it as the `reconcile` task. Its constructor takes a
  callable returning the resolver instead of the resolver itself, so a config reload doesn't strand
  it on the pre-edit desired state. Per-board `mismatch_count` added to `GET /api/admin/hardware`.
  Covered by `tests/test_reconcile.py`.

### Added

- `tools/deploy.sh` — one-command update for the NUC. Refuses to run with uncommitted work outside
  `config/`, commits and pushes live config drift (the admin panel writes into the checkout), backs
  up the database before the automatic schema migration, pulls, reinstalls dependencies only if
  `requirements.txt` changed, restarts both services and health-checks the API.

### Changed

- Show step `hold_ms` ceiling raised from 10 s to 60 s in `ShowStepBody` and in the two admin show
  editor inputs. The old limit rejected legitimate slow attract sequences with a 422; the loader and
  show player never had a cap.

### Fixed — count-in and GM health LEDs

- **The count-in raced 8→7→6 instead of counting seconds.** `display_in` rendered the *pulse
  index* (`countdown_total - countdown_step`), and the pulse ramp is deliberately accelerating —
  eight pulses in 2.13 s. The runner now broadcasts `countdown_remaining_ms` (a real deadline,
  computed server-side per invariant 2) and the display shows `ceil(remaining / 1000)`. The
  ramp in `config/game.yaml` was retimed to exactly 3000 ms so the digits land on 3, 2, 1 at
  one-second intervals; the accelerating relay clatter is preserved, just with a slower opening.
  The GO deadline is anchored in `_handle_start_count_in` rather than inside the ramp task,
  because `BroadcastState` runs first and would otherwise emit one frame with no deadline set.

- **The GM console's SM-NODE-DMX LED was amber forever.** It was set to "unknown" at init and
  never updated — there was no DMX health anywhere in the WebSocket message. `HazerController`
  now tracks whether its Art-Net keepalive is sending without a socket error, exposes it as
  `link_ok`, and the GM console polls `/api/gm/hazer` every 5 s to drive the LED. Art-Net is
  fire-and-forget UDP, so green means "we are transmitting", not "the node acknowledged" —
  noted in the code so nobody reads more into it later.

### Fixed — kiosk displays

Neither HDMI panel had ever shown the right thing. Three independent causes in `kiosk.sh`:

- **Only one browser window ever existed.** Both Chromium invocations shared the default profile
  directory, so the second one handed its URL to the first and exited (`Opening in existing
  browser session` in the journal). `/display/in` was never on screen. Each window now gets its
  own `--user-data-dir`.
- **Nothing was fullscreen.** There is no window manager on the box, so `--kiosk` — which asks
  the WM for fullscreen — did nothing and Chromium sized its own window (1265x1420 on a
  2560x1440 panel). Both windows are now positioned and sized explicitly from what xrandr
  reports, and `--app` hides the chrome that `--kiosk` used to.
- **A Chromium infobar covered the top of both panels.** `--disable-infobars` no longer
  suppresses the "unsupported command-line flag: --no-sandbox" warning; `--test-type` does.

Also in `kiosk.sh`: mode selection prefers the highest resolution that runs at ≥ 50 Hz rather
than taking `--auto`, because the outdoor panel's native 3840x2160 is a 30 Hz mode and a
stopwatch at 30 Hz reads as stuttering. Output-to-page mapping is overridable via
`SCANMANIA_OUT_IN` / `SCANMANIA_OUT_OUT` since which panel is which is cabling, not logic.
Chromium runs under `dbus-run-session` to stop it flooding the journal with dbus errors.

### Admin panel — CSS / UX / accessibility pass

All in `web/static/admin/index.html`; no behaviour changes to any API.

- **Contrast.** `--accent` (#264f9a) is 2.3:1 on the panel surfaces and was used for body text.
  It is now chrome-only (borders, glows); a new `--accent-text` (#5b8fd6, 5.5:1) covers the nine
  text uses and the login overlay. `--muted` went #5a7899 → #6c8cae, clearing AA for the 10–12 px
  labels it is used on.
- **Focus is visible again.** `outline: none` removed from the config editor, the shared
  `input, select` rule and the login field; a `:focus-visible` ring added globally plus explicit
  overrides for controls that set their own outline. Mouse users see no change.
- **Tabs are a real ARIA tablist.** `role="tablist"` / `tab` / `tabpanel` with `aria-controls`,
  `aria-labelledby` and `aria-selected`, a roving `tabindex` so Tab enters the strip once, and
  ArrowLeft / ArrowRight / Home / End to move between tabs.
- **The beam grid and the maze-editor grid are keyboard-operable.** Both were `<div>`s with a
  click handler and no way in from the keyboard. They now carry `role="button"`, `tabindex="0"`,
  `aria-pressed` and an accessible name, activate on Enter and Space, and the maze editor restores
  focus to the toggled strip after its re-render.
- **Labels.** `aria-label` on the 17 inputs, selects and textareas that had only a placeholder or
  nothing at all; the confirm modal is now `role="dialog" aria-modal="true"` wired to its title
  and body.
- **Responsive.** Both maze diagrams changed from a fixed `width` to `max-width`, and breakpoints
  added at 700 px and for coarse pointers. A skip link and `<header>` element round it out, and
  `prefers-reduced-motion: reduce` disables the transitions and the state-pill pulse.

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
