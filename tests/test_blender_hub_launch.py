# SPDX-License-Identifier: MIT
"""The Blender add-on's stop detection (qaros_anari/hub_launch.py), with plain Python: a Blender
that QAROS started quits when QAROS closes its stdin; one started by hand never does."""
import importlib.util
import os
from pathlib import Path
import tempfile
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


if __name__ == "__main__":
    unittest.main()
