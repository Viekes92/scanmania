"""
tools/fake_run.py — drive a full game run from the CLI using fake backends.

Inputs:  --scenario (clean | busted | aborted | voided), optional --beam
Outputs: final run record from the DB printed as formatted JSON to stdout
Invariant: all fakes active — no real hardware required.

Usage:
  python tools/fake_run.py --scenario clean
  python tools/fake_run.py --scenario busted [--beam b01]
  python tools/fake_run.py --scenario aborted
  python tools/fake_run.py --scenario voided
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

# Ensure the repo root is on sys.path when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import load_all, AppConfig
from core.events import (
    PlayerRegistered, PlateHigh, CountInRequested, RampComplete,
    Cp1Pressed, Cp2Pressed, StopPressed, BreakConfirmed, GmVoid,
    GmForceReset, GmConfirmBreak, ATTRACT, ARM, RUN_SEG_1, RESULT, RESET,
)

log = logging.getLogger("fake_run")

# ---------------------------------------------------------------------------
# Minimal fakes for the standalone script
# (The real fakes in io/fake.py etc. exist but require the full runner wiring.
# Here we provide just enough to drive events through the FSM without a server.)
# ---------------------------------------------------------------------------

class FakeStateWatcher:
    """
    Receives state-change callbacks so we can await a target state.
    Hooks into the runner's broadcast mechanism.
    """

    def __init__(self) -> None:
        self._state = ATTRACT
        self._waiters: list[asyncio.Future] = []
        self._targets: list[str] = []

    def on_state(self, state: str) -> None:
        self._state = state
        for fut, target in zip(self._waiters, self._targets):
            if state == target and not fut.done():
                fut.set_result(state)

    async def wait_for(self, target: str, timeout: float = 30.0) -> str:
        if self._state == target:
            return target
        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        self._waiters.append(fut)
        self._targets.append(target)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Timed out waiting for state={target!r} (current={self._state!r})")
        finally:
            idx = self._waiters.index(fut)
            self._waiters.pop(idx)
            self._targets.pop(idx)


# ---------------------------------------------------------------------------
# In-memory DB replacement for the script
# ---------------------------------------------------------------------------

class MemoryDB:
    """Simple in-memory store so the script needs no real SQLite."""

    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}
        self.players: dict[str, dict] = {}

    async def insert_player(self, player_id: str, nickname: str) -> None:
        self.players[player_id] = {"id": player_id, "nickname": nickname}

    async def insert_run(self, run_id: str, **kwargs) -> None:
        """
        Mirror persist/db.py: a second insert on the same id is a conflict.

        This used to be a forgiving upsert, which is how the GM void bug hid in
        plain sight — `--scenario voided` passed while production swallowed an
        IntegrityError and left the run marked 'clean'. A fake that is kinder
        than production tests nothing. Keep this strict.
        """
        if run_id in self.runs:
            raise RuntimeError(
                f"duplicate run id {run_id!r} — production raises IntegrityError here"
            )
        self.runs[run_id] = {"id": run_id, "voided_reason": None,
                             "pre_void_outcome": None, **kwargs}

    async def void_run(self, run_id: str, reason: str) -> None:
        """Mirror persist/db.py::void_run — idempotent update, preserves prior outcome."""
        row = self.runs.get(run_id)
        if row is None:
            raise RuntimeError(f"void_run on unknown run id {run_id!r}")
        if row.get("outcome") != "voided":
            row["pre_void_outcome"] = row.get("outcome")
        row["outcome"] = "voided"
        row["voided_reason"] = reason

    async def get_run(self, run_id: str) -> dict | None:
        return self.runs.get(run_id)


# ---------------------------------------------------------------------------
# Minimal standalone runner (drives FSM + fakes; no web server, no asyncio
# networking — just pure event injection)
# ---------------------------------------------------------------------------

# Set from --silent before the runner is constructed.
_SILENT = False


def _make_audio(cfg):
    """
    Wire up the soundtrack, or return None.

    On by default: the whole point of driving a run from the CLI is to see —
    and now hear — what the container does. --silent turns it off, and so does
    a box with no sound card, all by itself.
    """
    acfg = getattr(cfg.game, "audio", None)
    if acfg is None or not acfg.enabled or _SILENT:
        return None
    try:
        from audio.cues import AudioCuePlayer
        from audio.player import AudioPlayer
        sounds = Path(__file__).resolve().parent.parent / acfg.sounds_dir
        player = AudioPlayer(sounds_dir=sounds,
                             music_volume=acfg.music_volume,
                             cue_volume=acfg.cue_volume,
                             device=acfg.device)
        # cue_files(), not filenames(): the latter includes the BED, and
        # preloading it decodes the whole track into RAM as a Sound — measured
        # at 105 MB for the shipped 10-minute mp3, and several GB for the
        # 8-hour source the README warns about. __main__ already gets this
        # right; this did not.
        if not player.start(preload=acfg.cue_files()):
            log.info("Audio: silent (%s)", player.status().get("error"))
            return None
        log.info("Audio: on — you should hear this run")
        return AudioCuePlayer(player, acfg)
    except Exception as exc:
        log.warning("Audio: unavailable (%s)", exc)
        return None


class StandaloneRunner:
    """
    Tiny runner that wires the FSM to the fake backends and lets the script
    inject events directly.  Not the production GameRunner — that lives in
    core/runner.py.  This one exists purely to make fake_run.py self-contained.
    """

    def __init__(self, cfg: AppConfig, db: MemoryDB) -> None:
        from core.fsm import FSMContext, transition
        self.cfg        = cfg
        self.db         = db
        self.fsm_state  = ATTRACT
        self.ctx        = FSMContext(detection_mode=cfg.game.detection_mode)
        self.transition = transition
        self.watcher    = FakeStateWatcher()
        self.run_id: str | None = None
        self.audio      = _make_audio(cfg)
        # Seconds to dwell after each state change. 0 keeps the original
        # behaviour: fire everything and print the row. Anything above 0 is
        # for listening/watching, where a run that finishes in 300 ms is
        # useless — the cues overlap into one noise and the last one is cut
        # off by the process exiting.
        self.pace = 0.0

    async def inject(self, event) -> None:
        """Inject an event into the FSM and execute side effects."""
        new_state, effects = self.transition(self.fsm_state, event, self.ctx)
        old_state = self.fsm_state
        self.fsm_state = new_state

        # Execute relevant side effects
        for fx in effects:
            await self._apply(fx)

        if old_state != new_state:
            log.info("FSM: %s → %s  (event=%s)", old_state, new_state, type(event).__name__)
            if self.audio is not None:
                # Same hook the real runner uses, so this script hears exactly
                # what the container would.
                try:
                    self.audio.set_state(new_state)
                except Exception as exc:
                    log.warning("audio cue failed: %s", exc)
            self.watcher.on_state(new_state)
            if self.pace:
                await asyncio.sleep(self.pace)

    async def _apply(self, fx) -> None:
        """Execute a side effect. Only the effects relevant to the CLI script."""
        from core.events import (
            SaveRun, VoidRun, BroadcastState, StartStopwatch, StopStopwatch,
            ResetStopwatch, ApplyPreset, PlayShow, StopShow, ArmDetection, DisarmDetection,
            StartCountIn, BeamPreflightCheck, ReadyBlink, EmitMetric,
            SaveBreakEvidence, AutoMaskBeam, DropDetectionMode,
        )

        if isinstance(fx, SaveRun):
            await self.db.insert_run(
                fx.run_id,
                outcome=fx.outcome,
                detection_mode=self.ctx.detection_mode,
                segment_reached=self.ctx.segment,
                player_id=self.ctx.player_id,
                player_nickname=self.ctx.player_nickname,
            )
            if self.run_id is None and fx.run_id:
                self.run_id = fx.run_id

        elif isinstance(fx, VoidRun):
            await self.db.void_run(fx.run_id, fx.reason)

        elif isinstance(fx, (BroadcastState, StartStopwatch,
                              StopStopwatch, ResetStopwatch, ApplyPreset,
                              PlayShow, StopShow,
                              ArmDetection, DisarmDetection, StartCountIn,
                              BeamPreflightCheck, ReadyBlink, EmitMetric,
                              SaveBreakEvidence, AutoMaskBeam, DropDetectionMode)):
            # Log but otherwise no-op for the CLI script
            log.debug("Side effect: %s", type(fx).__name__)

        else:
            # An unhandled effect used to vanish silently. That is how the
            # voided scenario kept reporting 'clean' after VoidRun was added.
            log.warning("Side effect %s not handled by fake_run", type(fx).__name__)

    async def simulate_countdown(self) -> None:
        """Fast-forward through the count-in ramp (emit RampComplete immediately)."""
        from core.events import RampComplete, PreflightPass
        # Emit preflight pass so COUNT IN button becomes enabled
        await self.inject(PreflightPass())
        # GM taps COUNT IN
        await self.inject(CountInRequested())
        # Simulate ramp completing instantly
        await asyncio.sleep(0.05)
        await self.inject(RampComplete())


# ---------------------------------------------------------------------------
# Scenario implementations
# ---------------------------------------------------------------------------

async def scenario_clean(runner: StandaloneRunner, cfg: AppConfig) -> str:
    """Walk through all three segments cleanly and press STOP."""
    # The run_id needs to be set before SaveRun fires; set it now.
    runner.run_id = str(uuid.uuid4())
    runner.ctx.run_id = runner.run_id

    await runner.inject(Cp1Pressed())
    await asyncio.sleep(0.05)
    await runner.inject(Cp2Pressed())
    await asyncio.sleep(0.05)
    await runner.inject(StopPressed())
    return runner.run_id


async def scenario_busted(runner: StandaloneRunner, cfg: AppConfig, beam: str) -> str:
    """Inject a beam break and wait for BUSTED."""
    runner.run_id = str(uuid.uuid4())
    runner.ctx.run_id = runner.run_id

    await asyncio.sleep(0.1)   # small delay — the player is "running"
    await runner.inject(BreakConfirmed(
        beam_id=beam,
        ratio=0.15,
        run_id=runner.run_id,
    ))
    # In assisted mode BreakConfirmed halts and waits for GM confirm.
    # In auto mode it goes to BUSTED immediately — GmConfirmBreak is a no-op there.
    if runner.ctx.pending_break:
        await runner.inject(GmConfirmBreak())
    return runner.run_id


async def scenario_aborted(runner: StandaloneRunner, cfg: AppConfig) -> str:
    """Let max_run_ms expire (simulated immediately via MaxRunExceeded)."""
    from core.events import MaxRunExceeded
    runner.run_id = str(uuid.uuid4())
    runner.ctx.run_id = runner.run_id

    log.info("Injecting MaxRunExceeded to simulate timeout…")
    await runner.inject(MaxRunExceeded())
    return runner.run_id


async def scenario_voided(runner: StandaloneRunner, cfg: AppConfig) -> str:
    """Run clean, then void the run."""
    run_id = await scenario_clean(runner, cfg)
    await asyncio.sleep(0.05)
    await runner.inject(GmVoid(reason="Test void from fake_run.py"))
    return run_id


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    global _SILENT
    _SILENT = bool(getattr(args, "silent", False))

    log.info("Loading config…")
    cfg = load_all()

    log.info("Creating in-memory DB and runner…")
    db     = MemoryDB()
    runner = StandaloneRunner(cfg, db)
    runner.pace = max(0.0, float(getattr(args, "pace", 0.0) or 0.0))

    # Seed a player
    player_id = str(uuid.uuid4())
    await db.insert_player(player_id, nickname="FakePlayer")

    log.info("Injecting PlayerRegistered…")
    await runner.inject(PlayerRegistered(player_id=player_id, nickname="FakePlayer"))
    await runner.watcher.wait_for("REGISTERED")

    log.info("Injecting PlateHigh → ARM…")
    await runner.inject(PlateHigh())
    await runner.watcher.wait_for("ARM")

    log.info("Simulating count-in → RUN_SEG_1…")
    await runner.simulate_countdown()
    await runner.watcher.wait_for("RUN_SEG_1")

    log.info("Running scenario: %s", args.scenario)
    scenario = args.scenario.lower()
    beam     = getattr(args, "beam", None) or "b01"

    if scenario == "clean":
        run_id = await scenario_clean(runner, cfg)
    elif scenario == "busted":
        run_id = await scenario_busted(runner, cfg, beam)
    elif scenario == "aborted":
        run_id = await scenario_aborted(runner, cfg)
    elif scenario == "voided":
        run_id = await scenario_voided(runner, cfg)
    else:
        log.error("Unknown scenario: %s", scenario)
        sys.exit(1)

    # Brief pause for side effects to settle
    await asyncio.sleep(0.1)

    # Fetch and print the final run record
    record = await db.get_run(run_id)
    if record is None:
        # Some scenarios (e.g. aborted before RUN) may not have a record
        log.warning("No run record found for id=%s", run_id)
        record = {"run_id": run_id, "note": "no DB record — run may not have reached RUN state"}

    print(json.dumps(record, indent=2, default=str))
    if runner.audio is not None:
        # The victory/defeat cue fires on the last transition. Exiting straight
        # after it truncates the one sound the run was building towards.
        await asyncio.sleep(max(2.5, runner.pace))
        runner.audio.stop()
    log.info("Done.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive a full ScanMania game run from the CLI using fake backends.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Scenarios
---------
  clean    Player walks through all three segments and presses STOP.
  busted   Player breaks a beam (default: b01, override with --beam).
  aborted  max_run_ms timer fires (injected immediately).
  voided   Clean run, then the gamemaster voids it.

Examples
--------
  python tools/fake_run.py --scenario clean
  python tools/fake_run.py --scenario busted --beam b03
  python tools/fake_run.py --scenario aborted
  python tools/fake_run.py --scenario voided
        """,
    )
    parser.add_argument(
        "--scenario",
        required=True,
        choices=["clean", "busted", "aborted", "voided"],
        help="Which scenario to run.",
    )
    parser.add_argument(
        "--beam",
        default="b01",
        help="Beam ID to bust on (busted scenario only). Default: b01.",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Dwell this long in each state. Use with audio to actually hear "
             "the run (try --pace 3). Default 0: as fast as possible.",
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="Do not play the soundtrack. Audio is on by default — the point "
             "of driving a run from here is to see AND hear what the container "
             "does.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
