"""
vision/baseline.py — rolling EMA and flash-capture baselines, per dot per maze.

Inputs:  brightness samples from DotDetector; dot ids; config path for persistence
Outputs: current baseline float per dot; writes updated values back to beams.json
Invariant: the rolling EMA (alpha=0.01) runs only in ATTRACT — freeze() at RUN start,
           unfreeze() at RESET. Frozen baselines never move, however many samples
           arrive, so the detector cannot slowly accept a broken beam as normal.
Invariant: baselines are scoped to a maze. The same dot is a different brightness
           under maze_1 and maze_2, because its neighbours differ.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from config.loader import BeamsConfig

log = logging.getLogger(__name__)

_DEFAULT_ALPHA = 0.01  # ~30 s time constant at 30 fps


class BaselineManager:
    """
    Tracks per-dot brightness baselines with freeze/unfreeze semantics.

    During ATTRACT the rolling EMA keeps baselines current as haze density
    drifts. At RUN start they freeze, so the detector cannot adapt to a broken
    beam mid-run. At RESET they thaw and the EMA resumes.

    Values are keyed "<maze>/<dot_id>". A dot id is only unique within the
    camera that saw it, and its brightness only means something under the maze
    it was captured with — half the ceiling is dark in any given shape, so the
    light bouncing onto a dot changes with the shape.
    """

    def __init__(self, beams_config: BeamsConfig) -> None:
        self._baselines: dict[str, float] = {}
        for maze_name, rois in beams_config.mazes.items():
            for dot in rois.all_dots():
                self._baselines[f"{maze_name}/{dot.id}"] = dot.baseline
        self._frozen: bool = False
        self._maze: str | None = None

    def set_maze(self, maze: str | None) -> None:
        """Scope subsequent get/set/update_ema calls to this maze."""
        self._maze = maze

    def _key(self, dot_id: str) -> str:
        return f"{self._maze}/{dot_id}"

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    def get(self, dot_id: str) -> float:
        """Current baseline for dot_id in the scoped maze. 0.0 if unknown."""
        return self._baselines.get(self._key(dot_id), 0.0)

    def set(self, dot_id: str, value: float) -> None:
        """
        Set a baseline directly, e.g. from flash capture during count-in.
        Allowed while frozen — a flash capture is an intentional override.
        """
        self._baselines[self._key(dot_id)] = value
        log.debug("BaselineManager: set %s baseline=%.1f", self._key(dot_id), value)

    def for_maze(self, maze: str) -> dict[str, float]:
        """Every baseline belonging to one maze, keyed by bare dot id."""
        prefix = f"{maze}/"
        return {k[len(prefix):]: v for k, v in self._baselines.items()
                if k.startswith(prefix)}

    # ------------------------------------------------------------------
    # EMA update (ATTRACT only)
    # ------------------------------------------------------------------

    def update_ema(self, dot_id: str, sample: float,
                   alpha: float = _DEFAULT_ALPHA) -> None:
        """
        Fold one sample into the rolling baseline.

        Silently dropped while frozen, which is the whole point: during a run
        this is a no-op no matter how often it is called.
        """
        if self._frozen:
            return
        key = self._key(dot_id)
        current = self._baselines.get(key, sample)
        # Bootstrap: a dot with no usable baseline takes the first real sample.
        updated = sample if current <= 0 else alpha * sample + (1.0 - alpha) * current
        self._baselines[key] = updated

    # ------------------------------------------------------------------
    # Freeze / unfreeze
    # ------------------------------------------------------------------

    def freeze(self) -> None:
        """Freeze every baseline. Called at RUN start."""
        if not self._frozen:
            log.info("BaselineManager: baselines frozen")
        self._frozen = True

    def unfreeze(self) -> None:
        """Resume EMA updates. Called at RESET, when ATTRACT comes back."""
        if self._frozen:
            log.info("BaselineManager: baselines unfrozen")
        self._frozen = False

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_to_config(self, config_path: str) -> None:
        """
        Write current baselines back into beams.json, under each maze capture.

        Atomic: write .tmp, then rename. Logs and continues on any error — a
        save failure must never interrupt gameplay.
        """
        path = Path(config_path)
        try:
            with open(path) as f:
                data = json.load(f)

            for maze_name, maze in (data.get("mazes") or {}).items():
                for cam in (maze.get("cameras") or {}).values():
                    for dot in cam.get("dots", []):
                        key = f"{maze_name}/{dot.get('id')}"
                        if key in self._baselines:
                            dot["baseline"] = round(self._baselines[key], 3)

            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
            tmp_path.replace(path)
            log.info("BaselineManager: saved baselines to %s", path)
        except Exception as exc:
            log.error("BaselineManager: save_to_config failed (%s) — continuing", exc)

    def all_baselines(self) -> dict[str, float]:
        """A copy of every baseline, keyed '<maze>/<dot_id>'. Admin and tests."""
        return dict(self._baselines)
