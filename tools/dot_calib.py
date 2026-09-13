#!/usr/bin/env python3
"""
tools/dot_calib.py — tune laser-dot detection on one camera, live, in four panels.

Inputs:  one RTSP URL (frames arrive via an ffmpeg subprocess, not cv2 — see below)
Outputs: a 2x2 window: camera feed | signal channel | intensity mask | detected ROIs.
         --save writes the detected centroids to JSON.
Invariant: sandbox only. Imports nothing from the game, writes no game config,
           drives no relay. Every stage recomputes per frame so a parameter
           change is visible immediately.

Why ffmpeg and not cv2.VideoCapture: the OpenCV wheel installed here is built
with FFMPEG=NO, so cv2 cannot open an RTSP URL at all — it fails in 0.0 s with
isOpened() False, which looks exactly like a network fault and is not.

Topology note: a segment is an array of 5 lasers on one relay channel, and there
are 45 segments, so expect ~225 dots. Five dots vanish together when a segment
switches; that is what makes an off-by-one-segment sweep able to label them.

Usage:
  python3 tools/dot_calib.py rtsp://user:pass@172.16.0.201:8554/<path>
  python3 tools/dot_calib.py <url> --channel red --save /tmp/dots.json

Keys: g/r  grayscale vs redness channel      [ ]  threshold down/up
      t    top-hat on/off    -/+  kernel     a    auto-threshold
      , .  min blob area down/up             b    cycle blur
      c    lock baselines (monitor mode)     x    clear baselines
      s    save JSON                         q    quit
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

PANEL_W = 512           # each of the four panels; window is 2x this wide
PATCH_R = 2             # half-size of the brightness patch, px (5x5 at r=2)
DROP_RATIO = 0.5        # dot counts as broken below this fraction of baseline
CONSECUTIVE = 2         # consecutive frames under the ratio before it counts
MAX_AREA = 400          # blobs larger than this are glare, not a dot
BLURS = [0, 3, 5]


def probe_size(url: str) -> tuple[int, int]:
    """Ask ffprobe for the frame size — the raw pipe carries no header."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
         "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", url],
        capture_output=True, text=True, timeout=15,
    )
    if out.returncode != 0:
        raise SystemExit(f"ffprobe failed:\n{out.stderr.strip()}")
    w, h = out.stdout.strip().split(",")[:2]
    return int(w), int(h)


class Stream:
    """ffmpeg subprocess → newest BGR frame. Always draining, never queueing."""

    def __init__(self, url: str, w: int, h: int) -> None:
        self.w, self.h = w, h
        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-rtsp_transport", "tcp", "-fflags", "nobuffer",
             "-flags", "low_delay", "-avioflags", "direct",
             "-i", url, "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        nbytes = self.w * self.h * 3
        assert self.proc.stdout is not None
        while not self._stop.is_set():
            # A short read mid-frame would shear every frame after it, so insist
            # on the full payload and treat anything less as end of stream.
            buf = self.proc.stdout.read(nbytes)
            if len(buf) < nbytes:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
            with self._lock:
                self._frame = frame

    def latest(self) -> np.ndarray | None:
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def close(self) -> None:
        self._stop.set()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def signal_of(frame: np.ndarray, channel: str, blur: int) -> np.ndarray:
    """
    Reduce a BGR frame to the single-channel image detection runs on.

    'gray' is plain brightness — what laser.py used, and what survives a dot
    core clipping to white. 'red' is R - max(G, B), which rejects white lights
    but goes weak on exactly those clipped cores. Look at both panels before
    choosing: on these cameras redness peaks around 59/255.
    """
    if channel == "red":
        b, g, r = cv2.split(frame)
        sig = cv2.subtract(r, cv2.max(g, b))
    else:
        sig = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if blur:
        sig = cv2.GaussianBlur(sig, (blur, blur), 0)
    return sig


