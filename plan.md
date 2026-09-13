# ScanMania! — System Plan

**Client:** Carrefour Belgium × CityCubes
**Builder:** Videofy - Technify
**Revision:** 3
**Audience:** the build team, and any Claude Code session picking this up cold. Start with §1, then read `CLAUDE.md` and `docs/glossary.md` before touching code.

A laser maze in a shipping container. Haze makes the beams visible; each laser terminates as a **dot on the ceiling**, and the disappearance of a dot is how we know a beam was broken. The player stands on a start plate, the gamemaster counts them in, the maze snaps on, the stopwatch runs. Break a beam and the run is over. Fastest clean run wins.

---

## 1. Scope and operating model

**In scope:** control system, game logic, beam-break detection, displays, player/gamemaster/admin frontends, results persistence and local backup.

**Out of scope, owned elsewhere:** laser safety classification, GDPR/DPA, venue smoke-detection coordination, structural fit-out, and the electro-mechanical build of the start plate and checkpoint plates. This plan treats every plate as "a momentary dry contact" and stops there.

**Operating model:**
- **Attended at all times.** A gamemaster with an iPad runs every session: sign-in, count-in, reset.
- **No E-stop.** Power is cut at the board if needed.
- **Power up/down is manual.** Someone switches the PSUs and the NUC on in the morning. The system must boot to a playable state with no keyboard, but nothing here needs to survive an unattended 03:00 power blip.
- **The gamemaster is the recovery mechanism.** Every fault must be legible on their console with one obvious action. That's a far cheaper reliability target than autonomy — spend the savings on making their console excellent.

---

## 2. Locked decisions

| Area | Decision |
|---|---|
| Laser control | **Modbus TCP** direct to Waveshare relay boards. No Art-Net, no translator, no broker. |
| Presets / "shows" | YAML channel lists in `config/mazes.yaml`, applied with one `write_coils` (0x0F) per board. |
| Break detection | **Ceiling-dot detection** on PoE camera(s). Dot present = beam intact. Proven by the team. |
| Detection failover | **Three modes — `auto` / `assisted` / `manual`** plus per-beam masking and a full manual **master mode**. §6. |
| Scoring | **Stopwatch, counting up. A break is a hard cutoff — run over.** Fastest clean run wins. §5. |
| Start sequence | Player on start plate → gamemaster initiates → **accelerating flash** of the maze → solid at GO → stopwatch starts. §5.2. |
| Compute | One NUC: game logic, vision, web, both displays. |
| Physical inputs | Raspberry Pi Pico over USB CDC serial. No OS, no SD card. §4.4. |
| Displays | HDMI 1 → in-container stopwatch. HDMI 2 → outdoor screen (live feed + stopwatch + leaderboard). |
| Frontends | Three separate apps: **player sign-in**, **gamemaster console**, **admin portal**. §7. |
| Results durability | Local SQLite is source of truth. Cloud sync **removed** (ADR 0008); local snapshots + CSV export are the whole story. §8. |
| Metrics | Schema-stable event stream + pluggable sink, built now, populated later. §9. |
| Runtime | Native Python + systemd. Not Docker. §10.1. |
| Calibration UI | **Deferred.** v1 uses a hand-edited `config/beams.json` plus a read-only overlay verification page. §4.3. |

---

## 3. Laser control

### 3.1 What replaced Art-Net

Art-Net was doing two jobs and only one was real.

**Transport** is now `pymodbus` writing coils over TCP. Function **0x0F (write multiple coils)** sets an entire 16-channel board in one atomic transaction — which is exactly what a maze change is. One call per board, one visual snap. Never loop single-coil writes; you'll watch the maze morph channel-by-channel.

**The mental model** you actually wanted — "a node holding named shows I can trigger externally" — was never a protocol feature. It's a config file and a function call:

```yaml
# config/mazes.yaml
presets:
  blackout:  { channels: [] }
  attract:   { channels: [1, 4, 7, 10, 13, 16, 19, 22],
               description: "slow idle look, reads well from outside" }
  segment_1: { channels: [1, 2, 3, 8, 9, 14, 17, 18, 23, 24, 29, 30],
               description: "wide gaps, low crawl-under" }
  segment_2: { channels: [1, 2, 5, 6, 11, 12, 15, 20, 21, 26, 27, 31] }
  segment_3: { channels: [3, 4, 7, 9, 10, 13, 16, 19, 22, 25, 28, 32],
               description: "tight, high step-over" }
  all_on:    { channels: "*" }
  bust:      { channels: "*", description: "everything on for the BUSTED moment" }

shows:
  main_game: [segment_1, segment_2, segment_3]
```

`io.apply_preset("segment_2")` resolves channels → per-board 16-bit coil arrays → one `write_coils` per board. Write the full board every time; it's one transaction either way.

### 3.2 Reconciliation — Art-Net's one good habit, kept

Art-Net is robust in practice because it is **idempotent and continuously re-asserted**. Modbus TCP is acknowledged, but a board can still reboot, drop its session, or come back with coils cleared, and nothing would tell you.

```
every 500 ms, per board:
    actual = read_coils(0, 16)          # function 0x01
    if actual != desired_state[board]:
        log MISMATCH with the diff
        emit metric relay.mismatch
        write_coils(0, desired_state[board])
```

~30 lines, and it doubles as board-health telemetry: a board mismatching repeatedly is a board about to die.

### 3.3 Boards
`Waveshare Modbus POE ETH Relay 16CH` (SKU 30795) — 16 ch, dual Ethernet with PoE cascade, Modbus RTU/TCP, 7–36 V or 802.3af.

- **Home-run each board to the switch.** Cascading is convenient but a mid-chain failure takes out everything downstream. If routing forces a cascade, document which board feeds which in `docs/wiring.md`.
- Static IPs set via each board's web config; recorded in `docs/network.md` (credentials in the password manager, never the repo).
- **One async task and one TCP connection per board.** A slow or dead board must never stall the others. 200 ms timeout, 3 consecutive failures → `DEGRADED`, surfaced on the gamemaster console and the admin portal.

---

## 4. Detection — ceiling dots

Each laser terminates on the ceiling as a bright dot. Dot present → beam intact. Dot gone → something is in the beam. This is a much better signal than sampling the beam line: it's a small, high-contrast, spatially-fixed target, and it's binary.

### 4.1 Why this works, and the one thing to respect

The dot is a specular/diffuse spot on a fixed surface, so its position in frame never changes and its brightness has a huge margin over the surroundings. Haze slightly reduces the delivered power but the dot stays far above the noise floor — hence "works even with fog."

The consequence of respecting: **the camera must not move and must not auto-adjust.** A 3 px shift or an auto-exposure step invalidates every ROI. Therefore:

- Lock exposure, gain, white balance, focus. Disable auto-exposure, auto-gain, auto-WB, and IR-cut auto-switching. **Expose for the dot, not the room** — the target image is near-black with bright dots.
- Bolt the camera down, thread-lock the mount.
- **Boot-time drift check:** compare a stored reference frame against the live frame (phase correlation or a few static feature points). If shift > 2 px, raise `CAMERA_MOVED` on the admin portal. Don't silently carry on with stale ROIs.
- **Work on the red channel isolated:** `R - (G+B)/2`. Kills white highlights from work lights, phone flashes and the displays while keeping the dots.
- **1280×720 @ 25–30 fps is plenty.** Configure a dedicated camera substream at that resolution. Don't pull 4K; it costs CPU and adds decode latency for no gain.
- A **narrow bandpass filter** matched to the laser wavelength (e.g. 650 nm ±10 nm, ~€30 threaded lens filter) is optional here but cheap insurance if ambient light ever becomes uncontrolled. Note it as a Phase 4 option, not a v1 requirement.

