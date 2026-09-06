"""
tools/pick_rois.py — click ceiling dots to generate beams.json ROI stanzas.

Inputs:  --camera (RTSP URL) or --image (path to a reference frame PNG/JPG)
Outputs: JSON stanzas printed to stdout, ready to paste into config/beams.json.
Invariant: read-only; does not modify any file automatically.

Usage:
  python tools/pick_rois.py --camera rtsp://10.0.0.30:554/substream
  python tools/pick_rois.py --image ref/cam_a.png

Controls:
  Left-click  → mark a dot (draws circle, prints stanza)
  Right-click → undo last point
  Q           → quit and print all stanzas as a JSON array

Requires OpenCV with display support (not headless).
Works on a laptop — NOT on the headless NUC.

Note: auto-suggests r=9 (plan §4.2 default ROI radius).
      IDs start from the next available after existing beams in config/beams.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Check for OpenCV early and give a clear error
# ---------------------------------------------------------------------------
try:
    import cv2
    import numpy as np
except ImportError:
    print(
        "Error: OpenCV is not installed or not available with display support.\n"
        "Install it on your laptop (not the headless NUC):\n"
        "  pip install opencv-python\n"
        "Note: opencv-python-headless (in requirements.txt) does NOT support GUI.",
        file=sys.stderr,
    )
    sys.exit(1)

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_ROI_RADIUS = 9       # px — matches plan §4.2 default
DEFAULT_BREAK_RATIO = 0.40   # from plan §4.2 example
DEFAULT_CLEAR_RATIO = 0.65   # from plan §4.2 example
DEFAULT_BOARD_ID    = "board_01"
DEFAULT_CAMERA_ID   = "cam_left_front"
DEFAULT_CLUSTER     = 1

# Colour constants (BGR for OpenCV)
COLOUR_DOT    = (0, 229, 255)   # cyan — drawn circle
COLOUR_TEXT   = (255, 255, 255) # white label
COLOUR_UNDO   = (0, 0, 200)     # red-ish — undo highlight


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_existing_beams(beams_json_path: Path) -> list[dict]:
    """Load existing beams from config/beams.json if it exists."""
    if not beams_json_path.exists():
        return []
    try:
        with open(beams_json_path) as f:
            data = json.load(f)
        return data.get("beams", [])
    except (json.JSONDecodeError, OSError):
        return []


def next_beam_id(existing: list[dict]) -> int:
    """Return the next sequential beam number (e.g. 5 for b05)."""
    used = set()
    for b in existing:
        bid = b.get("id", "")
        if bid.startswith("b") and bid[1:].isdigit():
            used.add(int(bid[1:]))
    n = 1
    while n in used:
        n += 1
    return n


def make_stanza(
    seq_num: int,
    cx: int,
    cy: int,
    r: int,
    camera_id: str,
    board_id: str,
    cluster: int,
    relay_channel: int,
) -> dict:
    """Build a beams.json stanza dict for one dot."""
    return {
        "id": f"b{seq_num:02d}",
        "cluster": cluster,
        "relay_channel": relay_channel,
        "board_id": board_id,
        "camera": camera_id,
        "roi": {"cx": cx, "cy": cy, "r": r},
        "baseline": 0.0,       # filled at runtime during ARM blink
        "break_ratio": DEFAULT_BREAK_RATIO,
        "clear_ratio": DEFAULT_CLEAR_RATIO,
        "masked": False,
        "note": "",
    }


def draw_overlay(frame: np.ndarray, points: list[tuple[int, int, int]], seq_start: int) -> np.ndarray:
    """Draw circles and labels on a copy of the frame."""
    canvas = frame.copy()
    for i, (cx, cy, r) in enumerate(points):
        bid = f"b{seq_start + i:02d}"
        cv2.circle(canvas, (cx, cy), r, COLOUR_DOT, 2)
        cv2.circle(canvas, (cx, cy), 2, COLOUR_DOT, -1)  # centre dot
        cv2.putText(canvas, bid, (cx + r + 4, cy + 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, COLOUR_TEXT, 1, cv2.LINE_AA)
    return canvas


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    # ---- Load the source frame ----
    if args.image:
        frame = cv2.imread(str(args.image))
        if frame is None:
            print(f"Error: could not load image: {args.image}", file=sys.stderr)
            sys.exit(1)
    else:
        cap = cv2.VideoCapture(args.camera)
        if not cap.isOpened():
            print(f"Error: could not open camera: {args.camera}", file=sys.stderr)
            sys.exit(1)
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            print("Error: could not read a frame from the camera.", file=sys.stderr)
            sys.exit(1)
        print(f"Captured one frame from {args.camera}")

    # ---- Load existing beams for ID sequencing ----
    repo_root = Path(__file__).resolve().parent.parent
    beams_path = repo_root / "config" / "beams.json"
    existing   = load_existing_beams(beams_path)
    seq_start  = next_beam_id(existing)

    roi_r      = args.radius
    camera_id  = args.camera_id
    board_id   = args.board_id
    cluster    = args.cluster

    # Points recorded by clicks: list of (cx, cy, r)
    points: list[tuple[int, int, int]] = []

    window_name = "pick_rois — left-click=mark  right-click=undo  Q=done"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_name, min(frame.shape[1], 1280), min(frame.shape[0], 720))

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            points.append((x, y, roi_r))
            bid = f"b{seq_start + len(points) - 1:02d}"
            print(f"  Marked: {bid}  cx={x}  cy={y}  r={roi_r}")
        elif event == cv2.EVENT_RBUTTONDOWN and points:
            removed = points.pop()
            print(f"  Undone: b{seq_start + len(points):02d}  cx={removed[0]}  cy={removed[1]}")

    cv2.setMouseCallback(window_name, on_mouse)

    print(f"\nImage: {frame.shape[1]}×{frame.shape[0]}  |  Starting ID: b{seq_start:02d}  |  ROI radius: {roi_r}")
    print("Left-click to mark a dot.  Right-click to undo.  Press Q to finish.\n")

    while True:
        canvas = draw_overlay(frame, points, seq_start)

        # Instructions overlay
        cv2.putText(canvas,
                    f"Dots: {len(points)}  |  Left=mark  Right=undo  Q=done",
                    (10, canvas.shape[0] - 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, COLOUR_TEXT, 1, cv2.LINE_AA)

        cv2.imshow(window_name, canvas)
        key = cv2.waitKey(30) & 0xFF
        if key in (ord("q"), ord("Q"), 27):
            break

    cv2.destroyAllWindows()

    if not points:
        print("\nNo dots marked. Exiting.")
        return

    # ---- Build stanzas ----
    stanzas = []
    for i, (cx, cy, r) in enumerate(points):
        seq = seq_start + i
        # Relay channel: assign sequentially from 1 (user can edit afterwards)
        relay_channel = len(existing) + i + 1
        stanzas.append(make_stanza(
            seq_num=seq,
            cx=cx, cy=cy, r=r,
            camera_id=camera_id,
            board_id=board_id,
            cluster=cluster,
            relay_channel=relay_channel,
        ))

    # ---- Print output ----
    print("\n" + "=" * 60)
    print("# Ready to paste into config/beams.json  (under \"beams\": [])")
    print("=" * 60)
    print(json.dumps(stanzas, indent=2))
    print("=" * 60)
    print(f"\n{len(stanzas)} stanza(s) generated.")
    print("Tip: verify ROIs on /admin/beams overlay page before a run.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Click ceiling dots to generate beams.json ROI stanzas.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Requires OpenCV with display (GUI) support — install opencv-python, not
opencv-python-headless. Run this on a laptop, not the headless NUC.

Examples
--------
  python tools/pick_rois.py --image ref/cam_a.png
  python tools/pick_rois.py --camera rtsp://10.0.0.30:554/substream
  python tools/pick_rois.py --image ref/cam_a.png --radius 11 --cluster 2
        """,
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--camera", metavar="URL",
                        help="RTSP or HTTP URL of the camera stream. One frame is grabbed.")
    source.add_argument("--image",  metavar="PATH",
                        help="Path to a reference frame image (PNG, JPG, etc.).")

    parser.add_argument("--radius",    type=int,   default=DEFAULT_ROI_RADIUS,
                        help=f"ROI radius in pixels. Default: {DEFAULT_ROI_RADIUS}.")
    parser.add_argument("--camera-id", default=DEFAULT_CAMERA_ID,
                        help=f"Camera ID to embed in stanzas. Default: {DEFAULT_CAMERA_ID!r}.")
    parser.add_argument("--board-id",  default=DEFAULT_BOARD_ID,
                        help=f"Relay board ID to embed in stanzas. Default: {DEFAULT_BOARD_ID!r}.")
    parser.add_argument("--cluster",   type=int,   default=DEFAULT_CLUSTER,
                        help=f"Cluster number for all marked dots. Default: {DEFAULT_CLUSTER}.")
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
