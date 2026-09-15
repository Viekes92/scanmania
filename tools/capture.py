#!/usr/bin/env python3
"""
tools/capture.py — calibrate beams.json by lighting a whole maze and recording
                   every dot the cameras can see.

Inputs:  live relay boards + the ceiling cameras, config/hardware.yaml, beams.json
Outputs: a calibrated config/beams.json (per maze: per camera, params + dots +
         baselines), written atomically after validation, with a backup
Invariant: drives relays only through PresetResolver (invariant 4), never raw
           write_coils. Never overwrites a good beams.json with a failed capture.

Light the maze. Tune each camera until its count looks right. Save what it saw.

That is the whole method. There is no channel sweep and no dot-to-relay mapping,
because the game does not need one: a dot going dark means a beam was broken,
and which relay drives it changes nothing about ending the run. Dropping the
mapping is what makes calibration a two-minute job per maze instead of 45 relay
switches with an ambiguous labelling pass in the middle.

Detection params are PER CAMERA. A camera two metres from its dots and one six
metres away need different top-hat kernels — the kernel has to be bigger than a
dot and smaller than the spacing, and both of those scale with distance. One
global setting is why the first sweep found 5 dots on one camera and 11 on
another looking at the same lasers.

Captures are per maze. Each shape lights roughly half the floor, so a dot with
two lit neighbours in one shape and none in another reads differently. One
baseline cannot serve all three.

Stop the game service first — kiosk too, or its Wants= drags the game back up
and ReconcileLoop re-lights channels underneath you:

    systemctl stop scanmania-kiosk scanmania
    .venv/bin/python tools/capture.py       # then open :8090
    systemctl start scanmania scanmania-kiosk

Prerequisites, in order: house lights OFF; camera exposure, white balance and
substream resolution locked; cameras physically fixed; game stopped; nobody in
the container; lasers warm.
"""

from __future__ import annotations

import argparse
import collections
import asyncio
import json
import logging
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config.loader as loader
from iobackend.modbus import ModbusIOBackend
from iobackend.presets import PresetResolver
from vision.camera import CameraStream
from vision.detect import sample_circle

log = logging.getLogger("capture")


class AmbientTooBright(RuntimeError):
    """Cameras see light with every laser off. Calibrating now records ghosts."""


# ---------------------------------------------------------------------------
# Dot finding
#
# White top-hat, then a low ABSOLUTE threshold. Dots span a huge brightness
# range — close ones near saturation, far ones barely above the ceiling — so a
# threshold set as a fraction of the global peak only ever finds the brightest
# few. The top-hat subtracts local background and puts them all on the same
# footing.
#
# The kernel must be LARGER than a dot and SMALLER than the spacing between
# dots. Too small and a dot becomes a ring, because tophat = img - opening(img)
# keeps only what the kernel could not contain; that ring then fragments into
# arcs and one dot is counted three times.
# Dot-finding moved to vision/dots.py so the running game can use the same
# implementation this calibration was made with. Re-exported here because
# the rest of this file — and the operator's muscle memory — uses these names.
from vision.dots import DEFAULT_PARAMS, stages, find_dots  # noqa: E402

# Capture-tool tuning. Not dot-finding: these govern how many frames this
# tool medians and how patient it is, neither of which the running game does.
_MEDIAN_FRAMES = 7      # frames to median per capture; kills sensor noise
_PREVIEW_MEDIAN = 5     # frames to median per preview pass; ~200 ms at 25 fps
_PREVIEW_HZ = 2.0
# How many preview passes to report the dot-count spread over. A count that
# swings across this window is not tuned, however good the middle value looks.
_COUNT_WINDOW = 10

# A capture takes several medians spread over a few seconds and keeps only dots
# that persist. Somebody standing in a beam, or haze drifting through it,
# removes real dots from a single pass — and a dot whose baseline was measured
# while it was blocked reads permanently dark at runtime.
_CAPTURE_PASSES = 5
_CAPTURE_PASS_GAP_S = 0.6       # ~3 s total; long enough to outlast a person moving
# 3 of 5, not 4. Somebody walking through blocks a dot for about two passes,
# and dropping it there leaves a blind spot — a beam nothing watches — which is
# worse than keeping a marginal dot, because a bad baseline at least shows up as
# a dim or zero reading in validate(). A single-pass ghost is still rejected.
_CAPTURE_MIN_HITS = 3           # of _CAPTURE_PASSES
_MATCH_TOL = 4                  # px; the cameras do not move during a capture

# With every laser off, a correctly exposed ceiling camera sees near-nothing.
# More blobs than this means something else is lighting the scene.
_MAX_AMBIENT_BLOBS = 15


# Capture-tool tuning. Not dot-finding: these govern how many frames this
# tool medians and how patient it is, which the running game never does.




# ---------------------------------------------------------------------------
# Shared state between the asyncio worker and the HTTP threads
# ---------------------------------------------------------------------------