### 4.2 Algorithm

```json
// config/beams.json  — hand-authored in v1
{
  "cameras": {
    "cam_a": { "url": "rtsp://10.0.0.30:554/substream", "reference_frame": "ref/cam_a.png" }
  },
  "beams": [
    {
      "id": "b07",
      "cluster": 2,
      "relay_channel": 14,
      "camera": "cam_a",
      "roi": { "cx": 512, "cy": 188, "r": 9 },
      "baseline": 214.0,
      "break_ratio": 0.40,
      "clear_ratio": 0.65,
      "masked": false,
      "note": "second dot from the left, above the crawl-under"
    }
  ]
}
```

Per frame, per unmasked beam:
1. Sample the mean of the top 20% brightest pixels inside the circular ROI on the red-isolated image. Taking the top percentile rather than the full-ROI mean makes the reading insensitive to small positional drift and to ROI radius.
2. `ratio = value / baseline`
3. **Hysteresis:** `broken` after `ratio < break_ratio` for **N consecutive frames**; `clear` after `ratio > clear_ratio` for N frames. Asymmetric thresholds stop chatter at the boundary.
4. `N = 3` at 30 fps ≈ 100 ms detection latency.

**Because a break is now a hard cutoff, N and `break_ratio` are the two most consequential numbers in the codebase.** They live in `beams.json`, are adjustable per beam from the admin portal, and every change is logged with who/when. Do not hardcode them.

### 4.3 Baseline capture — riding the count-in flash

The maze goes solid at the same instant the stopwatch starts, so there is no quiet window with the maze lit and the container empty. The count-in solves this for free: **the accelerating flash (§5.2) is also the baseline capture.**

- **Baseline is captured during pulse 0** — the first and longest "on" window of the ramp (~280 ms ≈ 8 frames at 30 fps). The player is standing on the start plate, outside the beam field, so the reference is clean.
- **The flash must use `segment_1`, not `all_on`.** The baseline has to be measured against exactly the channels that will be lit when detection arms. Flashing everything would give you baselines for beams that aren't in play and, worse, a wrong reference for the ones that are.
- **A second, silent 150 ms blink happens at ARM** (when the player steps onto the plate, before the gamemaster taps COUNT IN). It reads as a "system ready" acknowledgement and it runs the pre-flight beam check *then* — so a dead laser is caught before the count-in starts, rather than aborting a countdown in front of a queue. The count-in's own pulse-0 capture refreshes the baseline and wins.
- Later pulses in the ramp get too short for the dots to reach full brightness. That's fine — they're theatre. Only pulse 0 feeds the baseline; the rest are explicitly excluded.
- If pulse 0's capture fails a beam that passed at ARM, abort the count-in to `ARM` with a named reason. Rare, but it must not fall through into a run with a bad reference.

Regardless of capture point:
- **Rolling baseline in ATTRACT.** While idle with the attract preset lit, update each beam's baseline as a slow EMA (`α ≈ 0.01`, ~30 s time constant) so haze drift is tracked for free.
- **Freeze the baseline during a run.** Never adapt while a player is inside, or the algorithm slowly accepts a broken beam as normal.
- **Pre-flight gate at ARM:** if any unmasked beam reads below `break_ratio` before the count-in, refuse to start and name it: *"Beam b07 (cluster 2) — no dot. Check laser or camera."* This catches dead lasers, dust and camera drift **before** a customer plays, and it's the highest-value diagnostic in the system.

### 4.4 Fail-safe rules — a stalled stream must never bust a player

Hard cutoff inverts the risk profile: a missed break costs a player nothing, but a **phantom break ends someone's run in front of a queue.** The pipeline therefore biases toward silence whenever it isn't confident:

- **Stall suppression.** Frame gap > 300 ms → vision `STALLED`, **emit no break events**, amber on the gamemaster console, and if a run is in progress **auto-drop to `manual` mode** (§6.2) rather than busting anyone.
- **Global rate limit.** More than ~6 breaks in 1 s across the whole maze is not a player, it's a haze puff or an exposure glitch. Log it, don't act on it.
- **Per-beam flap detector.** If a beam breaks and clears more than 5 times in 60 s while idle, **auto-mask it** and tell the gamemaster. One flaky dot must not ruin every run of the afternoon.
- **Evidence on every break.** Save a small JPEG crop of the triggering ROI (plus the two preceding frames) to disk against the run id. When someone disputes a bust, you look at the picture. Worth more than any amount of threshold tuning.
- **Never bust on the first frame of a run.** Ignore break events in the first 150 ms after arming.

### 4.5 Calibration UI: deferred, but leave the door open

v1 authors `beams.json` by hand. To make that survivable:

- **`/admin/beams` overlay page (build in v1):** live frame with every ROI drawn on it, id labels, live ratio bars, and a colour per state. Read-only apart from `masked` and threshold sliders. This is enough to find and fix a bad beam without editing JSON on site.
- **`tools/pick_rois.py` (build in v1):** grabs one frame, opens an OpenCV window, click a dot → prints a JSON stanza. Ten minutes of work, saves hours.
- **Full drag-and-drop calibration UI:** deferred to a later phase. Keep `beams.json` as the only source of truth so the UI, when it arrives, is just an editor over it.

### 4.6 One decode, two consumers

The outdoor display shows the inside of the container — the same view the detection camera has, and it's the good-looking shot.

**Decode each RTSP stream exactly once.** `scanmania-vision` owns the decode, runs detection, and publishes:
- beam states → core, over a local unix socket
- a downscaled MJPEG stream → `http://localhost:8081/cam_a.mjpg` for the outdoor display page

Never open the same stream from two processes: double the CPU, and two different notions of "now."

---

## 5. Game model

### 5.1 Rules
```yaml
# config/game.yaml
mode: hard_cutoff              # a break ends the run
max_run_ms:        180000      # hard abort if nobody finishes
result_display_ms: 15000
arm_timeout_ms:    180000      # registered but never counted in
detection_mode:    auto        # auto | assisted | manual
arm_grace_ms:      150         # ignore breaks right after arming
```

- **Stopwatch counts up.** Score = elapsed ms from GO to the stop button. Lower is better. Leaderboard sorts ascending.
- **A confirmed break is a hard cutoff:** stopwatch stops, maze snaps to `bust` (everything on), buzzer, "BUSTED" on both displays. Outcome `busted`, elapsed time recorded but excluded from the leaderboard.
- **Reaching the stop button clean** → outcome `clean`, goes on the leaderboard.
- `max_run_ms` exceeded → outcome `aborted`.
- Gamemaster can always `VOID` a run so it's recorded but flagged `voided` and excluded — the escape hatch for "the system was wrong."

### 5.2 Start sequence — the accelerating flash

```
player signs in (iPad)                → REGISTERED
player steps onto the start plate     → plate HIGH
                                      → 150 ms "ready" blink of segment_1
                                      → silent pre-flight beam check
                                        fail → FAULT, named beam, gamemaster informed
                                        pass → ARM, start button LED pulses
gamemaster taps COUNT IN              → COUNTDOWN
    segment_1 pulses on/off with a shrinking period:
      on 280 / off 320   ← baseline captured here (pulse 0)
      on 200 / off 250
      on 150 / off 190
      on 110 / off 140
      on  80 / off 100
      on  60 / off  75
      on  45 / off  55
      on  35 / off  40
                                      → GO
                                        segment_1 solid
                                        stopwatch starts
                                        detection arms after arm_grace_ms
```

The ramp accelerates until the gaps close and the maze simply *is* on. Total ~2.7 s, tuned by eye.

