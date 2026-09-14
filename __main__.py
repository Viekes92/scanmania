"""
__main__.py — entry point for the ScanMania system.

Inputs:  CLI flags selecting real or fake backends, optional --config-dir
Outputs: all services running as asyncio tasks; clean shutdown on SIGINT/SIGTERM
Invariant: never resumes a run after a restart (runner starts clean every time).

Usage:
  python -m scanmania                   # production: reads /etc/scanmania/config
  python -m scanmania --fake-all        # development: all fakes, config from ./config/
  python -m scanmania --fake-io         # fake relay boards only
  python -m scanmania --fake-vision     # fake camera only
  python -m scanmania --fake-inputs     # fake Pico only
  python -m scanmania --config-dir /path/to/config
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys as _sys
import signal
import sys
from pathlib import Path

log = logging.getLogger("scanmania")

# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scanmania",
        description="ScanMania laser maze game system.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Backend flags
-------------
  --fake-all       Activate all fake backends (io + vision + inputs).
                   Config is loaded from ./config/ (not /etc/scanmania/).
                   Use this for laptop development.
  --fake-io        Use the in-memory relay board fake (io/fake.py).
  --fake-vision    Use the recorded-video vision fake (vision/fake.py).
  --fake-inputs    Use the no-op Pico link fake (inputs/fake.py).

Any combination is valid. A flag only matters if the corresponding real
backend would otherwise be loaded.

Examples
--------
  python -m scanmania --fake-all
  python -m scanmania --fake-io --fake-inputs
  python -m scanmania --config-dir /etc/scanmania/config
        """,
    )
    parser.add_argument("--fake-all",    action="store_true",
                        help="Activate all fake backends (implies all --fake-* flags).")
    parser.add_argument("--fake-io",     action="store_true",
                        help="Use fake relay board backend (io/fake.py).")
    parser.add_argument("--fake-vision", action="store_true",
                        help="Use fake camera/vision backend (vision/fake.py).")
    parser.add_argument("--fake-inputs", action="store_true",
                        help="Use fake Pico serial link (inputs/fake.py).")
    parser.add_argument("--config-dir",  default=None, metavar="DIR",
                        help="Path to config directory. Defaults to ./config/ "
                             "(--fake-all) or /etc/scanmania/config (production).")
    parser.add_argument("--log-level",   default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging verbosity. Default: INFO.")
    return parser


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

class _ColorFormatter(logging.Formatter):
    """Colored log formatter for terminal output."""
    COLORS = {
        "DEBUG":    "\033[90m",       # grey
        "INFO":     "\033[36m",       # cyan
        "WARNING":  "\033[33m",       # yellow
        "ERROR":    "\033[31m",       # red
        "CRITICAL": "\033[1;31m",     # bold red
    }
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    def format(self, record):
        c = self.COLORS.get(record.levelname, "")
        lvl = f"{c}{record.levelname:<7}{self.RESET}"
        name = f"{self.DIM}{record.name}{self.RESET}"
        ts = self.formatTime(record, "%H:%M:%S")
        return f"{self.DIM}{ts}{self.RESET} {lvl} {name}: {record.getMessage()}"


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if sys.stdout.isatty():
        handler.setFormatter(_ColorFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
    logging.root.handlers.clear()
    logging.root.addHandler(handler)
    logging.root.setLevel(getattr(logging, level))


# ---------------------------------------------------------------------------
# Config directory resolution
# ---------------------------------------------------------------------------

def resolve_config_dir(args: argparse.Namespace) -> Path:
    if args.config_dir:
        return Path(args.config_dir).resolve()
    if args.fake_all or args.fake_io or args.fake_vision or args.fake_inputs:
        # Development mode: look for config/ relative to the repo root
        return Path(__file__).resolve().parent / "config"
    # Production: systemd sets the working directory; use /etc/scanmania/config
    prod = Path("/etc/scanmania/config")
    if prod.exists():
        return prod
    # Fallback: local config/ (useful for development without --fake-all)
    fallback = Path(__file__).resolve().parent / "config"
    log.warning("Production config %s not found; using %s", prod, fallback)
    return fallback


# ---------------------------------------------------------------------------
# Admin password
# ---------------------------------------------------------------------------

def _ensure_admin_password() -> None:
    """
    The admin API fails closed when SCANMANIA_ADMIN_PASSWORD is unset. Generate a
    per-boot password and log it at WARNING so an operator can always recover it
    from `journalctl -u scanmania-core`, rather than being locked out entirely.
    """
    import secrets
    from web.routes_admin import ENV_PASSWORD_KEY

    if os.environ.get(ENV_PASSWORD_KEY):
        return
    generated = secrets.token_urlsafe(12)
    os.environ[ENV_PASSWORD_KEY] = generated
    log.warning(
        "%s is not set — generated a temporary admin password for this boot: %s",
        ENV_PASSWORD_KEY, generated,
    )
    log.warning("Set %s in the service environment to make it permanent.", ENV_PASSWORD_KEY)


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------

def create_backends(args: argparse.Namespace, cfg):
    """
    Instantiate real or fake backends based on CLI flags.
    Returns a dict: {"io": ..., "vision": ..., "inputs": ...}
    """
    use_fake_io      = args.fake_all or args.fake_io
    use_fake_vision  = args.fake_all or args.fake_vision
    use_fake_inputs  = args.fake_all or args.fake_inputs

    backends = {}

    # ---- IO backend (Modbus relay boards) ----
    if use_fake_io:
        from iobackend.fake import FakeIOBackend
        backends["io"] = FakeIOBackend(cfg.hardware)
        log.info("IO backend: FakeIOBackend (in-memory relay simulation)")
    else:
        from iobackend.modbus import ModbusIOBackend
        backends["io"] = ModbusIOBackend(cfg.hardware)
        log.info("IO backend: ModbusIOBackend (real Modbus TCP)")

    # ---- Vision backend ----
    if use_fake_vision:
        from vision.fake import FakeVision
        backends["vision"] = FakeVision(cfg.beams)
        log.info("Vision backend: FakeVision (no-op / replay mode)")
    else:
        from vision.service import VisionService
        import core.metrics as _m
        backends["vision"] = VisionService(cfg, metrics_emit=_m.emit)
        log.info(
            "Vision backend: VisionService (%d RTSP camera(s))",
            len(cfg.hardware.cameras),
        )

    # ---- Inputs backend (Arduino Opta over Modbus TCP) ----
    if use_fake_inputs:
        from inputs.fake import FakeInputs
        backends["inputs"] = FakeInputs()
        log.info("Inputs backend: FakeInputs (no-op)")
    else:
        from inputs.modbus_inputs import ModbusInputs
        hw_inputs = getattr(cfg.hardware, "inputs", None)
        if hw_inputs:
            backends["inputs"] = ModbusInputs(
                ip=hw_inputs.get("ip", "172.16.0.100"),
                port=hw_inputs.get("port", 502),
                poll_ms=hw_inputs.get("poll_ms", 50),
                num_inputs=hw_inputs.get("num_inputs", 4),
                input_map={int(k): v for k, v in hw_inputs.get("input_map", {}).items()},
                timeout_ms=hw_inputs.get("timeout_ms", 200),
            )
        else:
            backends["inputs"] = ModbusInputs()
        log.info("Inputs backend: ModbusInputs (Arduino Opta at %s)",
                 getattr(backends["inputs"], "_ip", "?"))

    return backends


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def install_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown_event: asyncio.Event) -> None:
    """Install SIGINT and SIGTERM handlers that set the shutdown event."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            # add_signal_handler is not supported on Windows
            pass


# ---------------------------------------------------------------------------
# Main async entry point
# ---------------------------------------------------------------------------

async def async_main(args: argparse.Namespace) -> int:
    """
    Start all services as asyncio tasks.
    Returns exit code (0 = clean shutdown).
    """
    # ================================================================
    #  PHASE 1 — Config & Database
    # ================================================================
    config_dir = resolve_config_dir(args)
    # Sweep temp files left by an interrupted config write. They are harmless
    # on their own, but deploy.sh does `git add config` and would commit them.
    for stale in list(config_dir.glob("*.json.new")) + list(config_dir.glob("*.tmp")):
        try:
            stale.unlink()
            log.warning("Removed stale config temp file %s (interrupted write?)", stale)
        except OSError:
            pass
    log.info("Loading config from %s", config_dir)

    _ensure_admin_password()

    import config.loader as loader_module
    loader_module.CONFIG_DIR = config_dir

    try:
        from config.loader import load_all
        cfg = load_all()
    except Exception as e:
        log.error("Config load failed: %s", e)
        return 1

    log.info("Config: %d boards, %d beams, %d presets",
             len(cfg.hardware.relay_boards), len(cfg.beams.beams),
             len(cfg.mazes.presets))

    if args.fake_all:
        db_path = ":memory:"
    elif _sys.platform != "darwin":
        # Create it rather than fall back. A missing directory (fresh box,
        # unmounted volume, botched deploy) silently relocated the DB to a
        # gitignored file inside the repo that no backup path covers and the
        # next deploy overwrites — an evening of runs recorded somewhere nobody
        # would ever look, with only an INFO line to say so.
        try:
            os.makedirs("/var/lib/scanmania", exist_ok=True)
            db_path = "/var/lib/scanmania/scanmania.db"
        except OSError as exc:
            log.critical("Cannot create /var/lib/scanmania (%s). Refusing to "
                         "start rather than write runs somewhere unbacked.", exc)
            raise
    else:
        db_path = str(Path(__file__).resolve().parent / "scanmania.db")
    log.info("Database: %s", db_path)
    from persist.db import Database
    db = Database(db_path)
    await db.init()
    # Settle any run the last process left open. Invariant 7 still holds — the
    # run is never resumed, only recorded honestly instead of sitting
    # "in_progress" forever.
    try:
        await db.close_orphaned_runs()
    except Exception as exc:
        log.warning("could not close orphaned runs: %s", exc)

    # ================================================================
    #  PHASE 2 — Hardware connections
    # ================================================================
    backends = create_backends(args, cfg)

    # Relay boards
    if hasattr(backends["io"], "connect_all"):
        log.info("Connecting relay boards...")
        results = await backends["io"].connect_all()
        for bid, ok in results.items():
            if ok:
                log.info("  ✓ %s", bid)
            else:
                log.warning("  ✗ %s FAILED", bid)
    else:
        log.info("Relay boards: fake mode")

    # Inputs (Opta)
    if hasattr(backends["inputs"], "_ip"):
        log.info("Inputs: %s:%s (Opta Modbus TCP)", backends["inputs"]._ip, backends["inputs"]._port)
    else:
        log.info("Inputs: fake mode")

    # Vision
    log.info("Vision: %s", type(backends["vision"]).__name__)

    # ---- Services (each is an async context manager or coroutine) ----
    # We collect all service tasks into a TaskGroup so a crash in one
    # cancels the rest and surfaces the error.

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    install_signal_handlers(loop, shutdown_event)

    tasks: list[asyncio.Task] = []

    # WebSocket hub — shared between runner (for broadcasts) and web server
    from web.server import WebSocketHub, ScanManiaApp
    # Hazer (Art-Net DMX)
    hazer = None
    hazer_cfg = getattr(cfg.hardware, "hazer", None) or {}
    if isinstance(hazer_cfg, dict) and hazer_cfg.get("artnet_ip"):
        try:
            from iobackend.hazer import HazerController
            hazer = HazerController(
                artnet_ip=hazer_cfg["artnet_ip"],
                universe=hazer_cfg.get("universe", 1),
                fan_channel=hazer_cfg.get("fan_channel", 1),
                haze_channel=hazer_cfg.get("haze_channel", 2),
                default_fan=hazer_cfg.get("default_fan", 200),
                default_haze=hazer_cfg.get("default_haze", 128),
            )
            if not hazer_cfg.get("enabled", True):
                hazer.set_enabled(False)
            log.info("Hazer: %s universe=%d (default %s)",
                     hazer_cfg["artnet_ip"], hazer_cfg.get("universe", 1),
                     "ON" if hazer_cfg.get("enabled", True) else "OFF")
        except Exception as e:
            log.warning("Hazer: unavailable — %s", e)
    else:
        log.info("Hazer: not configured")

    # ================================================================
    #  PHASE 3 — Start services
    # ================================================================
    log.info("Starting services...")

    hub = WebSocketHub()

    from core.runner import GameRunner
    runner = GameRunner(cfg, backends["io"], backends["inputs"], backends["vision"], db, hub=hub)
    # The runner drives everything off on shutdown; it needs the hazer to do it.
    runner.hazer = hazer
    tasks.append(asyncio.create_task(runner.run(), name="runner"))

    if hazer:
        tasks.append(asyncio.create_task(hazer.run(), name="hazer"))

    # WebServer
    web_app = ScanManiaApp(cfg, db, hub, config_dir=config_dir, fake_mode=args.fake_all)
    web_app.set_runner(runner)
    if hazer:
        web_app.set_hazer(hazer)
    fastapi_app = web_app.build()

    import uvicorn
    server_config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=cfg.hardware.network.web_port,
        log_level="warning",
        lifespan="off",
    )
    uvicorn_server = uvicorn.Server(server_config)
    # Prevent uvicorn from competing with our signal handlers
    uvicorn_server.install_signal_handlers = lambda: None
    tasks.append(asyncio.create_task(uvicorn_server.serve(), name="web"))
    log.info("Web server starting on port %d", cfg.hardware.network.web_port)

    # ---- Rolling DB snapshots ----
    # persist/backup.py documented this as an invariant but nothing ever started
    # the loop. Manual and on-stop snapshots worked, so an unclean shutdown
    # (power cut, OOM kill) lost every run back to the last button press.
    snap_min = getattr(cfg.game, "snapshot_interval_min", 60)
    if snap_min > 0:
        import tempfile
        from persist.backup import rolling_snapshot_loop
        snap_dir = (
            os.path.join(tempfile.gettempdir(), "scanmania-backups")
            if _sys.platform == "darwin"
            else "/var/backups/scanmania"
        )
        tasks.append(asyncio.create_task(
            rolling_snapshot_loop(
                db, snap_dir,
                interval_min=snap_min,
                keep=getattr(cfg.game, "snapshot_keep", 48),
            ),
            name="rolling_snapshot",
        ))
        log.info("Rolling snapshots: every %d min → %s", snap_min, snap_dir)
    else:
        log.info("Rolling snapshots disabled (snapshot_interval_min = 0)")

    if not tasks:
        log.error("No services started. Check that core/runner.py and web/server.py exist.")
        return 1

    log.info("All services started (%d tasks). Press Ctrl-C to stop.", len(tasks))

    # Wait for a signal OR for any service task to exit. Waiting on the signal
    # alone let a dead service sit unnoticed: the process stayed up, so systemd
    # Restart=always never fired and the fault only surfaced at shutdown.
    _signal_task = asyncio.create_task(shutdown_event.wait(), name="shutdown_signal")
    await asyncio.wait([_signal_task, *tasks], return_when=asyncio.FIRST_COMPLETED)

    exit_code = 0
    if not _signal_task.done():
        _signal_task.cancel()
        for task in tasks:
            if task.done() and not task.cancelled() and task.exception() is not None:
                log.critical(
                    "Service %s died — exiting so systemd can restart us: %r",
                    task.get_name(), task.exception(),
                )
                exit_code = 1
        if exit_code == 0:
            log.error("A service task exited unexpectedly without an error — shutting down")
            exit_code = 1
    else:
        log.info("Shutdown signal received — cancelling tasks…")

    # Cancel all tasks gracefully
    for task in tasks:
        task.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            log.error("Task %s raised: %s", task.get_name(), result)

    # Everything off before we exit. runner.run() already does this on its own
    # exit paths; this covers the case where it never started, or died before
    # reaching them. Relay coils latch, so "the process is gone" is not the same
    # as "the lasers are off".
    try:
        await asyncio.wait_for(runner.blackout(), timeout=5.0)
    except Exception as e:
        log.error("Shutdown blackout failed (%s) — LASERS MAY STILL BE LIT", e)

    # Export a snapshot before exit (best-effort)
    try:
        import tempfile
        from persist.backup import export_snapshot
        backup_dir = (
            os.path.join(tempfile.gettempdir(), "scanmania-backups")
            if _sys.platform == "darwin"
            else "/var/backups/scanmania"
        )
        snap_path = await export_snapshot(db, backup_dir)
        log.info("Snapshot exported to %s", snap_path)
    except Exception as e:
        log.warning("Snapshot export failed: %s", e)

    await db.close()
    if exit_code == 0:
        log.info("Clean shutdown complete.")
    else:
        log.error("Shutdown complete after a service failure — exiting %d", exit_code)
    return exit_code


# ---------------------------------------------------------------------------
# Synchronous entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    # Resolve --fake-all early so logging picks up the right level
    configure_logging(args.log_level)

    if args.fake_all:
        # Convenience: set all individual fake flags too
        args.fake_io      = True
        args.fake_vision  = True
        args.fake_inputs  = True
        log.info("Running in --fake-all mode (no hardware required)")


    exit_code = asyncio.run(async_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
