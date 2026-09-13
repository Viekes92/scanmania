#!/usr/bin/env python3
"""
tools/sweep.py — calibrate beams.json by switching one channel at a time.

Inputs:  live relay boards + the ceiling cameras, config/hardware.yaml, beams.json
Outputs: a calibrated config/beams.json (dots, per-dot camera, baseline, dark_floor)
         written atomically after validation, with a timestamped backup
Invariant: drives relays only through PresetResolver (invariant 4), never raw
           write_coils. Never overwrites a good beams.json with a failed run.

Stop the game service first. ReconcileLoop re-asserts desired coil state every
500 ms and would re-light channels mid-step, corrupting the labelling silently:

    systemctl stop scanmania      # on the NUC
    python3 tools/sweep.py        # then this
    systemctl start scanmania

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

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config.loader as loader
from vision.camera import CameraStream
from vision.detect import sample_circle

log = logging.getLogger("sweep")

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
        return await loop.run_in_executor(None, self._median, stacks)

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

async def run_sweep(cfg, cams: Cameras, resolver, io, channels: list, args) -> dict:
    board_order = [b.id for b in cfg.hardware.relay_boards]
    all_globals = [g for g in (global_channel(b, board_order) for b in channels)
                   if g is not None]
    results: dict[str, dict] = {}

    # ---- LABEL pass -------------------------------------------------------
    log.info("LABEL pass: %d channels, one at a time", len(channels))
    await light_only(resolver, io, [])
    await asyncio.sleep(0.5)
    dark = await cams.median_capture()

    for i, beam in enumerate(channels, 1):
        g = global_channel(beam, board_order)
        if g is None:
            log.error("channel %s: board '%s' not in hardware.yaml — skipped",
                      beam.id, beam.board_id)
            continue
        before = await cams.median_capture(3)
        await light_only(resolver, io, [g])
        if not await cams.wait_for_change(before):
            log.warning("channel %s: no visible change after switching on", beam.id)
        lit = await cams.median_capture()

        found: list[dict] = []
        for cid, frame in lit.items():
            base = dark.get(cid)
            for (cx, cy, r) in find_dots(frame):
                # Must be new relative to the dark frame, or it is ambient.
                if base is not None and match_dot((cx, cy, r), find_dots(base)):
                    continue
                found.append({"cx": cx, "cy": cy, "r": r, "camera": cid})

        results[beam.id] = {"dots": found}
        log.info("  [%2d/%d] %s -> %d dot(s) %s", i, len(channels), beam.id,
                 len(found), sorted({d["camera"] for d in found}))
        await light_only(resolver, io, [])

    # ---- MEASURE pass -----------------------------------------------------
    log.info("MEASURE pass: all lit, blinking each channel off")
    await light_only(resolver, io, all_globals)
    await asyncio.sleep(1.0)
    all_on = await cams.median_capture()

    for i, beam in enumerate(channels, 1):
        rec = results.get(beam.id)
        if not rec or not rec["dots"]:
            continue
        g = global_channel(beam, board_order)
        for d in rec["dots"]:
            frame = all_on.get(d["camera"])
            d["baseline"] = round(sample_circle(frame, d["cx"], d["cy"], d["r"]), 2) \
                if frame is not None else 0.0

        rest = [c for c in all_globals if c != g]
        before = await cams.median_capture(3)
        await light_only(resolver, io, rest)
        await cams.wait_for_change(before)
        off = await cams.median_capture()

        for d in rec["dots"]:
            frame = off.get(d["camera"])
            d["dark_floor"] = round(sample_circle(frame, d["cx"], d["cy"], d["r"]), 2) \
                if frame is not None else 0.0
        await light_only(resolver, io, all_globals)
        log.info("  [%2d/%d] %s measured", i, len(channels), beam.id)

    # ---- reference frames -------------------------------------------------
    if args.write_refs:
        ref_dir = Path(__file__).resolve().parent.parent / "ref"
        ref_dir.mkdir(exist_ok=True)
        for cid, frame in all_on.items():
            cv2.imwrite(str(ref_dir / f"{cid}.png"), frame)
        log.info("wrote %d reference frames to %s", len(all_on), ref_dir)

    results["_all_on_counts"] = {cid: len(find_dots(f)) for cid, f in all_on.items()}
    return results


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(cfg, results: dict, channels: list) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors block the write; warnings do not."""
    errors: list[str] = []
    warnings: list[str] = []
    break_ratio = channels[0].break_ratio if channels else 0.4

    counts = results.get("_all_on_counts", {})
    total_found = sum(counts.values())
    log.info("all_on dot counts per camera: %s (total %d, expect ~%d)",
             counts, total_found, len(channels) * 5)

    if all(v == 0 for v in counts.values()):
        errors.append("no dots detected at all_on — cameras dark, lasers off, or "
                      "the threshold is wrong")

    short, empty = [], []
    for beam in channels:
        rec = results.get(beam.id)
        dots = (rec or {}).get("dots", [])
        if not dots:
            empty.append(beam.id)
            continue
        if len(dots) < 4:
            # Below 4, detect.py cannot tell a real break from a dead channel:
            # a body can plausibly cover 2-3 dots, so all-dark stops meaning
            # hardware. See _MIN_DOTS_FOR_FAULT.
            short.append(f"{beam.id}({len(dots)})")

        res = colinearity_residual([(d["cx"], d["cy"], d["r"]) for d in dots])
        if res > 25:
            warnings.append(f"{beam.id}: dots {res:.0f}px off a straight line — "
                            f"possible ghost or mis-assignment (lens distortion "
                            f"alone should not do this)")

        # Aggregate per channel. One warning per dot floods the report and
        # buries the channel-level picture, which is what the operator acts on.
        no_base = [d for d in dots if d.get("baseline", 0.0) <= 0]
        blind = [d for d in dots
                 if d.get("baseline", 0.0) > 0
                 and d.get("dark_floor", 0.0) / d["baseline"] >= break_ratio]
        if no_base:
            warnings.append(
                f"{beam.id}: {len(no_base)}/{len(dots)} dot(s) read 0 at all_on — "
                f"found in the label pass but not lit in the measure pass. Either "
                f"the channel did not come back on, or the dot moved between "
                f"passes (camera nudged?)")
        if blind:
            worst = max(blind, key=lambda d: d["dark_floor"] / d["baseline"])
            warnings.append(
                f"{beam.id}: {len(blind)}/{len(dots)} dot(s) can never fire — worst "
                f"at ({worst['cx']},{worst['cy']}) floor {worst['dark_floor']:.0f} / "
                f"baseline {worst['baseline']:.0f} = "
                f"{worst['dark_floor'] / worst['baseline']:.2f}, at or above "
                f"break_ratio {break_ratio}. A neighbour is blooming into the ROI")

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

