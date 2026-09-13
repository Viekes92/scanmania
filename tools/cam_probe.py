#!/usr/bin/env python3
"""
tools/cam_probe.py — standalone RTSP viewer for the ceiling cameras.

Inputs:  RTSP URLs, either given in full or built from --hosts/--user/--password/--path
Outputs: a live 2x2 mosaic window (or --no-window stats on a headless box), plus
         per-camera connect/decode/latency numbers on stdout
Invariant: imports nothing from the game. No FSM, no SQLite, no web server, no
           config/beams.json. Running this can never affect a live run.
           One reader thread per camera, always draining; only the newest frame
           is kept, so a slow display never backs up the decode.

Usage:
    export SCANMANIA_CAM_USER=scanmania SCANMANIA_CAM_PASSWORD='...'
    python3 tools/cam_probe.py \
      'SM-CAM-1=rtsp://[User]:[Password]@172.16.0.201:8554/9c756e2e9121-0_m' \
      'SM-CAM-2=rtsp://[User]:[Password]@172.16.0.202:8554/9c756e2e9aad-0_m'

    # every camera on one path template? build the URLs instead:
    python3 tools/cam_probe.py --hosts 172.16.0.201-204 --path '/stream1'

Keys in the window: q / Esc quit, s save a PNG of each live frame, 1-4 solo a camera.
"""

from __future__ import annotations

# Must be set before cv2 imports its FFmpeg backend — OpenCV reads this env var
# once, when the capture is created, and there is no API equivalent.
#   rtsp_transport=tcp   packet loss is a torn frame over UDP; TCP is steadier
#                        on a switched LAN and costs nothing here. --udp flips it.
#   fflags=nobuffer      do not accumulate a demuxer buffer
#   flags=low_delay      decode without waiting for reordered frames
#   reorder_queue_size=0 no jitter buffer; we want the newest frame, not a smooth one
#   max_delay=0          microseconds FFmpeg may hold a packet for reordering
import os
import sys

_FFMPEG_BASE = "fflags;nobuffer|flags;low_delay|reorder_queue_size;0|max_delay;0"
_TRANSPORT = "udp" if "--udp" in sys.argv else "tcp"
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{_TRANSPORT}|{_FFMPEG_BASE}"

import argparse
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit, urlunsplit

import cv2
import numpy as np

RECONNECT_S = 2.0
STALL_MS = 300.0        # same threshold vision/camera.py calls STALLED
TILE_WIDTH = 640        # per-tile width in the mosaic


def mask_url(url: str) -> str:
    """Same URL with the password replaced, for logs and on-screen labels."""
    parts = urlsplit(url)
    if not parts.password:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{parts.username}:***@{host}"
    return urlunsplit(parts._replace(netloc=netloc))


@dataclass
class Camera:
    """One RTSP source and everything the display needs to know about it."""

    name: str
    url: str

    frame: np.ndarray | None = None      # newest decoded frame
    frame_ns: int = 0                    # monotonic_ns it was decoded at
    seq: int = 0                         # frames decoded since start
    status: str = "connecting"
    intervals: list[float] = field(default_factory=list)  # last 60 gaps, seconds
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def fps(self) -> float:
        with self.lock:
            gaps = list(self.intervals)
        if not gaps:
            return 0.0
        avg = sum(gaps) / len(gaps)
        return 1.0 / avg if avg > 0 else 0.0

    def latest(self) -> tuple[np.ndarray | None, int, int]:
        with self.lock:
            return self.frame, self.frame_ns, self.seq


def reader(cam: Camera, stop: threading.Event) -> None:
    """
    Drain one RTSP stream as fast as it delivers, keeping only the newest frame.

    This thread must never block on anything but cap.read(). The moment it does,
    frames pile up in the socket and every later frame is late by that much —
    which is the whole reason the display does not pull from the capture itself.
    """
    while not stop.is_set():
        cap = cv2.VideoCapture(cam.url, cv2.CAP_FFMPEG)
        # Honoured by some backends only, ignored silently by the rest. When it
        # does apply it is the difference between 1 and 5 frames of lag.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not cap.isOpened():
            cam.status = "no connect"
            print(f"[{cam.name}] cannot open {mask_url(cam.url)}", file=sys.stderr)
            cap.release()
            stop.wait(RECONNECT_S)
            continue

        cam.status = "live"
        print(f"[{cam.name}] connected")
        last_ns = 0

        while not stop.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                cam.status = "dropped"
                print(f"[{cam.name}] stream dropped, reconnecting", file=sys.stderr)
                break

            now = time.monotonic_ns()
            with cam.lock:
                cam.frame = frame
                cam.frame_ns = now
                cam.seq += 1
                if last_ns:
                    cam.intervals.append((now - last_ns) / 1e9)
                    if len(cam.intervals) > 60:
                        cam.intervals.pop(0)
            last_ns = now

        cap.release()
        if not stop.is_set():
            stop.wait(RECONNECT_S)


