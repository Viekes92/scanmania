"""
config/loader.py — loads and validates all config files at startup.

Inputs:  YAML/JSON files in config/
Outputs: typed dataclass instances passed to every service at startup.
Invariant: if this module raises, the process must not start. Bad config is always a startup error.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

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
    # The substream resolution this camera is SET to. ROIs are frame pixels, so
    # a change in the camera UI silently invalidates every saved dot; declaring
    # it here lets tools/capture.py catch the mismatch at capture time instead
    # of the game quietly sampling the wrong coordinates.
    w: int = 0
    h: int = 0
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
    # Which camera's frame these pixels belong to. Per DOT, not per channel:
    # the cameras' fields of view overlap, so a channel's 5 colinear dots can
    # straddle two of them. A single camera on the parent channel could not
    # express that, and the dots on the other camera would be sampled against
    # coordinates that mean nothing.
    camera: str = ""

    # Baselines are PER MAZE, because a dot's reading depends on which
    # neighbours are lit and the mazes differ a lot: measured at 46-53% of the
    # floor each, a dot with two lit neighbours in maze_1 and none in maze_3
    # reads meaningfully brighter in maze_1. One number cannot serve both.
    # Key is the preset name; a missing key means the dot is not lit in that
    # maze, and the watch-list means it is never evaluated there either.
    baselines: dict[str, float] = field(default_factory=dict)

    # Residual brightness in this ROI when its own channel is off but the rest
    # of that maze is lit. Must sit well below break_ratio or the dot can never
    # fire — neighbouring dots bloom into the ROI and hold it above threshold.
    dark_floors: dict[str, float] = field(default_factory=dict)

    # Single-value fallback for a config written before per-maze baselines, and
    # for setups with only one shape.
    baseline: float = 0.0
    dark_floor: float = 0.0

    def baseline_for(self, preset: str | None) -> float:
        """Baseline in the named maze, falling back to the flat value."""
        if preset and preset in self.baselines:
            return self.baselines[preset]
        return self.baseline

    def dark_floor_for(self, preset: str | None) -> float:
        if preset and preset in self.dark_floors:
            return self.dark_floors[preset]
        return self.dark_floor
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
class Dot:
    """
    One watched point on the ceiling.

    Detection is per DOT, not per relay channel. The calibration capture lights
    a whole maze and records whatever the cameras can see; which relay drives a
    given dot is never established and is not needed. A dot going dark means a
    beam was broken — knowing which beam adds nothing to the game.

    `id` is stable within a maze capture (cam_3:d17) so a bust can name what
    broke, a dot can be masked individually, and evidence can crop the region.
    """
    id: str
    cx: int
    cy: int
    r: int
    # Measured with this maze lit, so it is already the right reference for the
    # only condition this dot is ever evaluated in.
    baseline: float = 0.0
    masked: bool = False
    note: str = ""


@dataclass
class CameraCapture:
    """What one camera saw of one maze, and the settings that found it."""
    camera: str
    w: int                      # frame size the coordinates were measured at
    h: int
    params: dict                # detection params that produced these dots
    dots: list[Dot] = field(default_factory=list)


@dataclass
class MazeROIs:
    """Every dot watched while one maze is lit."""
    name: str
    captured_at: str = ""
    cameras: dict[str, CameraCapture] = field(default_factory=dict)

    def dots_for(self, camera_id: str) -> list[Dot]:
        cap = self.cameras.get(camera_id)
        return cap.dots if cap else []

    def all_dots(self) -> list[Dot]:
        return [d for cap in self.cameras.values() for d in cap.dots]

    @property
    def total(self) -> int:
        return sum(len(c.dots) for c in self.cameras.values())


@dataclass
class DetectionConfig:
    consecutive_frames: int
    arm_grace_ms: int
    stall_threshold_ms: int
    global_break_rate_limit: int
    flap_count_threshold: int
    flap_window_s: int
    # More than this many dots going dark at once is not a person. A body blocks
    # a handful; a relay failure, a preset change or a camera glitch kills
    # dozens. Replaces the old per-channel "all 5 of its dots dark" rule, which
    # needed a dot-to-channel mapping we no longer have. Sane range 6-25.
    max_simultaneous_breaks: int = 10
    # Hysteresis thresholds, as a fraction of a dot's baseline. These used to be
    # read from beams[0] — one entry of the 45-channel wiring record that
    # invariant 3 says is not read at runtime — so one entry supplied the
    # threshold for all ~225 dots and editing any other did nothing.
    break_ratio: float = 0.4
    clear_ratio: float = 0.65


@dataclass
class BeamsConfig:
    # The 45 relay channels. Kept as the wiring reference — which channel sits
    # on which board, which row and strip it is — but detection no longer uses
    # them. Dots are not mapped to channels.
    beams: list[BeamConfig]
    detection: DetectionConfig
    # preset name -> the dots watched while that maze is lit.
    mazes: dict[str, MazeROIs] = field(default_factory=dict)


@dataclass
class CountInPulse:
    on_ms: int
    off_ms: int


@dataclass
class CountInConfig:
    preset: str
    ready_blink_ms: int
    baseline_pulse_index: int          # parsed, unused at runtime — see game.yaml
    pulses: list[CountInPulse]
    solid_at_end: bool
    # What the ramp FLASHES, and what stays lit at ARM. Defaulted, so they go
    # last: a dataclass cannot put a defaulted field before a required one.
    flash_preset: str = "maze_1"
    arm_preset: str = "arm_box"


@dataclass
class LeaderboardConfig:
    scope: str
    show_busted: bool
    max_entries: int


@dataclass
class AudioConfig:
    """The container's soundtrack. Everything here degrades to silence."""
    enabled: bool = True
    device: str = ""
    sounds_dir: str = "sounds"
    music_volume: float = 0.6        # 0.0-1.0
    cue_volume: float = 0.9          # 0.0-1.0
    fade_ms: int = 400               # crossfade on a bed change. 0-5000 ms.
    # FSM state -> filename. A state absent from `music` keeps whatever is
    # playing; "silence" is how you ask for quiet.
    music: dict[str, str] = field(default_factory=dict)
    cues: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _real(names) -> list[str]:
        return sorted({n for n in names
                       if n and n.lower() not in ("silence", "none", "off")})

    def cue_files(self) -> list[str]:
        """One-shots. Decoded into RAM at startup, so keep these short .wav."""
        return self._real(self.cues.values())

    def music_files(self) -> list[str]:
        """Bed tracks. These stream, so a long one belongs in .mp3."""
        return self._real(self.music.values())

    def filenames(self) -> list[str]:
        """Every real file named here."""
        return self._real(list(self.music.values()) + list(self.cues.values()))


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
    # Boot into MASTER with the house lights up, so the GM must walk the
    # container and press FORCE RESET before anything is playable.
    boot_to_master: bool = True
    # How long the GM has to decide on an assisted-mode break before the system
    # defaults to BUST. Without it an undecided halt wedges the game.
    assisted_timeout_ms: int = 60_000
    # How long a signed-in player may idle before the run is cancelled.
    registered_timeout_ms: int = 180_000
    # Minutes between automatic DB snapshots. 0 disables. Sane range 5-240.
    snapshot_interval_min: int = 60
    # How many rolling snapshots to keep. Sane range 4-200.
    snapshot_keep: int = 48
    # Grace after a maze shape change before newly-lit channels can report a
    # break. They are physically still coming on. Sane range 100-500 ms.
    preset_settle_ms: int = 250
    # Pause between a checkpoint firing and the maze taking its next shape.
    # 0 keeps the old snap-immediately behaviour. Sane range 0-1000 ms.
    checkpoint_shape_delay_ms: int = 300
    # The soundtrack. Absent from game.yaml means silence, not an error.
    audio: AudioConfig = field(default_factory=AudioConfig)