```yaml
# config/game.yaml
count_in:
  preset: segment_1            # MUST match what's lit at GO — see §4.3
  ready_blink_ms: 150          # at ARM, doubles as the pre-flight window
  baseline_pulse_index: 0      # only pulse 0 feeds the baseline
  # [on_ms, off_ms] — hand-tunable; regenerate with tools/ramp.py
  pulses:
    - [280, 320]
    - [200, 250]
    - [150, 190]
    - [110, 140]
    - [80, 100]
    - [60, 75]
    - [45, 55]
    - [35, 40]
  solid_at_end: true
arm_grace_ms: 150              # ignore breaks right after arming
arm_timeout_ms: 180000
```

Implementation notes:
- **`tools/ramp.py`** generates a pulse list from `(total_ms, pulse_count, ratio, duty)` so the curve can be retuned in seconds. The explicit list stays the source of truth in config — someone will hand-tweak the last three pulses to make the ending land right.
- **The relay clatter is the sound design.** Sixteen relays accelerating from a lazy click to a machine-gun rattle and then silence is exactly the "vault mechanism arming" sound this game wants. Don't fight it — position the boards where it's audible in the container.
- **Timing is driven server-side**, one `write_coils` per pulse edge, scheduled off `monotonic_ns` with the next edge computed absolutely (not by accumulating sleeps, which drifts). Late edges are skipped rather than delayed, so GO always lands on time.
- **Mechanical relays have a floor.** Below roughly 30 ms on-time the dots may not visibly reach full brightness. That's fine — the last pulses are theatre, and it looks like the ramp is "winning." But if the tail reads as mush rather than acceleration, stop the ramp earlier and hold a longer final gap before GO; a beat of silence before the snap is stronger than a blur.
- **The in-container display syncs to the ramp** — a bar or ring that fills at the same accelerating rate, then a hard cut to the stopwatch at GO. Same WebSocket clock as everything else.
- **False start:** if the start plate goes LOW during the countdown, abort to `ARM` and tell the gamemaster *"player left the plate."* Free, and it stops the most obvious cheat.
- The gamemaster can cancel at any point; cancelling blacks out immediately.

### 5.3 State machine

Pure function of `(state, event)` with **no I/O inside it**, in `core/fsm.py`. That's what lets the whole game be developed and tested on a laptop.

```
BOOT
 └→ SELF_TEST      relay boards reachable? Pico heartbeat? cameras streaming?
      ├ fail → FAULT
      └ ok   → ATTRACT
ATTRACT            attract preset, rolling baselines, leaderboard on both displays
 └→ REGISTERED     gamemaster submitted sign-in
REGISTERED
 └→ ARM            waiting for player on the start plate
      ├ plate HIGH     → ready blink + silent pre-flight beam check
      │                   fail → FAULT (named beam)
      │                   pass → stay in ARM, start LED pulses
      ├ arm_timeout    → RESET
      └ COUNT IN       → COUNTDOWN
COUNTDOWN          accelerating flash of segment_1; baseline captured on pulse 0
 ├ plate LOW         → ARM       (false start)
 ├ gamemaster cancel → ARM       (immediate blackout)
 ├ baseline capture fail → ARM   (named beam)
 └ ramp complete     → RUN_SEG_1 (segment_1 solid, stopwatch starts, detection arms)
RUN_SEG_1
 ├ cp1             → segment_2 → RUN_SEG_2
 ├ break confirmed → BUSTED
 └ max_run_ms      → ABORTED
RUN_SEG_2
 ├ cp2             → segment_3 → RUN_SEG_3
 ├ break confirmed → BUSTED
 └ max_run_ms      → ABORTED
RUN_SEG_3
 ├ stop pressed    → FINISHED
 ├ break confirmed → BUSTED
 └ max_run_ms      → ABORTED
FINISHED / BUSTED / ABORTED
 └→ RESULT         time + rank (or BUSTED) on both displays, result_display_ms
      └→ RESET     lasers → attract, counters cleared, baselines resume, row queued to outbox
           └→ ATTRACT
ANY STATE
 ├ gamemaster BUST        → BUSTED           (manual call)
 ├ gamemaster ABORT       → ABORTED → RESET
 ├ gamemaster VOID        → mark run voided
 ├ gamemaster FORCE RESET → RESET
 ├ MASTER MODE engaged    → MASTER  (§6.3)
 └ process restart        → ABORTED → RESET  (never resume a run)
```

Ordering and idempotence:
- **Checkpoints only count in order.** `cp2` during `RUN_SEG_1` is ignored and logged. A repeat `cp1` after passing it is ignored — people walk back.
- Segment transitions are one-way.
- Every transition logged with `time.monotonic_ns()`.

### 5.4 Authoritative timing
- The **server owns the stopwatch** using `time.monotonic_ns()`. Never wall-clock, never `Date.now()` in a browser.
- Broadcast `{state, started_at_mono, server_mono_now, elapsed_ms, countdown_step}` over WebSocket at 10 Hz.
- Browsers **interpolate locally** for smooth 60 fps rendering and hard-correct on each message. A frozen or disconnected display cannot affect the recorded time.
- The Pico timestamps its own button events; the host maps Pico clock → host monotonic via an offset from the heartbeat, so USB jitter stays out of the score. §10.4.

---

## 6. Detection failover, override and master mode

This is the difference between a system that works and a system that survives a Saturday. Hard cutoff means a wrong detection ends someone's run, so the gamemaster needs authority over the machine at all times.

### 6.1 Three detection modes

| Mode | Vision behaviour | Gamemaster |
|---|---|---|
| `auto` | A confirmed break immediately busts the run. | Watches; can `VOID` afterwards. |
| `assisted` | A confirmed break **halts the stopwatch** and raises a full-screen CONFIRM / VETO prompt with the evidence thumbnail. Veto → stopwatch resumes with the halted duration credited back. | Adjudicates each call. |
| `manual` | Vision is **advisory only** — it lights an indicator, it does not bust. | Presses **BUST** to end a run. |

Set in `game.yaml`, switchable live from the gamemaster console with one tap. Every mode change is logged and appears in the run record, so results are always interpretable after the fact.

`assisted` is the interesting one: it keeps automatic sensitivity while making every bust a human decision. Consider it the default for the first days on site, dropping to `auto` once the numbers justify it.

### 6.2 Automatic degradation

The system drops to a safer mode **by itself** and says so loudly:

| Trigger | Action |
|---|---|
| Vision `STALLED` (frame gap > 300 ms) | → `manual`, red banner: "detection offline — call busts manually" |
| Camera unreachable, 3 reconnects failed | → `manual`, offer PoE port power-cycle |
| `CAMERA_MOVED` on boot drift check | → `manual`, "recheck beam ROIs" |
| Beam flapping (§4.4) | Auto-mask that beam, stay in current mode |
| Global break rate limit exceeded | Suppress the burst, log, stay in current mode |

**It never escalates back automatically.** Returning to `auto` is always a human tap, so nobody is surprised by the system silently becoming stricter.

### 6.3 Master mode

A separate state where the gamemaster drives the hardware directly. For demos, VIP runs, press, fault-finding, and the moment when everything is broken but the show must go on.

- Trigger any preset from a button grid.
- Toggle individual relay channels.
- Start / stop / reset the stopwatch by hand.
- Force any FSM state.
- Runs performed in master mode are recorded with `mode: master` and excluded from the leaderboard by default.

Master mode is reachable from the admin portal and from a long-press on the gamemaster console. Exiting returns to `SELF_TEST` so the system re-verifies itself.

### 6.4 Per-beam masking

One tap on the gamemaster console or the admin portal sets `masked: true` on a beam. Masked beams are excluded from detection and from the pre-flight gate, and appear in a persistent "3 beams masked" banner so nobody forgets. Masks persist to `beams.json` with a timestamp and reason, and the admin portal lists them with an "unmask all" action for the morning check.

