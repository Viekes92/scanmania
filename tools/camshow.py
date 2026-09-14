#!/usr/bin/env python3
"""
tools/camshow.py — show the ceiling cameras, low latency.

Inputs:  a camera name or number, or "all" / "left" / "right"
Outputs: an ffplay window per camera; blocks until you close it or Ctrl-C
Invariant: sandbox only. Drives no relay, writes nothing. Reads hardware.yaml
           for addresses and nothing else.
           Uses ffplay rather than cv2 — the OpenCV wheel on the Mac is built
           with FFMPEG=NO, so cv2.VideoCapture fails on any RTSP URL in 0.0 s
           with isOpened() False, which looks exactly like a network fault.

Usage:
  python3 tools/camshow.py 13           # SM-CAM-13
  python3 tools/camshow.py SM-CAM-21
  python3 tools/camshow.py left         # the four on side 1
  python3 tools/camshow.py all          # all eight, tiled
  python3 tools/camshow.py --probe      # which address answers which path

Addresses come from config/hardware.yaml, so there is one place to be wrong.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config.loader as loader


FFPLAY_FLAGS = [
    "-rtsp_transport", "tcp",   # UDP drops frames on a busy switch
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-avioflags", "direct",
    "-framedrop",
    "-an",                      # the cameras have no audio worth decoding
    "-loglevel", "error",
]


class Cam:
    """One camera from hardware.yaml, split into the parts ffplay and RTSP need."""

    def __init__(self, cfg) -> None:
        u = urlsplit(cfg.url)
        self.id: str = cfg.id
        self.url: str = cfg.url
        self.ip: str = u.hostname or ""
        self.port: int = u.port or 8554
        self.path: str = u.path.lstrip("/")
        self.user: str = unquote(u.username or "")
        self.password: str = unquote(u.password or "")
        self.note: str = cfg.note

    def at(self, ip: str) -> str:
        """The same stream, addressed at a different IP. Used by --probe."""
        return self.url.replace(f"@{self.ip}:", f"@{ip}:", 1)


def load_cams() -> list[Cam]:
    return [Cam(c) for c in loader.load_hardware().cameras]


def select(cams: list[Cam], which: str) -> list[Cam]:
    """
    Resolve a selector to cameras.

    Accepts the full id (SM-CAM-13), the bare number (13), a side
    (left / right / 1 / 2), or all.
    """
    w = which.strip().lower()
    if w == "all":
        return cams
    if w in ("left", "1"):
        return [c for c in cams if c.id.rsplit("-", 1)[-1].startswith("1")]
    if w in ("right", "2"):
        return [c for c in cams if c.id.rsplit("-", 1)[-1].startswith("2")]
    exact = [c for c in cams if c.id.lower() == w]
    if exact:
        return exact
    return [c for c in cams if c.id.rsplit("-", 1)[-1] == w]


def describe(cam: Cam, ip: str, timeout: float = 2.0) -> bool:
    """
    Ask one address whether it serves this camera's path.

    Raw DESCRIBE over a socket, because it separates the two failures that look
    identical from the outside: a wrong path answers a clean 404, a wrong
    password answers 401 with a Digest challenge.
    """
    import hashlib
    import re

    u = cam.at(ip)
    try:
        s = socket.create_connection((ip, cam.port), timeout=timeout)
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
        resp = h(f"{h(f'{cam.user}:{realm}:{cam.password}')}:{nonce}:"
                 f"{h(f'DESCRIBE:{u}')}")
        auth = (f'Digest username="{cam.user}", realm="{realm}", nonce="{nonce}", '
                f'uri="{u}", response="{resp}"')
        s.sendall(f"DESCRIBE {u} RTSP/1.0\r\nCSeq: 2\r\n"
                  f"Authorization: {auth}\r\n\r\n".encode())
        return " 200 " in s.recv(4096).decode("latin1")
    except OSError:
        return False
    finally:
        s.close()


def probe(cams: list[Cam], timeout: float = 2.0) -> None:
    """
    Report which configured address actually answers for each camera.

    The MAC-derived path is the one thing about a camera that cannot be
    reconfigured from its own UI, so it is what identifies one. The addresses
    are static; if a camera answers at someone else's, hardware.yaml is stale.
    """
    ips = [c.ip for c in cams]
    for cam in cams:
        if describe(cam, cam.ip, timeout):
            print(f"  {cam.id:10} {cam.ip:14} OK   {cam.path}")
            continue
        for ip in ips:
            if ip != cam.ip and describe(cam, ip, timeout):
                print(f"  {cam.id:10} {cam.ip:14} MOVED -> {ip}   {cam.path}  "
                      f"** hardware.yaml is stale **")
                break
        else:
            print(f"  {cam.id:10} {cam.ip:14} no answer for {cam.path}")


def play(cam: Cam, w: int, h: int, x: int, y: int) -> subprocess.Popen:
    return subprocess.Popen(
        ["ffplay", *FFPLAY_FLAGS,
         "-x", str(w), "-y", str(h), "-left", str(x), "-top", str(y),
         "-window_title", f"{cam.id}  ({cam.ip})",
         cam.url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def grid(n: int) -> tuple[int, int]:
    """Columns x rows for n tiles, wide rather than tall (screens are)."""
    if n <= 1:
        return 1, 1
    if n <= 4:
        return 2, (n + 1) // 2
    return 4, (n + 3) // 4


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("camera", nargs="?", default="all",
                    help="SM-CAM-13, 13, left, right, or all")
    ap.add_argument("--probe", action="store_true",
                    help="report which address answers for each camera, and "
                         "exit without playing")
    ap.add_argument("--size", default="1600x900", metavar="WxH",
                    help="total window area to tile into")
    ap.add_argument("--list", action="store_true", help="list cameras and exit")
    args = ap.parse_args()

    cams = load_cams()
    if args.list:
        for c in cams:
            print(f"  {c.id:10} {c.ip:14} {c.path:20} {c.note}")
        return 0

    chosen = select(cams, args.camera)
    if not chosen:
        print(f"unknown camera {args.camera!r}. Known: "
              f"{', '.join(c.id for c in cams)}", file=sys.stderr)
        return 1

    if args.probe:
        print(f"probing {len(chosen)} camera(s):")
        probe(chosen)
        return 0

    if not shutil.which("ffplay"):
        print("ffplay not found. brew install ffmpeg", file=sys.stderr)
        return 1

    try:
        w, h = (int(v) for v in args.size.lower().split("x"))
    except ValueError:
        print(f"bad --size {args.size!r}, expected WxH", file=sys.stderr)
        return 1

    cols, rows = grid(len(chosen))
    tw, th = w // cols, h // rows
    procs = [play(c, tw, th, (i % cols) * (tw + 8), (i // cols) * (th + 40))
             for i, c in enumerate(chosen)]
    print(f"{len(chosen)} camera(s) at {tw}x{th}: "
          f"{', '.join(c.id for c in chosen)}. Ctrl-C to stop.")

    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