def tophat(sig: np.ndarray, k: int) -> np.ndarray:
    """
    White top-hat: the image minus its morphological opening, i.e. everything
    smaller than the kernel that is brighter than its surroundings.

    This is what makes one threshold work across the whole frame. The dots span
    a huge brightness range — a near-saturated one at the far end, dim ones in
    the cluster — so a single global cut either misses the dim ones or blooms
    the bright ones. Top-hat subtracts each dot's *local* background, which
    flattens that range. Keep k comfortably larger than a dot and smaller than
    the spacing between them.
    """
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    return cv2.morphologyEx(sig, cv2.MORPH_TOPHAT, kernel)


def detect(sig: np.ndarray, thr: int, min_area: int
           ) -> tuple[np.ndarray, list[tuple[int, int, int]]]:
    """
    Threshold at an absolute level, then take each surviving blob as a dot.

    Absolute rather than a fraction of the peak: with top-hat applied the units
    are "brightness above local background", which is stable frame to frame,
    whereas the global peak jumps as soon as one bright dot is occluded.
    Returns (mask, [(cx, cy, r)]).
    """
    _, mask = cv2.threshold(sig, thr, 255, cv2.THRESH_BINARY)
    # Opening removes isolated hot pixels; a real dot is several px across.
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    n, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    dots = []
    for i in range(1, n):                      # label 0 is the background
        area = stats[i, cv2.CC_STAT_AREA]
        if not (min_area <= area <= MAX_AREA):
            continue
        cx, cy = centroids[i]
        dots.append((int(round(cx)), int(round(cy)),
                     max(2, int(round(np.sqrt(area / np.pi))))))
    dots.sort(key=lambda d: (d[1], d[0]))      # top-to-bottom, left-to-right
    return mask, dots


def patch_mean(sig: np.ndarray, x: int, y: int, r: int = PATCH_R) -> float:
    """Mean signal in a small box around (x, y). Small on purpose: a wide patch
    borrows light from the neighbouring dot once the grid gets dense."""
    h, w = sig.shape
    sub = sig[max(0, y - r):min(h, y + r + 1), max(0, x - r):min(w, x + r + 1)]
    return float(sub.mean()) if sub.size else 0.0