def tile(cam: Camera, w: int, h: int) -> np.ndarray:
    """Render one camera into a w*h BGR tile with its stats burned in."""
    canvas = np.zeros((h, w, 3), dtype=np.uint8)
    frame, frame_ns, _ = cam.latest()

    if frame is None:
        label = f"{cam.name}  {cam.status}"
        colour = (60, 60, 200)
    else:
        fh, fw = frame.shape[:2]
        scale = min(w / fw, h / fh)
        # INTER_NEAREST on purpose: this is a probe, and a cheaper resize keeps
        # the display loop from becoming the thing that adds latency.
        small = cv2.resize(
            frame, (int(fw * scale), int(fh * scale)), interpolation=cv2.INTER_NEAREST
        )
        y = (h - small.shape[0]) // 2
        x = (w - small.shape[1]) // 2
        canvas[y:y + small.shape[0], x:x + small.shape[1]] = small

        age_ms = (time.monotonic_ns() - frame_ns) / 1e6
        stalled = age_ms > STALL_MS
        label = f"{cam.name}  {fw}x{fh}  {cam.fps:4.1f} fps  age {age_ms:4.0f} ms"
        colour = (60, 60, 200) if stalled else (120, 230, 120)

    cv2.rectangle(canvas, (0, 0), (w - 1, h - 1), (50, 50, 50), 1)
    cv2.putText(canvas, label, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
    cv2.putText(canvas, label, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, colour, 1)
    return canvas


def mosaic(cams: list[Camera], solo: int | None) -> np.ndarray:
    """2x2 grid of all cameras, or one full-size tile when a camera is soloed."""
    if solo is not None:
        return tile(cams[solo], TILE_WIDTH * 2, int(TILE_WIDTH * 2 * 9 / 16))

    tw, th = TILE_WIDTH, int(TILE_WIDTH * 9 / 16)
    tiles = [tile(c, tw, th) for c in cams]
    while len(tiles) % 2:
        tiles.append(np.zeros((th, tw, 3), dtype=np.uint8))
    rows = [np.hstack(tiles[i:i + 2]) for i in range(0, len(tiles), 2)]
    return np.vstack(rows)


def expand_hosts(spec: str) -> list[str]:
    """'172.16.0.201-204' -> four addresses. A plain address passes through."""
    m = re.fullmatch(r"(\d+\.\d+\.\d+\.)(\d+)-(\d+)", spec.strip())
    if not m:
        return [spec.strip()]
    prefix, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
    return [f"{prefix}{n}" for n in range(lo, hi + 1)]


def fill_credentials(url: str, user: str, password: str) -> str:
    """
    Replace [User] / [Password] placeholders, percent-encoding as we go.

    The real password contains an '@', which splits an RTSP URL in the wrong
    place if it is pasted in literally — so substitution has to quote, and
    that is the whole reason these placeholders exist.
    """
    return re.sub(
        r"\[user\]", quote(user, safe=""),
        re.sub(r"\[password\]", quote(password, safe=""), url, flags=re.I),
        flags=re.I,
    )


def build_urls(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Return [(name, url)], from explicit URLs or from --hosts + credentials."""
    if args.urls:
        out = []
        for i, spec in enumerate(args.urls):
            # "SM-CAM-1=rtsp://..." keeps the operator's label on the tile.
            name, _, url = spec.partition("=") if "=" in spec.split("://")[0] else ("", "", spec)
            out.append((name or f"cam{i + 1}",
                        fill_credentials(url, args.user, args.password)))
        return out

    if not args.hosts:
        raise SystemExit("give RTSP URLs, or --hosts (e.g. --hosts 172.16.0.201-204)")

    auth = ""
    if args.user:
        auth = quote(args.user, safe="")
        if args.password:
            auth += ":" + quote(args.password, safe="")
        auth += "@"

    out = []
    for host in [h for spec in args.hosts for h in expand_hosts(spec)]:
        path = args.path if args.path.startswith("/") else "/" + args.path
        # {n} is the last octet, so one --path template can cover a whole range.
        path = path.replace("{n}", host.rsplit(".", 1)[-1])
        out.append((host, f"rtsp://{auth}{host}:{args.port}{path}"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("urls", nargs="*",
                    help="[NAME=]rtsp://... — may contain [User]/[Password]")
    ap.add_argument("--hosts", nargs="+", help="IPs or ranges, e.g. 172.16.0.201-204")
    ap.add_argument("--user", default=os.environ.get("SCANMANIA_CAM_USER", ""))
    ap.add_argument("--password", default=os.environ.get("SCANMANIA_CAM_PASSWORD", ""),
                    help="defaults to $SCANMANIA_CAM_PASSWORD, to keep it out of history")
    ap.add_argument("--port", type=int, default=8554)
    ap.add_argument("--path", default="/", help="stream path; {n} = last IP octet")
    ap.add_argument("--udp", action="store_true", help="RTSP over UDP (read above)")
    ap.add_argument("--no-window", action="store_true", help="stats only, no GUI")
    ap.add_argument("--snap", metavar="DIR",
                    help="headless: save one frame per camera to DIR, then exit")
    ap.add_argument("--fps", type=float, default=30.0, help="display refresh rate")
    args = ap.parse_args()

    cams = [Camera(name=n, url=u) for n, u in build_urls(args)]
    transport = "udp" if args.udp else "tcp"
    print(f"Opening {len(cams)} stream(s) over RTSP/{transport}:")
    for c in cams:
        print(f"  {c.name}: {mask_url(c.url)}")

    stop = threading.Event()
    threads = [
        threading.Thread(target=reader, args=(c, stop), name=c.name, daemon=True)
        for c in cams
    ]
    for t in threads:
        t.start()

    solo: int | None = None
    period = 1.0 / max(args.fps, 1.0)
    try:
        if args.snap:
            # The NUC has no display, so this is how you actually look at a
            # camera from there: grab one frame each, then copy the files off.
            os.makedirs(args.snap, exist_ok=True)
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and any(c.seq == 0 for c in cams):
                time.sleep(0.2)
            for c in cams:
                frame, _, _ = c.latest()
                if frame is None:
                    print(f"  {c.name}: NO FRAME ({c.status})")
                    continue
                path = os.path.join(args.snap, f"{c.name}.png")
                cv2.imwrite(path, frame)
                h, w = frame.shape[:2]
                print(f"  {c.name}: {w}x{h} -> {path}")
        elif args.no_window:
            while True:
                time.sleep(1.0)
                print("  ".join(
                    f"{c.name} {c.status} {c.fps:4.1f}fps n={c.seq}" for c in cams
                ))
        else:
            cv2.namedWindow("cam_probe", cv2.WINDOW_NORMAL)
            while True:
                cv2.imshow("cam_probe", mosaic(cams, solo))
                key = cv2.waitKey(max(1, int(period * 1000))) & 0xFF
                if key in (ord("q"), 27):
                    break
                if key == ord("s"):
                    stamp = time.strftime("%H%M%S")
                    for c in cams:
                        frame, _, _ = c.latest()
                        if frame is not None:
                            path = f"/tmp/cam_probe_{c.name}_{stamp}.png"
                            cv2.imwrite(path, frame)
                            print(f"saved {path}")
                if ord("1") <= key <= ord("9"):
                    idx = key - ord("1")
                    solo = None if solo == idx or idx >= len(cams) else idx
                if cv2.getWindowProperty("cam_probe", cv2.WND_PROP_VISIBLE) < 1:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if not args.no_window and not args.snap:
            cv2.destroyAllWindows()
        for t in threads:
            t.join(timeout=1.0)

    print("\nSummary:")
    for c in cams:
        print(f"  {c.name}: {c.status}, {c.seq} frames, {c.fps:.1f} fps avg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
