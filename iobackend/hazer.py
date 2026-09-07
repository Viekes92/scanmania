"""
iobackend/hazer.py — Art-Net DMX control for the hazer.

Inputs:  IP of ShowTec NET-2/3 Art-Net node, DMX channel, intensity (0-255)
Outputs: UDP Art-Net ArtDmx packets to the DMX controller
Invariant: hazer is ON by default. GM can toggle on/off and adjust intensity.
           Art-Net packets are re-sent every 2 seconds to keep the hazer alive
           (some DMX devices timeout without continuous signal).

Device: ShowTec NET-2/3 (SM-NODE-DMX) at 172.16.0.201:6454
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct

log = logging.getLogger(__name__)

ARTNET_PORT = 6454


def _build_artdmx(universe: int, dmx_data: bytearray) -> bytes:
    """Build an Art-Net ArtDmx packet."""
    header = b"Art-Net\x00"
    opcode = struct.pack("<H", 0x5000)   # ArtDmx
    version = struct.pack(">H", 14)      # protocol version 14
    sequence = b"\x00"
    physical = b"\x00"
    uni = struct.pack("<H", universe)
    length = struct.pack(">H", len(dmx_data))
    return header + opcode + version + sequence + physical + uni + length + bytes(dmx_data)


class HazerController:
    """
    Controls a hazer via Art-Net DMX through a ShowTec NET-2/3 node.

    The hazer is ON by default at the configured intensity.
    Call set_intensity(0-255) to adjust, or set_enabled(False) to turn off.
    A background task re-sends the DMX frame every 2 seconds.
    """

    def __init__(
        self,
        artnet_ip: str = "172.16.0.201",
        universe: int = 0,
        channel: int = 1,       # DMX channel (1-indexed)
        fan_channel: int = 2,   # fan speed channel (0 = no fan control)
        default_intensity: int = 128,
        default_fan: int = 200,
    ) -> None:
        self._ip = artnet_ip
        self._universe = universe
        self._channel = channel - 1      # convert to 0-indexed
        self._fan_channel = fan_channel - 1 if fan_channel > 0 else -1
        self._intensity = default_intensity
        self._fan = default_fan
        self._enabled = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        log.info("HazerController: %s universe=%d ch=%d intensity=%d",
                 artnet_ip, universe, channel, default_intensity)

    def set_intensity(self, value: int) -> None:
        """Set haze intensity (0-255)."""
        self._intensity = max(0, min(255, value))
        self._send()
        log.info("Hazer: intensity=%d", self._intensity)

    def set_fan(self, value: int) -> None:
        """Set fan speed (0-255)."""
        self._fan = max(0, min(255, value))
        self._send()

    def set_enabled(self, enabled: bool) -> None:
        """Turn hazer on/off."""
        self._enabled = enabled
        self._send()
        log.info("Hazer: %s", "ON" if enabled else "OFF")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def intensity(self) -> int:
        return self._intensity

    @property
    def fan(self) -> int:
        return self._fan

    def _send(self) -> None:
        """Send current DMX state to the Art-Net node."""
        dmx = bytearray(512)
        if self._enabled:
            dmx[self._channel] = self._intensity
            if self._fan_channel >= 0:
                dmx[self._fan_channel] = self._fan
        # else: all zeros = off
        packet = _build_artdmx(self._universe, dmx)
        try:
            self._sock.sendto(packet, (self._ip, ARTNET_PORT))
        except OSError as exc:
            log.warning("Hazer: send failed: %s", exc)

    async def run(self) -> None:
        """Background task: re-send DMX every 2 seconds to keep the hazer alive."""
        log.info("Hazer: keepalive loop started")
        while True:
            self._send()
            await asyncio.sleep(2.0)