def label(img: np.ndarray, text: str, colour=(120, 230, 120)) -> np.ndarray:
    """Caption a panel, with a dark outline so it reads over any background."""
    cv2.putText(img, text, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
    cv2.putText(img, text, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1)
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("url", help="RTSP URL (percent-encode '@' in the password)")
    ap.add_argument("--channel", choices=("gray", "red"), default="gray")
    ap.add_argument("--thr", type=int, default=25, help="absolute threshold")
    ap.add_argument("--tophat", type=int, default=15,
                    help="top-hat kernel px; 0 disables it")
    ap.add_argument("--min-area", type=int, default=2)
    ap.add_argument("--drop", type=float, default=DROP_RATIO)
    ap.add_argument("--save", metavar="FILE", help="write detected dots as JSON")
    ap.add_argument("--label", default="dot_calib",
                    help="window title; use a distinct one per camera when "
                         "running several side by side")
    ap.add_argument("--pos", metavar="X,Y", default=None,
                    help="window position, e.g. 0,0 — for tiling several")
    args = ap.parse_args()

    w, h = probe_size(args.url)
    ph = int(PANEL_W * h / w)
    print(f"Stream {w}x{h}. Light the whole maze, tune until the dot count is "
          f"stable, then press 'c'.")

    stream = Stream(args.url, w, h)
    channel, thr, min_area, blur_i = args.channel, args.thr, args.min_area, 1
    th_k = args.tophat
    baselines: dict[tuple[int, int], float] = {}
    misses: dict[tuple[int, int], int] = {}

    win = args.label
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    if args.pos:
        x, y = (int(v) for v in args.pos.split(","))
        cv2.moveWindow(win, x, y)
    cv2.resizeWindow(win, PANEL_W * 2, ph * 2)

    try:
        while True:
            frame = stream.latest()
            if frame is None:
                if stream.proc.poll() is not None:
                    print("ffmpeg exited — stream lost.", file=sys.stderr)
                    return 1
                time.sleep(0.02)
                continue

            sig = signal_of(frame, channel, BLURS[blur_i])
            if th_k:
                sig = tophat(sig, th_k)
            mask, dots = detect(sig, thr, min_area)

            # Panel 4: ROIs over the live frame, green normally, red when a
            # locked-in baseline says the dot went dark.
            roi_panel = frame.copy()
            broken = 0
            for (x, y, r) in dots:
                colour = (0, 255, 0)
                key = (x, y)
                if key in baselines:
                    level = patch_mean(sig, x, y)
                    ratio = level / baselines[key] if baselines[key] > 0 else 1.0
                    misses[key] = misses[key] + 1 if ratio < args.drop else 0
                    if misses[key] >= CONSECUTIVE:
                        colour = (0, 0, 255)
                        broken += 1
                cv2.circle(roi_panel, (x, y), max(4, r + 3), colour, 1)

            sig_bgr = cv2.applyColorMap(sig, cv2.COLORMAP_INFERNO)
            mask_bgr = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)

            def panel(img, text, colour=(120, 230, 120)):
                return label(cv2.resize(img, (PANEL_W, ph)), text, colour)

            peak = int(sig.max())
            th_txt = f"tophat={th_k}" if th_k else "raw"
            top = np.hstack([
                panel(frame, "1 camera feed"),
                panel(sig_bgr, f"2 {channel} {th_txt}  peak={peak}"),
            ])
            bottom = np.hstack([
                panel(mask_bgr, f"3 mask  thr={thr}  area>={min_area}"),
                panel(roi_panel,
                      f"4 ROIs  dots={len(dots)}  locked={len(baselines)}  broken={broken}",
                      (0, 0, 255) if broken else (120, 230, 120)),
            ])
            cv2.imshow(win, np.vstack([top, bottom]))

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            elif key == ord("g"):
                channel = "gray"
            elif key == ord("r"):
                channel = "red"
            elif key == ord("["):
                thr = max(1, thr - 2)
            elif key == ord("]"):
                thr = min(254, thr + 2)
            elif key == ord("t"):
                th_k = 0 if th_k else args.tophat or 15
            elif key == ord("-"):
                th_k = max(3, th_k - 2) if th_k else 0
            elif key in (ord("="), ord("+")):
                th_k = th_k + 2 if th_k else 5
            elif key == ord("a"):
                # Auto-threshold: the dots are a tiny fraction of the frame, so
                # the 99.9th percentile sits in the gap between noise and dot.
                thr = max(4, int(np.percentile(sig, 99.9)))
                print(f"auto thr={thr}")
            elif key == ord(","):
                min_area = max(1, min_area - 1)
            elif key == ord("."):
                min_area += 1
            elif key == ord("b"):
                blur_i = (blur_i + 1) % len(BLURS)
            elif key == ord("x"):
                baselines.clear(); misses.clear()
                print("Baselines cleared.")
            elif key == ord("c"):
                baselines = {(x, y): patch_mean(sig, x, y) for x, y, _ in dots}
                misses = {k: 0 for k in baselines}
                vals = sorted(baselines.values())
                print(f"Locked {len(baselines)} dots — baseline {channel} "
                      f"min={vals[0]:.0f} median={vals[len(vals)//2]:.0f} max={vals[-1]:.0f}"
                      if vals else "No dots to lock.")
            elif key == ord("s") and args.save:
                payload = [{"n": i + 1, "roi": {"cx": x, "cy": y, "r": r}}
                           for i, (x, y, r) in enumerate(dots)]
                with open(args.save, "w") as fh:
                    json.dump({"width": w, "height": h, "channel": channel,
                               "thr": thr, "tophat": th_k, "min_area": min_area,
                               "dots": payload}, fh, indent=1)
                print(f"Wrote {len(payload)} dots to {args.save}")

            if cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:
                break
    except KeyboardInterrupt:
        pass
    finally:
        stream.close()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
