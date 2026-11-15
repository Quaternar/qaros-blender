# SPDX-License-Identifier: MIT
"""Blender deciding what a hand gesture does (qaros_anari/gestures.py), with plain Python."""
import importlib.util
import math
from pathlib import Path
import sys
import threading
import types
import unittest

folder = Path(__file__).resolve().parents[1] / "blender" / "qaros_anari"


def load(name):
    """`qaros_anari.<name>` without the package's __init__ (it needs bpy)."""
    if "qaros_anari" not in sys.modules:
        package = types.ModuleType("qaros_anari")
        package.__path__ = [str(folder)]
        sys.modules["qaros_anari"] = package
    spec = importlib.util.spec_from_file_location(f"qaros_anari.{name}", folder / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pointing = load("pointing")
gestures = load("gestures")

HALF = math.sqrt(0.5)
QUARTER_TURN_Y = (0.0, HALF, 0.0, HALF)  # +X to -Z, about app volume space's up


def close(a, b, tolerance=1e-6):
    return all(math.isclose(x, y, abs_tol=tolerance) for x, y in zip(a, b))


class TargetTests(unittest.TestCase):
    def test_nothing_selected_moves_the_view(self):
        for kind in gestures.MANIPULATIONS:
            self.assertEqual(gestures.target(kind, gestures.POLICY_AUTO, False, False), gestures.VIEW)

    def test_a_drag_moves_the_selection_only_when_it_grabs_it(self):
        self.assertEqual(gestures.target(pointing.ONE_HAND, gestures.POLICY_AUTO, True, True), gestures.OBJECT)
        self.assertEqual(gestures.target(pointing.ONE_HAND, gestures.POLICY_AUTO, True, False), gestures.VIEW)

    def test_two_hands_take_the_selection_wherever_they_are(self):
        for kind in (gestures.TWO_HAND_SCALE, gestures.TWO_HAND_ROTATE):
            self.assertEqual(gestures.target(kind, gestures.POLICY_AUTO, True, False), gestures.OBJECT)

    def test_view_only_never_moves_an_object(self):
        for kind in gestures.MANIPULATIONS:
            self.assertEqual(gestures.target(kind, gestures.POLICY_VIEW, True, True), gestures.VIEW)


class SpreadTests(unittest.TestCase):
    def test_hands_apart_grow_and_together_shrink(self):
        self.assertAlmostEqual(gestures.spread((0.06, 0.0, 0.08)), 0.1)
        self.assertAlmostEqual(gestures.spread((-0.06, 0.0, -0.08)), -0.1)
        self.assertAlmostEqual(gestures.scale_factor((gestures.SCALE_DISTANCE, 0.0, 0.0)), math.e)
        self.assertAlmostEqual(gestures.scale_factor((-gestures.SCALE_DISTANCE, 0.0, 0.0)), 1.0 / math.e)
        self.assertEqual(gestures.scale_factor((0.0, 0.0, 0.0)), 1.0)


class ViewTests(unittest.TestCase):
    START = gestures.AppTransform((0.2, 0.0, -0.4), (0.0, 0.0, 0.0, 1.0), 0.5)

    def test_a_drag_carries_the_content_with_the_hand(self):
        after = gestures.view_after(pointing.ONE_HAND, self.START, (0.1, 0.2, 0.3), (0.0, 0.0, 0.0, 1.0))
        self.assertTrue(close(after.position, (0.3, 0.2, -0.1)))
        self.assertEqual((after.orientation, after.scale), (self.START.orientation, 0.5))

    def test_a_spread_zooms_about_the_volume_centre(self):
        after = gestures.view_after(gestures.TWO_HAND_SCALE, self.START, (gestures.SCALE_DISTANCE, 0.0, 0.0), None)
        self.assertAlmostEqual(after.scale, 0.5 * math.e)
        # The volume centre shows the same content point: content at -position/scale stays at 0.
        self.assertTrue(close(after.position, tuple(p * math.e for p in self.START.position)))

    def test_a_zoom_stays_within_the_app_scale_limits(self):
        after = gestures.view_after(gestures.TWO_HAND_SCALE, self.START, (-100.0, 0.0, 0.0), None)
        self.assertEqual(after.scale, gestures.MIN_APP_SCALE)

    def test_a_turn_turns_the_content_about_the_volume_centre(self):
        after = gestures.view_after(gestures.TWO_HAND_ROTATE, self.START, (0.0, 0.0, 0.0), QUARTER_TURN_Y)
        self.assertTrue(close(after.orientation, QUARTER_TURN_Y))
        self.assertTrue(close(after.position, (-0.4, 0.0, -0.2)), after.position)
        self.assertEqual(after.scale, 0.5)


class ObjectRotationTests(unittest.TestCase):
    def test_a_turn_about_up_turns_about_blender_z(self):
        # Unturned app: volume Y up is content Y up is Blender Z up.
        self.assertTrue(close(gestures.object_rotation(QUARTER_TURN_Y, (0.0, 0.0, 0.0, 1.0)), (0.0, 0.0, HALF, HALF)))

    def test_a_turned_app_does_not_change_a_turn_about_up(self):
        turned = gestures.object_rotation(QUARTER_TURN_Y, QUARTER_TURN_Y)
        self.assertTrue(close(turned, (0.0, 0.0, HALF, HALF)), turned)


def event(phase, gesture_id=7, kind=pointing.ONE_HAND, translation=(0.0, 0.0, 0.0), peer="a"):
    return {"peer": peer, "gesture_id": gesture_id, "kind": kind, "phase": phase,
            "translation": translation, "rotation": (0.0, 0.0, 0.0, 1.0)}


class ViewGestureTests(unittest.TestCase):
    def setUp(self):
        self.view = gestures.AppTransform()
        self.writes = []

        def write(transform):
            self.writes.append(transform)
            self.view = transform

        self.gestures = gestures.ViewGestures(lambda: self.view, write)

    def test_updates_apply_from_the_start_not_cumulatively(self):
        self.gestures.handle(event(pointing.STARTED))
        self.gestures.handle(event(pointing.UPDATED, translation=(0.1, 0.0, 0.0)))
        self.gestures.handle(event(pointing.UPDATED, translation=(0.3, 0.0, 0.0)))
        self.gestures.handle(event(pointing.ENDED, translation=(0.3, 0.0, 0.0)))
        self.assertTrue(close(self.view.position, (0.3, 0.0, 0.0)))

    def test_a_canceled_gesture_puts_the_view_back(self):
        self.gestures.handle(event(pointing.STARTED))
        self.gestures.handle(event(pointing.UPDATED, translation=(0.1, 0.0, 0.0)))
        self.gestures.handle(event(pointing.CANCELED))
        self.assertEqual(self.view, gestures.AppTransform())

    def test_a_gesture_whose_start_was_missed_moves_nothing(self):
        self.gestures.handle(event(pointing.UPDATED, translation=(0.1, 0.0, 0.0)))
        self.assertEqual(self.writes, [])


class CoalesceTests(unittest.TestCase):
    def test_only_the_newest_update_of_each_gesture_is_kept(self):
        events = [event(pointing.STARTED), event(pointing.UPDATED, translation=(1.0, 0.0, 0.0)),
                  event(pointing.UPDATED, gesture_id=8), event(pointing.UPDATED, translation=(2.0, 0.0, 0.0)),
                  event(pointing.ENDED)]
        kept = gestures.coalesce(events)
        self.assertEqual([(e["phase"], e["gesture_id"]) for e in kept],
                         [(pointing.STARTED, 7), (pointing.UPDATED, 8), (pointing.UPDATED, 7), (pointing.ENDED, 7)])
        self.assertEqual(kept[2]["translation"], (2.0, 0.0, 0.0))


class ViewWorkerTests(unittest.TestCase):
    def test_the_worker_moves_the_view_off_the_callers_thread(self):
        done = threading.Event()
        written = []

        def write(transform):
            written.append((threading.current_thread().name, transform))
            done.set()

        worker = gestures.ViewWorker(gestures.AppTransform, write, print)
        try:
            worker.put(event(pointing.STARTED))
            worker.put(event(pointing.UPDATED, translation=(0.5, 0.0, 0.0)))
            self.assertTrue(done.wait(2.0))
        finally:
            worker.close()
        self.assertEqual(written[0][0], "qaros-anari-view")
        self.assertTrue(close(written[-1][1].position, (0.5, 0.0, 0.0)))


if __name__ == "__main__":
    unittest.main()
