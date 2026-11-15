# SPDX-License-Identifier: MIT
"""Pointing into the app volume (qaros_anari/pointing.py): rays, the selection, drags and double taps,
and the gesture configuration the add-on gives the volume (qaros_anari/panel.py), with plain Python."""
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
        self.state.selection = "Cube"

    def test_one_selection_for_every_user(self):
        self.assertTrue(self.state.aims_at_selection("Cube"))
        self.assertFalse(self.state.aims_at_selection("Stand"))
        self.assertFalse(self.state.aims_at_selection(None))
        self.state.selection = None
        self.assertFalse(self.state.aims_at_selection(None), "no selection: nothing to aim at")

    def test_a_drag_moves_the_object_by_its_content_delta_in_blender_axes(self):
        self.assertEqual(self.state.drag_started("a", 7, (1.0, 2.0, 3.0), "Cube"), "Cube")
        # The grabbed point moved 0.5 up (content +Y) and 0.25 towards the viewer (content +Z).
        name, target = self.state.drag_moved("a", 7, (0.0, 0.5, 0.25))
        self.assertEqual(name, "Cube")
        self.assertTrue(close(target, (1.0, 2.0 - 0.25, 3.0 + 0.5)))
        self.assertEqual(self.state.drag_finished("a", 7, canceled=False), ("Cube", None, True))

    def test_a_canceled_drag_puts_the_object_back(self):
        self.state.drag_started("a", 1, (1.0, 2.0, 3.0), "Cube")
        self.state.drag_moved("a", 1, (1.0, 0.0, 0.0))
        self.assertEqual(self.state.drag_finished("a", 1, canceled=True), ("Cube", (1.0, 2.0, 3.0), True))

    def test_a_drag_without_a_selection_or_delta_does_nothing(self):
        self.state.selection = None
        self.assertIsNone(self.state.drag_started("a", 1, (0.0, 0.0, 0.0), "Cube"))
        self.state.selection = "Cube"
        self.state.drag_started("a", 1, (0.0, 0.0, 0.0), "Cube")
        self.assertIsNone(self.state.drag_moved("a", 1, None))
        self.assertIsNone(self.state.drag_moved("a", 2, (0.0, 0.0, 0.0)))
        self.assertIsNone(self.state.drag_finished("a", 2, canceled=False))

    def test_drags_are_kept_per_user_and_gesture(self):
        self.state.drag_started("a", 1, (0.0, 0.0, 0.0), "Cube")
        self.state.drag_started("b", 1, (0.0, 0.0, 0.0), "Cube")
        _, target = self.state.drag_moved("b", 1, (1.0, 0.0, 0.0))
        self.assertTrue(close(target, (1.0, 0.0, 0.0)))
        self.assertIn(("a", 1), self.state.drags)

    def test_a_gone_object_is_no_selection(self):
        self.state.drag_started("a", 1, (0.0, 0.0, 0.0), "Cube")
        self.assertFalse(self.state.forget_object("Light"))
        self.assertTrue(self.state.forget_object("Cube"))
        self.assertIsNone(self.state.selection)
        self.assertEqual(self.state.drags, {})


class ViewNotModelTests(unittest.TestCase):
    """Only grabbing the selection edits Blender."""

    def setUp(self):
        self.state = pointing.Pointing()
        self.state.selection = "Cube"

    def test_a_drag_that_grabbed_nothing_moves_no_object(self):
        # A pinch on a GUI panel's title bar or on empty space reaches the volume with no hit.
        self.assertIsNone(self.state.drag_started("a", 1, (0.0, 0.0, 0.0), None))
        self.assertIsNone(self.state.drag_moved("a", 1, (1.0, 0.0, 0.0)))
        self.assertIsNone(self.state.drag_finished("a", 1, canceled=False))
        self.assertFalse(self.state.dragging("a"))

    def test_a_drag_that_grabbed_another_object_moves_no_object(self):
        self.assertIsNone(self.state.drag_started("a", 1, (0.0, 0.0, 0.0), "Stand"))
        self.assertEqual(self.state.drags, {})

    def test_a_running_drag_is_its_users_only(self):
        self.state.drag_started("a", 3, (0.0, 0.0, 0.0), "Cube")
        self.assertTrue(self.state.dragging("a"))
        self.assertFalse(self.state.dragging("b"))


