# SPDX-License-Identifier: MIT
"""Pointing into the app volume (qaros_anari/pointing.py) and the object gesture mode
(qaros_anari/panel.py), with plain Python."""
import ctypes
import importlib.util
import math
from pathlib import Path
import unittest

folder = Path(__file__).resolve().parents[1] / "blender" / "qaros_anari"


def load(name):
    spec = importlib.util.spec_from_file_location(name, folder / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pointing = load("pointing")
panel = load("panel")


def close(a, b):
    return all(math.isclose(x, y, abs_tol=1e-6) for x, y in zip(a, b))


class ConversionTests(unittest.TestCase):
    def test_content_space_is_blender_with_y_up(self):
        # sync.py sends Blender (x, y, z) as (x, z, -y); the inverse takes it back.
        blender = (1.0, 2.0, 3.0)
        content = (blender[0], blender[2], -blender[1])
        self.assertEqual(pointing.content_to_blender(content), blender)

    def test_a_click_ray_runs_from_the_hand_to_the_hit(self):
        # Hand 5 units in front of the volume (content +Z), hit at the origin.
        origin, direction = pointing.click_ray({"start": (0.0, 0.0, 5.0), "action": (0.0, 0.0, 0.0)})
        self.assertTrue(close(origin, (0.0, -5.0, 0.0)))
        self.assertTrue(close(direction, (0.0, 1.0, 0.0)))

    def test_a_click_without_a_hit_or_length_casts_nothing(self):
        self.assertIsNone(pointing.click_ray({"start": (0.0, 0.0, 1.0), "action": None}))
        self.assertIsNone(pointing.click_ray({"start": (1.0, 1.0, 1.0), "action": (1.0, 1.0, 1.0)}))


class VolumeDeltaTests(unittest.TestCase):
    def test_an_unrotated_app_divides_by_its_scale(self):
        delta = pointing.volume_to_content_delta((0.25, 0.0, -0.5), (0.0, 0.0, 0.0, 1.0), 0.25)
        self.assertTrue(close(delta, (1.0, 0.0, -2.0)))

    def test_a_turned_app_turns_the_delta_back(self):
        half = math.sqrt(0.5)
        quarter_turn_about_y = (0.0, half, 0.0, half)  # content +X shows as volume -Z
        delta = pointing.volume_to_content_delta((0.0, 0.0, -1.0), quarter_turn_about_y, 1.0)
        self.assertTrue(close(delta, (1.0, 0.0, 0.0)), delta)


class HoverFallbackTests(unittest.TestCase):
    def test_a_click_without_a_hit_casts_the_hands_latest_hover_ray(self):
        # Measured live (simulated hand): the pinched hand sits nearer and lower than the hovering
        # one, so the click's start with the hover's hit passed under the cube.
        click = {"start": (0.0, 1.8, 8.35), "action": (0.0, 1.8, 8.35)}  # no hit: action == start
        self.assertFalse(pointing.has_hit(click))
        hover = ((0.0, 2.0, 9.0), (0.0, 0.0, 1.0))
        origin, direction = pointing.click_ray(click, hover=hover)
        self.assertTrue(close(origin, pointing.content_to_blender((0.0, 2.0, 9.0))))
        expected = pointing.content_to_blender((0.0, -2.0, -8.0))
        length = math.sqrt(sum(c * c for c in expected))
        self.assertTrue(close(direction, tuple(c / length for c in expected)))

    def test_the_hover_of_the_hand_that_clicked_is_chosen(self):
        # Measured live: the Visualizer's pointing hand and its pinching hand are both right hands.
        pinching = ((0.0, 1.8, 8.35), (0.1, -1.3, 0.7))
        pointing_hand = ((0.0, 1.88, 8.99), (0.0, 0.05, 0.93))
        hovers = [(9.0, pinching), (9.1, pointing_hand), (9.2, pinching), (9.3, pointing_hand)]
        self.assertEqual(pointing.closest_hover(hovers, (0.0, 1.81, 8.36), now=10.0), pinching)
        self.assertEqual(pointing.closest_hover(hovers, (0.0, 1.87, 8.98), now=10.0), pointing_hand)

    def test_hovers_older_than_the_window_are_not_used(self):
        hovers = [(1.0, ((0.0, 0.0, 5.0), (0.0, 0.0, 1.0)))]
        self.assertIsNone(pointing.closest_hover(hovers, (0.0, 0.0, 5.0), now=1.0 + pointing.HOVER_FALLBACK + 0.1))
        self.assertIsNotNone(pointing.closest_hover(hovers, (0.0, 0.0, 5.0), now=1.5))

    def test_a_click_with_its_own_hit_ignores_the_hover(self):
        click = {"start": (0.0, 0.0, 5.0), "action": (0.0, 0.0, 0.0)}
        self.assertTrue(pointing.has_hit(click))
        _, direction = pointing.click_ray(click, hover=((9.0, 9.0, 9.0), (8.0, 8.0, 8.0)))
        self.assertTrue(close(direction, (0.0, 1.0, 0.0)))


class PointingTests(unittest.TestCase):
    def setUp(self):
        self.state = pointing.Pointing()

    def test_a_click_on_an_object_selects_it_and_needs_the_object_mode(self):
        self.assertEqual(self.state.clicked("a", "Cube"), "object")
        self.assertEqual(self.state.selected, {"a": "Cube"})
        self.assertEqual(self.state.clicked("a", None), "default")
        self.assertEqual(self.state.selected, {})

    def test_users_select_on_their_own(self):
        self.state.clicked("a", "Cube")
        self.state.clicked("b", "Light")
        self.state.clicked("a", None)
        self.assertEqual(self.state.selected, {"b": "Light"})

    def test_a_drag_moves_the_object_by_its_content_delta_in_blender_axes(self):
        self.state.clicked("a", "Cube")
        self.assertEqual(self.state.drag_started("a", 7, (1.0, 2.0, 3.0)), "Cube")
        # The grabbed point moved 0.5 up (content +Y) and 0.25 towards the viewer (content +Z).
        name, target = self.state.drag_moved("a", 7, (0.0, 0.5, 0.25))
        self.assertEqual(name, "Cube")
        self.assertTrue(close(target, (1.0, 2.0 - 0.25, 3.0 + 0.5)))
        self.assertEqual(self.state.drag_finished("a", 7, canceled=False), ("Cube", None, True))

    def test_a_canceled_drag_puts_the_object_back(self):
        self.state.clicked("a", "Cube")
        self.state.drag_started("a", 1, (1.0, 2.0, 3.0))
        self.state.drag_moved("a", 1, (1.0, 0.0, 0.0))
        self.assertEqual(self.state.drag_finished("a", 1, canceled=True), ("Cube", (1.0, 2.0, 3.0), True))

    def test_a_drag_without_a_selection_or_delta_does_nothing(self):
        self.assertIsNone(self.state.drag_started("a", 1, (0.0, 0.0, 0.0)))
        self.state.clicked("a", "Cube")
        self.state.drag_started("a", 1, (0.0, 0.0, 0.0))
        self.assertIsNone(self.state.drag_moved("a", 1, None))
        self.assertIsNone(self.state.drag_moved("a", 2, (0.0, 0.0, 0.0)))
        self.assertIsNone(self.state.drag_finished("a", 2, canceled=False))

    def test_drags_are_kept_per_user_and_gesture(self):
        self.state.clicked("a", "Cube")
        self.state.clicked("b", "Cube")
        self.state.drag_started("a", 1, (0.0, 0.0, 0.0))
        self.state.drag_started("b", 1, (0.0, 0.0, 0.0))
        _, target = self.state.drag_moved("b", 1, (1.0, 0.0, 0.0))
        self.assertTrue(close(target, (1.0, 0.0, 0.0)))
        self.assertIn(("a", 1), self.state.drags)

    def test_a_gone_object_is_nobodys_selection(self):
        self.state.clicked("a", "Cube")
        self.state.clicked("b", "Light")
        self.state.drag_started("a", 1, (0.0, 0.0, 0.0))
        self.assertEqual(self.state.forget_object("Cube"), ["a"])
        self.assertEqual(self.state.selected, {"b": "Light"})
        self.assertEqual(self.state.drags, {})


class ObjectModeTests(unittest.TestCase):
    def test_only_one_hand_drags_stop_moving_the_app(self):
        default = panel.GestureConfiguration()
        kinds = [
            (panel.GESTURE_ONE_HAND_6DOF, panel.MAPPING_APP_POSE),
            (3, 2),  # two-hand distance -> app scale
            (4, panel.MAPPING_APP_POSE),  # two-hand rotate -> app pose
            (panel.GESTURE_CLICK, panel.MAPPING_NONE),
        ]
        for index, (kind, mapping) in enumerate(kinds):
            default.mapping_rules[index].gesture_kind = kind
            default.mapping_rules[index].app_transform_mapping = mapping
            default.mapping_rules[index].precision = 2.0
        default.mapping_rule_count = len(kinds)
        mode = panel.object_mode_from(default)
        self.assertEqual(mode.mapping_rule_count, 4)
        self.assertEqual(
            [(mode.mapping_rules[i].gesture_kind, mode.mapping_rules[i].app_transform_mapping) for i in range(4)],
            [(panel.GESTURE_ONE_HAND_6DOF, panel.MAPPING_NONE), (3, 2), (4, panel.MAPPING_APP_POSE), (0, 0)],
        )
        self.assertEqual(mode.mapping_rules[0].precision, 2.0, "everything else is the default mode's")
        self.assertEqual(default.mapping_rules[0].app_transform_mapping, panel.MAPPING_APP_POSE, "a copy")

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit layouts")
    def test_layouts_match_the_c_structures(self):
        # clang on qar_streaming_c/v0/types.h, 64-bit Windows.
        self.assertEqual(ctypes.sizeof(panel.MappingRule), 56)
        self.assertEqual(panel.MappingRule.precision.offset, 24)
        self.assertEqual(panel.MappingRule.enable_gestures_from_outside_of_volume.offset, 40)
        self.assertEqual(panel.MappingRule.controller_ray_style.offset, 44)
        self.assertEqual(panel.MappingRule.app_transform_mapping.offset, 52)
        self.assertEqual(ctypes.sizeof(panel.GestureConfiguration), 920)
        self.assertEqual(panel.GestureConfiguration.mapping_rule_count.offset, 912)


if __name__ == "__main__":
    unittest.main()
