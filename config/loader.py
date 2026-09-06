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
    board_id: Optional[str]
    channel: Optional[int]


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
    hazer: HazerConfig
    inputs: dict | None = None  # Arduino Opta input module config


@dataclass
class BeamROI:
    cx: int
    cy: int
    r: int


@dataclass
class BeamConfig:
    id: str
    relay_channel: int
    board_id: str
    camera: str
    roi: BeamROI
    baseline: float
    break_ratio: float
    clear_ratio: float
    masked: bool
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
            nuc_ip=net.get("nuc_ip", "10.0.0.1"),
            web_port=net.get("web_port", 8000),
        ),
        hazer=HazerConfig(
            board_id=hazer_d.get("board_id"),
            channel=hazer_d.get("channel"),
        ),
        inputs=d.get("inputs"),
    )


def load_beams() -> BeamsConfig:
    d = _load_json("beams.json")
    beams = [
        BeamConfig(
            id=b["id"],
            relay_channel=b["relay_channel"],
            board_id=b["board_id"],
            camera=b["camera"],
            roi=BeamROI(**b["roi"]),
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

    return AppConfig(hardware=hardware, beams=beams, game=game, mazes=mazes)
