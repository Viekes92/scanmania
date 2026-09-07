# Architecture

## Overview

One NUC runs everything. Six systemd units communicate over localhost sockets and shared SQLite. Hardware is connected over Ethernet (Modbus TCP) and USB (Pico serial). The gamemaster uses a Wi-Fi iPad.

```
┌─────────────────────────────────────────────────────────────────┐
│  NUC (Debian 12)                                                │
│                                                                 │
│  scanmania-io.service          scanmania-vision.service         │
│  ┌────────────────────┐        ┌───────────────────────────┐   │
│  │ Modbus master       │        │ RTSP decode               │   │
│  │ Preset resolution   │        │ Dot detection             │   │
│  │ Reconciliation loop │        │ Baseline management       │   │
│  └────────┬───────────┘        │ Evidence thumbnails        │   │
│           │ unix socket         │ MJPEG stream out          │   │
│           │                    └─────────────┬─────────────┘   │
│  scanmania-core.service                      │ unix socket      │
│  ┌──────────────────────────────────────┐   │                  │
│  │ FSM (pure function, core/fsm.py)     │◄──┘                  │
│  │ Stopwatch                            │                       │
│  │ Scoring                              │                       │
│  │ Pico serial (inputs/pico_link.py)    │                       │
│  └──────────────┬───────────────────────┘                       │
│                 │ WebSocket broadcast                            │
│  scanmania-web.service                                          │
│  ┌──────────────▼───────────────────────┐                       │
│  │ FastAPI + uvicorn                    │                       │
│  │ /signin  /gm  /admin                 │                       │
│  │ /display/in  /display/out            │                       │
│  └──────────────────────────────────────┘                       │
│                                                                 │
│  scanmania-sync.service          scanmania-kiosk.service        │
│  ┌────────────────────┐          ┌───────────────────────┐      │
│  │ Outbox drain       │          │ X session             │      │
│  │ Cloud POST         │          │ Chromium × 2 (kiosk)  │      │
│  │ Snapshots/exports  │          │ HDMI1: /display/in    │      │
│  └────────────────────┘          │ HDMI2: /display/out   │      │
│                                  └───────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
       │ Ethernet (Modbus TCP)          │ USB (serial, 115200)
       ▼                               ▼
Waveshare relay boards ×N         Raspberry Pi Pico
(16ch each, static IPs)           (buttons, plates, LEDs)
       │ PoE
       ▼
PoE cameras ×1–2 (RTSP)
```

## Data flow during a run

```
Pico: plate HIGH event
  → inputs/pico_link.py maps Pico timestamp → host monotonic
  → core/runner.py dispatches PLATE_HIGH to FSM
  → FSM returns (ARM, [BeamPreflightCheck, ReadyBlink])
  → runner.py executes side effects: io/presets.py, vision/detect.py

Gamemaster taps COUNT IN
  → WebSocket message → web/routes_gm.py → runner.py
  → FSM returns (COUNTDOWN, [StartCountin])
  → runner.py schedules pulse edges off monotonic_ns
  → Each pulse: io/presets.py.apply_preset() → pymodbus write_coils
  → Pulse 0 on: vision/baseline.py captures baseline
  → Ramp complete: FSM → (RUN_SEG_1, [StartStopwatch, ArmDetection])

Vision detects break on beam b07
  → vision/detect.py: ratio < break_ratio for N=3 frames
  → vision/evidence.py: saves JPEG crop
  → runner.py: dispatches BREAK_CONFIRMED(beam_id="b07") to FSM
  → FSM returns (BUSTED, [StopStopwatch, ApplyPreset("bust"), BuzzerOn, LogRun])
  → runner.py: persist/db.py records run + events
  → persist/outbox.py: queues run for cloud sync
  → WebSocket: broadcasts BUSTED to all displays
```

## Communication protocols

