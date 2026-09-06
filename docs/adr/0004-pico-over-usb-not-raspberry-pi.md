# ADR 0004: Raspberry Pi Pico over USB for physical inputs, not a full SBC

**Status:** Accepted
**Date:** 2024-01-15

## Context

We need to read five momentary dry contacts: start plate, stop button, checkpoint 1, checkpoint
2, and four spares wired to a terminal block. We need to drive LEDs on the start and stop
buttons. The inputs must be reliable, low-latency, and self-recovering without requiring human
intervention when a cable is unplugged.

## Decision

**Raspberry Pi Pico over USB CDC serial at 115200 baud.** The Pico runs MicroPython with a
hardware watchdog (`machine.WDT`). Inputs use 30 ms firmware debounce and 100 nF capacitors at
the Pico end to suppress phantom triggers.

The Pico timestamps its own events with `time.ticks_ms()`. The host maintains a `pico_ms →
host monotonic` offset estimated from 250 ms heartbeats, so USB scheduling jitter never leaks
into the score.

Protocol (line-oriented ASCII, 115200, directly debuggable with `screen`):
```
Pico → host:  EV <seq> <pico_ms> <input_id> <state>
              HB <seq> <pico_ms> <input_bitmask>   (every 250 ms)
              BOOT <firmware_version>
host → Pico:  LED <led_id> <off|on|pulse|flash>
              PING
              RESET
```

The 250 ms heartbeat carries the full input bitmask: liveness detection (no HB for 1 s →
`INPUT_LINK_DOWN`) and state resync so a missed edge self-corrects.

## Alternatives considered

**Raspberry Pi (full SBC) as a dedicated input controller:** Linux boot time (20–40 s) is
unacceptable for "power on → playable" with no keyboard. An SD card is a failure mode in a
high-vibration container environment. The Pico does the job with no OS, no SD card, and a
hardware watchdog that recovers a wedged device in seconds.

**Direct GPIO on the NUC:** The NUC has no GPIO headers. This is not an option without
additional hardware.

**I2C/SPI GPIO expander (e.g. MCP23017):** Works electrically, but requires the NUC to be
physically close to the expander (I2C has a short reach). There is no firmware layer to handle
debounce, timestamps, and LED drive. The Pico handles all of this natively.

**Arduino (C/C++ firmware):** Functionally equivalent. MicroPython was chosen because the
firmware is readable and patchable by anyone on the team without a C toolchain. On-site firmware
updates require only a text editor.

## Consequences

- Mount the Pico in the rack (USB reliable to ~5 m). Run button wiring to it, not USB cable to
  the buttons.
- Shielded or twisted pair for long button runs. Pico GND as the return. Internal pull-ups plus
  100 nF at the Pico end. If phantom triggers appear at real cable lengths, upgrade to 24 V
  optocoupler loops (Phase 2 decision, to be made with real cable in hand).
- Host opens Pico by stable path (`/dev/serial/by-id/...` via udev rule). Auto-reconnects
  forever — someone will unplug it.
- Wedged Pico reboots via hardware watchdog and re-announces with `BOOT`.
- Sequence numbers on `EV` lines let the host detect dropped lines.
