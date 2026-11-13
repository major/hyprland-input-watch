"""Test read-only CLI commands and shutdown without hardware."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_watcher import Desktop

from hyprland_input_watch import cli

CONFIG_TEXT = """
monitor_serial = "LEFT123"
fallback_serial = "RIGHT456"
active_input = 15
away_inputs = [17, 27]
[restore]
mode = "preferred"
position = "auto-left"
scale = 1
"""


class CliTests(unittest.TestCase):
    """Exercise the command line through its subprocess boundary."""

    def setUp(self):
        """Prepare isolated configuration and a simulated desktop."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.toml"
        self.path.write_text(CONFIG_TEXT)
        self.desktop = Desktop()
        self.commands = patch(
            "hyprland_input_watch.watcher.subprocess.run", side_effect=self.desktop.run
        )
        self.commands.start()
        self.addCleanup(self.commands.stop)

    def invoke(self, args):
        """Capture the result and output of a CLI invocation."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = cli.main(args)
        return result, out.getvalue(), err.getvalue()

    def test_probe_without_config_is_read_only(self):
        """Probe reports input codes without loading configuration or changing outputs."""
        result, output, _ = self.invoke(
            ["--config", str(self.path / "missing"), "probe"]
        )
        self.assertEqual(result, 0)
        data = json.loads(output)
        self.assertEqual(len(data["monitors"]), 2)
        self.assertEqual(data["monitors"][0]["input"], "0x0f")
        self.assertEqual(self.desktop.actions, [])

    def test_probe_reports_ambiguous_identity_without_ddc(self):
        """An ambiguous Hyprland serial is reported without a query."""
        self.desktop.monitors.append(self.desktop.monitors[0].copy())
        with patch(
            "hyprland_input_watch.watcher.subprocess.run", wraps=self.desktop.run
        ) as run:
            result, output, _ = self.invoke(["probe", "--serial", "LEFT123"])
        self.assertNotEqual(result, 0)
        self.assertIn("error", json.loads(output)["monitors"][0])
        self.assertFalse(
            any(call.args[0][0] == "ddcutil" for call in run.call_args_list)
        )

    def test_probe_without_monitors_reports_an_error(self):
        """An empty inventory must not look like successful discovery."""
        self.desktop.monitors = []

        result, output, _ = self.invoke(["probe"])

        self.assertNotEqual(result, 0)
        data = json.loads(output)
        self.assertEqual(data["monitors"], [])
        self.assertIn("errors", data)
        self.assertEqual(self.desktop.actions, [])

    def test_check_is_read_only(self):
        """Check reports both matched outputs and the current input."""
        result, output, _ = self.invoke(["--config", str(self.path), "check"])
        self.assertEqual(result, 0)
        data = json.loads(output)
        self.assertEqual(data["target"]["name"], "DP-1")
        self.assertEqual(data["fallback"]["name"], "DP-2")
        self.assertEqual(data["input"], "0x0f")
        self.assertEqual(self.desktop.actions, [])

    def test_read_only_commands_report_ddc_failures(self):
        """A failed DDC query is reported without attempting any monitor changes."""
        self.desktop.source = subprocess.TimeoutExpired("ddcutil", 8)
        for args in (["probe"], ["--config", str(self.path), "check"]):
            with self.subTest(args=args):
                result, output, _ = self.invoke(args)
                self.assertNotEqual(result, 0)
                self.assertIn("error", output)
                json.loads(output)
                self.assertEqual(self.desktop.actions, [])

    def test_configuration_errors_are_clean(self):
        """Missing and malformed files return errors rather than tracebacks."""
        for contents in (None, "not = [valid"):
            with self.subTest(contents=contents):
                if contents is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.write_text(contents)
                result, _, error = self.invoke(["--config", str(self.path), "check"])
                self.assertNotEqual(result, 0)
                self.assertNotIn("Traceback", error)
                self.assertTrue(error)

    def test_run_cleans_up_and_releases_lock(self):
        """A shutdown request ends the loop and leaves the lock available."""

        def request_stop(signum, handler):
            """Invoke the installed handler immediately without delivering a signal."""
            handler(signum, None)

        with patch.dict("os.environ", {"XDG_RUNTIME_DIR": self.temp.name}):
            with patch(
                "hyprland_input_watch.cli.signal.signal", side_effect=request_stop
            ):
                result, _, _ = self.invoke(["--config", str(self.path), "run"])
            self.assertEqual(result, 0)
            self.assertTrue(
                (Path(self.temp.name) / "hyprland-input-watch.lock").exists()
            )
            with cli.lock_instance(Path(self.temp.name) / "hyprland-input-watch.lock"):
                pass
        self.assertEqual(self.desktop.actions, [])