class TapTests(unittest.TestCase):
    """QAROS reports no double tap in an app volume: two clicks of one hand close together are one."""

    def setUp(self):
        self.taps = pointing.Taps()

    def test_two_clicks_within_the_window_are_a_double_tap(self):
        self.assertFalse(self.taps.tap("a", 1, 10.0))
        self.assertTrue(self.taps.tap("a", 1, 10.0 + pointing.DOUBLE_TAP_SECONDS - 0.01))

    def test_two_clicks_further_apart_are_two_clicks(self):
        self.assertFalse(self.taps.tap("a", 1, 10.0))
        self.assertFalse(self.taps.tap("a", 1, 10.0 + pointing.DOUBLE_TAP_SECONDS + 0.01))
        self.assertTrue(self.taps.tap("a", 1, 10.0 + pointing.DOUBLE_TAP_SECONDS + 0.2), "the second one starts a pair")

    def test_a_third_click_starts_the_next_pair(self):
        self.taps.tap("a", 1, 10.0)
        self.assertTrue(self.taps.tap("a", 1, 10.2))
        self.assertFalse(self.taps.tap("a", 1, 10.4))
        self.assertTrue(self.taps.tap("a", 1, 10.6))

    def test_clicks_of_another_hand_or_user_are_not_paired(self):
        self.taps.tap("a", 1, 10.0)
        self.assertFalse(self.taps.tap("a", 2, 10.1), "the other hand")
        self.assertFalse(self.taps.tap("b", 1, 10.2), "another user")
        self.assertTrue(self.taps.tap("a", 1, 10.3))

    def test_a_click_reported_out_of_order_is_not_a_double_tap(self):
        self.taps.tap("a", 1, 10.0)
        self.assertFalse(self.taps.tap("a", 1, 9.9))


def configuration(kinds):
    """A gesture configuration of (kind, mapping) rules, each with precision 2, sensitivity 2 (based
    on app scale) and its index as ray colour."""
    config = panel.GestureConfiguration()
    for index, (kind, mapping) in enumerate(kinds):
        rule = config.mapping_rules[index]
        rule.gesture_kind = kind
        rule.enabled = True
        rule.app_transform_mapping = mapping
        rule.precision = 2.0
        rule.app_scale_sensitivity_mode = 2
        rule.controller_ray_style.color[0] = index
    config.mapping_rule_count = len(kinds)
    return config


def rules(config):
    return [
        (config.mapping_rules[i].gesture_kind, config.mapping_rules[i].app_transform_mapping)
        for i in range(config.mapping_rule_count)
    ]


