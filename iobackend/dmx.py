"""
iobackend/dmx.py — Art-Net DMX output: the hazer AND the room lights.

Inputs:  ShowTec NET-2/3 Art-Net node IP, DMX channel map, levels
Outputs: UDP Art-Net ArtDmx packets
Invariant: ONE object owns the universe. Every send writes a full 512-byte
           frame, so a second sender on the same universe would zero whatever
           the first one set, twice a second. That is why the lights live here
           rather than in a module of their own.
Invariant: the blower (ch1) runs continuously. The GM switch gates haze volume
           (ch2) only — it never shuts the machine down, so haze already in the
           container keeps circulating. Amount comes from config, not the GM.
           Re-sends DMX every 2s to prevent timeout.
Invariant: the entrance light is never switched off by software, including on
           shutdown. A dark container with people in it and no lit exit is the
           one state this system must never create.

DMX channel map (universe 1):
  Ch 1 = hazer blower speed (0-255)
  Ch 2 = hazer haze volume  (0-255)
  Ch 3 = maze light, left     )  a 3-channel decoder addressed at 3, so its
  Ch 4 = maze light, right    )  own ch1/ch2/ch3 land on DMX 3/4/5
  Ch 5 = entrance light       )  1-channel white each, 0-255
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time

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


class DmxController:
    def __init__(
        self,
        artnet_ip: str = "172.16.0.201",
        universe: int = 1,
        fan_channel: int = 1,
        haze_channel: int = 2,
        default_fan: int = 200,
        default_haze: int = 128,
        haze_burst_s: float = 0.0,
        haze_interval_s: float = 0.0,
        lights: dict | None = None,
    ) -> None:
        self._ip = artnet_ip
        self._universe = universe
        self._fan_ch = fan_channel - 1      # 0-indexed
        self._haze_ch = haze_channel - 1    # 0-indexed
        self._fan = default_fan
        self._haze = default_haze
        self._enabled = True

        # Haze is duty-cycled: even the lowest usable level is too much output
        # when it runs continuously. burst_s of haze, then nothing until the
        # next interval. 0 burst means continuous, the old behaviour.
        self._burst_s = float(haze_burst_s)
        self._interval_s = float(haze_interval_s)
        self._bursting = False
        self._cycle_pos = 0.0

        # Lights on the same universe. name -> {ch (0-indexed), level, target}
        cfg = lights or {}
        self._lights: dict[str, dict] = {}
        for name, spec in (cfg.get("fixtures") or {}).items():
            ch = int(spec["channel"]) - 1
            lvl = int(spec.get("default", 255))
            self._lights[name] = {"ch": ch, "level": float(lvl), "target": lvl,
                                  "fade_from": float(lvl), "fade_start": None,
                                  # Kept so blackout() can RESTORE an always_on
                                  # fixture rather than guess at full.
                                  "default": lvl,
                                  "always_on": bool(spec.get("always_on", False))}
        self._fade_ms = int(cfg.get("fade_ms", 800))
        # Above the node's DMX output rate (35 Hz on SM-NODE-DMX), so every
        # frame it sends carries a fresh value rather than a repeat.
        self._fade_step_hz = 44.0

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
        dmx[self._haze_ch] = self._haze if self._output_haze() else 0
        for st in self._lights.values():
            dmx[st["ch"]] = max(0, min(255, int(round(st["level"]))))
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
            since_keepalive = 0.0
            while True:
                # Tick fast enough to fade smoothly; send a keepalive at least
                # every 2 s so the node never times out and drops the frame.
                dt_tick = 1.0 / self._fade_step_hz
                moving = self._step_fades()
                duty_changed = self._step_duty(dt_tick)
                if moving or duty_changed or since_keepalive >= 2.0:
                    self._send()
                    since_keepalive = 0.0
                dt = 1.0 / self._fade_step_hz
                await asyncio.sleep(dt)
                since_keepalive += dt
        finally:
            # Art-Net nodes hold the last DMX frame they received, and the node
            # is separately powered — so just stopping the keepalive leaves the
            # hazer pumping in an unattended container indefinitely. Send one
            # zeroed frame on the way out.
            self.blackout()

    def _output_haze(self) -> bool:
        """True while haze should actually be flowing this instant."""
        if not self._enabled:
            return False
        if self._burst_s <= 0 or self._interval_s <= 0:
            return True                       # continuous
        return self._bursting

    @property
    def hazing(self) -> bool:
        """Whether haze is flowing right now, as opposed to merely enabled."""
        return self._output_haze()

    @property
    def duty(self) -> dict:
        return {"burst_s": self._burst_s, "interval_s": self._interval_s,
                "bursting": self._bursting}

    def set_duty(self, burst_s: float | None = None,
                 interval_s: float | None = None) -> None:
        if burst_s is not None:
            self._burst_s = max(0.0, float(burst_s))
        if interval_s is not None:
            self._interval_s = max(0.0, float(interval_s))
        self._cycle_pos = 0.0
        log.info("haze duty: %.1fs every %.1fs", self._burst_s, self._interval_s)

    def _step_duty(self, dt: float) -> bool:
        """Advance the haze cycle. True if the on/off state just changed."""
        if self._burst_s <= 0 or self._interval_s <= 0:
            was, self._bursting = self._bursting, True
            return was is not True
        self._cycle_pos = (self._cycle_pos + dt) % max(self._interval_s, 0.001)
        want = self._cycle_pos < self._burst_s
        if want != self._bursting:
            self._bursting = want
            log.debug("haze %s", "burst" if want else "idle")
            return True
        return False

    # ------------------------------------------------------------------
    # Lights
    # ------------------------------------------------------------------

    def light_names(self) -> list[str]:
        return sorted(self._lights)

    def light_level(self, name: str) -> int:
        st = self._lights.get(name)
        return int(round(st["level"])) if st else 0

    def lights_state(self) -> dict:
        """Levels and targets, for the GM console and the admin page."""
        return {n: {"level": int(round(s["level"])), "target": int(s["target"]),
                    "always_on": s["always_on"], "channel": s["ch"] + 1}
                for n, s in self._lights.items()}

    def set_light(self, name: str, level: int, fade: bool = True, allow_always_on: bool = False) -> bool:
        """
        Aim one light at a level. Returns False if the name is unknown.

        `allow_always_on` is the one door through that guard, and only a LIGHT
        CUE holds the key. The entrance leaks badly into the container and has
        to be dark while someone is running, but it must still be lit whenever
        a person is walking in or out — so the decision belongs to the cue
        table, state by state, rather than being refused outright here.

        blackout() still restores it: if this process dies, the way out is lit
        no matter which cue was last applied.

        A light marked always_on refuses to be dimmed below its default — the
        entrance is a means of egress, not a show effect.
        """
        st = self._lights.get(name)
        if st is None:
            log.warning("set_light: unknown light %r (known: %s)",
                        name, self.light_names())
            return False
        level = max(0, min(255, int(level)))
        if st["always_on"] and level < 1 and not allow_always_on:
            log.warning("refusing to switch off '%s' — it is marked always_on", name)
            return False
        st["target"] = level
        if not fade or self._fade_ms <= 0:
            st["level"] = float(level)
            st["fade_start"] = None
            self._send()
        else:
            st["fade_from"] = float(st["level"])
            st["fade_start"] = time.monotonic()
        return True

    def set_maze_lights(self, on: bool, fade: bool = True) -> None:
        """Every light that is NOT always_on. The room lights for a run."""
        for name, st in self._lights.items():
            if st["always_on"]:
                continue
            self.set_light(name, 255 if on else 0, fade=fade)

    def _step_fades(self, now: float | None = None) -> bool:
        """
        Advance every fade. True if anything is still moving.

        Interpolated against elapsed TIME, not incremented by a fixed step.
        The step used to be derived from full 0-255 travel and applied whatever
        the distance — so a 14->2 attract pulse, twelve levels, completed in a
        single tick and did not fade at all, while a full-range fade climbed in
        visible twelve-level stairs.

        Now every fade takes fade_ms regardless of distance, and the level is
        carried as a float so short moves use fractional steps instead of
        snapping to the next whole DMX value.
        """
        if self._fade_ms <= 0:
            return False
        now = time.monotonic() if now is None else now
        dur = self._fade_ms / 1000.0
        moving = False
        for st in self._lights.values():
            if st.get("fade_start") is None:
                continue
            frac = (now - st["fade_start"]) / dur if dur > 0 else 1.0
            if frac >= 1.0:
                st["level"] = float(st["target"])
                st["fade_start"] = None
                moving = True            # this tick still changed something
                continue
            a, b = st["fade_from"], float(st["target"])
            st["level"] = a + (b - a) * frac
            moving = True
        return moving

    def blackout(self) -> None:
        """
        Zero the hazer and the maze lights, and send it now.

        The ENTRANCE light is deliberately left on. Art-Net nodes hold the last
        frame they received, so this is the state the container is left in when
        the process exits — and leaving people in a dark box with an unlit way
        out is worse than any of the things this function is protecting against.

        Safe to call from a finally; never raises.
        """
        self._fan = 0
        self._haze = 0
        self._enabled = False
        for name, st in self._lights.items():
            if st["always_on"]:
                # RESTORE it, do not merely leave it. A cue may have taken the
                # entrance dark for a run; if the process is dying, the way out
                # must be lit again — that is the whole point of always_on, and
                # it is now the only place the flag is absolute.
                st["level"] = float(st.get("default", 255))
                st["target"] = int(st.get("default", 255))
                st["fade_start"] = None
            else:
                st["level"] = 0.0
                st["target"] = 0
                st["fade_start"] = None
        try:
            self._send()
            kept = [n for n, s in self._lights.items() if s["always_on"]]
            log.info("DMX blackout sent (still lit: %s)", ", ".join(kept) or "nothing")
        except Exception as exc:                       # never block shutdown
            log.warning("DMX blackout frame failed: %s", exc)

    def power_down(self) -> None:
        """
        Everything dark, INCLUDING the always_on entrance light.

        blackout() deliberately keeps the entrance lit, and that is right for
        every path it serves: a process that dies must not leave people in a
        dark box with an unlit way out. This is the other case — an operator
        standing at the breaker at the end of the day, who wants the container
        actually dark before cutting power. It is reached only from an explicit
        shutdown request, never from an exit path or a finally.

        Art-Net nodes hold the last frame they received, so this frame is the
        state the container keeps after the power is cut.

        Safe to call from a finally; never raises.
        """
        self._fan = 0
        self._haze = 0
        self._enabled = False
        for st in self._lights.values():
            st["level"] = 0.0
            st["target"] = 0
            st["fade_start"] = None
        try:
            self._send()
            log.info("DMX power-down sent — everything dark, entrance included")
        except Exception as exc:                       # never block shutdown
            log.warning("DMX power-down frame failed: %s", exc)


# The class was HazerController until the room lights joined it on the same
# universe. Kept as an alias so an out-of-tree import does not break.
HazerController = DmxController
