"""Entry point: python -m sentry.main [--config FILE] [--log-file PATH]."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path

from . import __version__ as VERSION
from .app import Engine
from .config import load_config, resolve


def setup_logging(log_file: str | None, verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if log_file:
        h = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5)
    else:
        h = logging.StreamHandler(sys.stdout)
    h.setFormatter(fmt)
    root.addHandler(h)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


EXIT_ALREADY_RUNNING = 3


def acquire_data_lock(data_dir: Path):
    """Exclusive, non-blocking flock on <data_dir>/engine.lock, held for the life of the process
    (the kernel drops it if the process dies, so a crash never leaves a stale lock).
    Returns the open file, or None when another engine already owns this data directory."""
    data_dir.mkdir(parents=True, exist_ok=True)
    fh = open(data_dir / "engine.lock", "a+")
    for attempt in range(10):
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            # `./wifi-sentry` checks for a running engine by taking this lock for a few microseconds;
            # don't mistake that for a second engine.
            if attempt < 9:
                time.sleep(0.1)
    else:
        fh.seek(0)
        other = fh.read().strip()
        fh.close()
        logging.getLogger("sentry").error(
            "another engine (pid %s) is already running on %s; not starting a second one",
            other or "?", data_dir)
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


async def run(cfg: dict) -> None:
    eng = Engine(cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, stop.set)
    await eng.start()
    bound = await eng.serve_http()
    if not bound:
        logging.getLogger("sentry").error("no HTTP address could be bound; running headless")
    await stop.wait()
    logging.getLogger("sentry").info("shutting down")
    await eng.stop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="wifi-sentry engine")
    ap.add_argument("--config", help="config.toml (default: project root)")
    ap.add_argument("--log-file", help="rotating log file (default: stdout)")
    ap.add_argument("--port", type=int, help="override server.port")
    ap.add_argument("--hosts", help="replace the whole bind list, comma separated: loopback, tailscale, lan or "
                                    "addresses (e.g. --hosts loopback for a private test instance)")
    ap.add_argument("--esp32-port", type=int, help="override esp32.port")
    ap.add_argument("--data-dir", help="override paths.data_dir")
    ap.add_argument("--no-mac", action="store_true", help="do not spawn wifisense")
    ap.add_argument("--fake-mac", metavar="MODE", nargs="?", const="dual",
                    help="replay recorded samples instead of the real sampler (dual, single, multi, aggregate, "
                         "off, disconnected, none); for tests and a second instance next to a live one")
    ap.add_argument("--learning-s", type=float, help="override baseline.learning_s (e.g. 60 for a test instance)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    os.umask(0o077)  # database, recordings, logs, token, alert settings: readable by this user only
    over: dict = {}
    if args.learning_s:
        over.setdefault("baseline", {})["learning_s"] = args.learning_s
    if args.port:
        over.setdefault("server", {})["port"] = args.port
    if args.hosts:
        over.setdefault("server", {})["bind_override"] = [h.strip() for h in args.hosts.split(",") if h.strip()]
    if args.esp32_port:
        over.setdefault("esp32", {})["port"] = args.esp32_port
    if args.data_dir:
        over.setdefault("paths", {})["data_dir"] = args.data_dir
    if args.no_mac:
        over.setdefault("mac", {})["enabled"] = False
    if args.fake_mac:
        from .fake_sampler import MODES
        if args.fake_mac not in MODES:
            ap.error(f"--fake-mac must be one of {', '.join(MODES)}")
        over.setdefault("mac", {})["command"] = [sys.executable, "-m", "sentry.fake_sampler", "--mode", args.fake_mac]
    try:
        cfg = load_config(args.config, over)
    except ValueError as e:
        print(f"wifi-sentry: configuration error: {e}", file=sys.stderr)
        return 2
    resolve(cfg, cfg["paths"]["data_dir"]).joinpath("logs").mkdir(parents=True, exist_ok=True)
    setup_logging(args.log_file, args.verbose)
    lock = acquire_data_lock(resolve(cfg, cfg["paths"]["data_dir"]))
    if lock is None:
        return EXIT_ALREADY_RUNNING  # non-zero: a LaunchAgent retries (ThrottleInterval) until the other stops
    lg = logging.getLogger("sentry")
    lg.info("wifi-sentry %s engine starting (config: %s; sensitivity default %s)", VERSION,
            ", ".join(Path(f).name for f in cfg["_files"]) or "built-in defaults", cfg["detect"]["events"]["sensitivity"])
    for note in cfg["_notes"]:
        lg.warning("config: %s", note)
    asyncio.run(run(cfg))
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