---

## 7. Frontends

Four distinct surfaces, deliberately separated because their users and failure tolerances differ. All served by `scanmania-web` on the LAN; all pure WebSocket consumers so a dead client can never affect the game.

### 7.1 Player sign-in — `/signin` (iPad, held by a steward)
Sign-in fields TBD (§14 Q6). Big touch targets, one screen, no scrolling. Submit → `REGISTERED`. Nothing else on this page; it gets handed to members of the public.

### 7.2 Gamemaster console — `/gm` (iPad)
The most important screen in the project. One page, thumb-reachable, no scrolling for anything urgent.

- **Top:** state in huge type. Stopwatch mirror. Player nickname. Detection mode badge.
- **Primary actions:** `COUNT IN`, `BUST`, `ABORT`, `VOID`, `FORCE RESET`.
- **Detection mode switcher:** three segmented buttons.
- **Live beam strip:** one pill per beam, green/red/grey(masked). Tap to mask/unmask.
- **Health strip:** relay boards · Pico link · camera(s) · cloud sync. Green/amber/red; tapping a red item gives one sentence of explanation and one fix button.
- **Assisted-mode prompt:** full-screen CONFIRM / VETO with the evidence thumbnail when a break is raised.
- Below the fold: last 10 runs, leaderboard, link to admin.

### 7.3 Admin portal — `/admin` (laptop or iPad)
Everything monitorable and everything reachable. This is the "I'm not on site" surface and the "another Claude needs to understand the running system" surface.

| Section | Contents |
|---|---|
| **Dashboard** | State, uptime, runs today, clean/busted split, current faults |
| **Hardware** | Per board: IP, RTT, coil state (live grid, click to toggle), mismatch count. Pico: link state, firmware version, last heartbeat, input bitmask. Cameras: fps, stall count, last frame thumbnail |
| **Beams** | The §4.5 overlay page — live ROIs, ratios, thresholds, mask toggles |
| **Presets** | Preview and fire any preset; edit `mazes.yaml` with validation |
| **Config** | View/edit `game.yaml` and thresholds with a diff + confirm; every change written to an audit log |
| **Runs** | Searchable run history with events, break thumbnails, outcome, mode; re-void / un-void |
| **Leaderboard** | View, filter, hide entries, export CSV |
| **Cloud sync** | Outbox depth, last successful push, last error. **Push now · Pause / Resume backup · Retry failed · Test endpoint · Export snapshot** (§8.3) |
| **Logs** | Live `journalctl` tail per unit, filterable |
| **Metrics** | Whatever §9 defines, rendered from the same event stream |
| **Master mode** | The §6.3 control surface |

Auth: a single shared password in an env file plus LAN-only binding. Not a security project; just enough that a curious shopper with the Wi-Fi password can't fire the lasers.

### 7.4 Displays
**HDMI 1 — in-container.** A ring or bar that fills at the same accelerating rate as the flash ramp, hard-cutting at GO to an enormous mono stopwatch readable through haze at 10 m. Segment indicator (1/2/3). On a bust: full-red BUSTED. Nothing else.

**HDMI 2 — outdoor.** MJPEG feed as background, stopwatch overlaid, leaderboard when idle. This is the crowd-puller; treat it as a design object.

One X session, `xrandr` places the two outputs side by side, two Chromium windows in `--kiosk --app` positioned onto each. `xset s off -dpms`. Each page heartbeats back so the admin portal can show "outdoor display offline."

---

## 8. Results durability and cloud backup

> **Superseded in part, 2026-09-13 — see [ADR 0008](docs/adr/0008-remove-cloud-sync.md).**
> The cloud half of this section was built, never connected to an endpoint, and has
> been removed: no outbox table, no `QueueSync`, no `httpx`, no sync service. The
> local-first half stands and is what ships. Snapshots (on demand, on clean shutdown,
> and hourly) plus CSV export are the whole durability story, and someone has to
> collect the files off the NUC. The text below is kept as the original design record.

Requirement: **the scoreboard can never be lost.** Design principle: **local-first, cloud-eventually, gameplay never blocks on the network.**

### 8.1 Model — always pushing

```
run completes
  → INSERT into runs (local SQLite, WAL mode)      ← source of truth during operation
  → INSERT into outbox (run_uuid, payload_json, attempts, last_error)
  → wake the sync worker
  → return immediately; the game moves on
```

`scanmania-sync` is a **continuously running drain**, not a scheduled job. There is no batch window, no nightly job, nothing to miss:

- Woken on every insert, and it also sweeps every 10 s in case a wake was lost.
- POST with **`Idempotency-Key: run_uuid`** — UUIDv7 generated locally at run start, so retries are safe and duplicates are impossible.
- Exponential backoff with jitter per row, capped at 60 s, **retrying indefinitely**. Days offline is a normal condition, not an error state.
- Rows are only deleted on a 2xx. Nothing is ever dropped, expired or garbage-collected out of the outbox.
- A **heartbeat push** every 60 s carries system state, outbox depth and health, so the cloud side knows the box is alive even on a quiet afternoon.
- Every attempt logged; last error surfaced on the admin portal.

**Nothing in the game path ever awaits the network.** If the cloud is down for the whole activation, the local DB still holds every run and the outbox drains the moment connectivity returns.

### 8.2 Cloud side
Deliberately boring. Pick whichever the team can host and forget:
- A small FastAPI service + Postgres on a VPS, or
- Supabase (Postgres + REST + auth token), or
- Cloudflare Worker + D1.

Endpoint contract in `docs/protocols/cloud-api.md`:
```
POST /v1/runs          Idempotency-Key: <uuid>   → 201 | 200 (already stored)
GET  /v1/leaderboard?limit=50                    → for the outdoor screen if desired
GET  /v1/health
```
Bearer token from `/etc/scanmania/secrets.env`, `chmod 600`, never in git.

**The cloud is a backup and a reporting surface, never a dependency.** The outdoor leaderboard renders from local data; a cloud leaderboard, if used, is an optional enhancement that degrades to local on any failure.

### 8.3 Admin controls

Backup is operable from the admin portal, not from an SSH session:

| Control | Behaviour |
|---|---|
| **Push now** | Force an immediate drain of the whole outbox, ignoring backoff. Shows a live count as it empties. |
| **Pause backup** | Stops the worker. **Nothing is deleted and nothing is lost** — the outbox keeps accumulating, and resuming drains it in order. For when the endpoint is being migrated or is misbehaving. |
| **Resume backup** | Restarts the worker and immediately drains. |
| **Retry failed** | Resets the backoff clock on rows that have exhausted their current interval. |
| **Test endpoint** | One `GET /v1/health` with the configured token, result shown inline. First thing to press when sync looks wrong. |
| **Export snapshot** | `VACUUM INTO` a dated `.db` file plus a leaderboard CSV, downloadable from the browser. On demand, and automatically on service stop. |

Paused state is **loud**: a persistent amber banner on both the admin portal and the gamemaster console, plus a `sync.paused` metric. Nobody discovers three weeks later that it was left off.

Status line, always visible on both consoles: **outbox depth · last successful push · last error**. Green only when the depth is 0 and the last push is recent.

A **rolling local snapshot** also runs every `snapshot_interval_min` (default 60) into `/var/backups/scanmania/`, keeping the last 48. Local disk only, no schedule to coordinate — if everything else fails, someone can walk away with a `.db` file and a CSV.

---

## 9. Metrics

Metrics are "defined later," so the job now is to make adding them **free** rather than to guess at them.

### 9.1 Approach
Everything already flows through one place — the `events` table — so metrics are a **view over the event stream plus a thin façade**, not a parallel system.

```python
# core/metrics.py
metrics.emit("run.completed", value=elapsed_ms,
             tags={"outcome": "clean", "mode": "auto", "segment_reached": 3})
```

