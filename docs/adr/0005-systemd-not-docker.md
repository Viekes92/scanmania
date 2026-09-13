# ADR 0005: Native systemd on Debian 12, not Docker

**Status:** Accepted
**Date:** 2024-01-15

## Context

The NUC requires: USB serial access to the Pico, V4L2/VAAPI for camera decode, a graphical X
session across two HDMI outputs, and tight control over process restart behaviour. We needed to
choose a process management and deployment strategy.

## Decision

**Debian 12, one virtualenv, six systemd units, `Restart=always`, `RestartSec=2`.**

```
scanmania-io.service       Modbus master + reconciliation
scanmania-vision.service   per-camera decode, dot detection, MJPEG out
scanmania-core.service     FSM, stopwatch, scoring, Pico serial
scanmania-web.service      FastAPI + WebSocket + static frontends
(snapshots and exports now run inside scanmania.service — ADR 0008)
scanmania-kiosk.service    user unit: X session + 2x Chromium kiosk
```

Deploy = `git pull && systemctl restart scanmania-*`.
Debug = `journalctl -u scanmania-core -f`.
Hardware watchdog enabled via `RuntimeWatchdogSec=30` in core and web units.

## Alternatives considered

**Docker Compose:** To do what we need in Docker requires `--device /dev/ttyUSB0` (Pico),
`--device /dev/video0` (camera), `--privileged` or `SYS_ADMIN` for V4L2, `network_mode: host`
for Modbus TCP, and an X socket bind for the kiosk. At that point we have all of Docker's
opacity and none of its isolation benefit. The reproducible environment is already provided by
the pinned `requirements.txt` and a venv.

**Podman rootless:** Same device-access problems; rootless makes USB and V4L2 worse, not better.

**Bare Debian without a process manager:** Leaves us writing our own restart logic and logging.
systemd's `Restart=always`, `journalctl`, and `After=`/`Requires=` dependency graph are exactly
what we need. Building around them is correct.

## Consequences

- One person must be able to debug this at 02:00 on install day. `journalctl -u scanmania-core
  -f` is the entire debugging story. No container runtime to know.
- Service dependencies expressed with `After=` and `Requires=` so units start in the correct
  order after boot and after `Restart=`.
- `scanmania-kiosk.service` is a user unit (not root) owning the X session. It starts after
  `graphical.target` so both displays are available.
- Hardware watchdog: a kernel hang reboots into SELF_TEST → ATTRACT. Free insurance.
- Provisioning documented step-by-step in `docs/deployment.md`. A bare Debian 12 install should
  reach a playable state following that document alone.
