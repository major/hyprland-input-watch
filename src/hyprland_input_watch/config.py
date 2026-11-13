"""Parse and validate monitor watcher configuration."""

import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _validate_string(name: str, value: object) -> None:
    """Require a nonempty string without surrounding whitespace or controls."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(
            f"{name} must be a nonempty string without surrounding whitespace or control characters"
        )


def _is_number(value: object) -> bool:
    """Return whether a value is a finite, non-boolean number."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (isinstance(value, int) or math.isfinite(value))
    )


def _is_integer_in_range(value: object, minimum: int, maximum: int) -> bool:
    """Return whether a value is a non-boolean integer in the given range."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def _validate_keys(
    data: Mapping, required: set[str], optional: set[str], name: str
) -> None:
    """Reject missing required keys and unknown keys in a configuration table."""
    missing = required - data.keys()
    if missing:
        raise ValueError(f"{name} missing required keys: {', '.join(sorted(missing))}")
    unknown = data.keys() - required - optional
    if unknown:
        raise ValueError(
            f"{name} has unknown keys: {', '.join(sorted(map(repr, unknown)))}"
        )


@dataclass(frozen=True)
class Restore:
    """Monitor settings to restore when the active input returns."""

    mode: str
    position: str
    scale: float
    transform: int = 0

    def __post_init__(self) -> None:
        """Validate settings supplied by either the parser or a caller."""
        _validate_string("restore.mode", self.mode)
        _validate_string("restore.position", self.position)
        if not _is_number(self.scale) or self.scale <= 0:
            raise ValueError("restore.scale must be a finite positive number")
        if not _is_integer_in_range(self.transform, 0, 7):
            raise ValueError("restore.transform must be an integer from 0 to 7")


@dataclass(frozen=True)
class Config:
    """Validated configuration for monitoring input changes."""

    monitor_serial: str
    fallback_serial: str
    active_input: int
    away_inputs: frozenset[int]
    restore: Restore
    interval_seconds: float = 3.0
    confirmations: int = 2

    def __post_init__(self) -> None:
        """Validate and normalize settings supplied by any construction path."""
        _validate_string("monitor_serial", self.monitor_serial)
        _validate_string("fallback_serial", self.fallback_serial)
        if self.monitor_serial == self.fallback_serial:
            raise ValueError("monitor_serial and fallback_serial must be distinct")
        if not _is_integer_in_range(self.active_input, 0, 255):
            raise ValueError("active_input must be an integer from 0 to 255")
        if (
            not isinstance(self.away_inputs, (list, tuple, set, frozenset))
            or not self.away_inputs
        ):
            raise ValueError("away_inputs must be a nonempty collection of input bytes")
        if any(not _is_integer_in_range(value, 0, 255) for value in self.away_inputs):
            raise ValueError("away_inputs must contain integers from 0 to 255")
        away_inputs = frozenset(self.away_inputs)
        if len(away_inputs) != len(self.away_inputs):
            raise ValueError("away_inputs must not contain duplicates")
        if self.active_input in away_inputs:
            raise ValueError("away_inputs must not contain active_input")
        object.__setattr__(self, "away_inputs", away_inputs)
        if not isinstance(self.restore, Restore):
            raise ValueError("restore must be a Restore instance")
        if (
            not _is_number(self.interval_seconds)
            or not 1 <= self.interval_seconds <= 3600
        ):
            raise ValueError("interval_seconds must be a finite number from 1 to 3600")
        if not _is_integer_in_range(self.confirmations, 1, 10):
            raise ValueError("confirmations must be an integer from 1 to 10")

    @classmethod
    def from_mapping(cls, data: Mapping) -> "Config":
        """Parse a flattened configuration mapping with a nested restore table."""
        if not isinstance(data, Mapping):
            raise ValueError("config must be a table")
        _validate_keys(
            data,
            {
                "monitor_serial",
                "fallback_serial",
                "active_input",
                "away_inputs",
                "restore",
            },
            {"interval_seconds", "confirmations"},
            "config",
        )
        restore_data = data["restore"]
        if not isinstance(restore_data, Mapping):
            raise ValueError("restore must be a table")
        _validate_keys(
            restore_data, {"mode", "position", "scale"}, {"transform"}, "restore"
        )
        return cls(
            monitor_serial=data["monitor_serial"],
            fallback_serial=data["fallback_serial"],
            active_input=data["active_input"],
            away_inputs=data["away_inputs"],
            restore=Restore(**restore_data),
            interval_seconds=data.get("interval_seconds", 3.0),
            confirmations=data.get("confirmations", 2),
        )

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Load and validate a TOML configuration file."""
        with path.open("rb") as file:
            return cls.from_mapping(tomllib.load(file))
