# Architecture

## Overview

One NUC runs everything. Six systemd units communicate over localhost sockets and shared SQLite. Hardware is connected over Ethernet (Modbus TCP) and USB (Pico serial). The gamemaster uses a Wi-Fi iPad.

```
┌─────────────────────────────────────────────────────────────────┐
│  NUC (Debian 13)                                                │
│                                                                 │
│  scanmania.service   — ONE process, one asyncio loop            │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ core/fsm.py        pure transition function, no I/O        │ │
│  │ core/runner.py     event drain, side effects, timers       │ │
│  │ core/stopwatch.py  monotonic_ns; the server owns the clock │ │
│  │                                                            │ │
│  │ iobackend/   Modbus master, preset resolution, reconcile   │ │
│  │              loop, Art-Net DMX (hazer + room lights)       │ │
│  │ inputs/      Arduino Opta over Modbus TCP (plate, cp1,     │ │
│  │              cp2, stop). Pico/USB is superseded, ADR 0004  │ │
│  │ vision/      RTSP decode, dot detection, baselines,        │ │
│  │              evidence thumbnails                           │ │
│  │ audio/       pygame.mixer bed + one-shot cues              │ │
│  │ persist/     SQLite + local snapshots. No cloud, ADR 0008  │ │
│  │ web/         FastAPI + uvicorn, WebSocket broadcast 10 Hz  │ │
│  │              /signin /gm /admin /display/in /display/out   │ │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  scanmania-kiosk.service                                        │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ X session (no window manager — geometry is explicit)       │ │
│  │ Chromium × 2, one per panel. WHICH output carries which    │ │
│  │ page is config, not code: SCANMANIA_OUT_IN / _OUT in       │ │
│  │ /etc/default/scanmania (kiosk.sh defaults IN=HDMI-1,       │ │
│  │ OUT=HDMI-2; the container currently swaps them).           │ │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
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

Player crosses checkpoint 1
  → Pico CP1 event → runner.py dispatches CP1_PRESSED to FSM
  → FSM returns (RUN_SEG_2, [ApplyPreset("maze_2", defer=True), BroadcastState])
  → runner.py waits game.checkpoint_shape_delay_ms (300 ms) in a SEPARATE task,
    not in the event drain — the drain is single-consumer and sleeping in it
    would stall the stop button and every beam event for the duration
  → after the wait: vision/detect.py is repointed AND io/presets.py writes the
    coils, in that order and in the same step. They must move together: the
    detector watches the dots captured for a preset, so repointing it early
    would leave it reading dots that are not lit yet — dark — and bust the
    player for a shape that has not appeared
  → the OLD shape stays lit and watched throughout the wait, which is correct:
    the player is still in the container and those beams are still real

Vision detects break on beam b07
  → vision/detect.py: ratio < break_ratio for N frames (SHIPPED N=1, see ADR 0002)
  → vision/evidence.py: saves JPEG crop
  → runner.py: dispatches BREAK_CONFIRMED(beam_id="b07") to FSM
  → FSM returns (BUSTED, [StopStopwatch, ApplyPreset("bust"), BuzzerOn, LogRun])
  → runner.py: persist/db.py records run + events
  → WebSocket: broadcasts BUSTED to all displays
```

## Communication protocols

| Link | Protocol | Notes |
|------|----------|-------|
| NUC → relay boards | Modbus TCP, function 0x0F (write coils) + 0x01 (read coils) | One async connection per board, 200 ms timeout |
| NUC ← Opta | Modbus TCP (function 0x02, discrete inputs), polled | `inputs/modbus_inputs.py`. The Pico/USB path in ADR 0004 is superseded. |
| NUC ← cameras | RTSP (one decode per stream) | 8 × 1920×1080, locked exposure. ROIs are frame pixels — see invariant 3. |
| Browser ← NUC | WebSocket (10 Hz broadcast) + HTTP/static | `web/server.py` |

## Services

| Unit | Restart | Owns |
|------|---------|------|
| `scanmania` | always, 2 s | Everything: FSM, Modbus, inputs, vision, audio, DB, HTTP + WebSocket |
| `scanmania-kiosk` | always | X session, two Chromium kiosk windows |

Two units, not six. `deploy/` contains exactly these. The kiosk unit `Wants=`
the game, which is why stopping the game means stopping the kiosk **first** or
it drags the game back up within seconds.

systemd hardware watchdog enabled via `RuntimeWatchdogSec`. A kernel hang reboots the NUC into `SELF_TEST → ATTRACT`.

> An earlier design split this into `scanmania-io` / `-vision` / `-core` / `-web`
> over unix sockets. That split was never built and is not planned; the single
> process is the design. ADR 0005 records the systemd-not-docker decision, and
> its six-unit sketch is historical.

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

## Frontend accessibility conventions

The frontends are single-file HTML with no build step and no linter, so the rules below are
enforced by convention. They apply to `web/static/admin/index.html` today; extend the same
treatment to any new panel.

- **Colour tokens split by role.** `--accent` is chrome only — borders, glows, `accent-color`.
  Anything that renders as text uses `--accent-text`. `--accent` is 2.3:1 on the panel surfaces
  and fails AA outright; keeping the two apart is what stops it drifting back into body text.
- **Never `outline: none`.** A `:focus-visible` ring is defined once at the top of the stylesheet.
  Inline `style="outline:none"` beats it, so do not write one.
- **Anything clickable is focusable.** A `<div>` with a click handler needs `role="button"`,
  `tabindex="0"`, an accessible name, and Enter/Space activation. Prefer a real `<button>` when
  the layout allows it. If the handler re-renders its container, capture the focused element
  first and restore focus afterwards — otherwise focus falls back to `<body>` on every toggle.
- **Every input has a name.** A `placeholder` is not a label; use a wrapping `<label>` or
  `aria-label`.
- **Tabs follow the ARIA tabs pattern** — `aria-selected` tracks the active class, `tabindex` is
  roving, arrows and Home/End move between tabs.
- Diagrams use `max-width`, not `width`, so they survive a narrow viewport.

## State machine summary

Key states (there is no separate game-rules doc; this section is the prose):

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
- **Local-first durability** — SQLite is source of truth. Cloud sync removed (ADR 0008); snapshots and CSV export are the whole backup story.
- **Three detection modes + master mode** — `auto` / `assisted` / `manual` plus full manual control for the gamemaster.