- **Sinks are pluggable** and configured in `config/metrics.yaml`: `sqlite` (always on), `prometheus_textfile` (optional, for a future Grafana), `cloud` (piggybacks the §8 outbox).
- **Every emit is also an event row**, so nothing is lost if a sink is added after the fact — historical metrics can be recomputed from `events`.
- **Naming convention is fixed now** (`<domain>.<thing>.<verb|state>`, snake_case) and documented in `docs/metrics.md`, so later additions don't fragment the namespace.

### 9.2 Instrument these from day one
Not because they're the final metric set, but because they're free once the façade exists and impossible to recover retroactively:

`run.started` · `run.completed` (with outcome, elapsed, segment reached) · `run.voided` · `break.detected` (beam id, ratio, mode) · `break.manual` · `detection.mode_changed` · `beam.masked` · `vision.stall` · `vision.fps` · `relay.mismatch` · `relay.timeout` · `pico.link_down` · `preflight.failed` (beam id) · `sync.outbox_depth` · `sync.paused` · `sync.push_failed` · `countin.aborted` (reason) · `state.transition` (from, to, reason)

Two things this buys immediately: a **throughput number** (runs/hour, which the client will ask for) and a **detection-quality number** (busts per run, veto rate in assisted mode) — the latter is exactly the evidence needed to decide whether `auto` mode is trustworthy.

---

## 10. Software

### 10.1 Runtime — native systemd, not Docker
This box needs USB serial access, V4L2/VAAPI decode, and a graphical session on two HDMI outputs. In Docker that means `--device`, `--privileged`, `network_mode: host` and an X socket bind — all of Docker's opacity, none of its isolation benefit.

**Debian 12, one `venv`, six systemd units, `Restart=always`, `RestartSec=2`.** `journalctl -u scanmania-core -f` is the debugging story. Deploy = `git pull` + `systemctl restart`.

```
scanmania-io.service       Modbus master + reconciliation
scanmania-vision.service   per-camera decode, dot detection, MJPEG out
scanmania-core.service     FSM, stopwatch, scoring, Pico serial
scanmania-web.service      FastAPI + WebSocket + static frontends
scanmania-sync.service     outbox drain, snapshots, exports
scanmania-kiosk.service    user unit: X session + 2× Chromium
```

Enable the NUC hardware watchdog via systemd's `RuntimeWatchdogSec`. Free insurance against a kernel hang.

### 10.2 Stack
Python 3.12. `pymodbus` (async), `opencv-python-headless`, `pyserial-asyncio`, FastAPI + uvicorn, `aiosqlite`, `httpx`. Frontends are **plain HTML/CSS/JS with no build step** — nobody wants a broken `npm install` on install day.

One language end to end. One person has to fix this in a container.

### 10.3 Repo layout
```
scanmania/
├── CLAUDE.md                   ← read this first (§11)
├── README.md                   ← 20-line orientation + links
├── plan.md                     ← this document
├── config/
│   ├── hardware.yaml           boards, IPs, channel↔cluster map, camera URLs
│   ├── mazes.yaml              presets + shows
│   ├── beams.json              dot ROIs, thresholds, masks  (source of truth)
│   ├── game.yaml               timings, modes
│   └── metrics.yaml            sinks
├── core/
│   ├── fsm.py                  pure state machine — NO I/O
│   ├── stopwatch.py            monotonic elapsed timer
│   ├── scoring.py
│   ├── events.py               event dataclasses (the wire contract)
│   ├── metrics.py              emit façade
│   └── runner.py               wires FSM to io / vision / inputs
├── io/
│   ├── modbus.py               async master, one conn per board
│   ├── presets.py              preset → per-board coil arrays
│   ├── reconcile.py            desired-vs-actual loop
│   └── fake.py                 simulator backend — MANDATORY
├── inputs/
│   ├── pico_link.py            serial protocol, clock offset, reconnect
│   ├── fake.py
│   └── firmware/main.py        MicroPython for the Pico
├── vision/
│   ├── camera.py               RTSP decode, stall + drift detection
│   ├── detect.py               dot ROI sampling, hysteresis, cooldowns
│   ├── baseline.py             rolling EMA + flash capture
│   ├── evidence.py             break thumbnails
│   ├── mjpeg.py                localhost stream for the outdoor display
│   └── fake.py                 replay from recorded video — MANDATORY
├── persist/
│   ├── db.py                   SQLite schema + migrations
│   ├── outbox.py
│   └── sync.py                 cloud POST, backoff, snapshots, exports
├── web/
│   ├── server.py               FastAPI + WebSocket broadcast
│   ├── routes_signin.py
│   ├── routes_gm.py
│   ├── routes_admin.py
│   └── static/{signin,gm,admin,display_in,display_out}/
├── tools/
│   ├── ramp.py                 generate a count-in pulse list from a curve
│   ├── pick_rois.py            click dots → JSON stanza
│   ├── replay.py               run a recorded video through detection
│   └── fake_run.py             drive a full run from the CLI
├── tests/
└── docs/                       ← §11
```

**`io/fake.py`, `inputs/fake.py` and `vision/fake.py` are not optional.** They let the entire game be built and tested with no hardware, and the video-replay vision backend turns recorded footage into a deterministic regression suite. Start recording video the first day hardware exists.

### 10.4 Physical inputs — Pico on USB

`start`, `stop`, `plate_start`, `cp1`, `cp2`, plus four spares wired out to a terminal block anyway. Button LEDs driven from the Pico (start pulsing in ARM, solid during a run) — free polish, and a live indicator that the link is alive.

**Wiring reality:** USB is reliable to ~5 m, so **mount the Pico in the rack** and run the button wiring to it, not the reverse. Dry contacts over 15–20 m are an antenna: shielded or twisted pair, Pico GND as the return, internal pull-ups plus **100 nF to ground at the Pico end**, and a **30 ms firmware debounce**. If the bench still shows phantom triggers, step up to a 24 V loop with an optocoupler per input — decide that in Phase 2, not on site.

**Protocol** (line-oriented ASCII, 115200, debuggable with `screen`):
```
Pico → host:   EV <seq> <pico_ms> <input_id> <state>
               HB <seq> <pico_ms> <input_bitmask>        every 250 ms
               BOOT <firmware_version>
host → Pico:   LED <led_id> <off|on|pulse|flash>
               PING
               RESET
```
- The **Pico timestamps its own events** with `time.ticks_ms()`; the host keeps a `pico_ms → host monotonic` offset estimated from heartbeats, so USB scheduling jitter never leaks into the score.
- **Sequence numbers** let the host detect a dropped line.
- **The 250 ms heartbeat carries the full input bitmask** — liveness detection (no HB for 1 s → `INPUT_LINK_DOWN`) plus state resync, so a missed edge self-corrects.
- Enable the Pico's hardware watchdog (`machine.WDT`); a wedged Pico reboots and re-announces with `BOOT`.
- Host opens by stable path (`/dev/serial/by-id/...`, udev rule) and **auto-reconnects forever**. Someone will unplug it.

### 10.5 Timing budget
| Path | Target |
|---|---|
| Button press → stopwatch starts/stops | < 40 ms |
| Checkpoint → maze changed | < 150 ms |
| Dot lost → bust registered | < 250 ms |
| Coil readback poll | 500 ms |
| Pico heartbeat | 250 ms |
| WebSocket broadcast | 100 ms |

Measure each on the bench in Phase 2 and write the actuals into `docs/network.md`.