@dataclass
class PresetConfig:
    channels: list[int] | str  # list of ints or "*"
    description: str = ""


@dataclass
class ShowStep:
    preset: str
    hold_ms: int = 300
    # Drop this many channels, chosen fresh each step, from the named preset —
    # the attract "sparkle". The runner has always understood it; this field
    # did not exist, so _run_show read it off the dataclass as 0 and the
    # attract show sat as a dead, fully-lit grid. Range 0-45, one per segment.
    sparkle_off: int = 0


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
    # Room-light cues keyed by FSM state (lower-case). Show content, kept
    # beside the laser shows because an operator tunes both in the same sitting.
    light_cues: dict = field(default_factory=dict)


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


def _ranged(value, lo, hi, default, name: str):
    """
    Clamp a numeric config value to its documented range, loudly.

    Every timing and threshold key carries a "Range:" comment that nothing
    enforced. `max_simultaneous_breaks: 0` loaded clean and suppressed every
    break forever, with the config file reading as if it were fine.
    """
    try:
        v = type(default)(value)
    except (TypeError, ValueError):
        log.error("config: %s=%r is not a number — using %r", name, value, default)
        return default
    if v < lo or v > hi:
        clamped = min(max(v, lo), hi)
        log.error("config: %s=%r is outside %r..%r — using %r",
                  name, v, lo, hi, clamped)
        return clamped
    return v


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
            w=c.get("w", 0),
            h=c.get("h", 0),
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
                camera=d.get("camera", b.get("camera", "")),
                baselines=d.get("baselines", {}) or {},
                dark_floors=d.get("dark_floors", {}) or {},
                baseline=d.get("baseline", 0.0),
                dark_floor=d.get("dark_floor", 0.0),
                masked=d.get("masked", False),
                note=d.get("note", ""),
            )
            for d in b["dots"]
        ]
    roi = b.get("roi")
    if not roi:
        return []
    return [DotROI(cx=roi["cx"], cy=roi["cy"], r=roi.get("r", 9),
                   camera=b.get("camera", ""),
                   baseline=b.get("baseline", 0.0))]


