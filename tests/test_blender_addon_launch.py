# SPDX-License-Identifier: MIT
"""The add-on as QAROS starts it, in Blender itself (background mode): enabled by ``--addons`` alone,
with factory preferences that do not list it, and QAR_BLENDER_AUTO_START=1 (set here, before the
add-on registers). The ANARI library is replaced, so nothing joins a hub:

    blender -b --factory-startup --python-exit-code 1 --python test_blender_addon_launch.py

Regression (live test 2026-11-14): Start read ``preferences.addons["qaros_anari"]``, which a Blender
started with ``--addons`` has no entry for, so both the automatic and the manual Start failed with
'key "qaros_anari" not found'."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import bpy
from _bpy_internal.addons.cli import set_from_cli

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))
# What the Blender tile sets; read when the add-on registers.
os.environ["QAR_BLENDER_AUTO_START"] = "1"
# What `--addons qaros_anari` runs: addon_utils.enable(..., persistent=True), no default_set.
set_from_cli("qaros_anari")
import qaros_anari  # noqa: E402

FAKE_FAILURE = "fake ANARI library, no hub joined"


class FakeLibrary:
    calls = []

    def __init__(self, folder, name, on_status):
        FakeLibrary.calls.append((folder, name))
        raise RuntimeError(FAKE_FAILURE)


class LaunchedAddonTests(unittest.TestCase):
    def setUp(self):
        FakeLibrary.calls.clear()
        qaros_anari.STREAMER.error = None

    def test_1_launched_addon_is_not_in_the_preferences_and_reads_defaults(self):
        self.assertIn("qaros_anari", sys.modules)
        self.assertIsNone(bpy.context.preferences.addons.get("qaros_anari"), "the cause of the bug")
        prefs = qaros_anari.preferences()
        self.assertEqual(prefs.anari_folder, "")
        self.assertEqual(prefs.backend, qaros_anari.BACKEND_AUTOMATIC)
        self.assertEqual(prefs.max_users, 2)
        self.assertEqual(prefs.fps, 30)

    def test_1b_controls_panel_settings_read_and_keep_the_defaults(self):
        tabs, panels = qaros_anari.controls_settings()
        self.assertEqual(tabs, ("tools", "object", "material", "frame", "panels"))
        self.assertEqual(panels, qaros_anari.blender_panels.DEFAULT_PANELS)
        first = panels[0]
        self.assertEqual(bpy.ops.qaros.toggle_controls_panel(panel=first), {"FINISHED"})
        self.assertNotIn(first, qaros_anari.controls_settings()[1], "a toggle holds for the session")
        self.assertEqual(bpy.ops.qaros.reset_controls_panels(), {"FINISHED"})
        self.assertEqual(qaros_anari.controls_settings()[1], panels)

    def test_1c_the_tool_starts_at_navigation_and_the_headset_changes_it_for_the_session(self):
        self.assertEqual(qaros_anari.gesture_tool(), qaros_anari.TOOL_NAVIGATE)
        self.assertEqual(bpy.ops.qaros.set_tool(tool="SELECT"), {"FINISHED"}, "the QAROS tab's buttons")
        self.assertEqual(qaros_anari.gesture_tool(), "SELECT")
        qaros_anari.set_gesture_tool(qaros_anari.TOOL_NAVIGATE)  # the Tools tab in the headset
        self.assertEqual(qaros_anari.gesture_tool(), qaros_anari.TOOL_NAVIGATE)

    def test_2_manual_start_reaches_the_library(self):
        with patch.object(qaros_anari.anari, "Library", FakeLibrary):
            with self.assertRaises(RuntimeError):
                bpy.ops.qaros.start_streaming("EXEC_DEFAULT")
        self.assertEqual(FakeLibrary.calls, [("", qaros_anari.LIBRARY_NAME)])
        self.assertEqual(qaros_anari.STREAMER.error, f"Cannot start: {FAKE_FAILURE}")
        self.assertFalse(qaros_anari.STREAMER.running)

    def test_3_auto_start_is_scheduled_and_starts_once(self):
        self.assertEqual(os.environ.get("QAR_BLENDER_AUTO_START"), "1")
        self.assertTrue(bpy.app.timers.is_registered(qaros_anari.auto_start_streaming))
        # Background Blender runs no timers: call what the timer calls.
        with patch.object(qaros_anari.anari, "Library", FakeLibrary):
            self.assertIsNone(qaros_anari.auto_start_streaming())
            self.assertEqual(FakeLibrary.calls, [("", qaros_anari.LIBRARY_NAME)])
            self.assertEqual(qaros_anari.STREAMER.error, f"Cannot start: {FAKE_FAILURE}")
            # A failed start waits for the user: no second attempt.
            self.assertIsNone(qaros_anari.auto_start_streaming())
            self.assertEqual(len(FakeLibrary.calls), 1)


if __name__ == "__main__":
    result = unittest.main(argv=[sys.argv[0]], exit=False).result
    if not result.wasSuccessful():
        sys.exit(1)