### 10.6 Data model
```sql
players(id, nickname, created_at, extra_json)
runs(id TEXT PRIMARY KEY,          -- UUIDv7, generated at run start
     player_id, started_at, ended_at, elapsed_ms,
     outcome,                      -- clean | busted | aborted | voided
     detection_mode, busting_beam_id, segment_reached,
     voided_reason, created_at)
events(id, run_id, ts_mono_ns, ts_wall, type, source, payload_json)
beam_hits(id, run_id, beam_id, ts_mono_ns, ratio, thumb_path)
outbox(run_id, payload_json, attempts, last_attempt_at, last_error)
health(ts, component, status, detail)
config_audit(ts, actor, path, before_json, after_json)
```
`events` is the flight recorder: every button, checkpoint, break, coil write, board timeout, stall, mode change. Rotate at 30 days but **never rotate `runs`.**

---

## 11. Documentation regime

Explicit requirement: other people — and other Claude sessions — must be able to pick this up cold. Docs are a deliverable, not an afterthought. **A PR that changes behaviour without changing docs is not done.**

### 11.1 `CLAUDE.md` at the repo root
The single highest-leverage file. Kept under 200 lines and ruthlessly current. Contents:

1. **What this is**, in three sentences.
2. **How to run it on a laptop with no hardware** — the exact commands (`make dev`, which fakes are active, how to drive a run from `tools/fake_run.py`).
3. **How to run the tests** and what "green" means.
4. **The invariants** — the rules that must not be broken, each with its reason:
   - `core/fsm.py` contains no I/O, no `await`, no clock reads. It is a pure function.
   - The server owns the stopwatch. Browsers never compute time.
   - `config/beams.json` is the only source of truth for ROIs and thresholds.
   - Never write coils outside `io/presets.py`.
   - Vision suppresses events when unsure; a stalled stream never busts a player.
   - Gameplay never awaits the network.
   - Never resume a run after a restart.
5. **Where things live** — a one-line map of each top-level directory.
6. **Conventions** — naming, event types, metric names, commit message format, how to add a config key.
7. **Things that look wrong but aren't** — the traps that will otherwise get "fixed" by a well-meaning session. e.g. why the baseline is frozen mid-run; why the stopwatch uses `monotonic_ns` and not wall time; why the outbox never drops rows.
8. **Definition of done** for a change: tests pass · docs updated · ADR added if a decision changed · `CHANGELOG.md` entry.

### 11.2 `docs/`
```
docs/
├── README.md                  index — what to read for which task
├── glossary.md                beam · dot · cluster · channel · preset · segment ·
│                              run · bust · void · mask · gamemaster · master mode
├── architecture.md            the §3–§10 picture, with the topology diagram
├── game-rules.md              the FSM, as prose, for non-programmers
├── vision.md                  dot detection explained, thresholds, tuning procedure
├── protocols/
│   ├── modbus.md              register map, function codes, real RTT measurements
│   ├── pico-serial.md         wire format, clock offset maths, firmware flashing
│   ├── websocket.md           every message type, with examples
│   └── cloud-api.md           endpoint contract, idempotency, auth
├── network.md                 IP plan, ports, where credentials live, PoE budget
├── wiring.md                  channel ↔ physical cluster ↔ beam id, WITH PHOTOS
├── metrics.md                 naming convention, current metric list, sinks
├── development.md             laptop setup, fakes, replay corpus, how to add a beam
├── deployment.md              provisioning the NUC from bare Debian, step by step
├── runbook-gamemaster.md      ONE page. Laminated. Taped inside the rack door.
├── runbook-tech.md            fault tree: symptom → check → fix
├── maintenance.md             spares list, daily open/close, haze fluid
└── adr/
    ├── 0001-modbus-tcp-not-artnet.md
    ├── 0002-ceiling-dot-detection.md
    ├── 0003-hard-cutoff-scoring.md
    ├── 0004-pico-over-usb-not-raspberry-pi.md
    ├── 0005-systemd-not-docker.md
    ├── 0006-local-first-cloud-backup.md
    └── 0007-detection-modes-and-master-mode.md
```

### 11.3 ADRs — the part that matters most for future sessions
One file per decision, ~1 page, fixed template: **Context · Decision · Alternatives considered and why rejected · Consequences · Status**.

**ADRs are append-only.** Never edit one to reflect a change of mind; write a new ADR that supersedes it and mark the old one `Superseded by 00NN`. The value is the reasoning, including the reasoning that turned out to be wrong. Without this, every future session re-litigates Art-Net.

The seven above should be written in Phase 1, from the discussion that produced this plan, while the reasoning is still fresh.

### 11.4 In-code conventions
- **Every module opens with a docstring** stating its one job, its inputs, its outputs, and its invariants. Three to six lines. No exceptions.
- **`core/events.py` is the wire contract** and is documented exhaustively — every event type, every field, who emits it, who consumes it. It's the map of the whole system.
- **Config files are commented**, and every key has a one-line comment saying what it does and what a sane range is. `game.yaml` is the file people will edit on site.
- Type hints throughout; `mypy` in CI. Not for elegance — for the next reader.
- `docs/wiring.md` is written **while wiring**, with photos taken at the time. Never afterwards; it never happens afterwards.

---

## 12. Failure modes

| Failure | Detection | Behaviour |
|---|---|---|
| Relay board unreachable | 3× Modbus timeout | `DEGRADED` + named board; continue if not in the active preset, else `FAULT` |
| Board rebooted, coils cleared | Reconciliation mismatch | Silently re-assert, log, metric |
| Dead laser / no dot | Pre-flight gate at COUNT IN | Refuse to count in, name the beam |
| Camera stalls | Frame gap > 300 ms | Suppress breaks, **drop to `manual`**, amber banner, auto-resume |
| Camera unreachable | 3 failed reconnects | `manual` + PoE port power-cycle offered |
| Camera knocked | Boot drift check | `CAMERA_MOVED`, drop to `manual` |
| Flaky beam | Flap detector | Auto-mask, notify gamemaster |
| Haze puff / glitch burst | Global rate limit | Suppress, log |
| Pico unplugged or wedged | No HB for 1 s | `INPUT_LINK_DOWN`, auto-reconnect, Pico self-reboots via WDT |
| False start | Start plate LOW during countdown | Back to `ARM`, tell the gamemaster |
| Nobody finishes | `max_run_ms` | `ABORTED` → `RESET` |
| Service crash | systemd `Restart=always` | Back in < 5 s; run `ABORTED`. Never resume. |
| Kernel hang | Hardware watchdog | Reboot → `SELF_TEST` → `ATTRACT` |
| Display dies | Missing page heartbeat | Game unaffected; amber on admin |
| Internet down | Sync failures | Outbox grows, gameplay unaffected, admin shows depth |
| Cloud rejects a run | Non-2xx | Row stays in outbox, error surfaced, retries forever |
| Backup left paused | `sync.paused` metric | Persistent amber banner on admin **and** gamemaster console until resumed |
| Gamemaster confused | — | `FORCE RESET` available in every state; master mode as the last resort |

Theme: **fail visibly, name the component, offer one action, never bust a player on a guess.**

---

## 13. Build phases

Sequenced by risk. The frontends are the easy part and building them first hides the hard part.

### Phase 1 — Skeleton and docs, zero hardware
- [ ] Repo, systemd units, config schemas with validation.
- [ ] `CLAUDE.md`, `docs/glossary.md`, `docs/architecture.md`, ADRs 0001–0007.
- [ ] `core/events.py`, `core/fsm.py`, `core/stopwatch.py`, `core/scoring.py`, `core/metrics.py` + full unit tests: every transition, out-of-order checkpoints, false start, all three detection modes, bust-during-each-segment, restart mid-run.
- [ ] `io/fake.py`, `inputs/fake.py`, `vision/fake.py`.
- [ ] `persist/db.py` + `outbox.py` + a stub cloud endpoint; prove idempotent retry.
- [ ] Web server, WebSocket, both display pages, gamemaster console, admin skeleton — all on fakes.
- [ ] `tools/fake_run.py` drives a full run from the CLI.

