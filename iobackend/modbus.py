"""
iobackend/modbus.py — Modbus RTU-over-TCP client for Waveshare relay boards.

Inputs:  board config (board_id, ip, port, timeout_ms) from hardware.yaml
Outputs: coil read/write via raw TCP sockets with Modbus RTU framing (CRC16)
Invariant: one TCP connection per board instance; timeout per call;
           3 consecutive failures → status='DEGRADED'. Each board runs in its
           own async task so a slow/dead board never stalls others.

Protocol: Modbus RTU frames (slave + function + data + CRC16) sent over a raw
          TCP socket. Port 4196 (Waveshare transparent mode). NOT standard
          Modbus TCP (no MBAP header). Matches the working test script.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Literal

from config.loader import HardwareConfig

log = logging.getLogger(__name__)

_COIL_COUNT = 16
_FAILURE_THRESHOLD = 3
_SLAVE_ADDR = 0x01

Status = Literal["OK", "DEGRADED", "DISCONNECTED"]


# ---------------------------------------------------------------------------
# CRC16 — Modbus RTU standard (same as the test script)
# ---------------------------------------------------------------------------

def _crc16(data: bytes) -> bytes:
    """Modbus CRC16, returned as 2 bytes little-endian."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "little")


# ---------------------------------------------------------------------------
# ModbusBoard — one board, one TCP socket
# ---------------------------------------------------------------------------

