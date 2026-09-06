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

def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


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
        from vision.camera import VisionService
        backends["vision"] = VisionService(cfg)
        log.info("Vision backend: VisionService (real RTSP cameras)")

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
    config_dir = resolve_config_dir(args)
    log.info("Loading config from %s", config_dir)

    # Patch config loader to use the resolved directory
    import config.loader as loader_module
    loader_module.CONFIG_DIR = config_dir

    try:
        from config.loader import load_all
        cfg = load_all()
    except Exception as e:
        log.error("Config load failed: %s", e)
        return 1

    log.info("Config loaded OK: %d board(s), %d beam(s)",
             len(cfg.hardware.relay_boards), len(cfg.beams.beams))

    # ---- Database ----
    if args.fake_all:
        db_path = ":memory:"
    elif os.path.isdir("/var/lib/scanmania"):
        db_path = "/var/lib/scanmania/scanmania.db"
    else:
        db_path = str(Path(__file__).resolve().parent / "scanmania.db")
        log.info("Production DB path not found; using local %s", db_path)
    log.info("Initialising database at %s", db_path)
    from persist.db import Database
    db = Database(db_path)
    await db.init()

    # ---- Backends ----
    backends = create_backends(args, cfg)

    # Connect real Modbus boards if using the real IO backend
    if hasattr(backends["io"], "connect_all"):
        log.info("Connecting to relay boards…")
        results = await backends["io"].connect_all()
        for bid, ok in results.items():
            if ok:
                log.info("  %s: connected", bid)
            else:
                log.warning("  %s: FAILED to connect", bid)

    # ---- Services (each is an async context manager or coroutine) ----
    # We collect all service tasks into a TaskGroup so a crash in one
    # cancels the rest and surfaces the error.

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    install_signal_handlers(loop, shutdown_event)

    tasks: list[asyncio.Task] = []

    # WebSocket hub — shared between runner (for broadcasts) and web server
    from web.server import WebSocketHub, ScanManiaApp
    hub = WebSocketHub()

    # GameRunner — wires FSM to I/O, vision, inputs
    from core.runner import GameRunner
    runner = GameRunner(
        cfg,
        backends["io"],
        backends["inputs"],
        backends["vision"],
        db,
        hub=hub,
    )
    tasks.append(asyncio.create_task(runner.run(), name="runner"))

    # WebServer — FastAPI + WebSocket hub, served via uvicorn
    web_app = ScanManiaApp(cfg, db, hub)
    web_app.set_runner(runner)
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

    # OutboxWorker — drains the sync outbox to cloud (optional)
    outbox = None
    try:
        endpoint_url = os.environ.get("SCANMANIA_SYNC_URL", "")
        sync_token = os.environ.get("SCANMANIA_SYNC_TOKEN", "")
        if not endpoint_url:
            log.info("OutboxWorker skipped — SCANMANIA_SYNC_URL not set")
        else:
            from persist.outbox import OutboxWorker
            import core.metrics as _metrics
            outbox = OutboxWorker(db, endpoint_url, sync_token, _metrics.emit)
            web_app.set_outbox(outbox)
            tasks.append(asyncio.create_task(outbox.run(), name="outbox"))
            log.info("OutboxWorker started → %s", endpoint_url)
    except (ImportError, Exception) as e:
        log.warning("OutboxWorker unavailable — no cloud sync: %s", e)

    if not tasks:
        log.error("No services started. Check that core/runner.py and web/server.py exist.")
        return 1

    log.info("All services started (%d tasks). Press Ctrl-C to stop.", len(tasks))

    # Wait until a signal arrives or all tasks finish
    await shutdown_event.wait()

    log.info("Shutdown signal received — cancelling tasks…")

    # Cancel all tasks gracefully
    for task in tasks:
        task.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            log.error("Task %s raised: %s", task.get_name(), result)

    # Export a snapshot before exit (best-effort)
    try:
        import tempfile, sys as _sys
        from persist.sync import export_snapshot
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
    log.info("Clean shutdown complete.")
    return 0


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
