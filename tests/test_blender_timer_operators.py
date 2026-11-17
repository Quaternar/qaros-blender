# SPDX-License-Identifier: MIT
"""Operators the add-on runs from a timer (the controls panel's undo push, undo, redo, play, the automatic
Start) get a window override built fresh each time and only for a window that can take it: a temporary
window (a file browser) or one with a modal operator (an import) gets none. An undo push through a stale
override read the image editor's context through freed memory and crashed Blender 0.5.0:

    blender -b --factory-startup --python-exit-code 1 --python test_blender_timer_operators.py"""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bpy
from _bpy_internal.addons.cli import set_from_cli

ADDON = Path(__file__).resolve().parents[1] / "blender"
sys.path.insert(0, str(ADDON))
os.environ.pop("QAR_BLENDER_AUTO_START", None)
set_from_cli("qaros_anari")
import qaros_anari  # noqa: E402,F401
from qaros_anari import controls, shading  # noqa: E402

USD = Path(__file__).resolve().parent / "usd" / "usd_scene.usda"


def screen(areas=("VIEW_3D",), temporary=False):
    made = [SimpleNamespace(type=kind, regions=[SimpleNamespace(type="HEADER"), SimpleNamespace(type="WINDOW")])
            for kind in areas]
    return SimpleNamespace(is_temporary=temporary, areas=made)


def window(screen_, modal=()):
    return SimpleNamespace(screen=screen_, modal_operators=modal)


def manager(*windows):
    return SimpleNamespace(windows=list(windows))


class OverrideTests(unittest.TestCase):
    def override(self, *windows):
        with patch.object(controls.bpy, "context", SimpleNamespace(window_manager=manager(*windows))):
            return controls._window_override()

    def test_a_3d_view_window_gets_window_screen_area_and_region(self):
        good = window(screen())
        override = self.override(good)
        self.assertIs(override["window"], good)
        self.assertIs(override["screen"], good.screen)
        self.assertEqual(override["area"].type, "VIEW_3D")
        self.assertEqual(override["region"].type, "WINDOW")

    def test_a_temporary_window_is_skipped_for_the_next_one(self):
        browser = window(screen(("FILE_BROWSER",), temporary=True))
        good = window(screen())
        self.assertIs(self.override(browser, good)["window"], good)

    def test_a_window_with_a_modal_operator_is_not_used(self):
        self.assertIsNone(self.override(window(screen(), modal=(object(),))))

    def test_no_window_or_no_areas_gives_none(self):
        self.assertIsNone(self.override())
        self.assertIsNone(self.override(window(screen(areas=()))))
        self.assertIsNone(self.override(window(None)))

    def test_every_call_builds_a_new_override(self):
        good = window(screen())
        with patch.object(controls.bpy, "context", SimpleNamespace(window_manager=manager(good))):
            first, second = controls._window_override(), controls._window_override()
        self.assertIsNot(first, second)

    def test_a_window_without_a_3d_view_still_gets_window_and_screen(self):
        override = self.override(window(screen(("PROPERTIES",))))
        self.assertEqual(set(override), {"window", "screen"})

    def test_with_nothing_ready_the_undo_push_is_skipped_and_undo_says_so(self):
        with patch.object(controls.bpy, "context", SimpleNamespace(window_manager=manager())):
            controls.push_undo()  # no operator, no crash
            with self.assertRaisesRegex(RuntimeError, "no Blender window ready"):
                controls.run_call("undo")


class FakePanel:
    def __init__(self):
        self.posted = []

    def subscribe(self, handler):
        self.handler = handler

    def post(self, text):
        self.posted.append(text)


class TimerPathTests(unittest.TestCase):
    """The panel tick's sequence in Blender itself: shading changes, an import, a selection and a move
    through the add-on's callbacks, an undo push and an undo, each between timer-style calls."""

    def test_shading_import_selection_move_and_undo_around_the_timer_calls(self):
        wm = bpy.context.window_manager
        for mode in (shading.WIREFRAME, shading.SOLID, shading.RENDERED, shading.MATERIAL, shading.SOLID, shading.FOLLOW):
            wm.qaros_shading = mode
            controls.push_undo()
        before = set(bpy.data.objects)
        self.assertEqual(bpy.ops.wm.usd_import(filepath=str(USD), import_all_materials=True), {"FINISHED"})
        controls.push_undo()
        new = [obj.name for obj in bpy.data.objects if obj not in before]
        self.assertTrue(new)
        panel = controls.Controls(FakePanel())
        try:
            for name in new:
                obj = bpy.data.objects.get(name)  # an undo may have taken the imported objects away
                if obj is None:
                    break
                controls.select_only(name)
                obj.matrix_world.translation = (1.0, 2.0, 3.0)
                wm.qaros_shading = shading.WIREFRAME
                panel.undo_pending = True
                panel.step()  # the tick: follows the selection, then pushes the undo step
                self.assertFalse(panel.undo_pending)
                bpy.ops.ed.undo()
                panel.step()
        finally:
            panel.close()

    def test_an_undo_step_waits_for_a_window_that_can_take_it(self):
        panel = controls.Controls(FakePanel())
        try:
            panel.undo_pending = True
            with patch.object(controls, "push_undo", return_value=False) as push:
                panel.step()
                self.assertTrue(panel.undo_pending)
            with patch.object(controls, "push_undo", return_value=True):
                panel.step()
                self.assertFalse(panel.undo_pending)
            self.assertEqual(push.call_count, 1)
        finally:
            panel.close()


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