class AppDecidesTests(unittest.TestCase):
    # QAROS's default rules (AppVolume.hpp): spread -> scale, drag -> pose, hover, click; no turn.
    QAROS_DEFAULT = [
        (panel.GESTURE_TWO_HAND_SCALE, 2),
        (panel.GESTURE_ONE_HAND_6DOF, panel.MAPPING_APP_POSE),
        (panel.GESTURE_HOVER, panel.MAPPING_NONE),
        (panel.GESTURE_CLICK, panel.MAPPING_NONE),
    ]

    def test_no_gesture_moves_the_app_and_a_turn_follows_the_spread(self):
        default = configuration(self.QAROS_DEFAULT)
        config = panel.app_decides_from(default)
        self.assertEqual(
            rules(config),
            [(panel.GESTURE_TWO_HAND_SCALE, 0), (panel.GESTURE_TWO_HAND_ROTATE, 0), (panel.GESTURE_ONE_HAND_6DOF, 0),
             (panel.GESTURE_HOVER, 0), (panel.GESTURE_CLICK, 0)],
        )
        self.assertEqual(rules(default)[1], (panel.GESTURE_ONE_HAND_6DOF, panel.MAPPING_APP_POSE), "a copy")

    def test_manipulations_report_hand_metres_and_pointing_keeps_its_rule(self):
        config = panel.app_decides_from(configuration(self.QAROS_DEFAULT))
        for index in range(3):
            self.assertEqual(config.mapping_rules[index].precision, 1.0)
            self.assertEqual(config.mapping_rules[index].app_scale_sensitivity_mode, panel.SENSITIVITY_CONSTANT)
        self.assertEqual(config.mapping_rules[3].precision, 2.0, "a hover moves nothing anyway")

    def test_the_turn_draws_the_spreads_ray_and_turns_about_the_vertical(self):
        config = panel.app_decides_from(configuration(self.QAROS_DEFAULT))
        turn = config.mapping_rules[1]
        self.assertTrue(turn.enabled)
        self.assertEqual(turn.controller_ray_style.color[0], 0, "the spread's ray")
        self.assertEqual(turn.rotation_axes, panel.AXIS_Y)
        self.assertEqual(turn.translation_axes, panel.AXIS_NONE)
        self.assertEqual(config.mapping_rules[2].controller_ray_style.color[0], 1, "the drag's own ray")

    def test_a_volume_that_turns_already_keeps_its_turn(self):
        config = panel.app_decides_from(
            configuration([(panel.GESTURE_ONE_HAND_6DOF, 1), (panel.GESTURE_TWO_HAND_ROTATE, 1)])
        )
        self.assertEqual(
            rules(config),
            [(panel.GESTURE_ONE_HAND_6DOF, 0), (panel.GESTURE_TWO_HAND_ROTATE, 0), (panel.GESTURE_CLICK, 0)],
        )

    def test_without_a_spread_the_turn_comes_first(self):
        config = panel.app_decides_from(configuration([(panel.GESTURE_ONE_HAND_6DOF, 1)]))
        self.assertEqual(
            rules(config),
            [(panel.GESTURE_TWO_HAND_ROTATE, 0), (panel.GESTURE_ONE_HAND_6DOF, 0), (panel.GESTURE_CLICK, 0)],
        )

    def ray(self, rule):
        style = rule.controller_ray_style
        return style.line_style, tuple(style.color)

    def test_a_click_draws_a_solid_green_ray_and_the_others_keep_theirs(self):
        default = configuration(self.QAROS_DEFAULT)
        default.mapping_rules[2].controller_ray_style.line_style = panel.LINE_DOTTED
        config = panel.app_decides_from(default)
        click = config.mapping_rules[4]
        self.assertEqual(click.gesture_kind, panel.GESTURE_CLICK)
        self.assertEqual(self.ray(click), panel.CLICK_RAY)
        self.assertEqual(self.ray(config.mapping_rules[3]), (panel.LINE_DOTTED, (2, 0, 0, 0)), "the hover's own ray")
        self.assertEqual(self.ray(default.mapping_rules[3])[1], (3, 0, 0, 0), "a copy")

    def test_a_volume_without_a_click_rule_gets_one_last(self):
        config = panel.app_decides_from(configuration(self.QAROS_DEFAULT[:3]))
        self.assertEqual(
            rules(config),
            [(panel.GESTURE_TWO_HAND_SCALE, 0), (panel.GESTURE_TWO_HAND_ROTATE, 0), (panel.GESTURE_ONE_HAND_6DOF, 0),
             (panel.GESTURE_HOVER, 0), (panel.GESTURE_CLICK, 0)],
        )
        click = config.mapping_rules[4]
        self.assertTrue(click.enabled)
        self.assertEqual(click.header.type, panel.STRUCTURE_TYPE_GESTURE_MAPPING_RULE)
        self.assertEqual(self.ray(click), panel.CLICK_RAY)

    def test_a_full_configuration_gets_no_new_rule(self):
        full = configuration([(panel.GESTURE_HOVER, 0)] * panel.MAX_MAPPING_RULES)
        self.assertEqual(panel.app_decides_from(full).mapping_rule_count, panel.MAX_MAPPING_RULES)

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
