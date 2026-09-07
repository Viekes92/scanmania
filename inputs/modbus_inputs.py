"""
inputs/modbus_inputs.py — Modbus TCP digital input poller for Arduino Opta.

Inputs:  config (IP, port, poll interval, input-to-event mapping)
Outputs: async events() generator yielding (input_id, state, host_ns) tuples
Invariant: same interface as FakePicoLink so runner.py accepts either.
           Polls discrete inputs via Modbus TCP function 0x02 every poll_ms.
           Debounce handled in Opta firmware (30ms) — we just read and emit on change.

Device: Arduino Opta (AFX00001) with 4 opto-isolated digital inputs.
Protocol: Standard Modbus TCP (MBAP header, port 502). NOT RTU-over-TCP.
Network: SM-NODE-TRIG at 172.16.0.100:502
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from typing import Any

log = logging.getLogger(__name__)

# Modbus TCP uses MBAP header (no CRC), unlike the relay boards which use RTU-over-TCP.
# MBAP: transaction_id(2) + protocol_id(2, always 0) + length(2) + unit_id(1) + PDU
_PROTOCOL_ID = 0x0000
_UNIT_ID = 0x01

# Default input mapping: Opta input number → input_id for FSM
DEFAULT_INPUT_MAP = {
    0: "plate",   # Opta I1 → start pressure plate
    1: "cp1",     # Opta I2 → checkpoint 1
    2: "cp2",     # Opta I3 → checkpoint 2
    3: "stop",    # Opta I4 → stop button
}


class ModbusInputs:
    """
    Polls an Arduino Opta over standard Modbus TCP.

    Reads discrete inputs via function 0x02, detects changes, and emits
    (input_id, state, host_ns) events through the async events() generator.

    Same interface as FakePicoLink — runner.py accepts either.
    """

    def __init__(
        self,
        ip: str = "172.16.0.100",
        port: int = 502,
        poll_ms: int = 50,
        num_inputs: int = 4,
        input_map: dict[int, str] | None = None,
        timeout_ms: int = 200,
    ) -> None:
        self._ip = ip
        self._port = port
        self._poll_ms = poll_ms
        self._num_inputs = num_inputs
        self._input_map = input_map or DEFAULT_INPUT_MAP
        self._timeout_s = timeout_ms / 1000.0

        self._sock: socket.socket | None = None
        self._connected: bool = False
        self._prev_states: list[bool] = [False] * num_inputs
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._transaction_id: int = 0

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Open TCP connection to the Opta."""
        self._close()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(self._timeout_s)
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, sock.connect, (self._ip, self._port)),
                timeout=self._timeout_s * 2,
            )
            self._sock = sock
            self._connected = True
            log.info("ModbusInputs connected to %s:%d", self._ip, self._port)
            return True
        except Exception as exc:
            self._connected = False
            log.warning("ModbusInputs connect failed: %s", exc)
            return False

    def _close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
            self._connected = False

    # ------------------------------------------------------------------
    # Modbus TCP read
    # ------------------------------------------------------------------

    def _read_inputs_sync(self) -> list[bool] | None:
        """Read discrete inputs via Modbus TCP function 0x02. Blocking."""
        if not self._sock:
            return None

        self._transaction_id = (self._transaction_id + 1) & 0xFFFF

        # Build Modbus TCP frame:
        # MBAP header: transaction_id(2) + protocol_id(2) + length(2) + unit_id(1)
        # PDU: function(1) + start_addr(2) + quantity(2)
        pdu = struct.pack(">BHH", 0x02, 0x0000, self._num_inputs)
        mbap = struct.pack(">HHHB",
            self._transaction_id,
            _PROTOCOL_ID,
            len(pdu) + 1,  # length includes unit_id + PDU
            _UNIT_ID,
        )
        frame = mbap + pdu

        try:
            self._sock.sendall(frame)
            resp = self._sock.recv(256)
        except (socket.timeout, OSError) as exc:
            log.debug("ModbusInputs read error: %s", exc)
            return None

        # Parse response: MBAP(7) + function(1) + byte_count(1) + data(N)
        if not resp or len(resp) < 10:
            log.warning("ModbusInputs: short response (%d bytes): %s",
                        len(resp) if resp else 0, resp.hex() if resp else "None")
            return None

        # Check function code (byte 7)
        func = resp[7]
        if func == 0x82:  # exception response
            log.warning("ModbusInputs: Modbus exception code %d", resp[8] if len(resp) > 8 else -1)
            return None
        if func != 0x02:
            log.warning("ModbusInputs: unexpected function 0x%02X (expected 0x02), resp: %s",
                        func, resp.hex())
            return None

        byte_count = resp[8]
        input_bytes = resp[9:9 + byte_count]

        states = []
        for i in range(self._num_inputs):
            byte_idx = i // 8
            bit_idx = i % 8
            if byte_idx < len(input_bytes):
                states.append(bool(input_bytes[byte_idx] & (1 << bit_idx)))
            else:
                states.append(False)

        return states

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main poll loop. Connects and polls inputs, emitting events on change."""
        log.info("ModbusInputs starting — polling %s:%d every %dms",
                 self._ip, self._port, self._poll_ms)
        consecutive_failures = 0

        while True:
            if not self._connected:
                ok = await self.connect()
                if not ok:
                    await asyncio.sleep(3.0)
                    continue
                consecutive_failures = 0

            try:
                loop = asyncio.get_running_loop()
                states = await asyncio.wait_for(
                    loop.run_in_executor(None, self._read_inputs_sync),
                    timeout=self._timeout_s * 2,
                )
            except asyncio.TimeoutError:
                log.warning("ModbusInputs: read timeout")
                states = None

            if states is None:
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    log.warning("ModbusInputs: %d consecutive failures, reconnecting", consecutive_failures)
                    self._close()
                    await asyncio.sleep(2.0)
                else:
                    await asyncio.sleep(0.5)
                continue

            consecutive_failures = 0

            # Detect changes and emit events
            host_ns = time.monotonic_ns()
            for i, (prev, curr) in enumerate(zip(self._prev_states, states)):
                if prev != curr:
                    input_id = self._input_map.get(i)
                    if input_id:
                        state_int = 1 if curr else 0
                        log.info("ModbusInputs: %s → %d", input_id, state_int)
                        try:
                            self._queue.put_nowait((input_id, state_int, host_ns))
                        except asyncio.QueueFull:
                            log.warning("ModbusInputs: queue full, dropping %s=%d",
                                        input_id, state_int)
            self._prev_states = list(states)

            await asyncio.sleep(self._poll_ms / 1000.0)

    # ------------------------------------------------------------------
    # Events generator — same interface as FakePicoLink
    # ------------------------------------------------------------------

    async def events(self):
        """Async generator yielding (input_id, state, host_ns) tuples on input change."""
        while True:
            yield await self._queue.get()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected
