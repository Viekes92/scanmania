#!/usr/bin/env python3
"""
tools/dmxpatch.py — print the DMX patch, derived from config.

Inputs:  config/hardware.yaml
Outputs: the universe patch on stdout; --live also shows current levels
Invariant: generated from config, never hand-maintained. A patch sheet that has
           drifted from what the software actually sends is worse than none,
           because it is the document you trust at 2am with a dead fixture.

    python3 tools/dmxpatch.py
    python3 tools/dmxpatch.py --live      # read what the controller holds now
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

import config.loader as loader


def build_patch(hz: dict) -> list[dict]:
    """One row per used channel, in channel order."""
    rows: list[dict] = []
    rows.append({
        "ch": int(hz.get("fan_channel", 1)),
        "fixture": "hazer",
        "role": "blower speed",
        "value": f"{hz.get('default_fan', 255)}",
        "note": "runs continuously — the GM switch never stops the blower, "
                "so haze already in the container keeps circulating",
    })
    burst = float(hz.get("haze_burst_s", 0) or 0)
    interval = float(hz.get("haze_interval_s", 0) or 0)
    if burst > 0 and interval > 0:
        duty = f"{hz.get('default_haze', 0)} for {burst:g}s every {interval:g}s"
        note = (f"duty-cycled ({100*burst/interval:.1f}%) — continuous output at "
                f"any usable level is too much haze")
    else:
        duty = f"{hz.get('default_haze', 0)} continuous"
        note = "continuous — set haze_burst_s/haze_interval_s to duty-cycle it"
    rows.append({"ch": int(hz.get("haze_channel", 2)), "fixture": "hazer",
                 "role": "haze volume", "value": duty, "note": note})

    for name, spec in (hz.get("lights", {}).get("fixtures") or {}).items():
        always = bool(spec.get("always_on", False))
        rows.append({
            "ch": int(spec["channel"]),
            "fixture": f"light: {name}",
            "role": "1ch white dimmer",
            "value": f"{spec.get('default', 255)} default",
            "note": ("ALWAYS ON — never switched off by software, including on "
                     "shutdown" if always else
                     "driven by FSM light_cues; forced to 0 during COUNTDOWN and "
                     "every RUN state"),
        })
    return sorted(rows, key=lambda r: r["ch"])


def _listen(hz: dict, seconds: float) -> int:
    """
    Watch the wire for ArtDmx and report who is sending on our universe.

    This is the failure that cost an evening: the NUC was powered up and its
    service was transmitting on the same universe. Every Art-Net frame carries
    all 512 channels, so its build — which has no lights section — was writing
    zeros over ch3-5 twice a second. Nothing detects that from the sending
    side: our sendto() succeeds, link_ok stays green, and the fixtures simply
    never come on.
    """
    import socket
    import struct
    import time

    want = hz.get("universe")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", 6454))
    except OSError as exc:
        print(f"  cannot bind UDP 6454 ({exc}) — something else owns it.")
        return 1
    s.settimeout(0.5)

    me = {a for a in _my_addrs()}
    senders: dict[tuple, int] = {}
    print(f"  listening {seconds:g}s for ArtDmx (our universe is {want})…\n")
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            data, src = s.recvfrom(1024)
        except socket.timeout:
            continue
        if not data.startswith(b"Art-Net\x00") or len(data) < 18:
            continue
        if struct.unpack("<H", data[8:10])[0] != 0x5000:       # ArtDmx only
            continue
        uni = struct.unpack("<H", data[14:16])[0]
        senders[(src[0], uni)] = senders.get((src[0], uni), 0) + 1
    s.close()

    if not senders:
        print("  nothing is transmitting DMX right now.")
        return 0

    print(f"  {'SOURCE':<16} {'UNIVERSE':>9} {'FRAMES':>7}   WHO")
    print("  " + "-" * 62)
    clash = False
    for (ip, uni), n in sorted(senders.items(), key=lambda kv: -kv[1]):
        who = "this machine" if ip in me else "SOMETHING ELSE"
        if ip not in me and uni == want:
            who = "** ANOTHER SENDER ON OUR UNIVERSE **"
            clash = True
        print(f"  {ip:<16} {uni:>9} {n:>7}   {who}")
    print("  " + "-" * 62)
    if clash:
        print("\n  Two senders on one universe cannot be merged by wishing.")
        print("  Stop the other one — usually the game service on the NUC:")
        print("    ssh root@172.16.0.10 'systemctl stop scanmania-kiosk scanmania'")
    return 0


def _my_addrs() -> set:
    import socket
    out = {"127.0.0.1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 1))
        out.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return out


def _discover(hz: dict) -> int:
    """
    Ask every Art-Net node which Port-Address it listens on.

    Worth doing before trusting the universe in config: Art-Net is
    fire-and-forget UDP, so sending to the wrong universe looks identical to
    working — sendto() succeeds, link_ok stays green, and nothing lights. That
    is exactly how this rig shipped with universe 1 against a node listening
    on 2.
    """
    import socket
    import struct
    import time

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", 6454))
    except OSError as exc:
        print(f"  cannot bind UDP 6454 ({exc}).")
        print("  Something else owns it — the game service, or another copy "
              "of this tool.")
        return 1
    s.settimeout(0.4)

    pkt = (b"Art-Net\x00" + struct.pack("<H", 0x2000)
           + struct.pack(">H", 14) + b"\x02\x00")
    for addr in ("255.255.255.255", "172.16.0.255", hz.get("artnet_ip", "")):
        if addr:
            try:
                s.sendto(pkt, (addr, 6454))
            except OSError:
                pass

    seen: dict[str, dict] = {}
    end = time.monotonic() + 3.0
    while time.monotonic() < end:
        try:
            data, _src = s.recvfrom(1024)
        except socket.timeout:
            continue
        if not data.startswith(b"Art-Net\x00") or len(data) < 200:
            continue
        if struct.unpack("<H", data[8:10])[0] != 0x2100:      # ArtPollReply
            continue
        ip = ".".join(str(b) for b in data[10:14])
        ports = struct.unpack(">H", data[172:174])[0]
        # BindIndex (byte 211) identifies WHICH port this reply is for. A
        # multi-port node sends one reply per port, so keying on IP alone threw
        # every port but the last away — which is how this tool confidently
        # reported universe 2 for a node whose DMX-A port is universe 1.
        bind = data[211] if len(data) > 211 else 0
        seen[f"{ip}#{bind}"] = {
            "ip": ip, "bind": bind,
            "short": data[26:44].split(b"\x00")[0].decode("latin1"),
            "net": data[18], "sub": data[19],
            "swout": list(data[190:194][:max(1, ports)]),
        }
    s.close()

    if not seen:
        print("  no node answered ArtPoll.\n")
        print("  That is not proof of absence — some nodes never reply. But if "
              "the\n  fixtures are also dark, suspect the network before the "
              "code.")
        return 1

    want = hz.get("universe")
    all_addrs: set[int] = set()
    for key in sorted(seen):
        n = seen[key]
        addrs = [(n["net"] << 8) | (n["sub"] << 4) | (sw & 0x0F)
                 for sw in n["swout"]]
        all_addrs.update(addrs)
        shown = ", ".join(
            f"{a}{'  <-- config' if a == want else ''}" for a in addrs)
        print(f"  {n['ip']}  bind {n['bind']:>2}  '{n['short']:<18}' "
              f"Net={n['net']} SubNet={n['sub']}  universe {shown}")
    print()
    if want in all_addrs:
        print(f"  config universe {want} is served by this node.")
    else:
        print(f"  ** config says universe {want}, but this node only serves "
              f"{sorted(all_addrs)} **")
    print("  Which physical XLR port that is (DMX-A / DMX-B) is on the node's "
          "own\n  web page — ArtPoll reports the address, not the socket.\n")
    return 0


def _controller(hz: dict):
    from iobackend.dmx import DmxController
    return DmxController(
        artnet_ip=hz.get("artnet_ip", "127.0.0.1"),
        universe=hz.get("universe", 1),
        fan_channel=hz.get("fan_channel", 1),
        haze_channel=hz.get("haze_channel", 2),
        default_fan=hz.get("default_fan", 255),
        default_haze=hz.get("default_haze", 0),
        haze_burst_s=hz.get("haze_burst_s", 0),
        haze_interval_s=hz.get("haze_interval_s", 0),
        lights=hz.get("lights"),
    )


def _drive(hz: dict, args) -> int:
    """
    Actually send DMX. Art-Net is fire-and-forget UDP, so nothing here can
    confirm the node received anything — the confirmation is your eyes.
    """
    import time

    d = _controller(hz)
    print(f"  sending to {hz.get('artnet_ip')}:6454 universe {hz.get('universe')}")
    print("  NOTE: nothing about Art-Net can tell us the node got this.")
    print("  Stop the game service first, or it will fight for the universe.\n")

    if args.haze_test:
        lvl = hz.get("default_haze", 0)
        burst = hz.get("haze_burst_s", 0) or 10
        print(f"  haze: level {lvl}/255 for {burst:g}s (blower stays on)…")
        d.set_enabled(True)
        d._bursting = True
        end = time.monotonic() + burst
        while time.monotonic() < end:
            d._send()
            time.sleep(0.5)
        d._bursting = False
        d.set_enabled(False)
        d._send()
        print("  haze off, blower still running.")
        return 0

    # Channel walk: one light at a time, everything else down.
    names = d.light_names()
    if not names:
        print("  no lights configured"); return 1
    order = sorted(names, key=lambda n: d.lights_state()[n]["channel"])
    print("  Watch the container. Each fixture lights alone for "
          f"{args.hold:g}s.\n")
    try:
        for name in order:
            ch = d.lights_state()[name]["channel"]
            for other in names:
                d._lights[other]["level"] = d._lights[other]["target"] = 0
            d._lights[name]["level"] = d._lights[name]["target"] = 255
            d._send()
            print(f"    ch{ch:>2}  ->  config says this is '{name}'. "
                  f"Which fixture actually lit?")
            time.sleep(args.hold)
    finally:
        # Leave the room as the config says it should idle: entrance up.
        for n, st in d._lights.items():
            st["level"] = st["target"] = 255 if st["always_on"] else 0
        d._send()
        lit = [n for n, s in d._lights.items() if s["always_on"]]
        print(f"\n  done — left lit: {', '.join(lit) or 'nothing'}")
    print("\n  If a fixture lit on the wrong channel, fix `channel:` in "
          "hardware.yaml.\n  The always_on guard must land on the REAL "
          "entrance, or it can go\n  dark during a run while a wall light is "
          "the one being protected.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--live", action="store_true",
                    help="also show the levels the controller is holding now")
    ap.add_argument("--walk", action="store_true",
                    help="light each light channel in turn so you can SEE which "
                         "fixture is on which channel. Drives real DMX.")
    ap.add_argument("--hold", type=float, default=3.0,
                    help="seconds to hold each channel during --walk")
    ap.add_argument("--discover", action="store_true",
                    help="ArtPoll the network and report which Port-Address "
                         "each node actually LISTENS on. Do this before "
                         "believing the universe in config.")
    ap.add_argument("--listen", type=float, default=0, metavar="SECONDS",
                    help="watch for OTHER senders on this universe. Two "
                         "processes on one universe is the classic failure: "
                         "each zeroes the other's channels and nothing lights.")
    ap.add_argument("--haze-test", action="store_true",
                    help="run one haze burst at the configured level, then stop. "
                         "Drives the real hazer.")
    args = ap.parse_args()

    hw = yaml.safe_load((Path(loader.CONFIG_DIR) / "hardware.yaml").read_text())
    hz = hw.get("hazer") or {}
    rows = build_patch(hz)

    print(f"\n  ART-NET NODE   {hz.get('artnet_ip', '?')}:6454"
          f"   universe {hz.get('universe', 0)}   (SM-NODE-DMX)\n")
    print(f"  {'CH':>3}  {'FIXTURE':<16} {'ROLE':<17} {'VALUE':<26} NOTE")
    print("  " + "-" * 104)
    used = set()
    for r in rows:
        used.add(r["ch"])
        print(f"  {r['ch']:>3}  {r['fixture']:<16} {r['role']:<17} "
              f"{r['value']:<26} {r['note']}")
    print("  " + "-" * 104)

    gaps = [c for c in range(1, max(used) + 1) if c not in used]
    print(f"  channels used: {sorted(used)}"
          + (f"   UNPATCHED GAP: {gaps}" if gaps else "   (contiguous)"))
    print(f"  every frame carries all 512 channels, so ONE process owns this "
          f"universe —\n  a second sender would zero everything above.\n")

    if args.listen:
        return _listen(hz, args.listen)

    if args.discover:
        return _discover(hz)

    if args.walk or args.haze_test:
        return _drive(hz, args)

    if args.live:
        from iobackend.dmx import DmxController
        d = DmxController(
            artnet_ip="127.0.0.1", universe=hz.get("universe", 1),
            fan_channel=hz.get("fan_channel", 1), haze_channel=hz.get("haze_channel", 2),
            default_fan=hz.get("default_fan", 255), default_haze=hz.get("default_haze", 0),
            haze_burst_s=hz.get("haze_burst_s", 0), haze_interval_s=hz.get("haze_interval_s", 0),
            lights=hz.get("lights"),
        )
        print("  LEVELS AS CONSTRUCTED (not read back from the node — Art-Net")
        print("  is fire-and-forget UDP and cannot be queried):")
        for n, st in sorted(d.lights_state().items(), key=lambda kv: kv[1]["channel"]):
            print(f"    ch{st['channel']:>2}  {n:<9} {st['level']:>3}")
        print(f"    haze flowing right now: {d.hazing}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
