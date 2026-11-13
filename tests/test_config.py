"""Tests for monitor configuration validation and TOML loading."""

import math
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from hyprland_input_watch.config import Config, Restore


def valid_mapping():
    """Return a complete configuration mapping."""
    return {
        "monitor_serial": "monitor-α",
        "fallback_serial": "monitor-β",
        "active_input": 1,
        "away_inputs": [2, 3],
        "restore": {"mode": "1920x1080@60", "position": "0x0", "scale": 1},
    }


def valid_config_kwargs():
    """Return valid direct-constructor arguments."""
    data = valid_mapping()
    data["restore"] = Restore(**data["restore"])
    return data


class ConfigTests(unittest.TestCase):
    """Exercise the public configuration API."""

    def test_load_toml_and_defaults(self):
        """Load a real TOML file and apply default values."""
        content = (
            'monitor_serial = "monitor-α"\n'
            'fallback_serial = "monitor-β"\n'
            "active_input = 1\n"
            "away_inputs = [2, 3]\n"
            '[restore]\nmode = "1920x1080@60"\nposition = "0x0"\nscale = 1.25\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(content, encoding="utf-8")
            config = Config.load(path)

        self.assertEqual(config.monitor_serial, "monitor-α")
        self.assertEqual(config.away_inputs, frozenset({2, 3}))
        self.assertEqual(config.restore, Restore("1920x1080@60", "0x0", 1.25))
        self.assertEqual(config.interval_seconds, 3.0)
        self.assertEqual(config.confirmations, 2)

    def test_explicit_options_and_frozen_dataclasses(self):
        """Preserve explicit options and prevent subsequent mutation."""
        data = valid_mapping()
        data.update(interval_seconds=4.5, confirmations=3)
        data["restore"]["transform"] = 2

        config = Config.from_mapping(data)

        self.assertEqual(
            (config.interval_seconds, config.confirmations, config.restore.transform),
            (4.5, 3, 2),
        )
        with self.assertRaises(FrozenInstanceError):
            config.confirmations = 4
        with self.assertRaises(FrozenInstanceError):
            config.restore.mode = "other"

    def test_required_and_unknown_keys(self):
        """Reject missing or unexpected keys at either table level."""
        for key in (
            "monitor_serial",
            "fallback_serial",
            "active_input",
            "away_inputs",
            "restore",
        ):
            with self.subTest(missing=key):
                data = valid_mapping()
                del data[key]
                with self.assertRaisesRegex(ValueError, key):
                    Config.from_mapping(data)
        for key in ("mode", "position", "scale"):
            with self.subTest(missing_restore=key):
                data = valid_mapping()
                del data["restore"][key]
                with self.assertRaisesRegex(ValueError, key):
                    Config.from_mapping(data)
        for table in ("top", "restore"):
            with self.subTest(unknown=table):
                data = valid_mapping()
                target = data if table == "top" else data["restore"]
                target["unexpected"] = 1
                with self.assertRaisesRegex(ValueError, "unexpected"):
                    Config.from_mapping(data)

    def test_invalid_mapping_shapes(self):
        """Reject malformed tables with useful validation errors."""
        for value in (None, [], "invalid"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Config.from_mapping(value)
                data = valid_mapping()
                data["restore"] = value
                with self.assertRaises(ValueError):
                    Config.from_mapping(data)

    def test_constructor_serial_validation(self):
        """Require distinct, clean, nonempty serial strings."""
        for field in ("monitor_serial", "fallback_serial"):
            for value in (
                None,
                [],
                "",
                " leading",
                "trailing ",
                "a\nb",
                "a\x00b",
                "a\x7fb",
            ):
                with self.subTest(field=field, value=value):
                    kwargs = valid_config_kwargs()
                    kwargs[field] = value
                    with self.assertRaises(ValueError):
                        Config(**kwargs)
        kwargs = valid_config_kwargs()
        kwargs["fallback_serial"] = kwargs["monitor_serial"]
        with self.assertRaises(ValueError):
            Config(**kwargs)

    def test_restore_string_validation(self):
        """Allow Unicode while rejecting whitespace and Lua-unsafe controls."""
        for field in ("mode", "position"):
            for value in (None, [], "", " x", "x ", "x\ninjected", "x\x1by", "x\x7fy"):
                with self.subTest(field=field, value=value):
                    kwargs = {"mode": "模式", "position": "位置", "scale": 1.0}
                    kwargs[field] = value
                    with self.assertRaises(ValueError):
                        Restore(**kwargs)
        self.assertEqual(Restore("模式", "位置", 1).mode, "模式")

    def test_restore_numeric_validation(self):
        """Reject invalid scales and transforms, including boolean values."""
        for value in (None, [], True, 0, -1, math.nan, math.inf, -math.inf):
            with self.subTest(scale=value), self.assertRaises(ValueError):
                Restore("mode", "position", value)
        for value in (None, [], True, -1, 8, 1.0):
            with self.subTest(transform=value), self.assertRaises(ValueError):
                Restore("mode", "position", 1, value)
        self.assertEqual(Restore("mode", "position", 1, 7).transform, 7)

    def test_active_and_away_inputs(self):
        """Validate byte inputs, duplicates, disjointness, and collection types."""
        for value in (None, [], True, -1, 256, 1.0):
            with self.subTest(active=value):
                kwargs = valid_config_kwargs()
                kwargs["active_input"] = value
                with self.assertRaises(ValueError):
                    Config(**kwargs)
        for value in (
            None,
            "",
            [],
            (),
            set(),
            [2, 2],
            [1, 2],
            [True],
            [-1],
            [256],
            [[2]],
        ):
            with self.subTest(away=value):
                kwargs = valid_config_kwargs()
                kwargs["away_inputs"] = value
                with self.assertRaises(ValueError):
                    Config(**kwargs)
        for value in ([0, 255], (0, 255), {0, 255}, frozenset({0, 255})):
            with self.subTest(away=value):
                kwargs = valid_config_kwargs()
                kwargs["away_inputs"] = value
                self.assertEqual(Config(**kwargs).away_inputs, frozenset({0, 255}))
        data = valid_mapping()
        data["away_inputs"] = [2, 2]
        with self.assertRaises(ValueError):
            Config.from_mapping(data)

    def test_remaining_constructor_validation(self):
        """Validate restore, interval, and confirmations on direct construction."""
        for field, values in (
            ("restore", (None, {}, [])),
            ("interval_seconds", (None, [], True, 0, 3601, math.nan, math.inf)),
            ("confirmations", (None, [], True, 0, 11, 1.0)),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    kwargs = valid_config_kwargs()
                    kwargs[field] = value
                    with self.assertRaises(ValueError):
                        Config(**kwargs)
        kwargs = valid_config_kwargs()
        kwargs.update(interval_seconds=1, confirmations=10)
        self.assertEqual(Config(**kwargs).confirmations, 10)
        kwargs.update(interval_seconds=3600, confirmations=1)
        self.assertEqual(Config(**kwargs).interval_seconds, 3600)

    def test_invalid_toml(self):
        """Report malformed TOML as a value error."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("not = [valid", encoding="utf-8")
            with self.assertRaises(ValueError):
                Config.load(path)


if __name__ == "__main__":
    unittest.main()
