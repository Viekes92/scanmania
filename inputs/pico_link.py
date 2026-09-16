"""
inputs/pico_link.py — async serial link to the Raspberry Pi Pico over USB CDC.

Inputs:  serial port path (/dev/serial/by-id/...), baud 115200, event callbacks
Outputs: parsed EV/HB/BOOT messages dispatched to on_event/on_link_change;
         LED/PING/RESET commands written to the Pico
Invariant: auto-reconnects forever on disconnect; maps Pico ticks_ms to host
           monotonic_ns via a rolling heartbeat offset so USB jitter never leaks
           into event timestamps. No HB for 1 s → INPUT_LINK_DOWN.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

log = logging.getLogger(__name__)

_BAUD = 115200
_RECONNECT_DELAY_S = 2.0
_HEARTBEAT_TIMEOUT_S = 1.0  # no HB for this long → link down

# Input IDs in bitmask order (bit 0 = index 0)
INPUT_IDS = ["start_plate", "stop", "cp1", "cp2", "spare1", "spare2", "spare3", "spare4"]

# Valid LED modes the Pico firmware understands
LED_MODES = frozenset({"off", "on", "pulse", "flash"})


class PicoLink:
    """
    Manages the async serial connection to the Raspberry Pi Pico.

    Parses the line-oriented ASCII protocol and maintains a pico_ms → host_ns
    clock offset estimated from heartbeats (rolling average of the last 8).
    """

    def __init__(
        self,
        port: str,
        on_event: Callable,
        on_link_change: Callable,
    ) -> None:
        self._port = port
        self._on_event = on_event          # (input_id: str, state: int, host_ns: int) → None
        self._on_link_change = on_link_change  # (connected: bool) → None

        self._writer: asyncio.StreamWriter | None = None
        self._connected: bool = False
        self._last_heartbeat_ns: int | None = None

        # Clock offset: host_ns = pico_ms * 1_000_000 + offset
        self._clock_offsets: list[int] = []   # ring buffer, last 8 samples
        self._clock_offset: int | None = None  # current best estimate

        self._last_seq: int | None = None
        # Last level seen per input. The wire protocol is edge-based, so this
        # is the only way to answer "is the plate down RIGHT NOW".
        self._levels: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Open serial port, read lines forever, auto-reconnect on any failure."""
        while True:
            try:
                await self._connect_and_read()
            except Exception as exc:
                log.warning("PicoLink: session ended (%s), reconnecting in %.1f s", exc, _RECONNECT_DELAY_S)
            if self._connected:
                self._set_connected(False)
            await asyncio.sleep(_RECONNECT_DELAY_S)

    async def _connect_and_read(self) -> None:
        import serial_asyncio  # type: ignore[import]

        log.info("PicoLink: opening %s at %d baud", self._port, _BAUD)
        reader, writer = await serial_asyncio.open_serial_connection(
            url=self._port,
            baudrate=_BAUD,
        )
        self._writer = writer
        self._set_connected(True)

        async for raw_line in reader:
            line = raw_line.decode("ascii", errors="replace").strip()
            if not line:
                continue
            self._parse_line(line)
            self._check_heartbeat_timeout()

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_line(self, line: str) -> None:
        parts = line.split()
        if not parts:
            return
        msg_type = parts[0]

        if msg_type == "EV" and len(parts) == 5:
            # EV <seq> <pico_ms> <input_id> <state>
            try:
                seq = int(parts[1])
                pico_ms = int(parts[2])
                input_id = parts[3]
                state = int(parts[4])
            except ValueError:
                log.warning("PicoLink: malformed EV: %r", line)
                return
            self._check_seq(seq)
            host_ns = self.pico_ms_to_host_ns(pico_ms)
            self._levels[input_id] = state
            log.debug("PicoLink EV seq=%d %s=%d", seq, input_id, state)
            self._on_event(input_id, state, host_ns)

        elif msg_type == "HB" and len(parts) == 4:
            # HB <seq> <pico_ms> <bitmask>
            try:
                seq = int(parts[1])
                pico_ms = int(parts[2])
                bitmask = int(parts[3])
            except ValueError:
                log.warning("PicoLink: malformed HB: %r", line)
                return
            self._check_seq(seq)
            self._update_clock_offset(pico_ms)
            self._last_heartbeat_ns = time.monotonic_ns()
            log.debug("PicoLink HB seq=%d pico_ms=%d bitmask=0b%08b", seq, pico_ms, bitmask)

        elif msg_type == "BOOT" and len(parts) >= 2:
            # BOOT <firmware_version>
            fw = parts[1]
            log.info("PicoLink: Pico booted, firmware=%s", fw)
            self._on_link_change(True)

        else:
            log.debug("PicoLink: unrecognised message: %r", line)

    def _check_seq(self, seq: int) -> None:
        if self._last_seq is not None and seq != self._last_seq + 1:
            dropped = seq - self._last_seq - 1
            if dropped > 0:
                log.warning("PicoLink: dropped %d message(s) (last=%d, now=%d)", dropped, self._last_seq, seq)
        self._last_seq = seq

    def _check_heartbeat_timeout(self) -> None:
        if self._last_heartbeat_ns is None:
            return
        age_s = (time.monotonic_ns() - self._last_heartbeat_ns) / 1e9
        if age_s > _HEARTBEAT_TIMEOUT_S and self._connected:
            log.warning("PicoLink: heartbeat timeout (%.1f s) → INPUT_LINK_DOWN", age_s)
            self._set_connected(False)

    # ------------------------------------------------------------------
    # Clock offset
    # ------------------------------------------------------------------

    def _update_clock_offset(self, pico_ms: int) -> None:
        """
        Compute host_ns - pico_ms*1_000_000 and add to rolling buffer.
        Keep the last 8 samples; use their median as the current offset.
        """
        host_ns = time.monotonic_ns()
        sample = host_ns - pico_ms * 1_000_000
        self._clock_offsets.append(sample)
        if len(self._clock_offsets) > 8:
            self._clock_offsets.pop(0)
        sorted_offsets = sorted(self._clock_offsets)
        n = len(sorted_offsets)
        mid = n // 2
        if n % 2 == 0:
            self._clock_offset = (sorted_offsets[mid - 1] + sorted_offsets[mid]) // 2
        else:
            self._clock_offset = sorted_offsets[mid]

    def pico_ms_to_host_ns(self, pico_ms: int) -> int:
        """
        Convert a Pico ticks_ms value to host monotonic_ns using the rolling
        heartbeat offset. Falls back to current time if no heartbeat received yet.
        """
        if self._clock_offset is None:
            return time.monotonic_ns()
        return pico_ms * 1_000_000 + self._clock_offset

    # ------------------------------------------------------------------
    # Commands to Pico
    # ------------------------------------------------------------------

    def input_level(self, input_id: str) -> int | None:
        """
        Current level, or None if this input has not been seen since connect.

        Edges only tell you about changes; a plate already held down when the
        player signs in never sends one.
        """
        if not self._connected:
            return None
        return self._levels.get(input_id)

    async def send_led(self, led_id: str, mode: str) -> None:
        """Send LED command. mode must be one of: off, on, pulse, flash."""
        if mode not in LED_MODES:
            raise ValueError(f"Invalid LED mode '{mode}', expected one of {LED_MODES}")
        await self._send_line(f"LED {led_id} {mode}")

    async def send_ping(self) -> None:
        await self._send_line("PING")

    async def send_reset(self) -> None:
        log.info("PicoLink: sending RESET")
        await self._send_line("RESET")

    async def _send_line(self, line: str) -> None:
        if self._writer is None:
            log.warning("PicoLink: cannot send '%s' — not connected", line)
            return
        try:
            self._writer.write((line + "\r\n").encode("ascii"))
            await self._writer.drain()
            log.debug("PicoLink → Pico: %r", line)
        except Exception as exc:
            log.warning("PicoLink: send failed (%s): %r", exc, line)

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def _set_connected(self, value: bool) -> None:
        if self._connected != value:
            self._connected = value
            log.info("PicoLink: link %s", "UP" if value else "DOWN")
            self._on_link_change(value)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_heartbeat_ns(self) -> int | None:
        return self._last_heartbeat_ns
