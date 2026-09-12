"""
config/loader.py — loads and validates all config files at startup.

Inputs:  YAML/JSON files in config/
Outputs: typed dataclass instances passed to every service at startup.
Invariant: if this module raises, the process must not start. Bad config is always a startup error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

CONFIG_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RelayBoard:
    id: str
    ip: str
    port: int
    channels: int
    timeout_ms: int
    note: str = ""


@dataclass
class CameraConfig:
    id: str
    url: str
    reference_frame: str
    note: str = ""


@dataclass
class HazerConfig:
    board_id: Optional[str] = None    # legacy relay-based hazer
    channel: Optional[int] = None
    artnet_ip: Optional[str] = None   # Art-Net DMX hazer
    universe: int = 0
    dmx_channel: int = 1
    fan_channel: int = 0
    default_intensity: int = 128
    default_fan: int = 200
    enabled: bool = True


@dataclass
class NetworkConfig:
    subnet: str
    nuc_ip: str
    web_port: int


@dataclass
class HardwareConfig:
    relay_boards: list[RelayBoard]
    cameras: list[CameraConfig]
    network: NetworkConfig
    hazer: dict | None = None     # hazer config (Art-Net DMX or relay)
    inputs: dict | None = None   # Arduino Opta input module config


@dataclass
class BeamROI:
    cx: int
    cy: int
    r: int


@dataclass
class DotROI:
    """
    One laser dot on the ceiling.

    A relay channel drives 5 colinear dots, so a channel entry carries 5 of
    these. The older schema had a single `roi` per channel, which could only
    describe one of the five.

    ROIs are frame pixels in one camera's view. capture_w/h on the parent
    channel record the resolution they were measured at — a substream
    resolution change silently invalidates them otherwise.
    """
    cx: int
    cy: int
    r: int
    baseline: float = 0.0
    masked: bool = False        # one dead laser should not retire the other four
    note: str = ""


@dataclass
class BeamConfig:
    id: str
    relay_channel: int
    board_id: str
    camera: str
    roi: BeamROI                       # legacy single ROI; kept for the admin overlay
    baseline: float
    break_ratio: float
    clear_ratio: float
    masked: bool
    # The 5 dots this relay channel drives. Defaulted so older constructors
    # keep working while beams.json is migrated channel by channel.
    dots: list[DotROI] = field(default_factory=list)
    capture_w: int = 0                 # frame size the ROIs were measured at
    capture_h: int = 0
    row: int = 0                       # grid row (1-9)
    strip: int = 0                     # strip within row (1-5)
    segment: int = 1                   # which game segment (1-3)
    strip_type: str = ""               # H-bottom, V-right, H-center, V-left, H-top
    cluster: int = 0                   # legacy; kept for compat
    masked_at: Optional[str] = None
    masked_reason: Optional[str] = None
    note: str = ""


@dataclass
class DetectionConfig:
    consecutive_frames: int
    arm_grace_ms: int
    stall_threshold_ms: int
    global_break_rate_limit: int
    flap_count_threshold: int
    flap_window_s: int


@dataclass
class BeamsConfig:
    beams: list[BeamConfig]
    detection: DetectionConfig


@dataclass
class CountInPulse:
    on_ms: int
    off_ms: int


@dataclass
class CountInConfig:
    preset: str
    ready_blink_ms: int
    baseline_pulse_index: int
    pulses: list[CountInPulse]
    solid_at_end: bool


@dataclass
class LeaderboardConfig:
    scope: str
    show_busted: bool
    max_entries: int


@dataclass
class GameConfig:
    mode: str
    max_run_ms: int
    result_display_ms: int
    arm_timeout_ms: int
    detection_mode: str
    show: str
    count_in: CountInConfig
    arm_grace_ms: int
    leaderboard: LeaderboardConfig
    # Minutes between automatic DB snapshots. 0 disables. Sane range 5-240.
    snapshot_interval_min: int = 60
    # How many rolling snapshots to keep. Sane range 4-200.
    snapshot_keep: int = 48
    # Grace after a maze shape change before newly-lit channels can report a
    # break. They are physically still coming on. Sane range 100-500 ms.
    preset_settle_ms: int = 250


@dataclass
class PresetConfig:
    channels: list[int] | str  # list of ints or "*"
    description: str = ""


@dataclass
class ShowStep:
    preset: str
    hold_ms: int = 300


@dataclass
class ShowConfig:
    steps: list[ShowStep]
    loop: bool = False
    description: str = ""
    # Legacy: segments field for backwards compat with main_game show
    segments: list[str] | None = None


@dataclass
class MazesConfig:
    presets: dict[str, PresetConfig]
    shows: dict[str, ShowConfig]


@dataclass
class AppConfig:
    hardware: HardwareConfig
    beams: BeamsConfig
    game: GameConfig
    mazes: MazesConfig
    # preset name -> the channel ids lit by that preset. Detection watches
    # exactly these and ignores the rest, so a maze shape change is not a
    # beam break: the dots that vanish are simply no longer being asked about.
    watchlists: dict[str, frozenset[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    with open(path) as f:
        return yaml.safe_load(f)


def _load_json(name: str) -> dict:
    path = CONFIG_DIR / name
    with open(path) as f:
        return json.load(f)


def load_hardware() -> HardwareConfig:
    d = _load_yaml("hardware.yaml")
    boards = [
        RelayBoard(
            id=b["id"],
            ip=b["ip"],
            port=b.get("port", 502),
            channels=b.get("channels", 16),
            timeout_ms=b.get("timeout_ms", 200),
            note=b.get("note", ""),
        )
        for b in d.get("relay_boards", [])
    ]
    cameras = [
        CameraConfig(
            id=c["id"],
            url=c["url"],
            reference_frame=c["reference_frame"],
            note=c.get("note", ""),
        )
        for c in d.get("cameras", [])
    ]
    net = d.get("network", {})
    hazer_d = d.get("hazer", {})
    return HardwareConfig(
        relay_boards=boards,
        cameras=cameras,
        network=NetworkConfig(
            subnet=net.get("subnet", "10.0.0.0/24"),
            nuc_ip=net.get("nuc_ip", "172.16.0.1"),
            web_port=net.get("web_port", 8000),
        ),
        hazer=hazer_d,  # pass raw dict — __main__.py reads it directly
        inputs=d.get("inputs"),
    )


def _parse_dots(b: dict) -> list[DotROI]:
    """
    Read a channel's dot list.

    Accepts the old single-`roi` form so a half-migrated beams.json still loads:
    it becomes a one-dot list. Once the sweep tool has run, every channel should
    carry 5 dots — validate_config() reports the ones that do not.
    """
    if "dots" in b:
        return [
            DotROI(
                cx=d["cx"], cy=d["cy"], r=d.get("r", 9),
                baseline=d.get("baseline", 0.0),
                masked=d.get("masked", False),
                note=d.get("note", ""),
            )
            for d in b["dots"]
        ]
    roi = b.get("roi")
    if not roi:
        return []
    return [DotROI(cx=roi["cx"], cy=roi["cy"], r=roi.get("r", 9),
                   baseline=b.get("baseline", 0.0))]


def _first_dot_as_roi(b: dict) -> BeamROI:
    """Legacy `roi` field for callers that still want one circle per channel."""
    dots = b.get("dots") or []
    if not dots:
        return BeamROI(cx=0, cy=0, r=9)
    d = dots[0]
    return BeamROI(cx=d["cx"], cy=d["cy"], r=d.get("r", 9))


def load_beams() -> BeamsConfig:
    d = _load_json("beams.json")
    beams = [
        BeamConfig(
            id=b["id"],
            relay_channel=b["relay_channel"],
            board_id=b["board_id"],
            camera=b["camera"],
            roi=BeamROI(**b["roi"]) if "roi" in b else _first_dot_as_roi(b),
            dots=_parse_dots(b),
            capture_w=b.get("capture_w", 0),
            capture_h=b.get("capture_h", 0),
            baseline=b.get("baseline", 0.0),
            break_ratio=b["break_ratio"],
            clear_ratio=b["clear_ratio"],
            masked=b.get("masked", False),
            row=b.get("row", 0),
            strip=b.get("strip", 0),
            segment=b.get("segment", 1),
            strip_type=b.get("type", ""),
            cluster=b.get("cluster", 0),
            masked_at=b.get("masked_at"),
            masked_reason=b.get("masked_reason"),
            note=b.get("note", ""),
        )
        for b in d.get("beams", [])
    ]
    det = d.get("detection", {})
    return BeamsConfig(
        beams=beams,
        detection=DetectionConfig(
            consecutive_frames=det.get("consecutive_frames", 3),
            arm_grace_ms=det.get("arm_grace_ms", 150),
            stall_threshold_ms=det.get("stall_threshold_ms", 300),
            global_break_rate_limit=det.get("global_break_rate_limit", 6),
            flap_count_threshold=det.get("flap_count_threshold", 5),
            flap_window_s=det.get("flap_window_s", 60),
        ),
    )


def load_game() -> GameConfig:
    d = _load_yaml("game.yaml")
    ci = d.get("count_in", {})
    pulses = [CountInPulse(on_ms=p[0], off_ms=p[1]) for p in ci.get("pulses", [])]
    lb = d.get("leaderboard", {})
    return GameConfig(
        mode=d.get("mode", "hard_cutoff"),
        max_run_ms=d.get("max_run_ms", 180000),
        result_display_ms=d.get("result_display_ms", 15000),
        arm_timeout_ms=d.get("arm_timeout_ms", 180000),
        detection_mode=d.get("detection_mode", "assisted"),
        show=d.get("show", "main_game"),
        count_in=CountInConfig(
            preset=ci.get("preset", "maze_1"),
            ready_blink_ms=ci.get("ready_blink_ms", 150),
            baseline_pulse_index=min(ci.get("baseline_pulse_index", 0), max(len(pulses) - 1, 0)),
            pulses=pulses,
            solid_at_end=ci.get("solid_at_end", True),
        ),
        arm_grace_ms=d.get("arm_grace_ms", 150),
        snapshot_interval_min=max(0, int(d.get("snapshot_interval_min", 60))),
        snapshot_keep=max(1, int(d.get("snapshot_keep", 48))),
        preset_settle_ms=max(0, int(d.get("preset_settle_ms", 250))),
        leaderboard=LeaderboardConfig(
            scope=lb.get("scope", "daily"),
            show_busted=lb.get("show_busted", False),
            max_entries=lb.get("max_entries", 20),
        ),
    )


def load_mazes() -> MazesConfig:
    d = _load_yaml("mazes.yaml")
    presets = {}
    for name, p in d.get("presets", {}).items():
        ch = p["channels"]
        presets[name] = PresetConfig(
            channels=ch if isinstance(ch, str) else list(ch),
            description=p.get("description", ""),
        )
    shows = {}
    for name, s in d.get("shows", {}).items():
        # New format: steps with timing
        if "steps" in s:
            steps = [
                ShowStep(
                    preset=st.get("preset", "blackout"),
                    hold_ms=st.get("hold_ms", 300),
                )
                for st in s["steps"]
            ]
            shows[name] = ShowConfig(
                steps=steps,
                loop=s.get("loop", False),
                description=s.get("description", ""),
            )
        else:
            # Legacy format: segments list (convert to single-frame steps)
            segs = s.get("segments", [])
            shows[name] = ShowConfig(
                steps=[],
                loop=False,
                segments=list(segs),
                description=s.get("description", ""),
            )
    return MazesConfig(presets=presets, shows=shows)


def _global_channel(beam: BeamConfig, board_order: list[str], per_board: int = 16) -> int | None:
    """
    beams.json stores relay_channel LOCAL to a board (1-15).
    mazes.yaml presets list GLOBAL channels (1-48). Convert.

    global = board_index * per_board + local_channel
    """
    try:
        idx = board_order.index(beam.board_id)
    except ValueError:
        return None
    return idx * per_board + beam.relay_channel


def build_watchlists(beams: BeamsConfig, mazes: MazesConfig,
                     hardware: HardwareConfig) -> dict[str, frozenset[str]]:
    """
    Join mazes.yaml (which channels a preset lights) with beams.json (which
    channel each entry is) to get the set of channel ids to watch per preset.

    This is the whole answer to "how do we tell a shape change from a break":
    when the maze switches, the watch-list switches with it. A dot that goes
    dark because its relay opened is not in the new list, so nothing looks at
    it and nothing reports it.

    No extra config to author — both halves already exist.
    """
    board_order = [b.id for b in hardware.relay_boards]
    by_global: dict[int, str] = {}
    for beam in beams.beams:
        g = _global_channel(beam, board_order)
        if g is not None:
            by_global[g] = beam.id

    out: dict[str, frozenset[str]] = {}
    for name, preset in mazes.presets.items():
        if preset.channels == "*":
            out[name] = frozenset(by_global.values())
            continue
        out[name] = frozenset(
            by_global[c] for c in (preset.channels or []) if c in by_global
        )
    return out


def load_all() -> AppConfig:
    """Load and validate all config. Raises on any error."""
    hardware = load_hardware()
    beams = load_beams()
    game = load_game()
    mazes = load_mazes()

    # Cross-config validation
    board_ids = {b.id for b in hardware.relay_boards}
    camera_ids = {c.id for c in hardware.cameras}
    for beam in beams.beams:
        if beam.board_id not in board_ids and board_ids:
            raise ValueError(
                f"Beam {beam.id} references board_id '{beam.board_id}' "
                f"which is not in hardware.yaml (known: {board_ids})"
            )
        if beam.camera not in camera_ids and camera_ids:
            raise ValueError(
                f"Beam {beam.id} references camera '{beam.camera}' "
                f"which is not in hardware.yaml (known: {camera_ids})"
            )
        if beam.break_ratio >= beam.clear_ratio:
            raise ValueError(
                f"Beam {beam.id}: break_ratio ({beam.break_ratio}) must be < clear_ratio ({beam.clear_ratio})"
            )

    show = mazes.shows.get(game.show)
    if show is None:
        raise ValueError(f"game.yaml references show '{game.show}' which is not defined in mazes.yaml")
    for seg in (show.segments or []):
        if seg not in mazes.presets:
            raise ValueError(f"Show '{game.show}' references preset '{seg}' which is not defined in mazes.yaml")
    for step in (show.steps or []):
        if step.preset and step.preset not in mazes.presets:
            raise ValueError(f"Show '{game.show}' step references preset '{step.preset}' which is not defined in mazes.yaml")

    # Validate all show step presets
    for show_name, s in mazes.shows.items():
        for step in (s.steps or []):
            if step.preset and step.preset not in mazes.presets:
                raise ValueError(f"Show '{show_name}' step references preset '{step.preset}' not in mazes.yaml")

    if game.count_in.preset not in mazes.presets:
        raise ValueError(
            f"game.yaml count_in.preset '{game.count_in.preset}' is not defined in mazes.yaml"
        )

    return AppConfig(
        hardware=hardware, beams=beams, game=game, mazes=mazes,
        watchlists=build_watchlists(beams, mazes, hardware),
    )
