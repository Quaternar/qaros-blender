# SPDX-License-Identifier: MIT
"""Switching the Renderer of a running stream never freezes Blender: a back end whose first start is slow
(VisRTX compiling its OptiX programs: about two minutes with an empty cache) is built on a thread beside the
running stream, which keeps streaming; the stream restarts on it when it is ready, and a back end that fails or
does not answer in time leaves the stream on the renderer it had. Run from the folder that holds anari.dll and
anari_library_qaros.dll (Helide stands in for the slow back end):

    blender -b --factory-startup --python-exit-code 1 --python test_blender_renderer_switch.py"""
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bpy  # noqa: F401 - qaros_anari needs Blender's modules

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))
import qaros_anari  # noqa: E402
from qaros_anari import Streamer, Warmup  # noqa: E402

FOLDER = os.getcwd()
SLOW = "helide"


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.prefs = SimpleNamespace(backend="visgl", anari_folder=FOLDER)
        self.streamer = Streamer()
        self.streamer.device = object()  # a running stream (nothing here renders on it)
        self.streamer.library_key = "visgl"
        self.restarts = []
        self.streamer.restart_now = lambda context: self.restarts.append(self.prefs.backend)
        self.release = threading.Event()
        patches = [
            patch.object(qaros_anari, "preferences", lambda: self.prefs),
            patch.object(qaros_anari, "WARM_BACKENDS", frozenset({SLOW})),
            patch.object(Streamer, "warmed", set()),
            patch.object(Streamer, "warmups", {}),
            patch.object(Streamer, "libraries", {}),
            patch.object(Streamer, "folder", None),
            patch.object(qaros_anari, "tag_redraw", lambda: None),
            patch.object(qaros_anari.bpy.app.timers, "register", lambda *args, **kwargs: None),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.prefs.backend = SLOW

    def slow_warmup(self, error=None):
        def run(warmup, folder, on_status):
            self.release.wait(10)
            warmup.error = error
        return patch.object(Warmup, "run", run)

    def test_the_switch_returns_at_once_and_the_old_stream_keeps_streaming(self):
        with self.slow_warmup():
            started = time.monotonic()
            self.assertTrue(self.streamer.restart(None))
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(self.restarts, [])
            self.assertTrue(self.streamer.running)
            icon, text, alert = self.streamer.state()
            self.assertEqual(icon, "SORTTIME")
            self.assertIn("Starting", text)
            self.assertIn("first start can take a few minutes", text)
            self.assertFalse(alert)
            self.assertFalse(self.streamer.can_restart())
            self.assertEqual(self.streamer.restart_reasons(), ())
            # Still waiting: polling keeps the timer going and changes nothing.
            self.assertEqual(self.streamer.poll_switch(), qaros_anari.SWITCH_POLL_INTERVAL)
            self.release.set()
            self.streamer.switching.warmup.thread.join(5)
            self.assertIsNone(self.streamer.poll_switch())
            self.assertEqual(self.restarts, [SLOW])
            self.assertIn(SLOW, Streamer.warmed)
            self.assertIsNone(self.streamer.switching)

    def test_a_warm_back_end_restarts_at_once(self):
        Streamer.warmed.add(SLOW)
        self.assertTrue(self.streamer.restart(None))
        self.assertEqual(self.restarts, [SLOW])

    def test_a_failure_falls_back_to_the_previous_renderer(self):
        self.prefs.backend = SLOW
        with self.slow_warmup(error="no CUDA device"):
            self.assertTrue(self.streamer.restart(None))
            self.release.set()
            self.streamer.switching.warmup.thread.join(5)
            self.assertIsNone(self.streamer.poll_switch())
        self.assertEqual(self.restarts, [])
        self.assertEqual(self.prefs.backend, "visgl")
        self.assertTrue(self.streamer.running)
        icon, text, alert = self.streamer.state()
        self.assertEqual(icon, "ERROR")
        self.assertTrue(alert)
        self.assertIn("no CUDA device", text)
        self.assertIn("Still on", text)
        self.assertNotIn(SLOW, Streamer.warmups)

    def test_a_wait_that_never_ends_times_out_and_falls_back(self):
        with self.slow_warmup():
            self.assertTrue(self.streamer.restart(None))
            with patch.object(qaros_anari, "WARMUP_TIMEOUT_S", 0.0):
                time.sleep(0.05)
                self.assertIsNone(self.streamer.poll_switch())
            self.release.set()
        self.assertEqual(self.restarts, [])
        self.assertEqual(self.prefs.backend, "visgl")
        self.assertIn("no answer after", self.streamer.state()[1])

    def test_stopping_cancels_the_switch(self):
        with self.slow_warmup():
            self.assertTrue(self.streamer.restart(None))
            self.streamer.release = lambda: None
            self.streamer.stop()
            self.release.set()
            self.assertIsNone(self.streamer.poll_switch())
        self.assertEqual(self.restarts, [])

    def test_a_failed_restart_starts_the_previous_renderer_again(self):
        Streamer.warmed.add(SLOW)
        started = []

        def restart_now(context):
            raise RuntimeError("device refused")

        self.streamer.restart_now = restart_now
        self.streamer.stop = lambda wait=False: setattr(self.streamer, "device", None)
        self.streamer.start = lambda context: (started.append(self.prefs.backend), setattr(self.streamer, "device", object()))
        self.streamer.switching = qaros_anari.Switch(SLOW, "visgl", SimpleNamespace(alive=False, error=None, started=0.0))
        with patch.object(qaros_anari, "preferences", lambda: self.prefs):
            self.assertIsNone(self.streamer.poll_switch())
        self.assertEqual(started, ["visgl"])
        self.assertEqual(self.prefs.backend, "visgl")
        self.assertIn("device refused", self.streamer.switch_error)

    def test_a_real_warm_up_builds_a_device_on_its_own_thread_and_ends(self):
        warmup = Warmup(SLOW, FOLDER, lambda severity, message: None)
        warmup.thread.join(60)
        self.assertFalse(warmup.alive)
        self.assertIsNone(warmup.error)
        self.assertIsNot(warmup.thread, threading.current_thread())
        self.assertIn(SLOW, Streamer.libraries)

    def test_a_warm_up_that_cannot_load_reports_the_error(self):
        warmup = Warmup(SLOW, str(Path(FOLDER) / "missing"), lambda severity, message: None)
        warmup.thread.join(60)
        self.assertTrue(warmup.error)
        self.assertNotIn(SLOW, Streamer.libraries)

    def test_choosing_a_slow_back_end_without_a_stream_prepares_it_in_the_background(self):
        self.streamer.device = None
        with self.slow_warmup():
            warmup = self.streamer.prewarm(SLOW)
            self.assertIsNotNone(warmup)
            self.assertIs(self.streamer.prewarm(SLOW), warmup)
            self.release.set()
            warmup.thread.join(5)
        self.assertIsNone(self.streamer.prewarm("visgl"))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
