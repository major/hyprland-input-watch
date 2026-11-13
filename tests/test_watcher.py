"""Exercise monitor input changes without accessing hardware or sleeping."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hyprland_input_watch import watcher as watch
from hyprland_input_watch.config import Config, Restore

CONFIG = Config(
    "LEFT123",
    "RIGHT456",
    0x0F,
    frozenset({0x11, 0x1B}),
    Restore("preferred", "auto-left", 1),
)


class Desktop:
    """Replace only the external ddcutil and hyprctl command boundary."""

    def __init__(self):
        """Start with both monitor identities enabled on DisplayPort."""
        self.monitors = [
            {
                "name": "DP-1",
                "serial": "LEFT123",
                "disabled": False,
                "dpmsStatus": True,
            },
            {
                "name": "DP-2",
                "serial": "RIGHT456",
                "disabled": False,
                "dpmsStatus": True,
            },
        ]
        self.source = "VCP 60 SNC x0f\n"
        self.actions = []
        self.reply = "ok\n"
        self.apply_changes = True
        self.eval_error = None
        self.monitor_output = None

    def run(self, args, **kwargs):
        """Return command responses and simulate compositor state changes."""
        assert 0 < kwargs["timeout"] <= 8 and kwargs["check"] is True
        if args == ["ddcutil", "detect", "--terse"]:
            output = "Display 1\n Monitor: ABC:MODEL:LEFT123\n\nDisplay 2\n Monitor: ABC:MODEL:RIGHT456\n"
        elif args[0] == "ddcutil":
            assert "--sn" in args and args[args.index("--sn") + 1] in {
                "LEFT123",
                "RIGHT456",
            }
            if isinstance(self.source, Exception):
                raise self.source
            output = self.source
        elif args[1] == "-j":
            output = (
                json.dumps(self.monitors)
                if self.monitor_output is None
                else self.monitor_output
            )
        else:
            assert args[1] == "eval"
            self.actions.append(args[2])
            if self.apply_changes and self.reply.strip() == "ok":
                self.monitors[0]["disabled"] = "disabled = true" in args[2]
            if self.eval_error:
                raise self.eval_error
            output = self.reply
        return subprocess.CompletedProcess(args, 0, output, "")


class WatcherTests(unittest.TestCase):
    """Test the real parser, safety checks, and transitions together."""

    def setUp(self):
        """Create an isolated desktop and patch external command execution."""
        self.desktop = Desktop()
        self.commands = patch.object(
            watch.subprocess, "run", side_effect=self.run_external
        )
        self.run_mock = self.commands.start()
        self.addCleanup(self.commands.stop)
        self.sleep = patch.object(watch.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.watcher = watch.Watcher(CONFIG)

    def run_external(self, args, **kwargs):
        """Use the current fixture, including replacements within subtests."""
        return self.desktop.run(args, **kwargs)

    def switch_away(self, source="VCP 60 SNC x11\n"):
        """Provide two matching away observations to disable the left output."""
        self.desktop.source = source
        self.watcher.step()
        self.watcher.step()

    def test_switch_away_and_back(self):
        """Debounce both supported alternate inputs and restore the layout rule."""
        for source in ("VCP 60 SNC x11\n", "VCP 60 SNC x1b\n"):
            with self.subTest(source=source):
                self.desktop = Desktop()
                self.watcher = watch.Watcher(CONFIG)

                self.desktop.source = source
                self.watcher.step()
                self.assertEqual(self.desktop.actions, [])
                self.watcher.step()
                self.assertTrue(self.desktop.monitors[0]["disabled"])
                self.assertTrue(self.watcher.owned)

                self.desktop.source = "VCP 60 SNC x0f\n"
                self.watcher.step()
                self.assertEqual(len(self.desktop.actions), 1)
                self.watcher.step()

                self.assertFalse(self.desktop.monitors[0]["disabled"])
                self.assertFalse(self.watcher.owned)
                self.assertEqual(len(self.desktop.actions), 2)
                self.assertIn('mode = "preferred"', self.desktop.actions[-1])
                self.assertIn('position = "auto-left"', self.desktop.actions[-1])
                self.assertFalse(self.desktop.monitors[1]["disabled"])

    def test_errors_and_unknown_values_break_confirmation(self):
        """Never treat a failed or unsupported input reading as a switch."""
        failures = (
            "",
            "VCP 60 SNC x99\n",
            "VCP 60 ERR\n",
            subprocess.TimeoutExpired("ddcutil", 8),
            subprocess.CalledProcessError(1, "ddcutil"),
        )
        for failure in failures:
            with self.subTest(failure=failure):
                self.desktop = Desktop()
                self.watcher = watch.Watcher(CONFIG)
                self.desktop.source = "VCP 60 SNC x11\n"
                self.watcher.step()

                self.desktop.source = failure
                self.watcher.step()
                self.desktop.source = "VCP 60 SNC x11\n"
                self.watcher.step()

                self.assertEqual(self.desktop.actions, [])
                self.watcher.step()
                self.assertTrue(self.desktop.monitors[0]["disabled"])

    def test_disable_requires_correct_identities_and_an_awake_backup(self):
        """Leave the layout alone when either display is unsuitable."""
        changes = (
            (0, "serial", "another-display"),
            (0, "disabled", True),
            (1, "serial", "another-display"),
            (1, "disabled", True),
            (1, "dpmsStatus", False),
            (1, "dpmsStatus", None),
        )
        for index, key, value in changes:
            with self.subTest(index=index, key=key, value=value):
                self.desktop = Desktop()
                self.watcher = watch.Watcher(CONFIG)
                self.desktop.monitors[index][key] = value

                self.switch_away()

                self.assertEqual(self.desktop.actions, [])

    def test_missing_or_duplicate_identity_prevents_changes(self):
        """Reject ambiguous identities instead of picking a convenient output."""
        for monitors in (
            [],
            [self.desktop.monitors[0]],
            [self.desktop.monitors[1]],
            self.desktop.monitors + [self.desktop.monitors[0].copy()],
        ):
            with self.subTest(monitors=monitors):
                self.desktop.monitors = monitors
                self.watcher = watch.Watcher(CONFIG)

                self.switch_away()

                self.assertEqual(self.desktop.actions, [])

    def test_does_not_adopt_a_previously_disabled_display(self):
        """A new watcher must not undo a manual disable or assume crash ownership."""
        self.desktop.monitors[0]["disabled"] = True

        self.watcher.step()
        self.watcher.step()
        self.watcher.close()

        self.assertEqual(self.desktop.actions, [])

    def test_malformed_monitor_data_prevents_changes(self):
        """Reject partial, missing, or malformed monitor fields safely."""
        for output in ("null", "{}", "[null]", "invalid JSON"):
            with self.subTest(output=output):
                self.desktop.monitor_output = output

                self.switch_away()

                self.assertEqual(self.desktop.actions, [])
        self.desktop.monitor_output = None
        for key in ("name", "disabled"):
            with self.subTest(missing=key):
                self.desktop = Desktop()
                del self.desktop.monitors[0][key]
                self.watcher = watch.Watcher(CONFIG)

                self.switch_away()

                self.assertEqual(self.desktop.actions, [])

    def test_unknown_hyprctl_reply_does_not_claim_success(self):
        """Catch legacy-style unknown requests even when the exit code is zero."""
        self.desktop.reply = "unknown request\n"

        self.switch_away()

        self.assertFalse(self.desktop.monitors[0]["disabled"])
        self.assertFalse(self.watcher.owned)

    def test_unapplied_command_does_not_claim_success(self):
        """Confirm the actual state rather than trusting an ok response."""
        self.desktop.apply_changes = False

        self.switch_away()

        self.assertFalse(self.desktop.monitors[0]["disabled"])
        self.assertFalse(self.watcher.owned)

    def test_manual_reenable_stops_the_watcher_without_fighting(self):
        """Respect an external enable while the alternate input is selected."""
        self.switch_away()
        self.desktop.monitors[0]["disabled"] = False
        self.desktop.source = subprocess.TimeoutExpired("ddcutil", 8)

        keep_running = self.watcher.step()
        self.watcher.close()

        self.assertFalse(keep_running)
        self.assertFalse(self.watcher.owned)
        self.assertEqual(len(self.desktop.actions), 1)

    def test_disable_applied_despite_timeout_is_still_owned(self):
        """Retain ownership when the IPC request succeeds after timing out."""
        self.desktop.eval_error = subprocess.TimeoutExpired("hyprctl", 8)

        self.switch_away()

        self.assertTrue(self.watcher.owned)
        self.assertTrue(self.watcher.verified)
        self.desktop.eval_error = None
        self.watcher.close()
        self.assertFalse(self.desktop.monitors[0]["disabled"])

    def test_shutdown_flag_prevents_new_disable(self):
        """Never start another disable after shutdown has been requested."""
        self.watcher.stop.set()

        self.switch_away()

        self.assertEqual(self.desktop.actions, [])

    def test_failed_verification_retains_and_recovers_ownership(self):
        """Recover a partial operation without ignoring a later manual override."""

        def fail_verification(args, **kwargs):
            """Let the rule apply, then make the verification response invalid."""
            result = self.desktop.run(args, **kwargs)
            if args[0:2] == ["hyprctl", "eval"]:
                self.desktop.monitor_output = "null"
            return result

        for early_override in (False, True):
            with self.subTest(early_override=early_override):
                self.desktop = Desktop()
                self.watcher = watch.Watcher(CONFIG)
                with patch.object(
                    watch.subprocess, "run", side_effect=fail_verification
                ):
                    self.switch_away()
                self.assertTrue(self.watcher.owned)
                self.assertFalse(self.watcher.verified)

                self.desktop.monitor_output = None
                if not early_override:
                    self.watcher.step()
                    self.assertTrue(self.watcher.verified)
                self.desktop.monitors[0]["disabled"] = False
                self.desktop.source = subprocess.TimeoutExpired("ddcutil", 8)

                self.assertFalse(self.watcher.step())
                self.assertFalse(self.watcher.owned)

    def test_verification_uses_one_deadline(self):
        """Cap the whole verification phase, not just each individual query."""
        self.desktop.apply_changes = False
        clock = iter((100.0, 100.5))
        with patch.object(
            watch.time, "monotonic", side_effect=lambda: next(clock, 103.0)
        ):
            self.switch_away()

        verification_calls = [
            call for call in self.run_mock.call_args_list if call.kwargs["timeout"] < 8
        ]
        self.assertEqual(len(verification_calls), 1)
        self.assertEqual(verification_calls[0].kwargs["timeout"], 1.5)

    def test_failed_query_while_disabled_keeps_the_existing_layout(self):
        """A DDC failure must not restore or repeatedly disable an owned output."""
        self.switch_away()
        self.desktop.source = subprocess.TimeoutExpired("ddcutil", 8)

        self.watcher.step()

        self.assertTrue(self.desktop.monitors[0]["disabled"])
        self.assertTrue(self.watcher.owned)
        self.assertEqual(len(self.desktop.actions), 1)

    def test_close_restores_only_the_owned_matching_monitor(self):
        """Graceful shutdown restores its own output without selecting by name."""
        for replaced in (False, True):
            with self.subTest(replaced=replaced):
                self.desktop = Desktop()
                self.watcher = watch.Watcher(CONFIG)
                self.switch_away()
                if replaced:
                    self.desktop.monitors[0]["serial"] = "replacement"

                self.watcher.close()

                self.assertEqual(len(self.desktop.actions), 1 if replaced else 2)
                self.assertFalse(self.desktop.monitors[1]["disabled"])

    def test_lock_prevents_duplicate_watchers(self):
        """Hold an advisory lock for the process lifetime without writing state."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watcher.lock"
            with (
                watch.lock_instance(path),
                self.assertRaises(BlockingIOError),
                watch.lock_instance(path),
            ):
                self.fail("a second watcher acquired the lock")
            with watch.lock_instance(path):
                pass

    def test_ddc_detect_rejects_missing_duplicate_and_malformed_records(self):
        """Never query a serial unless every detected display is well formed and unique."""
        failures = (
            "",
            "No displays found",
            "Display 1\n Monitor: ABC:MODEL:OTHER\n",
            "Display 1\n Monitor: ABC:MODEL:LEFT123\n\n"
            "Display 2\n Monitor: DEF:MODEL:LEFT123\n",
            "Display 1\n Monitor: ABC:MODEL:NOTLEFT123\n",
            "Display 1\n Monitor: ABC:MODEL:LEFT123\n\nDisplay 2\n",
            "Display 1\n Monitor: ABC:MODEL:LEFT123\n\n"
            "Display 2\n Monitor: ABC:MODEL:\n",
            "Display 1\n Monitor: ABC:MODEL:LEFT123\n\nDisplay 2\n Monitor: broken\n",
            "Display 1\n Monitor: ABC:MODEL:LEFT123\n unexpected garbage\n",
            "Display 1\n Monitor: ABC:MODEL:LEFT123\n Display 1\n Monitor: A:B:OTHER\n",
            subprocess.TimeoutExpired("ddcutil", 8),
        )
        for detection in failures:
            with self.subTest(detection=detection):
                calls = []

                def run(args, *, detection=detection, calls=calls, **kwargs):
                    """Return a detection fixture and track attempted queries."""
                    calls.append(args)
                    if isinstance(detection, Exception):
                        raise detection
                    return subprocess.CompletedProcess(args, 0, detection, "")

                with (
                    patch.object(watch.subprocess, "run", side_effect=run),
                    self.assertRaises(RuntimeError),
                ):
                    watch.read_input("LEFT123")
                self.assertEqual(calls, [["ddcutil", "detect", "--terse"]])

    def test_generic_settings_and_lua_strings(self):
        """Use configured inputs and escape Unicode names and restore settings."""
        self.desktop.monitors[0]["name"] = 'DP-"雪\\1'
        config = Config(
            "LEFT123",
            "RIGHT456",
            0x20,
            frozenset({0x21}),
            Restore('1920x1080@"雪\\60', '1x"雪\\2', 1.25, 3),
            confirmations=3,
        )
        self.watcher = watch.Watcher(config)
        self.desktop.source = "VCP 60 SNC x21\n"
        for _ in range(3):
            self.watcher.step()
        self.assertTrue(self.watcher.owned)
        self.desktop.source = "VCP 60 SNC x20\n"
        for _ in range(3):
            self.watcher.step()
        rule = self.desktop.actions[-1]
        self.assertIn(
            json.dumps(self.desktop.monitors[0]["name"], ensure_ascii=False), rule
        )
        self.assertIn(json.dumps(config.restore.mode, ensure_ascii=False), rule)
        self.assertIn(json.dumps(config.restore.position, ensure_ascii=False), rule)
        self.assertIn("scale = 1.25", rule)
        self.assertIn("transform = 3", rule)
        self.assertEqual(self.watcher.samples, 3)

    def test_stop_during_verification_retains_uncertain_ownership(self):
        """Keep ownership when shutdown interrupts verification after an enabled sample."""
        self.desktop.apply_changes = False
        with patch.object(
            watch.time, "sleep", side_effect=lambda _: self.watcher.stop.set()
        ):
            self.switch_away()
        self.assertTrue(self.watcher.owned)
        self.assertFalse(self.watcher.verified)


if __name__ == "__main__":
    unittest.main()