class ModbusBoard:
    """
    Async Modbus RTU-over-TCP client for one Waveshare 16-channel relay board.

    Uses raw TCP sockets with CRC16 framing (transparent mode, port 4196).
    Call connect() once before read/write operations. Tracks consecutive
    failures and sets status='DEGRADED' after 3 misses.
    """

    def __init__(
        self,
        board_id: str,
        ip: str,
        port: int = 4196,
        timeout_ms: int = 200,
    ) -> None:
        self.board_id = board_id
        self._ip = ip
        self._port = port
        self._timeout_s = timeout_ms / 1000.0

        self._sock: socket.socket | None = None
        self._status: Status = "DISCONNECTED"
        self._consecutive_failures: int = 0
        self._last_coils: list[bool] = [False] * _COIL_COUNT
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Open TCP connection. Safe to call multiple times."""
        self._close_socket()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(self._timeout_s)
            # Run blocking connect in a thread to not block the event loop
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, sock.connect, (self._ip, self._port)),
                timeout=self._timeout_s * 2,
            )
            self._sock = sock
            self._status = "OK"
            self._consecutive_failures = 0
            log.info("ModbusBoard '%s' connected to %s:%d", self.board_id, self._ip, self._port)
            return True
        except Exception as exc:
            self._status = "DISCONNECTED"
            log.warning("ModbusBoard '%s' connect failed: %s", self.board_id, exc)
            return False

    def _close_socket(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    # ------------------------------------------------------------------
    # Raw send/recv
    # ------------------------------------------------------------------

    def _flush_recv(self) -> None:
        """Drain any stale data from the socket recv buffer."""
        if not self._sock:
            return
        self._sock.setblocking(False)
        try:
            while True:
                data = self._sock.recv(256)
                if not data:
                    # Peer closed the connection
                    self._close_socket()
                    return
        except (BlockingIOError, OSError):
            pass
        if self._sock:
            self._sock.setblocking(True)
            self._sock.settimeout(self._timeout_s)

    def _send_raw(self, body: bytes) -> bytes | None:
        """Send RTU frame (body + CRC16) and read response. Validates response CRC."""
        if not self._sock:
            return None
        self._flush_recv()  # drain stale responses
        frame = body + _crc16(body)
        try:
            self._sock.sendall(frame)
            resp = self._sock.recv(256)
            if not resp or len(resp) < 4:
                log.debug("ModbusBoard '%s': short response (%d bytes)", self.board_id, len(resp) if resp else 0)
                return None
            # Validate CRC
            payload, crc_received = resp[:-2], resp[-2:]
            if crc_received != _crc16(payload):
                log.warning("ModbusBoard '%s': CRC mismatch in response", self.board_id)
                return None
            return resp
        except (socket.timeout, OSError) as exc:
            log.debug("ModbusBoard '%s' send/recv error: %s", self.board_id, exc)
            return None

    # ------------------------------------------------------------------
    # Coil writes — function 0x0F (write multiple coils)
    # ------------------------------------------------------------------

    async def write_coils(self, coils: list[bool]) -> bool:
        """
        Write all 16 coils atomically using Modbus function 0x0F.

        Uses the Waveshare broadcast address 0x00FF to set all coils at once,
        falling back to 0x0F (write multiple coils) if needed.
        """
        if len(coils) != _COIL_COUNT:
            raise ValueError(f"Expected {_COIL_COUNT} coils, got {len(coils)}")

        async with self._lock:
            if not await self._ensure_connected():
                return self._record_failure("write_coils: not connected")

            try:
                loop = asyncio.get_running_loop()

                # Build write_multiple_coils frame: slave(1) + func(0x0F) +
                # start_addr(2) + coil_count(2) + byte_count(1) + coil_data(N)
                coil_bytes = bytearray(((_COIL_COUNT + 7) // 8))
                for i, v in enumerate(coils):
                    if v:
                        coil_bytes[i // 8] |= (1 << (i % 8))

                body = (
                    bytes([_SLAVE_ADDR, 0x0F])
                    + (0).to_bytes(2, "big")           # start address
                    + _COIL_COUNT.to_bytes(2, "big")   # quantity
                    + bytes([len(coil_bytes)])          # byte count
                    + bytes(coil_bytes)                 # coil data
                )

                resp = await asyncio.wait_for(
                    loop.run_in_executor(None, self._send_raw, body),
                    timeout=self._timeout_s * 2,
                )

                if resp and len(resp) >= 4 and resp[1] == 0x0F:
                    self._last_coils = list(coils)
                    self._record_success()
                    return True
                else:
                    return self._record_failure(
                        f"write_coils: bad response ({resp.hex() if resp else 'None'})"
                    )

            except asyncio.TimeoutError:
                return self._record_failure("write_coils: timeout")
            except Exception as exc:
                return self._record_failure(f"write_coils error: {exc}")

    # ------------------------------------------------------------------
    # Coil reads — function 0x01 (read coils)
    # ------------------------------------------------------------------

    async def read_coils(self) -> list[bool] | None:
        """Read all 16 coils using Modbus function 0x01."""
        async with self._lock:
            if not await self._ensure_connected():
                self._record_failure("read_coils: not connected")
                return None

            try:
                loop = asyncio.get_running_loop()
                body = (
                    bytes([_SLAVE_ADDR, 0x01])
                    + (0).to_bytes(2, "big")           # start address
                    + _COIL_COUNT.to_bytes(2, "big")   # quantity
                )

                resp = await asyncio.wait_for(
                    loop.run_in_executor(None, self._send_raw, body),
                    timeout=self._timeout_s * 2,
                )

                if resp and len(resp) >= 5 and resp[1] == 0x01:
                    byte_count = resp[2]
                    coil_data = resp[3:3 + byte_count]
                    coils = []
                    for i in range(_COIL_COUNT):
                        byte_idx = i // 8
                        bit_idx = i % 8
                        if byte_idx < len(coil_data):
                            coils.append(bool(coil_data[byte_idx] & (1 << bit_idx)))
                        else:
                            coils.append(False)
                    self._last_coils = coils
                    self._record_success()
                    return coils
                else:
                    self._record_failure(
                        f"read_coils: bad response ({resp.hex() if resp else 'None'})"
                    )
                    return None

            except asyncio.TimeoutError:
                self._record_failure("read_coils: timeout")
                return None
            except Exception as exc:
                self._record_failure(f"read_coils error: {exc}")
                return None

    # ------------------------------------------------------------------
    # Single coil write — function 0x05 (write single coil)
    # ------------------------------------------------------------------

    async def write_single_coil(self, channel: int, state: bool) -> bool:
        """Write a single coil (0-indexed channel). Uses function 0x05."""
        async with self._lock:
            if not await self._ensure_connected():
                return self._record_failure("write_single_coil: not connected")

            try:
                loop = asyncio.get_running_loop()
                value = b"\xff\x00" if state else b"\x00\x00"
                body = bytes([_SLAVE_ADDR, 0x05]) + channel.to_bytes(2, "big") + value

                resp = await asyncio.wait_for(
                    loop.run_in_executor(None, self._send_raw, body),
                    timeout=self._timeout_s * 2,
                )

                if resp and len(resp) >= 4 and resp[1] == 0x05:
                    self._last_coils[channel] = state
                    self._record_success()
                    return True
                else:
                    return self._record_failure(
                        f"write_single_coil: bad response ({resp.hex() if resp else 'None'})"
                    )

            except asyncio.TimeoutError:
                return self._record_failure("write_single_coil: timeout")
            except Exception as exc:
                return self._record_failure(f"write_single_coil error: {exc}")

    # ------------------------------------------------------------------
    # All-on / All-off shortcut — Waveshare broadcast address 0x00FF
    # ------------------------------------------------------------------

    async def all_coils(self, state: bool) -> bool:
        """Turn all relays on or off using the Waveshare broadcast address."""
        async with self._lock:
            if not await self._ensure_connected():
                return self._record_failure("all_coils: not connected")

            try:
                loop = asyncio.get_running_loop()
                value = b"\xff\x00" if state else b"\x00\x00"
                body = bytes([_SLAVE_ADDR, 0x05, 0x00, 0xFF]) + value

                resp = await asyncio.wait_for(
                    loop.run_in_executor(None, self._send_raw, body),
                    timeout=self._timeout_s * 2,
                )

                if resp and len(resp) >= 4 and resp[1] == 0x05:
                    self._last_coils = [state] * _COIL_COUNT
                    self._record_success()
                    return True
                else:
                    return self._record_failure(
                        f"all_coils: bad response ({resp.hex() if resp else 'None'})"
                    )

            except asyncio.TimeoutError:
                return self._record_failure("all_coils: timeout")
            except Exception as exc:
                return self._record_failure(f"all_coils error: {exc}")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def status(self) -> Status:
        return self._status

    @property
    def last_coils(self) -> list[bool]:
        """Last known coil state (from read or write)."""
        return list(self._last_coils)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> bool:
        if self._sock is not None:
            return True
        return await self.connect()

    def _record_failure(self, reason: str) -> bool:
        self._consecutive_failures += 1
        log.warning("ModbusBoard '%s' failure #%d: %s",
                    self.board_id, self._consecutive_failures, reason)
        if self._consecutive_failures >= _FAILURE_THRESHOLD:
            if self._status != "DEGRADED":
                log.error("ModbusBoard '%s' DEGRADED after %d failures",
                          self.board_id, self._consecutive_failures)
            self._status = "DEGRADED"
        return False

    def _record_success(self) -> None:
        if self._consecutive_failures > 0:
            log.info("ModbusBoard '%s' recovered after %d failure(s)",
                     self.board_id, self._consecutive_failures)
        self._consecutive_failures = 0
        self._status = "OK"


# ---------------------------------------------------------------------------
# ModbusIOBackend — collection of boards, same interface as FakeIOBackend
# ---------------------------------------------------------------------------

class ModbusIOBackend:
    """
    Real Modbus backend using RTU-over-TCP to Waveshare relay boards.

    Satisfies the IOBackend protocol consumed by PresetResolver and ReconcileLoop.
    Creates one ModbusBoard per entry in hardware_config.relay_boards.
    Call connect_all() to open connections to all boards.
    """

    def __init__(self, hardware_config: HardwareConfig) -> None:
        self._boards: dict[str, ModbusBoard] = {}
        for b in hardware_config.relay_boards:
            self._boards[b.id] = ModbusBoard(
                board_id=b.id,
                ip=b.ip,
                port=b.port,
                timeout_ms=b.timeout_ms,
            )
        log.info("ModbusIOBackend initialised with boards: %s",
                 list(self._boards.keys()))

    async def connect_all(self) -> dict[str, bool]:
        """Connect to all boards. Returns {board_id: connected}."""
        results = {}
        for bid, board in self._boards.items():
            results[bid] = await board.connect()
        return results

    def get_board(self, board_id: str) -> ModbusBoard:
        if board_id not in self._boards:
            raise KeyError(f"ModbusIOBackend: unknown board_id '{board_id}'")
        return self._boards[board_id]

    def all_boards(self) -> list[ModbusBoard]:
        return list(self._boards.values())