**Gate:** `pytest` green; a full run (clean, busted, aborted, voided) demoed with no hardware attached; a fresh reader can run it from `CLAUDE.md` alone.

### Phase 2 — Bench integration
- [ ] `io/modbus.py` against one real board: presets, reconciliation. Unplug it mid-run and prove self-heal.
- [ ] Pico firmware + `pico_link.py`, tested at **real cable length** for phantom triggers.
- [ ] One camera, one cluster, haze, real ceiling dots. `tools/pick_rois.py`. Tune `break_ratio` and N.
- [ ] Evidence thumbnails, stall suppression, flap detector, auto-degrade to manual — all tested by deliberately breaking things.
- [ ] Measure everything in §10.5; record actuals in `docs/protocols/modbus.md` and `docs/network.md`.
- [ ] `docs/vision.md` written from what was actually learned.

**Gate:** pull every cable one at a time — Ethernet, USB, camera, PoE. The system names the fault, degrades correctly, and recovers on reconnect. 200 manual hand-waves through one beam: count false positives and misses, and write the numbers down.

### Phase 3 — Full-scale mock-up
- [ ] All boards, all clusters, real geometry, ideally in the actual container off-site.
- [ ] All dot ROIs authored; `beams.json` committed.
- [ ] Presets tuned visually at real haze density; **flash ramp tuned by eye and ear** (`tools/ramp.py` then hand-edit the tail).
- [ ] Both displays, all three frontends, leaderboard.
- [ ] Real cloud endpoint live; sync verified across a deliberate 24 h network outage.
- [ ] **Record video of 30 real runs** → the `vision/fake.py` regression corpus.
- [ ] `docs/wiring.md` written while wiring, with photos.

**Gate:** 100 consecutive runs, event log reviewed afterwards, zero unexplained events. Detection false-positive rate measured against the corpus and written into `docs/vision.md`.

### Phase 4 — Hardening and handover
- [ ] Cold-boot test ×20: power on → playable, no keyboard, under 90 s.
- [ ] Soak test across full opening hours at target throughput.
- [ ] Thresholds frozen; `assisted` vs `auto` decision taken on the measured veto rate.
- [ ] `runbook-gamemaster.md` finalised, printed, laminated, taped inside the rack door.
- [ ] `runbook-tech.md` fault tree complete.
- [ ] Spares kit: relay board, Pico ×3 pre-flashed, camera, PSU, patch cables, lasers.
- [ ] Gamemaster training — 15 minutes against the one-pager.
- [ ] Tailscale on the NUC only; alerts to your phone.

### Phase 5 — Install and first week
- [ ] Day 1 on site in `assisted` mode; watch the first 50 runs.
- [ ] Review event log and break thumbnails daily; retune; consider `auto`.
- [ ] Confirm the outbox is draining to zero and rolling snapshots are landing.

### Later (explicitly deferred)
- [ ] Full drag-and-drop beam calibration UI.
- [ ] Metric definitions from the client, plus a Grafana or equivalent surface.
- [ ] Bandpass filters if ambient light proves troublesome.

---

## 14. What to build first, concretely

For a Claude Code session starting from zero:
1. `CLAUDE.md` + `docs/glossary.md` + `docs/architecture.md` + ADRs 0001–0007. **Docs first** — they're the spec everything else is checked against.
2. `config/*` schemas and loader with validation.
3. `core/events.py`, then `core/fsm.py` + tests. **No I/O.**
4. `core/stopwatch.py`, `core/scoring.py`, `core/metrics.py` + tests.
5. `persist/db.py`, `persist/outbox.py` + idempotency test.
6. `io/fake.py` → `web/server.py` + WebSocket → `static/display_in/`.
7. `static/gm/` — enough to drive a full run by hand.
8. `io/modbus.py`, `io/presets.py`, `io/reconcile.py`.
9. `inputs/firmware/main.py`, `inputs/pico_link.py`.
10. `vision/camera.py` → `baseline.py` → `detect.py` → `evidence.py`, developed against recorded video.
11. `static/admin/`, then `vision/mjpeg.py` + `static/display_out/`.
12. `persist/sync.py` against the real cloud endpoint.

Steps 1–7 need no hardware.

---

## 15. Stack summary

```
1 × NUC (Debian 12, systemd, hardware watchdog)   logic · vision · web · both displays
1 × Raspberry Pi Pico on USB                      2 buttons · start plate · 2 checkpoints · LEDs
N × Waveshare Modbus POE ETH Relay 16CH           laser clusters, Modbus TCP
1–2 × PoE camera                                  ceiling-dot detection AND the outdoor feed
1 × managed PoE switch with per-port PoE control
1 × Wi-Fi AP                                      gamemaster iPad
1 × hazer on a relay channel
1 × small cloud endpoint                          results backup only, never a dependency
0 × Raspberry Pi (full) · 0 × Art-Net · 0 × MQTT · 0 × Docker
```

Three protocols on site. One computer. One state machine that's a pure function. Everything else is wire.

---

## 16. Open questions

1. **How many clusters, and how many individually-detected dots?** Drives board count, ROI count, vision CPU. Needed before Phase 2 ordering.
2. **One camera or several?** Can a single ceiling-facing viewpoint see every dot, or does the geometry need two? Answer from the physical layout before authoring any ROIs.
3. **Flash ramp shape** — pulse count, total duration, and whether the tail accelerates all the way into GO or holds a beat of silence before the snap. §5.2, decide by looking and listening with haze in the container.
4. **Does the maze blackout between segments,** or snap straight from one preset to the next? A 200–300 ms blackout makes the transition read as intentional and hides relay settling. Recommend blackout; needs a look.
5. **What happens after a BUST** — does the player walk out, or get an immediate retry? Affects `RESULT` duration and gamemaster flow.
6. **Sign-in fields** — final list, so the form and `players.extra_json` can be settled.
7. **Leaderboard scope** — daily reset, or the whole activation? Are busted runs shown at all?
8. ~~**Cloud host and endpoint**~~ — void: cloud sync removed (ADR 0008).
9. ~~**Internet on site**~~ — void: nothing depends on it (ADR 0008).
10. **Which switch model?** Confirm per-port PoE control via API or SNMP; §12 relies on it for camera recovery.
11. **Metric definitions** from the client, when available (§9).

---

## 17. Task: remove cloud sync — DONE 2026-09-13

Completed. See [ADR 0008](docs/adr/0008-remove-cloud-sync.md). The impact map below
is kept as a record of what was touched.

Decided 2026-09-12. Cloud sync is being dropped. It is inert today — the
OutboxWorker only starts when `SCANMANIA_SYNC_URL` is set — but it still costs
a dependency, six admin routes, a DB table and a large share of the persistence
surface. Open questions 8 and 9 above are void once this is done.

Do it in one pass. Half-removed is worse than either end state.

### What it touches

**Delete**
- `persist/outbox.py`, `tests/test_outbox.py`
- `persist/sync.py` — **keep `export_snapshot()`, `export_leaderboard_csv()`,
  `_prune_snapshots()` and `rolling_snapshot_loop()`.** Those are local backup,
  not cloud sync, and the snapshot loop is now wired into `__main__.py`.
- The `outbox` table (`persist/db.py:72`) plus `insert_outbox`,
  `get_pending_outbox`, `mark_outbox_success`, `mark_outbox_attempt`,
  `outbox_depth`, `reset_outbox_backoff`. Needs a migration, not a schema edit —
  existing NUC databases have the table and rows.
- `httpx` from `pyproject.toml` and `requirements.txt`. It is the only consumer.

**Side effect**
- `QueueSync` in `core/events.py`, its handler `_handle_queue_sync` and dispatch
  entry in `core/runner.py`, and **8 emission sites in `core/fsm.py`** (lines
  ~114, 129, 283, 376, 463, 480, 499, 521). Also the `isinstance` tuple in
  `tools/fake_run.py`. Removing the effect touches every terminal-outcome path,
  so lean on `test_fsm.py` — it asserts effect lists.

**Admin API** — six routes in `web/routes_admin.py`:
`/api/admin/outbox`, `/outbox/pause`, `/outbox/resume`, `/outbox/push`,
`/outbox/reset-backoff`, `/outbox/test`. Plus `web_app.set_outbox()` and the
OutboxWorker block in `__main__.py:376-385`.

**Frontend** — 18 references in `web/static/admin/index.html`. The sync card,
its poll, and the outbox depth readout.

**Config and env** — `SCANMANIA_SYNC_URL` and `SCANMANIA_SYNC_TOKEN` in
`__main__.py` and `/etc/default/scanmania` on the NUC.

**Docs** — invariant 6 in `CLAUDE.md` is about cloud sync; rewrite it around the
local-first guarantee or drop it. `docs/adr/0006-local-first-cloud-backup.md`
must be marked `Superseded by 00NN`, with a new ADR recording this decision.
`docs/architecture.md` shows `scanmania-sync.service` in its diagram.

### Watch out for

- **Invariant 6 loses its subject.** "Gameplay never awaits the network" still
  matters for Art-Net and Modbus. Rewrite rather than delete.
- **`QueueSync` currently sits next to `SaveRun` in every terminal path.** Do
  not remove the surrounding effects by accident.
- **The DB migration is the risky part.** Back up the NUC database first, per
  `deployment.md`. Dropping a table is not reversible by a `git revert`.
- The outbox is also the only thing that would have carried run data off the
  NUC. After this, `export_snapshot` and the CSV export are the whole backup
  story — confirm someone actually collects them.

---

## 18. Backlog: the outdoor display's camera

Raised 2026-09-13.

`/display/out` shows a live feed behind the stopwatch and leaderboard. That feed
is **not** one of the detection cameras.

`cam_1`-`cam_4` point straight up at the ceiling dots. Their view is a grid of
bright spots on a flat surface — meaningless to a crowd on the street, and it
would give away nothing about the game. The outdoor feed needs the **back cam at
172.16.0.205**, aimed at the play area, showing a player actually running the
maze.

### What this means for the code

- The back cam is a **display source only**. It must never reach `DotDetector`,
  and no `beams.json` entry should reference it.
- `VisionService._on_frame` no longer pushes ceiling frames to MJPEG — it used
  to, which would have put the ceiling on the outdoor screen the moment the
  MJPEG server could start.
- So the back cam wants its own path: either a fifth `CameraStream` whose only
  consumer is the MJPEG server, or a separate lightweight relay that never
  touches the vision pipeline at all.

### Decide first

**How MJPEG gets served.** `vision/mjpeg.py` imports `aiohttp`, which is declared
in neither `requirements.txt` nor `pyproject.toml` — nobody noticed, because
`MjpegServer` was never instantiated. Two options:

1. Add `aiohttp` and keep the standalone server on :8081, as `display_out`
   already expects (`http://${location.hostname}:8081/cam_a.mjpg`).
2. Serve the stream from the FastAPI app already running on :8000. No new
   dependency, no second port, one less thing to supervise — but the frontend
   URL changes and the route has to stream multipart properly.

Option 2 is cleaner. Either way the frontend's hardcoded `cam_a.mjpg` needs to
point at the back cam.

**Whether it belongs in `hardware.yaml` at all.** The `cameras:` list currently
means "detection cameras" — `config/loader.py` validates that every beam's
`camera` field matches one. Adding a non-detection camera to that list either
needs a `role:` field or a separate `display_camera:` key. The second is simpler
and harder to misuse.

Until then `/display/out` falls back to a black background, which it already
does gracefully.

---

## 19. Calibration: how it runs

`tools/sweep.py`. Two passes, because they answer different questions.

Calibration is scoped to the mazes, not to all 45 channels. Only 36 channels
appear in any shape; the other 9 are wired but unused, so they have no lighting
condition to measure. `--all-channels` includes them.

**LABEL** — one channel lit at a time from dark. Five bright spots on a
near-black frame: unambiguous, no diffing, no bloom. This is where a dot gets
its channel and its camera.

**MEASURE** — runs **once per maze**, that maze lit, each of its channels
blinked off in turn. The game never sees "a
dot appears in darkness", it sees "a dot vanishes while ~180 others stay lit".
This measures the lit baseline and the `dark_floor` — the residual when a dot's
own channel is off but the rest of that maze is on. **Both are stored per maze**,
because the shapes light 46-53% of the floor each and a dot with two lit
neighbours in one shape and none in another reads meaningfully differently. One
number cannot serve all three. A dot whose disappearance is
masked by a neighbour's bloom passes LABEL and is a dead sensor in the maze;
only the dark floor catches it.

### Prerequisites, in order

1. **House lights OFF.** Not optional. The cameras are exposed for bright dots
   on a dark ceiling. With the room lit, the top-hat picks up ceiling texture and
   light fittings, LABEL records those as dots, and MEASURE then reads 0 for
   every one — because ceiling texture is not red. The result is a calibration
   that looks fully populated and detects nothing. The tool refuses to start if
   any camera sees more than `_MAX_AMBIENT_BLOBS` with every laser off.
2. **Camera settings locked** — manual exposure, manual white balance, IR-cut
   and night mode disabled, substream resolution pinned. One visit to a camera
   UI after this invalidates every ROI measured before it.
3. **Cameras physically fixed.** ROIs are frame pixels. A bumped camera is a
   full recalibration.
4. **Game service stopped.** `ReconcileLoop` re-asserts desired coil state every
   500 ms and would re-light channels mid-step, corrupting the labelling with no
   visible symptom.
5. **Nobody in the container.** A body occludes dots and changes the scene.
6. **Lasers warm.** Diode brightness drifts for minutes after power-on.

### Notes from the field

- Verify a relay board with a **Modbus read**, not a ping. One sweep from the
  Mac got no ICMP reply from .100-.106 while Modbus on 4196 answered fine —
  whatever the cause, `connect_all()` returning True is the signal that matters.
  (Ping works from the operator's own machine.)
- Measured on one camera with 2 rows (50 lasers) lit: 46-48 dots found. So
  expect ~4-8% of dots to go unfound, and channels with fewer than 5 recorded
  dots are normal rather than an error.
- Below 4 dots on a channel, `detect.py` can no longer tell a real break from a
  dead channel (`_MIN_DOTS_FOR_FAULT`) — the sweep warns, and those channels
  want a look before opening.
- Colinearity is measured and reported, never enforced. The arrays are
  physically colinear and a pinhole projection preserves straight lines, but
  wide-angle lenses bow them, worst at the frame edges where the far dots land.

### Safety

`--no-write` still drives the relays. There is no flag that runs the sweep
without switching lasers on. Treat every invocation as "the maze is about to
light up".

### Running it

`tools/sweep.py` does not sweep on launch. It connects the cameras and relay
boards, then waits. You choose channels and mazes on the page and press START,
so you can leave it running, walk to the container, kill the house lights and
start the run from a phone. ABORT stops mid-run and leaves the maze dark.

`--now` sweeps immediately and exits, for scripting.

The page on **:8090** — progress, ambient reading,
dots found per channel, dot counts per maze against expected, and a preview per
camera with the detected dots circled. Open it beside the admin panel.

It is a separate server on purpose: the sweep needs the game service stopped, so
`/admin` is down while it runs.