| Link | Protocol | Notes |
|------|----------|-------|
| NUC → relay boards | Modbus TCP, function 0x0F (write coils) + 0x01 (read coils) | One async connection per board, 200 ms timeout |
| NUC ← Pico | USB CDC serial, 115200, ASCII line protocol | See `docs/protocols/pico-serial.md` |
| NUC ← cameras | RTSP (one decode per stream) | 1280×720 @ 25–30 fps, locked exposure |
| Browser ← NUC | WebSocket (10 Hz broadcast) + HTTP/static | See `docs/protocols/websocket.md` |
| NUC → cloud | HTTPS POST, idempotency key per run | See `docs/protocols/cloud-api.md` |

## Services

| Unit | Restart | Owns |
|------|---------|------|
| `scanmania-io` | always, 2 s | Modbus connections, coil state, reconciliation |
| `scanmania-vision` | always, 2 s | Camera decode, detection, MJPEG stream |
| `scanmania-core` | always, 2 s | FSM, stopwatch, Pico link, event log |
| `scanmania-web` | always, 2 s | HTTP + WebSocket, static frontends |
| `scanmania-sync` | always, 2 s | Outbox drain, cloud sync, snapshots |
| `scanmania-kiosk` | user unit | X session, two Chromium kiosk windows |

systemd hardware watchdog enabled via `RuntimeWatchdogSec`. A kernel hang reboots the NUC into `SELF_TEST → ATTRACT`.

> On the NUC these are currently collapsed into a single `scanmania.service` plus
> `scanmania-kiosk.service`. The split above is the target, not the deployed shape.

## Coil reconciliation

`iobackend/reconcile.py` is invariant 4's safety backstop. `GameRunner.run()` starts it as
the `reconcile` task; every 500 ms it reads actual coil state from each board concurrently
and compares it to `PresetResolver.desired_state()`. A difference is logged, counted per
board, emitted as `relay.mismatch`, and re-asserted with a full `write_coils`.

The re-assert here is the one permitted exception to "only `presets.py` writes coils".

It takes a **callable** returning the resolver rather than the resolver itself.
`GameRunner.reload_config()` builds a new `PresetResolver`, and a captured reference would
leave the reconciler enforcing the pre-edit desired state forever — quietly undoing every
config change. Per-board mismatch counts surface on the admin hardware page as
`mismatch_count`.

## Deploying

`tools/deploy.sh`, run on the box:

```bash
ssh root@172.16.0.10 '/opt/scanmania/tools/deploy.sh'
```

`git pull` alone is not enough — it does not restart the service, install new dependencies,
or back up the database before an automatic schema migration. The script does all of that
and refuses to run if there is uncommitted work outside `config/`.

`config/` is the exception because the admin panel writes into the live checkout, so shows
and beam geometry legitimately drift on the box. The script commits and pushes that drift
before pulling, which is why live show tuning ends up in git rather than being lost.

## State machine summary

See `docs/game-rules.md` for prose. Key states:

```
BOOT → SELF_TEST → ATTRACT → REGISTERED → ARM → COUNTDOWN → RUN_SEG_1 → RUN_SEG_2 → RUN_SEG_3
                                                                    └─────────────────────┘
                                                                    FINISHED / BUSTED / ABORTED
                                                                           → RESULT → RESET → ATTRACT
```

All transitions are pure (`core/fsm.py`). Side effects are executed by `core/runner.py`.

## Key design decisions

See `docs/adr/` for full reasoning. Short form:

- **Modbus TCP over Art-Net** — direct, no translator, one atomic `write_coils` per board.
- **Ceiling dots over beam sampling** — fixed, high-contrast, binary, unaffected by haze.
- **Hard cutoff scoring** — break = run over; no penalty accumulation, no grace periods.
- **Pico over USB** — no OS, no SD card, deterministic, WDT for self-recovery.
- **systemd over Docker** — USB serial, V4L2, two HDMI outputs; Docker gives opacity with no isolation benefit.
- **Local-first cloud backup** — SQLite is source of truth; outbox drains when network exists.
- **Three detection modes + master mode** — `auto` / `assisted` / `manual` plus full manual control for the gamemaster.
