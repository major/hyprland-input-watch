"""Command-line entry point for the monitor watcher."""

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .watcher import Watcher, find_monitor, lock_instance, read_input, read_monitors

LOG = logging.getLogger("hyprland-input-watch")


def _default_config():
    """Find the user's XDG configuration file."""
    root = os.environ.get("XDG_CONFIG_HOME")
    return (
        Path(root) if root else Path.home() / ".config"
    ) / "hyprland-input-watch/config.toml"


def _probe(serial):
    """Report every selected monitor and its input without changing outputs."""
    monitors = read_monitors()
    selected = []
    seen = set()
    for monitor in monitors:
        identity = monitor.get("serial")
        if serial is not None and identity != serial:
            continue
        # One result per serial, including one result for missing identities.
        key = (
            identity
            if isinstance(identity, str)
            and identity
            and identity == identity.strip()
            and all(ord(char) >= 32 and ord(char) != 127 for char in identity)
            else None
        )
        if key in seen:
            continue
        seen.add(key)
        entry = {"serial": key}
        match = find_monitor(monitors, key) if key is not None else None
        if match is None:
            entry["error"] = "monitor identity is missing, ambiguous, or incomplete"
        else:
            entry.update(name=match["name"], disabled=match["disabled"])
            try:
                entry["input"] = f"0x{read_input(key):02x}"
            except RuntimeError as error:
                entry["error"] = str(error)
        selected.append(entry)
    if serial is not None and not selected:
        selected.append({"serial": serial, "error": "monitor serial not found"})
    result = {"monitors": selected}
    if not selected:
        result["errors"] = ["no Hyprland monitors found"]
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return int(not selected or any("error" in item for item in selected))


def _check(config):
    """Report configured identities and input without acquiring a lock."""
    monitors = read_monitors()
    target = find_monitor(monitors, config.monitor_serial)
    fallback = find_monitor(monitors, config.fallback_serial)
    result = {"target": target, "fallback": fallback, "input": None}
    errors = []
    if target is None:
        errors.append("target monitor identity is missing, ambiguous, or incomplete")
    if fallback is None:
        errors.append("fallback monitor identity is missing, ambiguous, or incomplete")
    if target is not None:
        try:
            source = read_input(config.monitor_serial)
            result["input"] = f"0x{source:02x}"
            result["input_status"] = (
                "active"
                if source == config.active_input
                else "away"
                if source in config.away_inputs
                else "unknown"
            )
        except RuntimeError as error:
            errors.append(str(error))
    if errors:
        result["errors"] = errors
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return int(bool(errors))


def _run(config):
    """Hold the singleton lock until the owned monitor has been restored."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        raise RuntimeError("XDG_RUNTIME_DIR is required to run the watcher")
    watcher = Watcher(config)

    def request_stop(signum, frame):
        """Defer monitor changes to normal shutdown."""
        watcher.stop.set()

    with lock_instance(Path(runtime_dir) / "hyprland-input-watch.lock"):
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        LOG.info("watching %s", config.monitor_serial)
        try:
            while not watcher.stop.is_set():
                if not watcher.step() or watcher.stop.wait(config.interval_seconds):
                    break
        finally:
            watcher.close()
    return 0


def main(argv=None):
    """Parse arguments and run the requested command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=_default_config())
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("run", help="watch and update the output")
    probe = subcommands.add_parser("probe", help="report detected inputs")
    probe.add_argument("--serial", help="report only this monitor serial")
    subcommands.add_parser("check", help="report configured monitor status")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    try:
        if args.command == "probe":
            return _probe(args.serial)
        config = Config.load(args.config)
        if args.command == "check":
            return _check(config)
        return _run(config)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"hyprland-input-watch: {error}", file=sys.stderr)
        return 1
