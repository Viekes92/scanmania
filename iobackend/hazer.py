"""
iobackend/hazer.py — Art-Net DMX control for the hazer.

Inputs:  ShowTec NET-2/3 Art-Net node IP, DMX channels, intensity values
Outputs: UDP Art-Net ArtDmx packets
Invariant: the blower (ch1) runs continuously. The GM switch gates haze volume
           (ch2) only — it never shuts the machine down, so haze already in the
           container keeps circulating. Amount comes from config, not the GM.
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
        # Art-Net is fire-and-forget UDP, so this is only ever "our last sendto
        # did not raise" — never proof the node received anything. The GM DMX
        # LED says so too: green means we are transmitting, not that it landed.
        self._last_send_ok = True
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
        """
        Turn haze output on or off. The blower keeps running either way.

        True  -> ch2 = self._haze (the configured amount)
        False -> ch2 = 0
        """
        self._enabled = enabled
        self._send()
        log.info("Haze output: %s (blower stays at %d)",
                 "ON" if enabled else "OFF", self._fan)

    @property
    def enabled(self) -> bool:
        """True when haze volume is being output. The blower is always on."""
        return self._enabled

    @property
    def haze(self) -> int:
        return self._haze

    @property
    def fan(self) -> int:
        return self._fan

    @property
    def link_ok(self) -> bool:
        """True while the Art-Net keepalive is going out without a socket error."""
        return self._last_send_ok

    def _send(self) -> None:
        dmx = bytearray(512)
        # The blower runs continuously. The GM switch controls haze VOLUME only
        # (ch2), not the machine.
        #
        # Zeroing both channels stopped the blower as well, which is wrong on
        # two counts: haze already in the container stops circulating and
        # settles unevenly, and the machine loses the airflow it expects. The
        # switch means "stop making haze", not "shut the hazer down".
        dmx[self._fan_ch] = self._fan
        dmx[self._haze_ch] = self._haze if self._enabled else 0
        packet = _build_artdmx(self._universe, dmx)
        try:
            self._sock.sendto(packet, (self._ip, ARTNET_PORT))
            self._last_send_ok = True
        except OSError as exc:
            self._last_send_ok = False
            log.warning("Hazer send failed: %s", exc)

    async def run(self) -> None:
        log.info("Hazer keepalive started")
        try:
            while True:
                self._send()
                await asyncio.sleep(2.0)
        finally:
            # Art-Net nodes hold the last DMX frame they received, and the node
            # is separately powered — so just stopping the keepalive leaves the
            # hazer pumping in an unattended container indefinitely. Send one
            # zeroed frame on the way out.
            self.blackout()

    def blackout(self) -> None:
        """Zero fan and haze, and send it now. Safe to call from a finally."""
        self._fan = 0
        self._haze = 0
        self._enabled = False
        try:
            self._send()
            log.info("Hazer: blackout frame sent")
        except Exception as exc:                       # never block shutdown
            log.warning("Hazer: blackout frame failed: %s", exc)
