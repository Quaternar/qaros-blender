# SPDX-License-Identifier: MIT
"""The Blender add-on's stop detection and automatic Start (qaros_anari/hub_launch.py), with plain
Python: a Blender that QAROS started quits when QAROS closes its stdin; one started by hand never
does. One started from the Blender tile starts streaming by itself, once, also when its preferences
do not list the add-on."""
import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

path = Path(__file__).resolve().parents[1] / "blender" / "qaros_anari" / "hub_launch.py"
spec = importlib.util.spec_from_file_location("hub_launch", path)
hub_launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub_launch)

WAIT = 5.0


class StartedByHubTests(unittest.TestCase):
    def test_a_launcher_names_its_instance_and_pipes_stdin(self):
        self.assertTrue(hub_launch.started_by_hub({"QAR_INSTANCE": "dev-abc"}, True))

    def test_a_blender_started_by_hand_is_not_stopped_by_its_stdin(self):
        self.assertFalse(hub_launch.started_by_hub({}, True), "a piped stdin alone")
        self.assertFalse(hub_launch.started_by_hub({"QAR_INSTANCE": ""}, True), "an empty instance")
        self.assertFalse(hub_launch.started_by_hub({"QAR_INSTANCE": "default"}, False), "a console")


class StdinKindTests(unittest.TestCase):
    def test_a_pipe_is_a_pipe(self):
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb"):
            self.assertTrue(hub_launch.stdin_is_pipe(reader))

    def test_a_file_or_no_stdin_is_not(self):
        with tempfile.TemporaryFile() as file:
            self.assertFalse(hub_launch.stdin_is_pipe(file))
        self.assertFalse(hub_launch.stdin_is_pipe(object()))


class StopRequestWatcherTests(unittest.TestCase):
    def test_the_end_of_stdin_is_the_stop_request(self):
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as reader:
            writer = os.fdopen(write_fd, "wb")
            watcher = hub_launch.StopRequestWatcher(reader)
            watcher.start()
            # An app given an onboarding code reads it as its first line: that is no stop.
            writer.write(b"onboarding-code\n")
            writer.flush()
            self.assertFalse(watcher.wait(0.3), "a line on stdin is not a stop request")
            writer.close()
            self.assertTrue(watcher.wait(WAIT), "closing stdin must be seen as the stop request")


class AutoStartRequestedTests(unittest.TestCase):
    def parse(self, value):
        warnings = []
        environ = {} if value is None else {hub_launch.AUTO_START_ENVIRONMENT: value}
        return hub_launch.auto_start_requested(environ, warnings.append), warnings

    def test_the_tiles_variable_name(self):
        self.assertEqual(hub_launch.AUTO_START_ENVIRONMENT, "QAR_BLENDER_AUTO_START")

    def test_on_words_in_any_case(self):
        for value in ("1", "true", "on", "yes", "TRUE", "Yes", " on "):
            self.assertEqual(self.parse(value), (True, []), value)

    def test_off_words_are_off_without_a_warning(self):
        for value in ("0", "false", "off", "no", "OFF", "No"):
            self.assertEqual(self.parse(value), (False, []), value)

    def test_unset_or_empty_is_off_without_a_warning(self):
        self.assertEqual(self.parse(None), (False, []))
        self.assertEqual(self.parse(""), (False, []))
        self.assertEqual(self.parse("  "), (False, []))

    def test_other_text_is_off_with_a_warning(self):
        for value in ("2", "enabled", "y"):
            started, warnings = self.parse(value)
            self.assertFalse(started, value)
            self.assertEqual(len(warnings), 1, value)
            self.assertIn(repr(value), warnings[0])


class AutoStartOnceTests(unittest.TestCase):
    def test_a_requested_start_happens_once(self):
        auto_start = hub_launch.AutoStartOnce(True)
        self.assertTrue(auto_start.pending)
        self.assertTrue(auto_start.take(running=False))
        self.assertFalse(auto_start.pending)
        # A failed join leaves streaming off: no second attempt, the user presses Start.
        self.assertFalse(auto_start.take(running=False))

    def test_no_start_when_not_requested(self):
        auto_start = hub_launch.AutoStartOnce(False)
        self.assertFalse(auto_start.pending)
        self.assertFalse(auto_start.take(running=False))

    def test_no_start_when_the_user_already_started(self):
        auto_start = hub_launch.AutoStartOnce(True)
        self.assertFalse(auto_start.take(running=True))
        self.assertFalse(auto_start.take(running=False), "a Stop afterwards does not start it again")


class PreferenceDefaultsTests(unittest.TestCase):
    """Blender's deferred properties carry the property function and its keywords."""

    @staticmethod
    def deferred(function_name, **keywords):
        function = type(function_name, (), {})
        function.__name__ = function_name
        return SimpleNamespace(function=function, keywords=keywords)

    def test_declared_defaults_else_the_types_zero(self):
        defaults = hub_launch.preference_defaults({
            "anari_folder": self.deferred("StringProperty", name="ANARI folder", subtype="DIR_PATH"),
            "backend": self.deferred("EnumProperty", items=(), default="auto"),
            "max_users": self.deferred("IntProperty", default=2, min=1),
            "render_scale": self.deferred("FloatProperty", default=0.0),
            "enabled": self.deferred("BoolProperty"),
        })
        self.assertEqual(defaults.anari_folder, "")
        self.assertEqual(defaults.backend, "auto")
        self.assertEqual(defaults.max_users, 2)
        self.assertEqual(defaults.render_scale, 0.0)
        self.assertIs(defaults.enabled, False)


if __name__ == "__main__":
    unittest.main()
