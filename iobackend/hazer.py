"""
iobackend/hazer.py — Art-Net DMX control for the hazer.

Inputs:  ShowTec NET-2/3 Art-Net node IP, DMX channels, intensity values
Outputs: UDP Art-Net ArtDmx packets
Invariant: hazer ON by default. GM can toggle and adjust via sliders.
           Re-sends DMX every 2s to prevent timeout.

DMX channels (from hazer manual):
  Ch 1 = Blower Speed (0-255)
  Ch 2 = Haze Volume (0-255)
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct

log = logging.getLogger(__name__)

ARTNET_PORT = 6454


def _build_artdmx(universe: int, dmx_data: bytearray) -> bytes:
    header = b"Art-Net\x00"
    opcode = struct.pack("<H", 0x5000)
    version = struct.pack(">H", 14)
    sequence = b"\x00"
    physical = b"\x00"
    uni = struct.pack("<H", universe)
    length = struct.pack(">H", len(dmx_data))
    return header + opcode + version + sequence + physical + uni + length + bytes(dmx_data)


class HazerController:
    def __init__(
        self,
        artnet_ip: str = "172.16.0.201",
        universe: int = 1,
        fan_channel: int = 1,
        haze_channel: int = 2,
        default_fan: int = 200,
        default_haze: int = 128,
    ) -> None:
        self._ip = artnet_ip
        self._universe = universe
        self._fan_ch = fan_channel - 1      # 0-indexed
        self._haze_ch = haze_channel - 1    # 0-indexed
        self._fan = default_fan
        self._haze = default_haze
        self._enabled = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        log.info("HazerController: %s fan=ch%d haze=ch%d",
                 artnet_ip, fan_channel, haze_channel)

    def set_haze(self, value: int) -> None:
        self._haze = max(0, min(255, value))
        self._send()
        log.info("Hazer: haze=%d", self._haze)

    def set_fan(self, value: int) -> None:
        self._fan = max(0, min(255, value))
        self._send()
        log.info("Hazer: fan=%d", self._fan)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._send()
        log.info("Hazer: %s", "ON" if enabled else "OFF")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def haze(self) -> int:
        return self._haze

    @property
    def fan(self) -> int:
        return self._fan

    def _send(self) -> None:
        dmx = bytearray(512)
        if self._enabled:
            dmx[self._fan_ch] = self._fan
            dmx[self._haze_ch] = self._haze
        packet = _build_artdmx(self._universe, dmx)
        try:
            self._sock.sendto(packet, (self._ip, ARTNET_PORT))
        except OSError as exc:
            log.warning("Hazer send failed: %s", exc)

    async def run(self) -> None:
        log.info("Hazer keepalive started")
        while True:
            self._send()
            await asyncio.sleep(2.0)
