#!/usr/bin/env python3
"""
tools/camshow.py — show one ceiling camera, low latency.

Inputs:  a camera number 1-4, or "all" to tile every camera
Outputs: an ffplay window per camera; blocks until you close it or Ctrl-C
Invariant: sandbox only. Reads no game config, drives no relay, writes nothing.
           Uses ffplay rather than cv2 — the OpenCV wheel on the Mac is built
           with FFMPEG=NO, so cv2.VideoCapture fails on any RTSP URL in 0.0 s
           with isOpened() False, which looks exactly like a network fault.

Usage:
  python3 tools/camshow.py 3
  python3 tools/camshow.py all
  python3 tools/camshow.py 2 --probe      # rediscover IPs first (they are DHCP)
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
from urllib.parse import quote

USER = os.environ.get("SCANMANIA_CAM_USER", "scanmania")
PASSWORD = os.environ.get("SCANMANIA_CAM_PASS", "P@ssw0rd123_")

# The MAC-derived path is the stable identity of a camera. The IP is not — these
# are DHCP and have moved mid-session more than once. --probe re-derives the
# mapping by asking each address which path it serves.
CAMERAS = {
    1: ("172.16.0.201", "9c756e2e94bb-0_m"),
    2: ("172.16.0.202", "9c756e2e9aad-0_m"),
    3: ("172.16.0.203", "9c756e2e91c0-0_m"),
    4: ("172.16.0.204", "9c756e2e9121-0_m"),
}

FFPLAY_FLAGS = [
    "-rtsp_transport", "tcp",   # UDP drops frames on a busy switch
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-avioflags", "direct",
    "-framedrop",
    "-an",                      # the cameras have no audio worth decoding
    "-loglevel", "error",
]


def url(ip: str, path: str) -> str:
    """Build the RTSP URL. The password contains '@', which must be quoted."""
    return f"rtsp://{quote(USER, safe='')}:{quote(PASSWORD, safe='')}@{ip}:8554/{path}"


def probe(timeout: float = 2.0) -> dict[int, tuple[str, str]]:
    """
    Ask every known address which path it answers, and rebuild the mapping.

    A camera that has swapped IP keeps its path, so the path is what identifies
    it. Anything unreachable keeps its previous entry.
    """
    import hashlib
    import re

    def describe(ip: str, path: str) -> bool:
        u = url(ip, path)
        try:
            s = socket.create_connection((ip, 8554), timeout=timeout)
        except OSError:
            return False
        s.settimeout(timeout)
        try:
            s.sendall(f"DESCRIBE {u} RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
            first = s.recv(2048).decode("latin1")
            m = re.search(r'realm="([^"]+)".*?nonce="([^"]+)"', first, re.S)
            if not m:
                return " 200 " in first
            realm, nonce = m.groups()
            h = lambda x: hashlib.md5(x.encode()).hexdigest()  # noqa: E731
            resp = h(f"{h(f'{USER}:{realm}:{PASSWORD}')}:{nonce}:{h(f'DESCRIBE:{u}')}")
            auth = (f'Digest username="{USER}", realm="{realm}", nonce="{nonce}", '
                    f'uri="{u}", response="{resp}"')
            s.sendall(f"DESCRIBE {u} RTSP/1.0\r\nCSeq: 2\r\n"
                      f"Authorization: {auth}\r\n\r\n".encode())
            return " 200 " in s.recv(4096).decode("latin1")
        except OSError:
            return False
        finally:
            s.close()

    found = dict(CAMERAS)
    ips = [ip for ip, _ in CAMERAS.values()]
    for cam, (_, path) in CAMERAS.items():
        for ip in ips:
            if describe(ip, path):
                found[cam] = (ip, path)
                break
        else:
            print(f"  camera {cam}: no address serves {path}", file=sys.stderr)
    return found


def play(cam: int, ip: str, path: str, w: int, h: int, x: int, y: int) -> subprocess.Popen:
    return subprocess.Popen(
        ["ffplay", *FFPLAY_FLAGS,
         "-x", str(w), "-y", str(h), "-left", str(x), "-top", str(y),
         "-window_title", f"camera {cam}  ({ip})",
         url(ip, path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("camera", help="camera number 1-4, or 'all'")
    ap.add_argument("--probe", action="store_true",
                    help="rediscover which IP each camera is on before playing")
    ap.add_argument("--size", default="1280x720", metavar="WxH")
    args = ap.parse_args()

    if not shutil.which("ffplay"):
        print("ffplay not found. brew install ffmpeg", file=sys.stderr)
        return 1

    try:
        w, h = (int(v) for v in args.size.lower().split("x"))
    except ValueError:
        print(f"bad --size {args.size!r}, expected WxH", file=sys.stderr)
        return 1

    mapping = probe() if args.probe else CAMERAS
    if args.probe:
        for cam, (ip, path) in sorted(mapping.items()):
            print(f"  camera {cam} -> {ip}  {path}")

    if args.camera.lower() == "all":
        procs = []
        for i, (cam, (ip, path)) in enumerate(sorted(mapping.items())):
            hw, hh = w // 2, h // 2
            procs.append(play(cam, ip, path, hw, hh,
                              (i % 2) * (hw + 8), (i // 2) * (hh + 40)))
        print(f"4 cameras tiled at {w // 2}x{h // 2}. Ctrl-C to stop.")
    else:
        try:
            cam = int(args.camera)
            ip, path = mapping[cam]
        except (ValueError, KeyError):
            print(f"unknown camera {args.camera!r}; pick 1-4 or 'all'", file=sys.stderr)
            return 1
        procs = [play(cam, ip, path, w, h, 100, 60)]
        print(f"camera {cam} — {ip}  {path}  {w}x{h}. Ctrl-C to stop.")

    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