async def amain(args) -> int:
    cfg = loader.load_all()
    channels = [b for b in cfg.beams.beams
                if not args.channels or b.id in set(args.channels.split(","))]
    if not channels:
        log.error("no channels selected")
        return 1

    from iobackend.modbus import ModbusIOBackend
    from iobackend.presets import PresetResolver
    io = ModbusIOBackend(cfg.hardware)

    # Preflight the boards on connect_all()'s return value, not board.status —
    # status is initialised to "OK" and only becomes DISCONNECTED after a failed
    # transaction, so checking it before any traffic always passes. Without this
    # the sweep grinds through a Modbus timeout per write, 45 times, and the
    # failure only surfaces as an empty result at the end.
    connected = await io.connect_all()
    dead = sorted(bid for bid, ok in connected.items() if not ok)
    if dead:
        log.error("relay board(s) unreachable: %s", ", ".join(dead))
        log.error("power the boards and check config/hardware.yaml, then retry")
        return 2

    resolver = PresetResolver(cfg.mazes, cfg.hardware)

    cams = Cameras(cfg)
    await cams.start()
    sizes = {cid: (f.shape[1], f.shape[0])
             for cid, f in (await cams.median_capture(1)).items()}
    log.info("camera frame sizes: %s", sizes)

    try:
        results = await run_sweep(cfg, cams, resolver, io, channels, args)
    finally:
        await light_only(resolver, io, [])
        await cams.stop()

    errors, warnings = validate(cfg, results, channels)
    for w in warnings:
        log.warning("  %s", w)
    for e in errors:
        log.error("  %s", e)

    if errors and not args.force:
        log.error("NOT writing beams.json — %d error(s). Use --force to override.",
                  len(errors))
        return 1
    if args.dry_run:
        log.info("--dry-run: not writing. %d channel(s) would be updated.",
                 len([c for c in channels if results.get(c.id)]))
        return 0

    path = Path(loader.CONFIG_DIR) / "beams.json"
    backup = write_beams(path, results, channels, cams, sizes)
    log.info("wrote %s (backup: %s)", path, backup.name)
    try:
        loader.load_all()
        log.info("reloaded successfully — config is valid")
    except Exception as exc:
        shutil.copy2(backup, path)
        log.error("written config does not load (%s) — restored the backup", exc)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--channels", default="",
                    help="comma-separated channel ids to sweep, e.g. 11,12 "
                         "(default: all 45)")
    ap.add_argument("--dry-run", action="store_true", help="sweep but do not write")
    ap.add_argument("--force", action="store_true",
                    help="write even if validation reported errors")
    ap.add_argument("--write-refs", action="store_true",
                    help="save an all_on reference frame per camera to ref/")
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