def _first_dot_as_roi(b: dict) -> BeamROI:
    """Legacy `roi` field for callers that still want one circle per channel."""
    dots = b.get("dots") or []
    if not dots:
        return BeamROI(cx=0, cy=0, r=9)
    d = dots[0]
    return BeamROI(cx=d["cx"], cy=d["cy"], r=d.get("r", 9))


def _parse_mazes(d: dict) -> dict[str, MazeROIs]:
    """
    Read the per-maze dot captures.

    Absent means uncalibrated, which is legal: the game refuses to arm rather
    than the config refusing to load, so an operator can still reach the admin
    portal and see why.
    """
    out: dict[str, MazeROIs] = {}
    for name, m in (d.get("mazes") or {}).items():
        cams: dict[str, CameraCapture] = {}
        for cam_id, c in (m.get("cameras") or {}).items():
            cams[cam_id] = CameraCapture(
                camera=cam_id,
                w=int(c.get("w", 0)),
                h=int(c.get("h", 0)),
                params=dict(c.get("params") or {}),
                dots=[
                    Dot(
                        id=dot.get("id") or f"{cam_id}:d{i}",
                        cx=int(dot["cx"]), cy=int(dot["cy"]),
                        r=int(dot.get("r", 9)),
                        baseline=float(dot.get("baseline", 0.0)),
                        masked=bool(dot.get("masked", False)),
                        note=dot.get("note", ""),
                    )
                    for i, dot in enumerate(c.get("dots") or [])
                ],
            )
        out[name] = MazeROIs(name=name,
                             captured_at=m.get("captured_at", ""),
                             cameras=cams)
    return out


