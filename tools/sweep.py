#!/usr/bin/env python3
"""
tools/sweep.py — calibrate beams.json by switching one channel at a time.

Inputs:  live relay boards + the ceiling cameras, config/hardware.yaml, beams.json
Outputs: a calibrated config/beams.json (dots, per-dot camera, baseline, dark_floor)
         written atomically after validation, with a timestamped backup
Invariant: drives relays only through PresetResolver (invariant 4), never raw
           write_coils. Never overwrites a good beams.json with a failed run.

Stop the game service first — kiosk too, or its Wants= drags the game back up
and ReconcileLoop re-lights channels mid-step, corrupting the labelling with no
visible symptom:

    systemctl stop scanmania-kiosk scanmania
    .venv/bin/python tools/sweep.py         # then open :8090 and press START
    systemctl start scanmania scanmania-kiosk

The tool does NOT sweep on launch. It connects the cameras and boards, then
waits: you pick channels and mazes on the page and press START. That means you
can have it running, walk to the container, kill the house lights, and start the
run from a phone. --now restores the old sweep-immediately-and-exit behaviour
for scripting.

Two passes, because they answer different questions.

  LABEL pass (one channel lit, rest dark)
      Which dots belong to which channel. Five bright spots on a near-black
      frame — unambiguous, no diffing, no bloom from neighbours.

  MEASURE pass (all lit, one channel blinked off)
      Whether a break is actually detectable. The game never sees "dot appears
      in darkness"; it sees "dot vanishes while ~180 others stay lit". This
      measures the lit baseline and the dark floor per dot, in game conditions.
      A dot whose disappearance is masked by a neighbour's bloom passes the
      label pass and is a dead sensor in the maze. Only this pass catches it.

Captures are synchronised by OBSERVING the expected change, never by sleeping.
RTSP frames arrive 100-300 ms old, so switch-then-grab samples the pre-switch
world.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config.loader as loader
from iobackend.modbus import ModbusIOBackend
from iobackend.presets import PresetResolver
from vision.camera import CameraStream
from vision.detect import sample_circle

log = logging.getLogger("sweep")


class AmbientTooBright(RuntimeError):
    """Raised when the room is lit and the cameras cannot see laser dots."""


class SweepAborted(RuntimeError):
    """Raised when the operator stops a run from the web panel."""

# Detection parameters for FINDING dots. Deliberately not the game's sampler:
# that measures a known ROI, this locates unknown ones. The recipe is
# tools/dot_calib.py's, which was tuned against the real ceiling — a white
# top-hat to subtract local background, then a low ABSOLUTE threshold. A
# threshold set as a fraction of the global peak only ever finds the brightest
# few, because dots span a huge brightness range.
_TOPHAT_K = 15
_THRESHOLD = 25
_MIN_AREA = 2
_MAX_AREA = 400

_MEDIAN_FRAMES = 7          # frames to median per capture; kills sensor noise
_CHANGE_TIMEOUT_S = 4.0     # give up waiting for a switch to appear on camera
_SETTLE_AFTER_CHANGE_S = 0.15
_DOT_RADIUS_FALLBACK = 9



# ---------------------------------------------------------------------------
# Live UI — a page of its own on :8090
#
# The sweep needs the game service stopped (ReconcileLoop would re-light
# channels mid-step), so /admin is down while this runs. Hence a self-contained
# server with no dependency on the game: open it in a tab beside the admin
# panel. Serves its own state as JSON and the last capture per camera as JPEG
# with the detected dots drawn on, so you can see what it is actually seeing.
# ---------------------------------------------------------------------------

class _UI:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict = {
            "phase": "starting", "maze": None, "channel": None,
            "done": 0, "total": 0, "ambient": {}, "channels": {},
            "maze_counts": {}, "warnings": [], "errors": [], "finished": False,
        }
        self._frames: dict[str, bytes] = {}
        self._httpd = None
        # Set by the browser, consumed by the run loop. The HTTP handler runs on
        # its own thread, so everything crossing that boundary takes the lock.
        self._pending_start: dict | None = None
        self._abort = False
        self._busy = False

    def set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    # -- control, driven from the page --------------------------------------

    def request_start(self, params: dict) -> bool:
        """Queue a run. False if one is already going."""
        with self._lock:
            if self._busy:
                return False
            self._pending_start = params
            self._abort = False
            return True

    def take_start(self) -> dict | None:
        with self._lock:
            p, self._pending_start = self._pending_start, None
            if p is not None:
                self._busy = True
            return p

    def finished(self) -> None:
        with self._lock:
            self._busy = False
            self._abort = False

    def request_abort(self) -> None:
        with self._lock:
            self._abort = True

    @property
    def aborting(self) -> bool:
        with self._lock:
            return self._abort

    def check_abort(self) -> None:
        """Raise inside the sweep so it unwinds through its own finally."""
        if self.aborting:
            raise SweepAborted("aborted from the web panel")

    def channel_done(self, cid: str, dots: list, maze: str | None = None) -> None:
        with self._lock:
            entry = self._state["channels"].setdefault(cid, {})
            entry["dots"] = len(dots)
            entry["cameras"] = sorted({d["camera"] for d in dots})
            if maze:
                lit = [d["baselines"].get(maze, 0) for d in dots]
                floors = [d["dark_floors"].get(maze, 0) for d in dots]
                entry.setdefault("mazes", {})[maze] = {
                    "min_baseline": round(min(lit), 1) if lit else 0,
                    "max_floor": round(max(floors), 1) if floors else 0,
                }
            # Deliberately does NOT touch "done": run_sweep owns the counter
            # and sets it per channel. Incrementing here as well double-counted.

    def publish_frames(self, frames: dict, annotate: bool = True) -> None:
        """Store a JPEG per camera, dots circled, for the preview panes."""
        out = {}
        for cid, f in frames.items():
            img = f.copy()
            if annotate:
                for (cx, cy, r) in find_dots(f):
                    cv2.circle(img, (cx, cy), max(r + 4, 8), (0, 255, 0), 2)
            small = cv2.resize(img, (640, int(640 * img.shape[0] / img.shape[1])))
            ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                out[cid] = buf.tobytes()
        with self._lock:
            self._frames = out

    def snapshot(self) -> dict:
        with self._lock:
            st = json.loads(json.dumps(self._state))
            st["busy"] = self._busy
            st["aborting"] = self._abort
            return st

    def frame(self, cid: str) -> bytes | None:
        with self._lock:
            return self._frames.get(cid)

    def serve(self, port: int) -> None:
        ui = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):      # keep the sweep's own log readable
                pass

            def do_GET(self):
                if self.path.startswith("/status"):
                    body = json.dumps(ui.snapshot()).encode()
                    self._send(200, "application/json", body)
                elif self.path.startswith("/frame/"):
                    cid = self.path.split("/frame/")[1].split("?")[0].removesuffix(".jpg")
                    buf = ui.frame(cid)
                    if buf is None:
                        self._send(404, "text/plain", b"no frame yet")
                    else:
                        self._send(200, "image/jpeg", buf)
                else:
                    self._send(200, "text/html; charset=utf-8", _PAGE.encode())

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    body = {}
                if self.path.startswith("/start"):
                    ok = ui.request_start({
                        "channels": (body.get("channels") or "").strip(),
                        "mazes": body.get("mazes") or "maze_1",
                        "no_write": bool(body.get("no_write", True)),
                    })
                    self._send(200, "application/json",
                               json.dumps({"ok": ok}).encode())
                elif self.path.startswith("/abort"):
                    ui.request_abort()
                    self._send(200, "application/json", b'{"ok":true}')
                else:
                    self._send(404, "text/plain", b"no")

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        log.info("calibration UI on http://localhost:%d", port)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()


UI = _UI()

_PAGE = """<!doctype html><meta charset=utf-8><title>ScanMania calibration</title>
<style>
 :root{--bg:#050507;--surface:#080d18;--border:#0d1e3a;--text:#e8f0fc;
       --accent:#5b8fd6;--muted:#6c8cae;--ok:#00ff88;--warn:#ff8800;--err:#ff0033}
 *{box-sizing:border-box;margin:0;padding:0}
 body{background:var(--bg);color:var(--text);font:14px 'Courier New',monospace;padding:18px}
 h1{font-size:18px;letter-spacing:3px;color:var(--accent);margin-bottom:14px}
 .row{display:flex;gap:16px;flex-wrap:wrap}
 .card{background:var(--surface);border:1px solid var(--border);border-radius:8px;
       padding:14px;flex:1 1 300px;min-width:280px}
 .t{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--accent);
    margin-bottom:10px}
 .big{font-size:26px;font-weight:700}
 .bar{height:8px;background:#0d1525;border-radius:4px;overflow:hidden;margin-top:8px}
 .bar>div{height:100%;background:var(--accent);transition:width .3s}
 table{width:100%;border-collapse:collapse;font-size:12px}
 td,th{padding:3px 6px;text-align:left;border-bottom:1px solid var(--border)}
 th{color:var(--accent);font-size:10px;letter-spacing:1px}
 .ok{color:var(--ok)}.warn{color:var(--warn)}.err{color:var(--err)}.muted{color:var(--muted)}
 img{width:100%;border:1px solid var(--border);border-radius:4px;display:block}
 .cams{display:grid;grid-template-columns:1fr 1fr;gap:10px}
 li{margin:3px 0 3px 16px}
</style>
<h1>SCANMANIA // CALIBRATION</h1>
<div class=card style=margin-bottom:16px>
  <div class=t>Run</div>
  <div style="display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap">
    <label>channels <span class=muted>(blank = all)</span><br>
      <input id=chans placeholder="11,12,13" style="background:#0d1525;border:1px solid
        var(--border);color:var(--text);padding:7px;border-radius:4px;font-family:inherit"></label>
    <div>mazes<br>
      <label><input type=checkbox class=mz value=maze_1 checked> maze_1</label>
      <label><input type=checkbox class=mz value=maze_2 checked> maze_2</label>
      <label><input type=checkbox class=mz value=maze_3 checked> maze_3</label></div>
    <label><input type=checkbox id=nowrite checked> don't write beams.json</label>
    <button id=go style="background:var(--accent);color:#000;border:0;padding:11px 26px;
      border-radius:6px;font:700 14px 'Courier New',monospace;letter-spacing:2px;
      cursor:pointer">START</button>
    <button id=stop style="background:transparent;color:var(--err);border:1px solid
      var(--err);padding:11px 20px;border-radius:6px;font:700 13px 'Courier New',monospace;
      letter-spacing:2px;cursor:pointer;display:none">ABORT</button>
  </div>
  <div class=warn style=margin-top:10px>Lasers switch on. House lights off, container empty.</div>
</div>
<div class=row>
  <div class=card>
    <div class=t>Progress</div>
    <div class=big id=phase>—</div>
    <div class=muted id=chan></div>
    <div class=bar><div id=bar style=width:0%></div></div>
    <div class=muted id=count style=margin-top:6px></div>
  </div>
  <div class=card>
    <div class=t>Ambient (lasers off)</div>
    <div id=amb class=muted>—</div>
    <div class=muted style=margin-top:8px>More than a handful means the house
      lights are on and the cameras cannot see dots.</div>
  </div>
  <div class=card>
    <div class=t>Dots per maze</div>
    <table id=mz><tr><td class=muted>not measured yet</td></tr></table>
  </div>
</div>
<div class=row style=margin-top:16px>
  <div class=card style=flex:2>
    <div class=t>Cameras — green circles are detected dots</div>
    <div class=cams id=cams></div>
  </div>
  <div class=card>
    <div class=t>Channels</div>
    <div style=max-height:360px;overflow:auto>
      <table id=ch><tr><td class=muted>waiting</td></tr></table>
    </div>
  </div>
</div>
<div class=card id=issuesCard style=margin-top:16px;display:none>
  <div class=t>Issues</div><ul id=issues></ul>
</div>
<script>
const cams=['cam_1','cam_2','cam_3','cam_4'];
document.getElementById('cams').innerHTML=cams.map(c=>
  `<div><div class=muted>${c}</div><img id=img_${c} src="/frame/${c}.jpg"></div>`).join('');
setInterval(()=>cams.forEach(c=>{
  const i=document.getElementById('img_'+c); if(i) i.src='/frame/'+c+'.jpg?t='+Date.now();
}),1200);
async function tick(){
  let s; try{ s=await (await fetch('/status')).json(); }catch(e){ return; }
  phase.textContent=s.aborting?'ABORTING':(s.busy?s.phase:(s.finished?'DONE':'IDLE'));
  go.style.display=s.busy?'none':'';
  stop.style.display=s.busy?'':'none';
  chan.textContent=s.channel?('channel '+s.channel):'';
  const pct=s.total?Math.round(100*s.done/s.total):0;
  bar.style.width=pct+'%';
  count.textContent=s.total?`${s.done} / ${s.total}`:'';
  amb.innerHTML=Object.keys(s.ambient||{}).length
    ? Object.entries(s.ambient).map(([c,n])=>
        `${c}: <span class="${n>15?'err':'ok'}">${n}</span>`).join(' &nbsp; ')
    : '—';
  const mc=s.maze_counts||{};
  mz.innerHTML=Object.keys(mc).length
    ? '<tr><th>maze</th><th>expected</th><th>found</th></tr>'+
      Object.entries(mc).map(([m,v])=>{
        const tot=Object.values(v.found).reduce((a,b)=>a+b,0);
        const cls=tot>=v.expected*0.85?'ok':'warn';
        return `<tr><td>${m}</td><td>${v.expected}</td>
                <td class=${cls}>${tot}</td></tr>`;}).join('')
    : '<tr><td class=muted>not measured yet</td></tr>';
  const ch=s.channels||{};
  const keys=Object.keys(ch).sort();
  ch_.innerHTML=keys.length
    ? '<tr><th>ch</th><th>dots</th><th>cameras</th></tr>'+keys.map(k=>{
        const e=ch[k];const cls=e.dots>=5?'ok':(e.dots>=4?'':'warn');
        return `<tr><td>${k}</td><td class=${cls}>${e.dots}</td>
                <td class=muted>${(e.cameras||[]).join(' ')}</td></tr>`;}).join('')
    : '<tr><td class=muted>waiting</td></tr>';
  const all=[...(s.errors||[]).map(t=>['err',t]),...(s.warnings||[]).map(t=>['warn',t])];
  issuesCard.style.display=all.length?'block':'none';
  issues.innerHTML=all.map(([c,t])=>`<li class=${c}>${t}</li>`).join('');
}
const ch_=document.getElementById('ch');
go.onclick=async()=>{
  const mazes=[...document.querySelectorAll('.mz:checked')].map(c=>c.value).join(',');
  if(!mazes){alert('pick at least one maze');return;}
  go.disabled=true;
  const r=await (await fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({channels:chans.value,mazes:mazes,no_write:nowrite.checked})})).json();
  if(!r.ok) alert('a run is already going');
  setTimeout(()=>go.disabled=false,1500);
};
stop.onclick=()=>fetch('/abort',{method:'POST'});
setInterval(tick,600); tick();
</script>
"""


# ---------------------------------------------------------------------------
# Dot finding
# ---------------------------------------------------------------------------

def find_dots(frame: np.ndarray) -> list[tuple[int, int, int]]:
    """Return [(cx, cy, r)] for every dot-like blob. See the recipe note above."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_TOPHAT_K, _TOPHAT_K))
    sig = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, k)
    _, mask = cv2.threshold(sig, _THRESHOLD, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    n, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        if not (_MIN_AREA <= area <= _MAX_AREA):
            continue
        cx, cy = centroids[i]
        r = max(3, int(round((area / np.pi) ** 0.5)) + 2)
        out.append((int(round(cx)), int(round(cy)), r))
    return out


def match_dot(dot, candidates, tol: int = 12):
    """Find the candidate nearest `dot` within tol pixels, or None."""
    cx, cy, _ = dot
    best, best_d = None, tol + 1
    for c in candidates:
        d = ((c[0] - cx) ** 2 + (c[1] - cy) ** 2) ** 0.5
        if d < best_d:
            best, best_d = c, d
    return best


def colinearity_residual(dots: list[tuple[int, int, int]]) -> float:
    """
    Max perpendicular distance from the best-fit line, in pixels.

    Reported, never enforced. The arrays are physically colinear and a pinhole
    projection preserves straight lines, but these are wide-angle lenses looking
    at a ceiling, so barrel distortion bends them — worst at the frame edges,
    which is exactly where the far dots land. A large residual still flags a
    ghost or a mis-assignment; it just cannot be a gate.
    """
    if len(dots) < 3:
        return 0.0
    pts = np.array([[d[0], d[1]] for d in dots], dtype=np.float64)
    centred = pts - pts.mean(axis=0)
    _, _, vt = np.linalg.svd(centred)
    normal = vt[1]
    return float(np.max(np.abs(centred @ normal)))


# ---------------------------------------------------------------------------
# Camera capture
# ---------------------------------------------------------------------------

class Cameras:
    """Keeps the newest frame from every camera, so any step can grab one."""

    def __init__(self, cfg) -> None:
        self._latest: dict[str, np.ndarray] = {}
        self._streams = {
            c.id: CameraStream(camera_config=c,
                               on_frame=self._on_frame,
                               on_stall=lambda cid: None,
                               on_drift=lambda cid, px: None)
            for c in cfg.hardware.cameras
        }
        self._tasks: list[asyncio.Task] = []

    def _on_frame(self, frame, ts_ns, camera_id) -> None:
        self._latest[camera_id] = frame

    @property
    def ids(self) -> list[str]:
        return list(self._streams)

    async def start(self, timeout_s: float = 20.0) -> None:
        self._tasks = [asyncio.create_task(s.run(), name=f"cam_{cid}")
                       for cid, s in self._streams.items()]
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if len(self._latest) == len(self._streams):
                log.info("all %d cameras delivering frames", len(self._streams))
                return
            await asyncio.sleep(0.2)
        missing = set(self._streams) - set(self._latest)
        raise RuntimeError(f"cameras never delivered a frame: {sorted(missing)}")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def median_capture(self, n: int = _MEDIAN_FRAMES) -> dict[str, np.ndarray]:
        """
        Median of n frames per camera.

        A single frame carries sensor noise and haze shimmer; the median of
        several removes both without smearing a moving edge (nothing moves here).
        """
        stacks: dict[str, list[np.ndarray]] = {cid: [] for cid in self._streams}
        for _ in range(n):
            for cid in self._streams:
                f = self._latest.get(cid)
                if f is not None:
                    stacks[cid].append(f.copy())
            await asyncio.sleep(0.05)
        # Median off the event loop. Seven 1920x1080 frames is ~43 MB per
        # camera; doing it inline blocked the loop for ~700 ms and starved the
        # RTSP readers, which then reported themselves stalled.
        loop = asyncio.get_running_loop()
        frames = await loop.run_in_executor(None, self._median, stacks)
        UI.publish_frames(frames)
        return frames

    @staticmethod
    def _median(stacks: dict[str, list[np.ndarray]]) -> dict[str, np.ndarray]:
        return {cid: np.median(np.stack(v), axis=0).astype(np.uint8)
                for cid, v in stacks.items() if v}

    async def wait_for_change(self, before: dict[str, np.ndarray],
                              timeout_s: float = _CHANGE_TIMEOUT_S) -> bool:
        """
        Block until some camera's view actually changes.

        This is the sync primitive. Sleeping a fixed time after a relay write
        samples the pre-switch world, because RTSP frames are 100-300 ms old.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            for cid, ref in before.items():
                now = self._latest.get(cid)
                if now is None or now.shape != ref.shape:
                    continue
                diff = cv2.absdiff(cv2.cvtColor(now, cv2.COLOR_BGR2GRAY),
                                   cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY))
                if int(np.count_nonzero(diff > 40)) > 20:
                    await asyncio.sleep(_SETTLE_AFTER_CHANGE_S)
                    return True
        return False


# ---------------------------------------------------------------------------
# Relay control
# ---------------------------------------------------------------------------

def global_channel(beam, board_order: list[str]) -> int | None:
    """beams.json stores channels local to a board; mazes.yaml uses global."""
    return loader._global_channel(beam, board_order)


async def light_only(resolver, io, channels: list[int]) -> None:
    """Light exactly these global channels. Goes through the resolver (invariant 4)."""
    if channels:
        await resolver.apply_channels(channels, io)
    else:
        await resolver.apply_all_off(io)


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

# With every channel off, a correctly exposed ceiling camera sees near-nothing.
# More blobs than this means something other than lasers is lighting the scene.
_MAX_AMBIENT_BLOBS = 15


async def check_ambient(cams: Cameras, resolver, io) -> dict[str, int]:
    """
    Blobs visible with every laser off.

    House lights blind this completely. The cameras are exposed for bright dots
    on a dark ceiling, so with the room lit the top-hat picks up ceiling texture
    and light fittings, the LABEL pass records those as dots, and the MEASURE
    pass then reads 0 for every one of them — because ceiling texture is not
    red. The result is a calibration that looks populated and detects nothing.

    Cheaper to refuse than to debug later.
    """
    await light_only(resolver, io, [])
    await asyncio.sleep(0.6)
    frames = await cams.median_capture()
    return {cid: len(find_dots(f)) for cid, f in frames.items()}


async def run_sweep(cfg, cams: Cameras, resolver, io, channels: list, args) -> dict:
    board_order = [b.id for b in cfg.hardware.relay_boards]
    gmap = {b.id: global_channel(b, board_order) for b in channels}
    mazes = [m for m in args.mazes.split(",") if m in cfg.watchlists]
    results: dict[str, dict] = {}

    # ---- ambient check ----------------------------------------------------
    ambient = await check_ambient(cams, resolver, io)
    UI.set(ambient=ambient)
    log.info("ambient blobs with all lasers off: %s", ambient)
    hot = {cid: n for cid, n in ambient.items() if n > _MAX_AMBIENT_BLOBS}
    if hot and not args.ignore_ambient:
        raise AmbientTooBright(
            f"{len(hot)} camera(s) see light with every laser off: "
            f"{', '.join(f'{c}={n} blobs' for c, n in sorted(hot.items()))}. "
            f"Turn the house lights off — the cameras are exposed for dots on a "
            f"dark ceiling and cannot see them otherwise. "
            f"Override with --ignore-ambient if you know better."
        )

    # ---- LABEL pass -------------------------------------------------------
    # Maze-independent: a channel's dots are wherever they are, whichever shape
    # happens to be lit around them. So this runs once, per channel.
    log.info("LABEL pass: %d channels, one at a time", len(channels))
    UI.set(phase="label", total=len(channels), done=0)
    dark = await cams.median_capture()
    dark_dots = {cid: find_dots(f) for cid, f in dark.items()}

    for i, beam in enumerate(channels, 1):
        UI.check_abort()
        g = gmap[beam.id]
        if g is None:
            log.error("channel %s: board '%s' not in hardware.yaml — skipped",
                      beam.id, beam.board_id)
            continue
        UI.set(channel=beam.id, done=i - 1)
        before = await cams.median_capture(3)
        await light_only(resolver, io, [g])
        if not await cams.wait_for_change(before):
            log.warning("channel %s: no visible change after switching on", beam.id)
        lit = await cams.median_capture()

        found: list[dict] = []
        for cid, frame in lit.items():
            for (cx, cy, r) in find_dots(frame):
                if match_dot((cx, cy, r), dark_dots.get(cid, [])):
                    continue          # present with the lasers off — ambient
                found.append({"cx": cx, "cy": cy, "r": r, "camera": cid,
                              "baselines": {}, "dark_floors": {}})
        results[beam.id] = {"dots": found}
        UI.channel_done(beam.id, found)
        log.info("  [%2d/%d] %s -> %d dot(s) %s", i, len(channels), beam.id,
                 len(found), sorted({d["camera"] for d in found}))
        await light_only(resolver, io, [])

    # ---- MEASURE pass, once per maze --------------------------------------
    # Maze-DEPENDENT. A dot's reading depends on which neighbours are lit, and
    # the shapes light 46-53% of the floor each, so the same dot reads
    # differently in each. One baseline cannot serve all three.
    for maze in mazes:
        UI.check_abort()
        lit_ids = cfg.watchlists[maze]
        lit_globals = [gmap[b] for b in lit_ids if gmap.get(b) is not None]
        log.info("MEASURE pass '%s': %d channels lit (%d dots expected)",
                 maze, len(lit_ids), len(lit_ids) * 5)
        UI.set(phase=f"measure {maze}", maze=maze,
               total=len(lit_ids), done=0)

        await light_only(resolver, io, lit_globals)
        await asyncio.sleep(1.0)
        frames = await cams.median_capture()
        results.setdefault("_maze_counts", {})[maze] = {
            "expected": len(lit_ids) * 5,
            "found": {cid: len(find_dots(f)) for cid, f in frames.items()},
        }
        UI.set(maze_counts=results["_maze_counts"])

        for j, bid in enumerate(sorted(lit_ids), 1):
            UI.check_abort()
            rec = results.get(bid)
            if not rec or not rec["dots"]:
                continue
            UI.set(channel=bid, done=j - 1)
            for d in rec["dots"]:
                f = frames.get(d["camera"])
                d["baselines"][maze] = round(
                    sample_circle(f, d["cx"], d["cy"], d["r"]), 2) if f is not None else 0.0

            rest = [c for c in lit_globals if c != gmap[bid]]
            before = await cams.median_capture(3)
            await light_only(resolver, io, rest)
            await cams.wait_for_change(before)
            off = await cams.median_capture()
            for d in rec["dots"]:
                f = off.get(d["camera"])
                d["dark_floors"][maze] = round(
                    sample_circle(f, d["cx"], d["cy"], d["r"]), 2) if f is not None else 0.0
            await light_only(resolver, io, lit_globals)
            UI.channel_done(bid, rec["dots"], maze=maze)

        if args.write_refs:
            ref_dir = Path(__file__).resolve().parent.parent / "ref"
            ref_dir.mkdir(exist_ok=True)
            for cid, f in frames.items():
                cv2.imwrite(str(ref_dir / f"{cid}_{maze}.png"), f)

    await light_only(resolver, io, [])
    results["_mazes"] = mazes
    return results


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(cfg, results: dict, channels: list) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors block the write; warnings do not."""
    errors: list[str] = []
    warnings: list[str] = []
    break_ratio = channels[0].break_ratio if channels else 0.4
    mazes = results.get("_mazes", [])

    # Per maze, not all_on. "~225 at all_on" catches only gross failure and is
    # fooled by dots double-counted across two cameras. A maze has a known
    # expected count and is the condition the game actually runs in.
    for maze, v in (results.get("_maze_counts") or {}).items():
        found = sum(v["found"].values())
        pct = 100 * found / v["expected"] if v["expected"] else 0
        log.info("%s: %d dots found, %d expected (%.0f%%) %s",
                 maze, found, v["expected"], pct, v["found"])
        if found == 0:
            errors.append(f"{maze}: no dots detected at all — lasers off, "
                          f"cameras blind, or the threshold is wrong")
        elif pct < 70:
            warnings.append(f"{maze}: only {found} of {v['expected']} dots found "
                            f"({pct:.0f}%) — coverage gap or threshold too high")

    short, empty = [], []
    for beam in channels:
        rec = results.get(beam.id)
        dots = (rec or {}).get("dots", [])
        if not dots:
            empty.append(beam.id)
            continue
        if len(dots) < 4:
            short.append(f"{beam.id}({len(dots)})")

        res = colinearity_residual([(d["cx"], d["cy"], d["r"]) for d in dots])
        if res > 25:
            warnings.append(f"{beam.id}: dots {res:.0f}px off a straight line — "
                            f"possible ghost or mis-assignment (lens distortion "
                            f"alone should not do this)")

        # Per maze: a dot can be perfectly detectable in one shape and blind in
        # another, because its neighbours differ.
        for maze in mazes:
            lit = [d for d in dots if maze in d.get("baselines", {})]
            if not lit:
                continue
            no_base = [d for d in lit if d["baselines"][maze] <= 0]
            blind = [d for d in lit
                     if d["baselines"][maze] > 0
                     and d.get("dark_floors", {}).get(maze, 0) / d["baselines"][maze]
                     >= break_ratio]
            if no_base:
                warnings.append(
                    f"{beam.id}/{maze}: {len(no_base)}/{len(lit)} dot(s) read 0 "
                    f"while that maze was lit — found in the label pass but not "
                    f"lit in the measure pass")
            if blind:
                w = max(blind, key=lambda d: d["dark_floors"][maze] / d["baselines"][maze])
                warnings.append(
                    f"{beam.id}/{maze}: {len(blind)}/{len(lit)} dot(s) can never "
                    f"fire — worst floor {w['dark_floors'][maze]:.0f} / baseline "
                    f"{w['baselines'][maze]:.0f} = "
                    f"{w['dark_floors'][maze] / w['baselines'][maze]:.2f}, at or "
                    f"above break_ratio {break_ratio}. A neighbour blooms into it")

    if empty:
        errors.append(f"{len(empty)} channel(s) produced no dots: {', '.join(empty)}")
    if short:
        warnings.append(f"{len(short)} channel(s) under 4 dots (break/fault "
                        f"discrimination degraded): {', '.join(short)}")
    if len(empty) >= len(channels):
        errors.append("every channel came back empty — check relays and wiring")
    return errors, warnings


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write_beams(path: Path, results: dict, channels: list, cams: Cameras,
                sizes: dict[str, tuple[int, int]]) -> Path:
    """
    Merge results into beams.json atomically, keeping a timestamped backup.

    A half-written beams.json makes config/loader.py raise and the service
    refuses to boot — by design. So: write a sibling, then rename.
    """
    d = json.loads(path.read_text())
    by_id = {b["id"]: b for b in d["beams"]}
    for beam in channels:
        rec = results.get(beam.id)
        if not rec:
            continue
        entry = by_id[beam.id]
        dots = rec["dots"]
        entry["dots"] = dots
        entry.pop("roi", None)
        if dots:
            # Flat fallback = the brightest maze this dot is lit in, so a config
            # read without a preset still has something sane.
            for dd in dots:
                bl = [v for v in dd.get("baselines", {}).values() if v > 0]
                dd["baseline"] = round(max(bl), 2) if bl else 0.0
                fl = [dd.get("dark_floors", {}).get(m, 0.0)
                      for m in dd.get("baselines", {})]
                dd["dark_floor"] = round(max(fl), 2) if fl else 0.0
            # Channel-level camera = wherever most of its dots landed; per-dot
            # camera is what detection actually routes on.
            owner = max({dd["camera"] for dd in dots},
                        key=lambda c: sum(1 for dd in dots if dd["camera"] == c))
            entry["camera"] = owner
            w, h = sizes.get(owner, (0, 0))
            entry["capture_w"], entry["capture_h"] = w, h

    d["_calibration"] = (
        f"Swept {datetime.now(timezone.utc).isoformat()} by tools/sweep.py. "
        f"dots[] carry per-dot camera, baseline (lit, all_on) and dark_floor "
        f"(own channel off, rest lit). capture_w/h record the frame size the "
        f"pixels were measured at — a substream resolution change invalidates them."
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_suffix(f".json.{stamp}.bak")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(".json.new")
    tmp.write_text(json.dumps(d, indent=2) + "\n")
    tmp.replace(path)
    return backup


# ---------------------------------------------------------------------------

def _select_channels(cfg, args, channels_csv: str, mazes_csv: str) -> list:
    """Channels for one run: an explicit list, else everything those mazes light."""
    if channels_csv:
        want = {c.strip() for c in channels_csv.split(",") if c.strip()}
        return [b for b in cfg.beams.beams if b.id in want]
    used: set[str] = set()
    for m in mazes_csv.split(","):
        used |= set(cfg.watchlists.get(m, ()))
    if args.all_channels:
        return list(cfg.beams.beams)
    return [b for b in cfg.beams.beams if b.id in used]


async def one_run(cfg, cams, resolver, io, args, params: dict) -> None:
    """A single sweep, driven by the parameters the page sent."""
    run_args = argparse.Namespace(**vars(args))
    run_args.mazes = params["mazes"]
    run_args.channels = params["channels"]
    channels = _select_channels(cfg, run_args, params["channels"], params["mazes"])
    if not channels:
        UI.set(phase="idle", errors=["no channels selected"], finished=True)
        return

    UI.set(phase="starting", errors=[], warnings=[], channels={}, maze_counts={},
           finished=False, done=0, total=len(channels))
    log.info("run: %d channel(s), mazes=%s, write=%s",
             len(channels), params["mazes"], not params["no_write"])
    try:
        results = await run_sweep(cfg, cams, resolver, io, channels, run_args)
    except AmbientTooBright as exc:
        log.error("%s", exc)
        UI.set(phase="blocked", errors=[str(exc)], finished=True)
        return
    except SweepAborted as exc:
        log.warning("%s", exc)
        UI.set(phase="aborted", warnings=["run aborted — nothing written"],
               finished=True)
        return
    finally:
        # Always leave the maze dark, however the run ended.
        await light_only(resolver, io, [])

    errors, warnings = validate(cfg, results, channels)
    for w in warnings:
        log.warning("  %s", w)
    for e in errors:
        log.error("  %s", e)
    UI.set(errors=errors, warnings=warnings, finished=True, phase="done")

    if params["no_write"]:
        log.info("not writing (write is off for this run)")
        return
    if errors and not args.force:
        log.error("NOT writing beams.json — %d error(s)", len(errors))
        UI.set(phase="done — not written")
        return

    path = Path(loader.CONFIG_DIR) / "beams.json"
    sizes = {cid: (f.shape[1], f.shape[0])
             for cid, f in (await cams.median_capture(1)).items()}
    backup = write_beams(path, results, channels, cams, sizes)
    log.info("wrote %s (backup: %s)", path, backup.name)
    try:
        loader.load_all()
        UI.set(phase=f"written, backup {backup.name}")
    except Exception as exc:
        shutil.copy2(backup, path)
        log.error("written config does not load (%s) — restored the backup", exc)
        UI.set(phase="write reverted", errors=[f"config would not load: {exc}"])


async def amain(args) -> int:
    cfg = loader.load_all()
    UI.serve(args.ui_port)

    log.warning("This drives the relay boards. Lasers WILL switch on. "
                "House lights off, and nobody in the container.")

    io = ModbusIOBackend(cfg.hardware)
    connected = await io.connect_all()
    dead = sorted(bid for bid, ok in connected.items() if not ok)
    if dead:
        log.error("relay board(s) unreachable: %s", ", ".join(dead))
        return 2
    resolver = PresetResolver(cfg.mazes, cfg.hardware)

    cams = Cameras(cfg)
    await cams.start()
    await light_only(resolver, io, [])
    UI.set(phase="idle")

    # One-shot mode keeps the old behaviour for scripting; otherwise this is a
    # small service: cameras and boards stay connected (the RTSP handshake costs
    # ~10 s) and each run is triggered from the page. That matters when tuning —
    # you can be at the maze with the lights off and start a run from a phone.
    if args.now:
        await one_run(cfg, cams, resolver, io, args,
                      {"channels": args.channels, "mazes": args.mazes,
                       "no_write": args.no_write})
        await cams.stop()
        return 0

    log.info("ready — open http://<this-host>:%d and press START", args.ui_port)
    try:
        while True:
            params = UI.take_start()
            if params is None:
                await asyncio.sleep(0.25)
                continue
            try:
                await one_run(cfg, cams, resolver, io, args, params)
            finally:
                UI.finished()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await light_only(resolver, io, [])
        await cams.stop()
        UI.stop()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--channels", default="",
                    help="comma-separated channel ids to sweep, e.g. 11,12 "
                         "(default: all 45)")
    ap.add_argument("--no-write", action="store_true",
                    help="run the full sweep but leave beams.json alone. NOTE: "
                         "this still drives the relays — lasers will switch on")
    ap.add_argument("--ignore-ambient", action="store_true",
                    help="proceed even though the cameras see light with all "
                         "lasers off (house lights on). Produces ghost dots")
    ap.add_argument("--force", action="store_true",
                    help="write even if validation reported errors")
    ap.add_argument("--mazes", default="maze_1,maze_2,maze_3",
                    help="which presets to measure baselines for. Labelling is "
                         "maze-independent and always covers every selected "
                         "channel; baselines are per maze because a dot's "
                         "neighbours differ between shapes")
    ap.add_argument("--ui-port", type=int, default=8090,
                    help="live calibration page (0 disables)")
    ap.add_argument("--all-channels", action="store_true",
                    help="sweep all 45 channels, not just those a maze lights")
    ap.add_argument("--write-refs", action="store_true",
                    help="save an all_on reference frame per camera to ref/")
    ap.add_argument("--now", action="store_true",
                    help="sweep immediately and exit, instead of waiting for "
                         "START on the web page")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING"])
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        log.warning("interrupted — beams.json not written")
        return 130


if __name__ == "__main__":
    sys.exit(main())