class _UI:
    """
    Every field is guarded by one lock. The HTTP server runs on its own threads
    and the capture runs on the event loop; neither may see a half-written dict.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict = {
            "phase": "starting",
            "lit": None,            # which maze is currently lit
            "counts": {},           # camera_id -> dots found in the live preview
            "diag": {},             # camera_id -> signal peak, rejected counts
            "view": "overlay",      # which pipeline stage the previews show
            "captured": {},         # maze -> {camera_id: {"dots": n, "mean": f}}
            "ambient": None,
            "saved": None,
            "error": None,
        }
        self._params: dict[str, dict] = {}       # camera_id -> params
        self._frames: dict[str, bytes] = {}
        self._count_hist: dict[str, collections.deque] = {}
        self._light_req: str | None = None
        self._capture_req: str | None = None
        self._save_req: bool = False
        self._httpd: ThreadingHTTPServer | None = None

    # -- state ----------------------------------------------------------

    def set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def snapshot(self) -> dict:
        with self._lock:
            s = dict(self._state)
            s["params"] = {c: dict(p) for c, p in self._params.items()}
            s["cameras"] = sorted(self._frames)
            return s

    # -- params ---------------------------------------------------------

    def params_for(self, camera_id: str) -> dict:
        with self._lock:
            return dict(self._params.setdefault(camera_id, dict(DEFAULT_PARAMS)))

    def all_params(self) -> dict[str, dict]:
        with self._lock:
            return {c: dict(p) for c, p in self._params.items()}

    def register_camera(self, camera_id: str) -> None:
        with self._lock:
            self._params.setdefault(camera_id, dict(DEFAULT_PARAMS))

    def set_params(self, camera_id: str, p: dict) -> None:
        """
        Merge tuning from the page. A bad value is ignored, never applied: an
        even top-hat kernel or a zero threshold silently ruins a capture, and
        the page is being poked at from a phone in a dark container.
        """
        with self._lock:
            cur = self._params.setdefault(camera_id, dict(DEFAULT_PARAMS))
            for key in DEFAULT_PARAMS:
                if key not in p:
                    continue
                try:
                    v = int(p[key])
                except (TypeError, ValueError):
                    continue
                if v > 0 and cur[key] != v:
                    cur[key] = v
                    # Drop the spread history: it describes the previous
                    # setting, and carrying it over makes a good new value look
                    # unstable for the next five seconds.
                    self._count_hist.pop(camera_id, None)

    # -- requests from the page -----------------------------------------

    def apply_suggested(self, camera_id: str) -> None:
        """Adopt the measured suggestion for one camera."""
        with self._lock:
            sug = (self._state.get("diag", {}).get(camera_id) or {}).get("suggest") or {}
            cur = self._params.setdefault(camera_id, dict(DEFAULT_PARAMS))
            for key in ("tophat", "min_area", "max_area"):
                if isinstance(sug.get(key), int) and sug[key] > 0:
                    cur[key] = sug[key]
            self._count_hist.pop(camera_id, None)
        log.info("%s: applied suggested params %s", camera_id,
                 {k: v for k, v in sug.items() if k in ("tophat", "min_area", "max_area")})

    def request_light(self, maze: str) -> None:
        with self._lock:
            self._light_req = maze

    def take_light(self) -> str | None:
        with self._lock:
            m, self._light_req = self._light_req, None
            return m

    def request_capture(self, maze: str) -> None:
        with self._lock:
            self._capture_req = maze

    def take_capture(self) -> str | None:
        with self._lock:
            m, self._capture_req = self._capture_req, None
            return m

    def request_save(self) -> None:
        with self._lock:
            self._save_req = True

    def take_save(self) -> bool:
        with self._lock:
            s, self._save_req = self._save_req, False
            return s

    # -- preview ---------------------------------------------------------

    def set_view(self, view: str) -> None:
        """Which pipeline stage the previews show. See stages()."""
        if view in ("overlay", "raw", "signal", "mask"):
            with self._lock:
                self._state["view"] = view

    def publish_frames(self, frames: dict[str, np.ndarray]) -> None:
        """Encode previews at the selected stage. In an executor — cv2 is slow."""
        with self._lock:
            view = self._state.get("view", "overlay")
        out, counts, diag = {}, {}, {}
        for cid, frame in frames.items():
            st = stages(frame, self.params_for(cid))
            counts[cid] = len(st["dots"])
            small = sum(1 for r in st["rejected"] if r[4] == "small")
            large = sum(1 for r in st["rejected"] if r[4] == "large")
            areas = [int(round(np.pi * (r - 2) ** 2)) for _, _, r in st["dots"]]
            hist = self._count_hist.setdefault(cid, collections.deque(maxlen=_COUNT_WINDOW))
            hist.append(len(st["dots"]))
            diag[cid] = {
                "peak": st["signal_peak"],
                "too_small": small, "too_large": large,
                # The spread over the last _COUNT_WINDOW passes. A count that
                # swings is not tuned, however good the middle value looks —
                # every dot near the threshold will drop in and out of the real
                # capture too.
                "lo": min(hist), "hi": max(hist),
                "suggest": suggest_params(st["dots"]),
                # The median dot area is the number that tells you whether the
                # top-hat kernel is in the right range: it has to be comfortably
                # wider than a dot.
                "median_area": int(np.median(areas)) if areas else 0,
            }

            if view == "raw":
                vis = frame.copy()
            elif view == "signal":
                # Stretched, because a correct signal is mostly near-black with
                # small bright spikes and looks empty at native contrast.
                vis = cv2.applyColorMap(
                    cv2.normalize(st["signal"], None, 0, 255, cv2.NORM_MINMAX),
                    cv2.COLORMAP_INFERNO)
            elif view == "mask":
                vis = cv2.cvtColor(st["mask"], cv2.COLOR_GRAY2BGR)
            else:
                vis = frame.copy()

            if view != "raw":
                for (cx, cy, r) in st["dots"]:
                    cv2.circle(vis, (cx, cy), max(r + 3, 8), (0, 255, 0), 1)
                # Rejected blobs in amber (too small) and blue (too large), so a
                # bound that is cutting real dots is visible rather than inferred.
                for (cx, cy, r, _area, why) in st["rejected"]:
                    colour = (0, 170, 255) if why == "small" else (255, 140, 0)
                    cv2.circle(vis, (cx, cy), max(r + 3, 8), colour, 1)

            h, w = vis.shape[:2]
            if w > 960:
                vis = cv2.resize(vis, (960, int(h * 960 / w)))
            ok, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                out[cid] = buf.tobytes()
        with self._lock:
            self._frames.update(out)
            self._state["counts"] = counts
            self._state["diag"] = diag

    def frame(self, cid: str) -> bytes | None:
        with self._lock:
            return self._frames.get(cid)

    # -- server ----------------------------------------------------------

    def serve(self, port: int) -> None:
        ui = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):        # quiet; the tool logs its own
                pass

            def _send(self, code, body, ctype="application/json", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                # Split the query string off FIRST. The page cache-busts every
                # frame request (/frame/SM-CAM-13.jpg?1789415532579), so
                # matching on the raw path made every preview 404 — the id came
                # out as "SM-CAM-13.jpg?1789415532579" because removesuffix
                # found no ".jpg" at the end any more.
                path = urlparse(self.path).path
                if path == "/":
                    return self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
                if path == "/status":
                    return self._send(200, json.dumps(ui.snapshot()).encode())
                if path.startswith("/frame/"):
                    cid = unquote(path[len("/frame/"):]).removesuffix(".jpg")
                    jpg = ui.frame(cid)
                    if jpg is None:
                        return self._send(404, b"{}")
                    return self._send(200, jpg, "image/jpeg",
                                      extra={"Cache-Control": "no-store"})
                self._send(404, b"{}")

            def do_POST(self):
                path_ = urlparse(self.path).path
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, b'{"ok":false}')
                if path_ == "/light":
                    ui.request_light(str(body.get("maze") or ""))
                elif path_ == "/capture":
                    ui.request_capture(str(body.get("maze") or ""))
                elif path_ == "/save":
                    ui.request_save()
                elif path_ == "/params":
                    ui.set_params(str(body.get("camera") or ""), body)
                elif path_ == "/apply_suggested":
                    ui.apply_suggested(str(body.get("camera") or ""))
                elif path_ == "/view":
                    ui.set_view(str(body.get("view") or "overlay"))
                else:
                    return self._send(404, b'{"ok":false}')
                self._send(200, b'{"ok":true}')

        self._httpd = ThreadingHTTPServer(("0.0.0.0", port), H)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        log.info("calibration page on http://<this-host>:%d", port)

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()


UI = _UI()


_PAGE = """<!doctype html><meta charset=utf-8><title>ScanMania calibration</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{background:#111;color:#ddd;font:14px/1.5 system-ui,sans-serif;margin:0;padding:14px}
 h1{font-size:16px;margin:0 0 10px}
 .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
 button{background:#2a2a2a;color:#eee;border:1px solid #444;border-radius:6px;
        padding:9px 14px;font-size:14px;cursor:pointer}
 button:hover{background:#383838}
 button.go{background:#1d4e2a;border-color:#2f7a42}
 button.save{background:#1d3a5e;border-color:#2f5f9a}
 button.off{background:#4e1d1d;border-color:#7a2f2f}
 /* One column per position along the container, one row per side — so the
    two cameras that face each other (11 above 21) sit in the same column. */
 .cams{display:grid;gap:12px;align-items:start}
 .cam{border:1px solid #333;border-radius:8px;padding:8px;background:#181818}
 .rowlabel{grid-column:1/-1;font-size:11px;letter-spacing:.14em;color:#777;
           text-transform:uppercase;margin:6px 0 -4px}
 @media (max-width:1100px){
   /* Too narrow to keep four columns legible; fall back to flow and drop the
      facing-pair alignment rather than shrink every tile into uselessness. */
   .cams{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))!important}
   .cam{grid-column:auto!important;grid-row:auto!important}
 }
 .cam{border:1px solid #333;border-radius:8px;padding:8px;background:#181818}
 .cam h2{font-size:13px;margin:0 0 6px;display:flex;justify-content:space-between}
 .n{color:#6c6;font-variant-numeric:tabular-nums}
 img{width:100%;display:block;border-radius:4px;background:#000}
 .p{display:grid;grid-template-columns:auto 1fr auto;gap:4px 8px;margin-top:6px;
    font-size:12px;align-items:center}
 input[type=range]{width:100%}
 #status{font-size:13px;color:#999;margin-bottom:10px}
 button.view{padding:6px 10px;font-size:13px}
 button.view.on{background:#1d3a5e;border-color:#4a8fd8;color:#fff}
 .sep{width:16px}
 .vlabel{color:#888;font-size:12px;letter-spacing:.08em;text-transform:uppercase}
 .legend{font-size:12px;color:#888;margin-bottom:10px}
 .legend .k{display:inline-block;border:1px solid;border-radius:3px;
            padding:1px 6px;margin-right:6px;color:#bbb}
 .diag{font-size:11px;color:#777;margin-top:4px;font-family:ui-monospace,monospace}
 .diag b{color:#aaa;font-weight:600}
 .diag b.good{color:#6c6}
 .diag b.okish{color:#cc6}
 .diag b.bad{color:#e66}
 button.sug{margin-top:6px;width:100%;padding:6px;font-size:11px;
            background:#14301f;border-color:#2f7a42;color:#9d9}
 .warn{color:#e88}
 table{border-collapse:collapse;font-size:12px;margin-top:6px}
 td,th{border:1px solid #333;padding:3px 8px;text-align:left}
</style>
<h1>ScanMania calibration</h1>
<div id=status>connecting…</div>
<div class=row>
  <button class=go onclick="light('maze_1')">Light maze_1</button>
  <button class=go onclick="light('maze_2')">Light maze_2</button>
  <button class=go onclick="light('maze_3')">Light maze_3</button>
  <button class=off onclick="light('')">All off</button>
</div>
<div class=row>
  <button onclick="cap()">Capture lit maze</button>
  <button class=save onclick="save()">Write beams.json</button>
  <span class=sep></span>
  <span class=vlabel>view</span>
  <button class=view id=v_overlay onclick="setView('overlay')">overlay</button>
  <button class=view id=v_raw     onclick="setView('raw')">raw</button>
  <button class=view id=v_signal  onclick="setView('signal')">signal</button>
  <button class=view id=v_mask    onclick="setView('mask')">mask</button>
</div>
<div class=legend>
  <span class=k style="border-color:#2c2">found</span>
  <span class=k style="border-color:#f80">below min_area</span>
  <span class=k style="border-color:#08f">above max_area</span>
  &nbsp;&mdash;&nbsp; rings in <b>mask</b> mean the top-hat kernel is smaller
  than a dot: raise it.
</div>
<div id=captured></div>
<div class=cams id=cams></div>
<script>
const KEYS=[['thr','threshold',1,120],['tophat','top-hat px',3,61],
            ['min_area','min area',1,200],['max_area','max area',20,4000]];
let built=false, litMaze=null;

function light(m){fetch('/light',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({maze:m})});}
function cap(){if(!litMaze){alert('Light a maze first');return;}
  fetch('/capture',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({maze:litMaze})});}
function save(){if(confirm('Overwrite config/beams.json?'))
  fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}
function setView(v){fetch('/view',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({view:v})});}

function send(cam){
  const b={camera:cam};
  KEYS.forEach(([k])=>{b[k]=+document.getElementById(cam+'_'+k).value;});
  fetch('/params',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(b)});
}

/* SM-CAM-<side><pos>: side 1 is the left row of the container, side 2 the
   right, pos runs along its length. Lay out pos as columns and side as rows so
   a facing pair is vertically adjacent. Placement is explicit rather than flow
   order, so a camera dropping out leaves a gap instead of shifting the rest. */
function slot(cid){
  const t=cid.trim().slice(-2);           // "11" -> side 1, pos 1
  const side=+t[0], pos=+t[1];
  return (side>0 && pos>0) ? {side:side, pos:pos} : null;
}

function build(cams,params){
  const wrap=document.getElementById('cams'); wrap.innerHTML='';
  const slots=cams.map(slot);
  const placed=slots.every(Boolean);
  if(placed){
    const cols=Math.max(...slots.map(s=>s.pos));
    wrap.style.gridTemplateColumns='repeat('+cols+',minmax(0,1fr))';
  }else{
    wrap.style.gridTemplateColumns='repeat(auto-fit,minmax(340px,1fr))';
  }
  cams.forEach(cid=>{
    const p=params[cid]||{};
    const d=document.createElement('div'); d.className='cam';
    let h='<h2><span>'+cid+'</span><span class=n id="'+cid+'_n">–</span></h2>'
        + '<img id="'+cid+'_img" alt="'+cid+'">'
        + '<div class=p>';
    KEYS.forEach(([k,label,lo,hi])=>{
      h+='<label for="'+cid+'_'+k+'">'+label+'</label>'
        +'<input type=range id="'+cid+'_'+k+'" min='+lo+' max='+hi
        +' value="'+(p[k]||lo)+'">'
        +'<span id="'+cid+'_'+k+'_v">'+(p[k]||lo)+'</span>';
    });
    d.innerHTML=h+'</div><div class=diag id="'+cid+'_diag"></div>'
               +'<button class=sug id="'+cid+'_sug" hidden></button>';
    const sl=slot(cid);
    if(placed && sl){ d.style.gridColumn=sl.pos; d.style.gridRow=sl.side; }
    wrap.appendChild(d);
    KEYS.forEach(([k])=>{
      const el=document.getElementById(cid+'_'+k);
      el.oninput=()=>{document.getElementById(cid+'_'+k+'_v').textContent=el.value;};
      el.onchange=()=>send(cid);
    });
    document.getElementById(cid+'_sug').onclick=()=>{
      fetch('/apply_suggested',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({camera:cid})}).then(()=>{built=false;});
    };
  });
  built=true;
}

async function tick(){
  let s;
  try{ s=await (await fetch('/status')).json(); }
  catch(e){ document.getElementById('status').textContent='page lost the tool'; return; }
  litMaze = s.lit || null;
  if(!built && s.cameras.length) build(s.cameras, s.params||{});
  let txt = 'phase: '+s.phase+'   lit: '+(s.lit||'nothing');
  if(s.ambient!=null) txt += '   ambient blobs: '+s.ambient;
  if(s.saved) txt += '   saved: '+s.saved;
  const st=document.getElementById('status');
  st.textContent = txt; st.className = s.error ? 'warn' : '';
  if(s.error) st.textContent = 'ERROR: '+s.error+'  —  '+txt;

  ['overlay','raw','signal','mask'].forEach(v=>{
    const b=document.getElementById('v_'+v);
    if(b) b.className='view'+((s.view||'overlay')===v?' on':'');
  });

  (s.cameras||[]).forEach(cid=>{
    const n=document.getElementById(cid+'_n');
    if(n) n.textContent=(s.counts&&s.counts[cid]!=null)?s.counts[cid]+' dots':'–';
    const img=document.getElementById(cid+'_img');
    if(img) img.src='/frame/'+encodeURIComponent(cid)+'.jpg?'+Date.now();
    const dv=document.getElementById(cid+'_diag'), d=(s.diag||{})[cid];
    if(dv&&d){
      // peak is the top-hat signal maximum. Near zero means no dot is reaching
      // the sensor, and no slider on this page will fix that.
      // Range first: it is the number that says whether this camera is tuned.
      const spread=(d.hi!=null && d.lo!=null) ? (d.hi-d.lo) : 0;
      let t='';
      if(d.hi!=null){
        const cls = spread===0 ? 'good' : (spread<=2 ? 'okish' : 'bad');
        t += 'range <b class='+cls+'>'+d.lo+'\u2013'+d.hi+'</b>  ';
      }
      t+='peak <b>'+d.peak+'</b>  median area <b>'+d.median_area+'</b>';
      if(d.too_small) t+='  \u00b7 <b>'+d.too_small+'</b> under min';
      if(d.too_large) t+='  \u00b7 <b>'+d.too_large+'</b> over max';
      const sg=d.suggest||{};
      if(sg.dot_px) t+='<br>dot <b>'+sg.dot_px+'px</b> spacing <b>'+sg.spacing_px+'px</b>';
      if(sg.note)   t+='<br><b class=bad>'+sg.note+'</b>';
      dv.innerHTML=t;
      // Offer the measured kernel only when it disagrees with what is set —
      // the kernel must exceed a dot's diameter and stay inside the spacing,
      // and both change with resolution and camera distance.
      const btn=document.getElementById(cid+'_sug');
      const curTop=+document.getElementById(cid+'_tophat').value;
      if(btn){
        if(sg.tophat && sg.tophat!==curTop){
          btn.hidden=false;
          btn.textContent='apply measured: top-hat '+sg.tophat
                        +', area '+sg.min_area+'\u2013'+sg.max_area;
        } else { btn.hidden=true; }
      }
    }
  });

  const c=s.captured||{}; const names=Object.keys(c).sort();
  const box=document.getElementById('captured');
  if(!names.length){ box.innerHTML=''; return; }
  let h='<table><tr><th>maze</th><th>camera</th><th>dots</th><th>mean baseline</th></tr>';
  names.forEach(m=>Object.keys(c[m]).sort().forEach(cid=>{
    h+='<tr><td>'+m+'</td><td>'+cid+'</td><td>'+c[m][cid].dots+'</td><td>'
      +c[m][cid].mean.toFixed(1)+'</td></tr>';}));
  box.innerHTML=h+'</table>';
}
setInterval(tick,700); tick();
</script>
"""


# ---------------------------------------------------------------------------
# Camera capture
# ---------------------------------------------------------------------------

class Cameras:
    """Keeps the newest frame from every camera, so any step can grab one."""

    def __init__(self, cfg) -> None:
        self._latest: dict[str, np.ndarray] = {}
        self._recent: dict[str, collections.deque] = {}
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
        # A short rolling history so the PREVIEW can median like the capture
        # does. A single live frame makes every marginal blob flicker across the
        # threshold, so the count you tune against jitters and does not match
        # what a capture will actually record. Five frames at 25 fps is 200 ms —
        # enough to kill sensor noise and haze shimmer, short enough that a
        # slider change still shows up immediately.
        hist = self._recent.get(camera_id)
        if hist is None:
            hist = self._recent[camera_id] = collections.deque(maxlen=_PREVIEW_MEDIAN)
        hist.append(frame)

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

    async def preview_loop(self, hz: float = _PREVIEW_HZ) -> None:
        """
        Keep the page's camera panes fed while idle.

        The preview is how you confirm the house lights are off and how you tune,
        so it must run before anything is captured, not only during a capture.
        """
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(1.0 / hz)
            stacks = {cid: list(h) for cid, h in self._recent.items() if h}
            if not stacks:
                continue
            # Median off the event loop, and on the DEFAULT executor — the
            # camera pool is busy with blocking RTSP reads.
            frames = await loop.run_in_executor(None, self._median, stacks)
            if frames:
                await loop.run_in_executor(None, UI.publish_frames, frames)

    async def median_capture(self, n: int = _MEDIAN_FRAMES) -> dict[str, np.ndarray]:
        """
        Median of n frames per camera.

        A single frame carries sensor noise and haze shimmer; the median of
        several removes both without smearing an edge (nothing moves here).
        """
        stacks: dict[str, list[np.ndarray]] = {cid: [] for cid in self._streams}
        for _ in range(n):
            for cid in self._streams:
                f = self._latest.get(cid)
                if f is not None:
                    stacks[cid].append(f.copy())
            await asyncio.sleep(0.05)
        # Off the event loop: seven 1080p frames is ~43 MB per camera, and doing
        # it inline blocks the RTSP readers long enough that they report stalls.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._median, stacks)

    @staticmethod
    def _median(stacks: dict[str, list[np.ndarray]]) -> dict[str, np.ndarray]:
        return {cid: np.median(np.stack(v), axis=0).astype(np.uint8)
                for cid, v in stacks.items() if v}


# ---------------------------------------------------------------------------
# Relay control — invariant 4: never write_coils directly
# ---------------------------------------------------------------------------

async def light_maze(resolver, io, maze: str) -> bool:
    """Light one preset, or everything off when maze is empty."""
    if resolver is None or io is None:
        log.warning("no relay control — cannot light '%s'", maze or "blackout")
        return False
    if not maze:
        await resolver.apply_all_off(io)
        return True
    try:
        await resolver.apply_preset(maze, io)
        return True
    except KeyError:
        log.error("unknown preset '%s'", maze)
        return False


# ---------------------------------------------------------------------------
# The capture
# ---------------------------------------------------------------------------

async def check_ambient(cams: Cameras) -> int:
    """
    Count blobs with every laser off.

    With the room lit, the top-hat picks up ceiling texture and light fittings.
    Those get recorded as dots, and every one of them then reads a baseline that
    never changes, so the maze looks fully calibrated and detects nothing.
    """
    frames = await cams.median_capture()
    worst = 0
    for cid, f in frames.items():
        n = len(find_dots(f, UI.params_for(cid)))
        log.info("ambient: %s sees %d blobs with lasers off", cid, n)
        worst = max(worst, n)
    return worst


def _persistent_dots(
    per_pass: list[dict[str, loader.CameraCapture]],
) -> dict[str, loader.CameraCapture]:
    """
    Keep dots that appear in at least _CAPTURE_MIN_HITS passes.

    Dots are matched between passes by position: the cameras do not move during
    a capture, so the same dot lands within a couple of pixels each time.
    A kept dot takes the MEDIAN of its baselines across the passes it was seen
    in, which also throws out a pass where it was partly dimmed.
    """
    out: dict[str, loader.CameraCapture] = {}
    if not per_pass:
        return out

    for cid in per_pass[0]:
        clusters: list[dict] = []
        for cap in (p[cid] for p in per_pass if cid in p):
            for dot in cap.dots:
                for cl in clusters:
                    if abs(cl["cx"] - dot.cx) <= _MATCH_TOL and \
                       abs(cl["cy"] - dot.cy) <= _MATCH_TOL:
                        cl["hits"] += 1
                        cl["xs"].append(dot.cx)
                        cl["ys"].append(dot.cy)
                        cl["rs"].append(dot.r)
                        cl["bs"].append(dot.baseline)
                        break
                else:
                    clusters.append({"cx": dot.cx, "cy": dot.cy, "hits": 1,
                                     "xs": [dot.cx], "ys": [dot.cy],
                                     "rs": [dot.r], "bs": [dot.baseline]})

        kept = [c for c in clusters if c["hits"] >= _CAPTURE_MIN_HITS]
        dropped = len(clusters) - len(kept)
        base = per_pass[0][cid]
        dots = []
        for i, c in enumerate(sorted(kept, key=lambda c: (c["cx"], c["cy"]))):
            dots.append(loader.Dot(
                id=f"{cid}:d{i}",
                cx=int(round(float(np.median(c["xs"])))),
                cy=int(round(float(np.median(c["ys"])))),
                r=int(round(float(np.median(c["rs"])))),
                baseline=round(float(np.median(c["bs"])), 3),
            ))
        if dropped:
            total = len(dots) + dropped
            share = dropped / total if total else 0
            # A few is normal. A lot means the scene was not still, and the
            # whole capture is suspect — not just the dots that were dropped.
            (log.warning if share > 0.1 else log.info)(
                "%s: kept %d dot(s), dropped %d unstable (%.0f%%)%s",
                cid, len(dots), dropped, share * 100,
                "  ** was someone in the container? **" if share > 0.1 else "")
        out[cid] = loader.CameraCapture(camera=cid, w=base.w, h=base.h,
                                        params=dict(base.params), dots=dots)
    return out


def capture_from_frames(frames: dict[str, np.ndarray],
                        params: dict[str, dict],
                        expect: dict[str, tuple[int, int]] | None = None,
                        ) -> dict[str, loader.CameraCapture]:
    """
    Turn one median frame per camera into that maze's ROI set.

    Baselines are measured with vision/detect.py's sample_circle, the exact
    function the game uses at runtime. Measuring them any other way puts stored
    baselines and live samples in different units, and every ratio in the game
    is then silently wrong.
    """
    out: dict[str, loader.CameraCapture] = {}
    for cid, frame in frames.items():
        p = params.get(cid) or dict(DEFAULT_PARAMS)
        h, w = frame.shape[:2]
        # hardware.yaml declares the substream resolution each camera is set to.
        # A camera that came back at a different one invalidates every ROI
        # measured against it, and nothing about the frame looks wrong.
        want = (expect or {}).get(cid)
        if want and want != (0, 0) and want != (w, h):
            log.error("%s: frame is %dx%d but hardware.yaml says %dx%d — "
                      "the substream resolution changed. Fix the camera or the "
                      "config before capturing.", cid, w, h, want[0], want[1])
        dots = []
        for i, (cx, cy, r) in enumerate(sorted(find_dots(frame, p))):
            dots.append(loader.Dot(
                id=f"{cid}:d{i}", cx=cx, cy=cy, r=r,
                baseline=round(sample_circle(frame, cx, cy, r), 3),
            ))
        out[cid] = loader.CameraCapture(camera=cid, w=w, h=h,
                                        params=dict(p), dots=dots)
    return out


async def capture_maze(cams: Cameras, maze: str,
                       expect: dict[str, tuple[int, int]] | None = None,
                       ) -> dict[str, loader.CameraCapture]:
    """
    Capture a maze from SEVERAL passes spread over a few seconds, keeping only
    dots that show up in most of them.

    A single median is not enough. A dot can be genuinely absent from one pass —
    somebody standing in the beam, or a thicker patch of haze dimming it — and
    no threshold setting distinguishes that from a dot that is not there at all.
    A sweep over the whole parameter space on a lit maze showed a count spread
    of 3-5 at every single combination, which is what transient occlusion looks
    like: real dots, intermittently blocked.

    Recording those as ROIs would be the worst outcome — a dot whose baseline
    was measured while it was blocked reads permanently dark, so it either busts
    every player or, more likely, sits in the mass-dark suppression and switches
    detection off. Requiring persistence across passes rejects them.
    """
    passes: list[dict[str, np.ndarray]] = []
    for i in range(_CAPTURE_PASSES):
        passes.append(await cams.median_capture())
        if i < _CAPTURE_PASSES - 1:
            await asyncio.sleep(_CAPTURE_PASS_GAP_S)

    per_pass = [capture_from_frames(f, UI.all_params(), expect) for f in passes]
    caps = _persistent_dots(per_pass)
    frames = passes[-1]
    for cid, cap in caps.items():
        mean = (sum(d.baseline for d in cap.dots) / len(cap.dots)) if cap.dots else 0.0
        log.info("%s / %s: %d dots, mean baseline %.1f, frame %dx%d",
                 maze, cid, len(cap.dots), mean, cap.w, cap.h)
    return caps


# ---------------------------------------------------------------------------
# Validation and writing
# ---------------------------------------------------------------------------

def validate(captures: dict[str, dict[str, loader.CameraCapture]]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors block the write unless --force."""
    errors: list[str] = []
    warnings: list[str] = []

    if not captures:
        errors.append("nothing captured")
        return errors, warnings

    for maze, cams in sorted(captures.items()):
        total = sum(len(c.dots) for c in cams.values())
        if total == 0:
            errors.append(f"{maze}: no dots at all")
            continue
        for cid, cap in sorted(cams.items()):
            if not cap.dots:
                warnings.append(f"{maze}/{cid}: found nothing — blind spot, or "
                                f"params need tuning")
                continue
            dead = [d.id for d in cap.dots if d.baseline <= 0]
            if dead:
                errors.append(f"{maze}/{cid}: {len(dead)} dot(s) with a zero "
                              f"baseline — they can never register a break")
            dim = [d for d in cap.dots if 0 < d.baseline < 15]
            if dim:
                warnings.append(f"{maze}/{cid}: {len(dim)} dim dot(s) "
                                f"(baseline < 15) — marginal, watch these")
        log.info("%s: %d dots across %d camera(s)", maze, total, len(cams))

    # A resolution change silently invalidates every ROI, because ROIs are
    # frame pixels. Catch it here rather than at 3am in front of a queue.
    sizes = {(c.w, c.h) for cams in captures.values() for c in cams.values()}
    per_cam: dict[str, set] = {}
    for cams in captures.values():
        for cid, c in cams.items():
            per_cam.setdefault(cid, set()).add((c.w, c.h))
    for cid, s in per_cam.items():
        if len(s) > 1:
            errors.append(f"{cid}: captured at {sorted(s)} — the substream "
                          f"resolution changed mid-calibration")
    return errors, warnings


def write_beams(path: Path,
                captures: dict[str, dict[str, loader.CameraCapture]]) -> str:
    """
    Merge the captures into beams.json, atomically, keeping a backup.

    Only the `mazes` block is rewritten. The 45 channel entries stay untouched:
    detection no longer uses them, but they are the record of which relay drives
    which array, which is what you need with a soldering iron in your hand.
    """
    with open(path) as f:
        data = json.load(f)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_suffix(f".json.{stamp}.bak")
    shutil.copy2(path, backup)

    mazes = data.setdefault("mazes", {})
    for maze, cams in captures.items():
        mazes[maze] = {
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cameras": {
                cid: {
                    "w": cap.w, "h": cap.h, "params": cap.params,
                    "dots": [{"id": d.id, "cx": d.cx, "cy": d.cy, "r": d.r,
                              "baseline": d.baseline, "masked": d.masked}
                             for d in cap.dots],
                }
                for cid, cap in cams.items()
            },
        }

    tmp = path.with_suffix(".json.new")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        # fsync the file AND the directory. Without this a venue power drop in
        # the seconds after a calibration can leave beams.json zero-length, and
        # config/loader.py refuses to start on a file it cannot parse.
        f.flush()
        os.fsync(f.fileno())

    # Prove it loads before it becomes the live file. A beams.json the loader
    # rejects takes the whole game down at the next restart.
    try:
        loader.load_beams(tmp)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"refusing to write: the result does not load ({exc})")

    tmp.replace(path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        log.warning("could not fsync %s: %s", path.parent, exc)
    log.info("wrote %s (backup: %s)", path, backup.name)
    return backup.name


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def amain(args) -> int:
    cfg = loader.load_all()
    log.warning("This drives the relay boards. Lasers WILL switch on. "
                "House lights off, and nobody in the container.")

    io = ModbusIOBackend(cfg.hardware)
    resolver = None
    if args.no_relays:
        io = None
        log.warning("--no-relays: tuning only. Nothing will be switched, and "
                    "Light/Capture act on whatever is already lit.")
    else:
        connected = await io.connect_all()
        dead = sorted(bid for bid, ok in connected.items() if not ok)
        if dead:
            # Not fatal. Looking at cameras and tuning parameters is useful on
            # its own, and a board being unreachable is no reason to refuse it.
            log.error("relay board(s) unreachable: %s — continuing in "
                      "tuning-only mode. Nothing will be switched.",
                      ", ".join(dead))
            io = None
        else:
            resolver = PresetResolver(cfg.mazes, cfg.hardware)

    cams = Cameras(cfg)
    await cams.start()
    for cid in cams.ids:
        UI.register_camera(cid)
    if args.ui_port:
        UI.serve(args.ui_port)

    preview = asyncio.create_task(cams.preview_loop(), name="preview")
    await light_maze(resolver, io, "")
    UI.set(phase="idle", lit=None,
           error=None if resolver else "no relay control — tuning only")

    if not args.skip_ambient:
        UI.set(phase="ambient check")
        blobs = await check_ambient(cams)
        UI.set(ambient=blobs)
        if blobs > _MAX_AMBIENT_BLOBS and not args.ignore_ambient:
            UI.set(phase="blocked", error=f"{blobs} blobs with lasers off")
            log.error("ambient too bright: %d blobs with every laser off. "
                      "Kill the house lights, or pass --ignore-ambient to "
                      "record ghosts on purpose.", blobs)
            preview.cancel()
            await cams.stop()
            UI.stop()
            return 3
        UI.set(phase="idle")

    captures: dict[str, dict[str, loader.CameraCapture]] = {}
    expect_size = {c.id: (c.w, c.h) for c in cfg.hardware.cameras}
    beams_path = Path(loader.CONFIG_DIR) / "beams.json"

    log.info("ready — open http://<this-host>:%d", args.ui_port)
    try:
        while True:
            maze = UI.take_light()
            if maze is not None:
                UI.set(phase="switching", error=None)
                ok = await light_maze(resolver, io, maze)
                UI.set(phase="idle", lit=(maze if ok and maze else None),
                       error=None if ok else f"unknown preset '{maze}'")

            want = UI.take_capture()
            if want:
                # The page's `litMaze` is up to 700 ms old, and two tabs on the
                # unauthenticated :8090 can disagree. Capturing maze_3's dots
                # under the key "maze_1" passes every check in validate().
                lit_now = UI.snapshot().get("lit")
                if lit_now != want:
                    msg = (f"refusing to capture '{want}': '{lit_now or 'nothing'}' "
                           f"is what is actually lit")
                    log.error(msg)
                    UI.set(phase="idle", error=msg)
                    want = None
            if want:
                UI.set(phase=f"capturing {want}", error=None)
                try:
                    caps = await capture_maze(cams, want, expect_size)
                    captures[want] = caps
                    UI.set(captured={
                        m: {cid: {"dots": len(c.dots),
                                  "mean": (sum(d.baseline for d in c.dots)
                                           / len(c.dots)) if c.dots else 0.0}
                            for cid, c in cams_.items()}
                        for m, cams_ in captures.items()
                    })
                    errs, warns = validate({want: caps})
                    for w in warns:
                        log.warning(w)
                    UI.set(phase="idle", error="; ".join(errs) or None)
                except Exception as exc:
                    log.exception("capture failed")
                    UI.set(phase="idle", error=str(exc))

            if UI.take_save():
                UI.set(phase="writing")
                errs, warns = validate(captures)
                for w in warns:
                    log.warning(w)
                if errs and not args.force:
                    for e in errs:
                        log.error(e)
                    UI.set(phase="idle", error="; ".join(errs) + " (not written)")
                elif args.no_write:
                    UI.set(phase="idle", error="--no-write: nothing saved")
                else:
                    try:
                        backup = write_beams(beams_path, captures)
                        UI.set(phase="idle", error=None,
                               saved=f"{len(captures)} maze(s), backup {backup}")
                    except Exception as exc:
                        log.exception("write failed")
                        UI.set(phase="idle", error=str(exc))

            await asyncio.sleep(0.2)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        preview.cancel()
        await light_maze(resolver, io, "")
        await cams.stop()
        UI.stop()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--ui-port", type=int, default=8090,
                    help="calibration page (0 disables, but then there is no "
                         "way to drive it)")
    ap.add_argument("--no-write", action="store_true",
                    help="capture and validate but never touch beams.json. "
                         "NOTE: this still drives the relays")
    ap.add_argument("--ignore-ambient", action="store_true",
                    help="calibrate anyway with the house lights on. Records "
                         "ceiling texture as dots")
    ap.add_argument("--skip-ambient", action="store_true",
                    help="do not run the ambient check at startup")
    ap.add_argument("--no-relays", action="store_true",
                    help="never touch the relay boards. Tuning and capture "
                         "still work against whatever is already lit — this is "
                         "the mode for looking at cameras without switching "
                         "lasers on")
    ap.add_argument("--force", action="store_true",
                    help="write even if validation reported errors")
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