def load_beams(path: Path | None = None) -> BeamsConfig:
    """
    Load beams.json. `path` points at a different file — the calibration tool
    uses it to prove a freshly written file parses BEFORE it becomes the live
    one. A beams.json the loader rejects takes the game down at next restart.
    """
    if path is None:
        d = _load_json("beams.json")
    else:
        with open(path) as f:
            d = json.load(f)
    beams = [
        BeamConfig(
            id=b["id"],
            relay_channel=b["relay_channel"],
            board_id=b["board_id"],
            camera=b.get("camera", ""),   # vestigial; see ADR 0009
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
        mazes=_parse_mazes(d),
        detection=DetectionConfig(
            consecutive_frames=det.get("consecutive_frames", 3),
            arm_grace_ms=det.get("arm_grace_ms", 150),
            stall_threshold_ms=det.get("stall_threshold_ms", 300),
            global_break_rate_limit=det.get("global_break_rate_limit", 6),
            flap_count_threshold=det.get("flap_count_threshold", 5),
            flap_window_s=det.get("flap_window_s", 60),
            max_simultaneous_breaks=_ranged(
                det.get("max_simultaneous_breaks", 10), 1, 100, 10,
                "detection.max_simultaneous_breaks"),
            **_ratios(det),
        ),
    )


# The FSM hard-codes this at RampComplete. game.yaml tells the operator that
# count_in.preset "MUST match the preset that will be active at GO", but the GO
# preset is not in config at all, so nothing could check it — and a mismatch
# captures the baseline against one maze shape and then lights another, which
# poisons every dot whose lit neighbours differ. Busts and misses, both.
_GO_PRESET = "maze_1"


def _clean_channels(raw, name: str) -> list[int] | str:
    """
    Validate a preset's channel list.

    It had no type, range or duplicate check at all, and the two failure paths
    were both bad: an out-of-range channel resolved to a silently dark board,
    and `channels: all` (a plausible typo for '*') raised TypeError out of
    resolve(), killing whatever task was applying the preset and freezing the
    lasers on the previous step.
    """
    if isinstance(raw, str):
        if raw == "*":
            return raw
        log.error("config: preset %s channels=%r is not a list or '*' — "
                  "treating as empty", name, raw)
        return []
    if not isinstance(raw, list):
        log.error("config: preset %s channels must be a list — treating as empty",
                  name)
        return []
    out: list[int] = []
    for c in raw:
        if not isinstance(c, int) or isinstance(c, bool):
            log.error("config: preset %s has a non-integer channel %r — skipped",
                      name, c)
            continue
        if c < 1:
            log.error("config: preset %s channel %d is below 1 — skipped", name, c)
            continue
        if c in out:
            log.warning("config: preset %s lists channel %d twice", name, c)
            continue
        out.append(c)
    return out


def _clean_hold_ms(raw, name: str) -> int:
    """
    Bound a show step's hold.

    Only the admin API bounded this (50 ms-60 s), so a hand-edited mazes.yaml —
    the documented way to change shows, and the file that is routinely modified
    on the box — bypassed it. hold_ms: 0 on a looping show spins apply_preset as
    fast as Modbus accepts, starving the loop the stopwatch shares; a quoted
    "500" reached asyncio.sleep(str) and killed the show task silently.
    """
    try:
        v = int(raw)
    except (TypeError, ValueError):
        log.error("config: show %s hold_ms=%r is not a number — using 300", name, raw)
        return 300
    return _ranged(v, 50, 60_000, 300, f"show {name} hold_ms")


def _check_count_in_preset(preset: str) -> None:
    if preset != _GO_PRESET:
        log.error("config: count_in.preset=%r but the FSM lights %r at GO — "
                  "the baseline will be captured against the wrong maze shape",
                  preset, _GO_PRESET)


def _check_settle_vs_reconcile(settle_ms: int) -> None:
    """
    The settle window must outlast a reconcile cycle plus a write round trip.

    ApplyPreset deliberately ignores its return value because "reconciliation
    repairs failures" — but the reconciler runs every 500 ms and then needs a
    read and a write. With a 250 ms settle the detector re-armed while a failed
    relay write was still unrepaired: the dots for the segment that never fired
    read dark, stayed under max_simultaneous_breaks so they were NOT suppressed
    as a hardware fault, and the player was busted for a relay that did not
    close.
    """
    from iobackend.reconcile import _RECONCILE_INTERVAL_S
    floor_ms = int(_RECONCILE_INTERVAL_S * 1000) + 250
    if settle_ms < floor_ms:
        log.error("config: game.preset_settle_ms=%d is below the %d ms a "
                  "reconcile cycle needs — a failed relay write will bust the "
                  "player instead of being repaired", settle_ms, floor_ms)


def _one_of(value, allowed: tuple, default: str, key: str) -> str:
    """
    Coerce to one of `allowed`, or fall back loudly.

    detection_mode reached the FSM unchecked, and its branch is
    `if auto ... elif assisted ... else: manual`. So "Assisted", "automatic" or
    a trailing space silently meant MANUAL — nobody is ever busted — while the
    GM console displayed the string and looked configured.
    """
    v = str(value).strip().lower()
    if v in allowed:
        return v
    log.error("config: %s=%r is not one of %s — using %r",
              key, value, "/".join(allowed), default)
    return default


def _ratios(det: dict) -> dict:
    """
    Validate the thresholds every dot is actually judged by.

    load_all enforces break < clear on the 45 wiring entries in beams.json,
    which invariant 3 says are not read at runtime — while these, the values
    the detector really uses, had no range check and no ordering check at all.
    Inverted or overlapping ratios make every dot near the boundary flap dark /
    clear / dark at the hysteresis cadence, so runs end at random.
    """
    br = float(det.get("break_ratio", 0.4))
    cl = float(det.get("clear_ratio", 0.65))
    br = _ranged(br, 0.01, 0.99, 0.4, "detection.break_ratio")
    cl = _ranged(cl, 0.02, 1.0, 0.65, "detection.clear_ratio")
    if br >= cl:
        log.error("config: detection.break_ratio (%.2f) must be BELOW "
                  "clear_ratio (%.2f) or every dot flaps — using defaults",
                  br, cl)
        br, cl = 0.4, 0.65
    return {"break_ratio": br, "clear_ratio": cl}


def _parse_audio(d: dict) -> AudioConfig:
    """
    Read the audio block. Never raises: a bad value falls back to the default.

    Sound is decoration. A typo in a volume must not stop the box booting into
    a playable state at a venue.
    """
    def _vol(key: str, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(d.get(key, default))))
        except (TypeError, ValueError):
            log.warning("config: audio.%s is not a number — using %s", key, default)
            return default

    def _names(key: str) -> dict[str, str]:
        raw = d.get(key) or {}
        if not isinstance(raw, dict):
            log.warning("config: audio.%s must be a mapping — ignoring", key)
            return {}
        return {str(k).upper(): str(v) for k, v in raw.items() if v is not None}

    return AudioConfig(
        enabled=bool(d.get("enabled", True)),
        device=str(d.get("device", "") or ""),
        sounds_dir=str(d.get("sounds_dir", "sounds") or "sounds"),
        music_volume=_vol("music_volume", 0.6),
        cue_volume=_vol("cue_volume", 0.9),
        fade_ms=_ranged(d.get("fade_ms", 400), 0, 5_000, 400, "audio.fade_ms"),
        music=_names("music"),
        cues=_names("cues"),
    )


