# ADR 0001: Modbus TCP for laser control, not Art-Net

**Status:** Accepted
**Date:** 2024-01-15

## Context

We needed a protocol to control N Waveshare 16-channel relay boards (SKU 30795). The initial
design used Art-Net (DMX over UDP), familiar from stage lighting. The boards speak Modbus
RTU/TCP natively; Art-Net would have required a translator process sitting between the game
server and the boards.

Art-Net was being asked to do two jobs, and only one was real:
1. **Transport** — sending channel values to devices.
2. **Show control** — a mental model of named scenes triggerable externally.

Job 2 is just a config file (`config/mazes.yaml`) and a function call (`io/presets.py`).

## Decision

Use **`pymodbus` writing coils directly to each board over TCP**. Apply presets with Modbus
function **0x0F (write multiple coils)**, which sets an entire 16-channel board atomically in
one transaction. Read back coils with 0x01 for reconciliation.

**Implementation detail:** The Waveshare boards run in transparent (passthrough) mode on **port
4196**. The wire protocol is **Modbus RTU** (with CRC, no MBAP header), not standard Modbus TCP.
`io/modbus.py` opens a raw TCP socket and sends RTU frames directly. This is the board's default
factory configuration and is simpler than configuring Modbus TCP mode.

Named presets live in `config/mazes.yaml`. `io/presets.py` resolves a preset name to per-board
16-bit coil arrays, then calls one `write_coils` per board.

Hardware: 3 boards — SM-NODE-1 (10.0.0.11), SM-NODE-2 (10.0.0.12), SM-NODE-3 (10.0.0.13) —
covering 45 relay channels for 45 laser strips arranged in a 9-row × 5-column grid.

## Alternatives considered

**Art-Net + translator:** Adds a translator process, a UDP dependency, and a protocol designed
for DMX fixtures, not relay control. The "named shows" concept is a YAML file and a function.

**MQTT:** Adds a broker, publish/subscribe complexity, and ordering guarantees that QoS 0 does
not provide. More failure modes, no benefit over a direct TCP connection.

**Single-coil writes in a loop:** Each channel written individually means the maze morphs
channel-by-channel. Visually wrong and slower. One 0x0F call per board is atomic.

**Standard Modbus TCP (MBAP framing, port 502):** The boards support it, but require a firmware
config change from the factory default. RTU-over-TCP on port 4196 works out of the box and is
equally reliable on a LAN.

## Consequences

- One `write_coils` per board per preset change — atomic, fast, visually correct snap.
- Reconciliation loop every 500 ms: read actual coil state, re-assert if it differs. Handles
  board reboots and dropped TCP sessions without additional protocol.
- One async task and one TCP connection per board. 200 ms timeout, 3 consecutive failures →
  `DEGRADED`. A slow or dead board never stalls the others.
- `io/fake.py` simulates the same interface; the FSM and web layer never know the difference.
- Any future engineer seeing port 4196 and no MBAP header must read this ADR before assuming
  the code is wrong.
