"""
vision/baseline.py — manages rolling EMA baseline and flash-capture baseline per beam.

Inputs:  brightness samples from DotDetector; beam_id strings; config path for persistence
Outputs: current baseline float per beam; writes updated baselines to beams.json on save
Invariant: rolling EMA (alpha=0.01) runs only in ATTRACT state — call update_ema() only
           then. freeze() is called at RUN start; unfreeze() at RESET. Frozen baselines
           are never updated regardless of how many update_ema() calls arrive.
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
    Tracks per-beam brightness baselines with freeze/unfreeze semantics.

    During ATTRACT the rolling EMA keeps baselines current as haze density drifts.
    At RUN start the baselines are frozen so the algorithm cannot adapt to a broken
    beam mid-run. At RESET they thaw and EMA resumes.
    """

    def __init__(self, beams_config: BeamsConfig) -> None:
        self._baselines: dict[str, float] = {
            b.id: b.baseline for b in beams_config.beams
        }
        self._frozen: bool = False

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    def get(self, beam_id: str) -> float:
        """Return current baseline for beam_id. Returns 0.0 if unknown."""
        return self._baselines.get(beam_id, 0.0)

    def set(self, beam_id: str, value: float) -> None:
        """
        Directly set a baseline value (e.g. from flash-capture during count-in).
        Allowed even when frozen — flash capture is an intentional override.
        """
        self._baselines[beam_id] = value
        log.debug("BaselineManager: set beam='%s' baseline=%.1f", beam_id, value)

    # ------------------------------------------------------------------
    # EMA update (ATTRACT only)
    # ------------------------------------------------------------------

    def update_ema(self, beam_id: str, sample: float, alpha: float = _DEFAULT_ALPHA) -> None:
        """
        Update rolling EMA baseline for beam_id.

        Only effective when not frozen (i.e. in ATTRACT). During a run all
        update_ema() calls are silently dropped.
        """
        if self._frozen:
            return
        current = self._baselines.get(beam_id, sample)
        if current <= 0:
            # Bootstrap: accept first real sample as-is
            updated = sample
        else:
            updated = alpha * sample + (1.0 - alpha) * current
        self._baselines[beam_id] = updated
        log.debug(
            "BaselineManager: EMA beam='%s' sample=%.1f → %.1f",
            beam_id, sample, updated,
        )

    # ------------------------------------------------------------------
    # Freeze / unfreeze
    # ------------------------------------------------------------------

    def freeze(self) -> None:
        """
        Freeze all baselines. Called at RUN start.
        EMA updates are silently ignored while frozen.
        """
        if not self._frozen:
            log.info("BaselineManager: baselines frozen")
        self._frozen = True

    def unfreeze(self) -> None:
        """
        Resume EMA updates. Called at RESET (ATTRACT resumes).
        """
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
        Write current baselines back to beams.json.

        Reads the file, updates each beam's 'baseline' field, and writes it back
        atomically (write to .tmp then rename). Logs and continues on any error —
        a save failure must never interrupt gameplay.
        """
        path = Path(config_path)
        try:
            with open(path) as f:
                data = json.load(f)

            for beam in data.get("beams", []):
                beam_id = beam.get("id")
                if beam_id and beam_id in self._baselines:
                    beam["baseline"] = round(self._baselines[beam_id], 3)

            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
            tmp_path.replace(path)
            log.info("BaselineManager: saved baselines to %s", path)
        except Exception as exc:
            log.error("BaselineManager: save_to_config failed (%s) — continuing", exc)

    def all_baselines(self) -> dict[str, float]:
        """Return a copy of all current baselines. For admin portal and tests."""
        return dict(self._baselines)