def load_game() -> GameConfig:
    d = _load_yaml("game.yaml")
    _ci = d.get("count_in", {}) or {}
    _check_count_in_preset(_ci.get("preset", "maze_1"))
    _check_settle_vs_reconcile(int(d.get("preset_settle_ms", 800) or 800))
    ci = d.get("count_in", {})
    pulses = [CountInPulse(on_ms=p[0], off_ms=p[1]) for p in ci.get("pulses", [])]
    lb = d.get("leaderboard", {})
    return GameConfig(
        mode=d.get("mode", "hard_cutoff"),
        max_run_ms=_ranged(d.get("max_run_ms", 180000),
                           10_000, 3_600_000, 180000, "game.max_run_ms"),
        result_display_ms=_ranged(d.get("result_display_ms", 15000),
                                  1_000, 120_000, 15000, "game.result_display_ms"),
        arm_timeout_ms=_ranged(d.get("arm_timeout_ms", 180000),
                               10_000, 3_600_000, 180000, "game.arm_timeout_ms"),
        boot_to_master=bool(d.get("boot_to_master", True)),
        checkpoint_shape_delay_ms=_ranged(
            d.get("checkpoint_shape_delay_ms", 300), 0, 1_000, 300,
            "game.checkpoint_shape_delay_ms"),
        audio=_parse_audio(d.get("audio") or {}),
        assisted_timeout_ms=_ranged(d.get("assisted_timeout_ms", 60000),
                                    10_000, 300_000, 60000, "game.assisted_timeout_ms"),
        registered_timeout_ms=_ranged(d.get("registered_timeout_ms", 180000),
                                      30_000, 600_000, 180000,
                                      "game.registered_timeout_ms"),
        detection_mode=_one_of(d.get("detection_mode", "assisted"),
                               ("auto", "assisted", "manual"), "assisted",
                               "game.detection_mode"),
        show=d.get("show", "main_game"),
        count_in=CountInConfig(
            preset=ci.get("preset", "maze_1"),
            ready_blink_ms=ci.get("ready_blink_ms", 150),
            baseline_pulse_index=min(
                max(0, int(ci.get("baseline_pulse_index", 0))),
                max(len(pulses) - 1, 0)),
            pulses=pulses,
            solid_at_end=ci.get("solid_at_end", True),
            flash_preset=str(ci.get("flash_preset", ci.get("preset", "maze_1"))),
            arm_preset=str(ci.get("arm_preset", "arm_box")),
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
        presets[name] = PresetConfig(
            channels=_clean_channels(p.get("channels", []), name),
            description=p.get("description", ""),
        )
    shows = {}
    for name, s in d.get("shows", {}).items():
        # New format: steps with timing
        if "steps" in s:
            steps = [
                ShowStep(
                    preset=st.get("preset", "blackout"),
                    hold_ms=_clean_hold_ms(st.get("hold_ms", 300), name),
                    sparkle_off=_ranged(st.get("sparkle_off", 0), 0, 45, 0,
                                        f"shows.{name}.sparkle_off"),
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
    return MazesConfig(presets=presets, shows=shows,
                       light_cues=d.get("light_cues") or {})


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
        # A channel entry's `camera` field is NOT validated. These 45 entries are
        # the relay wiring record; detection reads the per-maze captures under
        # `mazes` instead (ADR 0009), and the field is a leftover from when a
        # channel owned its dots. Enforcing it would only block renaming a
        # camera until someone hand-edited 45 lines that nothing reads.
        if beam.break_ratio >= beam.clear_ratio:
            raise ValueError(
                f"Beam {beam.id}: break_ratio ({beam.break_ratio}) must be < clear_ratio ({beam.clear_ratio})"
            )

    # The captures ARE validated: these camera ids route live frames to ROIs, so
    # a stale one means dots sampled against a frame they do not belong to.
    for maze_name, rois in beams.mazes.items():
        for cam_id in rois.cameras:
            if camera_ids and cam_id not in camera_ids:
                raise ValueError(
                    f"beams.json: maze '{maze_name}' has a capture for camera "
                    f"'{cam_id}' which is not in hardware.yaml (known: "
                    f"{sorted(camera_ids)}). Recapture that maze."
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
    )
