# SPDX-License-Identifier: MIT
"""Pointing into the app volume against a real Blender scene (background mode, factory scene, no
device): the click ray picks, selection follows, and a drag moves the picked object.

    blender -b --factory-startup --python-exit-code 1 --python test_blender_pointing_scene.py"""
from pathlib import Path
import sys
import unittest

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))
from qaros_anari import controls, pointing  # noqa: E402


def content(blender):
    """Blender world -> app content space (sync.py's Z_UP_TO_Y_UP)."""
    x, y, z = blender
    return (x, z, -y)


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.cube = bpy.data.objects["Cube"]
        self.cube.location = (0.0, 0.0, 0.0)
        bpy.context.view_layer.update()

    def click(self, hand, target):
        return pointing.click_ray({"start": content(hand), "action": content(target)})

    def test_a_click_through_the_cube_picks_it(self):
        ray = self.click((0.0, -5.0, 0.0), (0.0, 0.0, 0.0))
        self.assertEqual(controls.pick(*ray), "Cube")

    def test_a_click_past_the_cube_picks_nothing(self):
        ray = self.click((0.0, -5.0, 3.0), (0.0, 0.0, 3.0))
        self.assertIsNone(controls.pick(*ray))

    def test_selecting_makes_the_object_the_only_selected_and_active_one(self):
        bpy.data.objects["Camera"].select_set(True)
        controls.select_only("Cube")
        layer = bpy.context.view_layer
        self.assertIs(layer.objects.active, self.cube)
        self.assertEqual([o.name for o in layer.objects if o.select_get()], ["Cube"])

    def test_a_drag_moves_the_picked_cube_in_blender_axes(self):
        state = pointing.Pointing()
        state.clicked("peer", controls.pick(*self.click((0.0, -5.0, 0.0), (0.0, 0.0, 0.0))))
        state.drag_started("peer", 1, tuple(self.cube.matrix_world.translation))
        name, target = state.drag_moved("peer", 1, content((1.0, 0.0, 0.5)))
        fake = controls.Controls.__new__(controls.Controls)  # only move(): no panel, no timer
        fake.pointing = state
        fake.gestures = None
        fake.move(name, target)
        bpy.context.view_layer.update()
        self.assertAlmostEqual(self.cube.location.x, 1.0, places=5)
        self.assertAlmostEqual(self.cube.location.z, 0.5, places=5)


if __name__ == "__main__":
    result = unittest.main(argv=[sys.argv[0]], exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
