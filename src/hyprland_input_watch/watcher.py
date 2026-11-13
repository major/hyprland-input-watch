"""Safely watch and restore a configured Hyprland monitor."""

import fcntl
import json
import logging
import re
import subprocess
import threading
import time
from contextlib import contextmanager

LOG = logging.getLogger("hyprland-input-watch")


def command(args, timeout=8):
    """Run one external operation with bounded time and captured diagnostics."""
    try:
        result = subprocess.run(
            args, check=True, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise RuntimeError(f"{args[0]} failed: {error}") from error
    return result.stdout.strip()


def read_monitors(timeout=8):
    """Read all outputs, including disabled ones, without accepting bad data."""
    try:
        monitors = json.loads(
            command(["hyprctl", "-j", "monitors", "all"], timeout=timeout)
        )
    except ValueError as error:
        raise RuntimeError("hyprctl returned invalid monitor JSON") from error
    if not isinstance(monitors, list) or any(
        not isinstance(monitor, dict) for monitor in monitors
    ):
        raise RuntimeError("hyprctl returned an invalid monitor list")
    return monitors


def find_monitor(monitors, serial):
    """Require one matching identity and explicit name and enabled-state fields."""
    matches = [monitor for monitor in monitors if monitor.get("serial") == serial]
    if len(matches) != 1:
        return None
    monitor = matches[0]
    if (
        not isinstance(monitor.get("name"), str)
        or not monitor["name"]
        or any(ord(char) < 32 or ord(char) == 127 for char in monitor["name"])
        or not isinstance(monitor.get("disabled"), bool)
    ):
        return None
    return monitor


def read_input(serial):
    """Query a serial only after detecting exactly one complete matching display."""
    detection = command(["ddcutil", "detect", "--terse"])
    serials = []
    displays = set()
    in_display = False
    record = None
    for line in detection.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.fullmatch(r"Display\s*\d+", line):
            display_number = int(re.search(r"\d+$", line)[0])
            if display_number in displays:
                raise RuntimeError("ddcutil detect returned duplicate display records")
            displays.add(display_number)
            if in_display:
                if record is None:
                    raise RuntimeError("ddcutil detect returned an incomplete display")
                serials.append(record)
            in_display, record = True, None
        elif line.startswith("Monitor"):
            if not in_display or record is not None or not line.startswith("Monitor:"):
                raise RuntimeError("ddcutil detect returned malformed monitor records")
            fields = line[len("Monitor:") :].strip().split(":")
            if len(fields) != 3 or any(not field.strip() for field in fields):
                raise RuntimeError(
                    "ddcutil detect returned an incomplete or ambiguous monitor record"
                )
            record = fields[2].strip()
        elif (
            not in_display
            or ":" not in line
            or re.match(r"(?:Invalid\s+)?[Dd]isplay\b", line)
        ):
            raise RuntimeError("ddcutil detect returned malformed display records")
    if not in_display or record is None:
        raise RuntimeError("ddcutil detect returned incomplete display records")
    serials.append(record)
    if serials.count(serial) != 1:
        raise RuntimeError(f"ddcutil serial {serial!r} is missing or ambiguous")
    output = command(["ddcutil", "--sn", serial, "--terse", "getvcp", "60"])
    match = re.fullmatch(r"VCP 60 SNC x([0-9a-fA-F]{2})", output)
    if not match:
        raise RuntimeError(f"unrecognized input response: {output!r}")
    return int(match[1], 16)


@contextmanager
def lock_instance(path):
    """Keep one locked inode open through shutdown; never unlink a live lock."""
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Watcher:
    """Debounce input changes and own only this process's disable attempts."""

    def __init__(self, config):
        """Initialize observation and ownership state without changing outputs."""
        self.config = config
        self.stop = threading.Event()
        self.owned = False
        self.verified = False
        self.candidate = None
        self.samples = 0
        self.last_error = None

    def report(self, message):
        """Log a persistent failure once instead of on every polling attempt."""
        if message != self.last_error:
            LOG.warning("%s", message)
            self.last_error = message

    def _stop_after_enable(self, message):
        """Stop without restoring after an enabled target has been observed."""
        self.owned = False
        self.verified = False
        self.stop.set()
        LOG.info("%s", message)

    def change(self, disabled):
        """Recheck identity and safety immediately before a verified mutation."""
        monitors = read_monitors()
        target = find_monitor(monitors, self.config.monitor_serial)
        fallback = find_monitor(monitors, self.config.fallback_serial)
        if target is None:
            if not disabled and self.owned:
                self.report("target display identity is unavailable; restore blocked")
            return False
        if disabled:
            if (
                self.stop.is_set()
                or target["disabled"]
                or fallback is None
                or fallback["disabled"]
                or fallback.get("dpmsStatus") is not True
            ):
                return False
            # A timed-out IPC request may still have disabled the output.
            self.owned = True
            self.verified = False
        elif not self.owned or not target["disabled"]:
            return False

        fields = (
            f"output = {json.dumps(target['name'], ensure_ascii=False)}, "
            f"disabled = {str(disabled).lower()}"
        )
        if not disabled:
            restore = self.config.restore
            fields += (
                f", mode = {json.dumps(restore.mode, ensure_ascii=False)}"
                f", position = {json.dumps(restore.position, ensure_ascii=False)}"
                f", scale = {restore.scale}, transform = {restore.transform}"
            )
        error_message = None
        try:
            if command(["hyprctl", "eval", f"hl.monitor({{ {fields} }})"]) != "ok":
                error_message = "hyprctl rejected the monitor rule"
        except RuntimeError as error:
            error_message = str(error)

        observed = None
        deadline = time.monotonic() + 2
        for attempt in range(10):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (disabled and self.stop.is_set()):
                break
            try:
                observed = find_monitor(
                    read_monitors(timeout=remaining), self.config.monitor_serial
                )
            except RuntimeError as error:
                error_message = str(error)
                observed = None
                break
            if observed is not None and observed["disabled"] is disabled:
                self.owned = disabled
                self.verified = disabled
                self.last_error = None
                LOG.info(
                    "%s %s", "disabled" if disabled else "restored", target["name"]
                )
                return True
            remaining = deadline - time.monotonic()
            if attempt < 9 and remaining > 0:
                time.sleep(min(0.1, remaining))

        if (
            disabled
            and not self.stop.is_set()
            and observed is not None
            and observed["disabled"] is False
        ):
            self._stop_after_enable(
                "target is enabled after a disable attempt; stopping"
            )
        self.report(error_message or "monitor state change could not be confirmed")
        return False

    def step(self):
        """Process one observation, stopping rather than fighting an override."""
        if self.stop.is_set():
            return False
        try:
            target = find_monitor(read_monitors(), self.config.monitor_serial)
            if self.owned and not self.verified and target is not None:
                # Recover an interrupted verification without adopting a
                # display that was disabled before this process started.
                self.verified = target["disabled"]
                self.owned = target["disabled"]
                if not self.owned:
                    self.candidate, self.samples = None, 0
                    self._stop_after_enable(
                        "target output is enabled after an uncertain disable; stopping"
                    )
                    return False
            # Check overrides even if the next DDC query fails.
            if (
                self.owned
                and self.verified
                and target is not None
                and target["disabled"] is False
            ):
                self._stop_after_enable(
                    "target output was re-enabled externally; stopping"
                )
                return False
            source = read_input(self.config.monitor_serial)
            if (
                source != self.config.active_input
                and source not in self.config.away_inputs
            ):
                raise RuntimeError(f"unsupported input code: 0x{source:02x}")
            if source == self.candidate:
                self.samples = min(self.samples + 1, self.config.confirmations)
            else:
                self.candidate, self.samples = source, 1
            if self.samples == self.config.confirmations and not self.stop.is_set():
                if source == self.config.active_input and self.owned:
                    self.change(False)
                elif source in self.config.away_inputs and not self.owned:
                    self.change(True)
        except RuntimeError as error:
            self.candidate, self.samples = None, 0
            self.report(str(error))
        return True

    def close(self):
        """Restore an owned, still-identifiable output during graceful shutdown."""
        if not self.owned:
            return
        try:
            target = find_monitor(read_monitors(), self.config.monitor_serial)
            if target is not None and target["disabled"]:
                self.change(False)
            elif target is not None:
                self.owned = False
                self.verified = False
            else:
                self.report(
                    "target display is unavailable; manual restore may be needed"
                )
        except RuntimeError as error:
            self.report(f"shutdown restore failed: {error}")
