# SPDX-License-Identifier: MIT
"""Pointing into the app volume against a real Blender scene (background mode, factory scene, no
device): the click ray picks, selection follows, and a drag moves the picked object.

    blender -b --factory-startup --python-exit-code 1 --python test_blender_pointing_scene.py"""
import math
from pathlib import Path
import sys
import unittest

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))
from qaros_anari import controls, gestures, pointing  # noqa: E402


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
        picked = controls.pick(*self.click((0.0, -5.0, 0.0), (0.0, 0.0, 0.0)))
        state.clicked("peer", picked)
        state.drag_started("peer", 1, tuple(self.cube.matrix_world.translation), picked)
        name, target = state.drag_moved("peer", 1, content((1.0, 0.0, 0.5)))
        fake = controls.Controls.__new__(controls.Controls)  # only move(): no panel, no timer
        fake.pointing = state
        fake.gestures = None
        fake.move(name, target)
        bpy.context.view_layer.update()
        self.assertAlmostEqual(self.cube.location.x, 1.0, places=5)
        self.assertAlmostEqual(self.cube.location.z, 0.5, places=5)


class FakeGestures:
    """The volume gestures as Controls uses them: an identity app transform."""

    def app_transform(self):
        return (0.0, 0.0, 0.0, 1.0), 1.0


class FakeView:
    """The view worker: records what it was given."""

    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def fake_controls():
    """A Controls without a panel or a timer: gestures, hovers, routing and pointing only."""
    import threading

    fake = controls.Controls.__new__(controls.Controls)
    fake.pointing = pointing.Pointing()
    fake.gestures = FakeGestures()
    fake.view = FakeView()
    fake.queue = FakeQueue()
    fake.hovers = {}
    fake.hovers_lock = threading.Lock()
    fake.hovers_followed = {}
    fake.twists = {}
    fake.routing_lock = threading.Lock()
    fake.routing = {}
    fake.policy = gestures.POLICY_AUTO
    fake.decided = {}
    return fake


class ViewNotModelTests(unittest.TestCase):
    """A drag that did not grab the selection (a GUI panel's title bar, empty space, another object)
    must leave Blender's scene as it is."""

    HAND = (0.0, -5.0, 0.0)

    def setUp(self):
        self.cube = bpy.data.objects["Cube"]
        self.cube.location = (0.0, 0.0, 0.0)
        bpy.context.view_layer.update()
        self.controls = fake_controls()
        self.controls.pointing.clicked("peer", "Cube")
        self.controls.set_aim("peer", True)

    def hover(self, target, when):
        self.controls.hovers[("peer", 1)] = [(when, (content(self.HAND), content(target)))]

    def drag(self, action, translation=(0.0, 0.0, 1.0)):
        start = {"peer": "peer", "kind": pointing.ONE_HAND, "mode": 0, "gesture_id": 9,
                 "hand": 1, "start": content(self.HAND), "action": action, "translation": (0.0, 0.0, 0.0)}
        self.controls.gesture({**start, "phase": pointing.STARTED})
        self.controls.gesture({**start, "phase": pointing.UPDATED, "translation": translation})
        return self.controls.gesture({**start, "phase": pointing.ENDED, "translation": translation})

    def test_a_drag_without_a_hit_or_hover_leaves_the_object(self):
        # A pinch on the controls panel's title bar: no hit in the volume, no hover on the cube.
        self.assertFalse(self.drag(content(self.HAND)))
        self.assertEqual(tuple(self.cube.location), (0.0, 0.0, 0.0))

    def test_a_drag_that_hit_something_else_leaves_the_object(self):
        self.assertFalse(self.drag(content((0.0, 0.0, 3.0))))
        self.assertEqual(tuple(self.cube.location), (0.0, 0.0, 0.0))

    def test_a_drag_that_grabbed_the_selection_moves_it(self):
        self.assertTrue(self.drag(content((0.0, 0.0, 0.0))))
        self.assertAlmostEqual(self.cube.location.y, -1.0, places=5)  # content +Z is Blender -Y

    def test_the_aim_follows_the_ray_on_and_off_the_selection(self):
        import time

        now = time.monotonic()
        self.hover((0.0, 0.0, 3.0), now)  # past the cube
        self.controls.follow_hovers()
        self.assertEqual(self.controls.routing["peer"], (True, False))
        self.hover((0.0, 0.0, 0.0), now + 0.1)  # on the cube
        self.controls.follow_hovers()
        self.assertEqual(self.controls.routing["peer"], (True, True))

    def test_a_user_moving_the_object_keeps_the_aim(self):
        import time

        self.controls.pointing.drag_started("peer", 1, (0.0, 0.0, 0.0), "Cube")
        self.hover((0.0, 0.0, 3.0), time.monotonic())
        self.controls.follow_hovers()
        self.assertEqual(self.controls.routing["peer"], (True, True))


def manipulation(kind, phase, gesture_id=4, translation=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0, 1.0), peer="peer"):
    return {"peer": peer, "kind": kind, "phase": phase, "gesture_id": gesture_id, "mode": 0, "hand": 0,
            "start": None, "action": None, "translation": translation, "rotation": rotation}


class RoutingTests(unittest.TestCase):
    """QAR's thread decides each manipulation once, when it starts: view gestures to the view worker,
    object ones to the UI thread."""

    def setUp(self):
        self.controls = fake_controls()

    def run_gesture(self, kind):
        for phase in (pointing.STARTED, pointing.UPDATED, pointing.ENDED):
            self.controls.receive_gesture(manipulation(kind, phase))

    def test_with_nothing_selected_every_manipulation_moves_the_view(self):
        for kind in gestures.MANIPULATIONS:
            self.run_gesture(kind)
        self.assertEqual(len(self.controls.view.events), 9)
        self.assertEqual(self.controls.queue.items, [])

    def test_two_hands_on_a_selection_go_to_blender(self):
        self.controls.pointing.clicked("peer", "Cube")
        self.controls.set_aim("peer", False)
        self.run_gesture(gestures.TWO_HAND_ROTATE)
        self.run_gesture(pointing.ONE_HAND)  # not aimed at the cube: the view
        self.assertEqual([item[1]["kind"] for item in self.controls.queue.items], [gestures.TWO_HAND_ROTATE] * 3)
        self.assertEqual([e["kind"] for e in self.controls.view.events], [pointing.ONE_HAND] * 3)

    def test_a_gesture_keeps_its_target_when_the_aim_changes(self):
        self.controls.receive_gesture(manipulation(pointing.ONE_HAND, pointing.STARTED))
        self.controls.pointing.clicked("peer", "Cube")
        self.controls.set_aim("peer", True)
        self.controls.receive_gesture(manipulation(pointing.ONE_HAND, pointing.UPDATED))
        self.controls.receive_gesture(manipulation(pointing.ONE_HAND, pointing.ENDED))
        self.assertEqual(len(self.controls.view.events), 3)
        self.assertEqual(self.controls.decided, {})

    def test_view_only_ignores_the_selection(self):
        self.controls.pointing.clicked("peer", "Cube")
        self.controls.set_aim("peer", True)
        self.controls.policy = gestures.POLICY_VIEW
        self.run_gesture(gestures.TWO_HAND_SCALE)
        self.assertEqual(self.controls.queue.items, [])


class TwistTests(unittest.TestCase):
    """Two hands scale and turn the selected object about its own origin."""

    def setUp(self):
        self.cube = bpy.data.objects["Cube"]
        self.cube.location = (1.0, 2.0, 0.0)
        self.cube.rotation_euler = (0.0, 0.0, 0.0)
        self.cube.scale = (1.0, 1.0, 1.0)
        bpy.context.view_layer.update()
        self.controls = fake_controls()
        self.controls.pointing.clicked("peer", "Cube")

    def tearDown(self):
        self.cube.location = (0.0, 0.0, 0.0)
        self.cube.rotation_euler = (0.0, 0.0, 0.0)
        self.cube.scale = (1.0, 1.0, 1.0)

    def twist(self, kind, last_phase=pointing.ENDED, **delta):
        self.controls.gesture(manipulation(kind, pointing.STARTED))
        self.controls.gesture(manipulation(kind, pointing.UPDATED, **delta))
        result = self.controls.gesture(manipulation(kind, last_phase, **delta))
        bpy.context.view_layer.update()
        return result

    def test_a_spread_scales_the_object_about_its_origin(self):
        self.assertTrue(self.twist(gestures.TWO_HAND_SCALE, translation=(gestures.SCALE_DISTANCE, 0.0, 0.0)))
        for axis in self.cube.scale:
            self.assertAlmostEqual(axis, math.e, places=4)
        self.assertEqual(tuple(round(c, 5) for c in self.cube.location), (1.0, 2.0, 0.0))

    def test_a_turn_turns_the_object_about_blender_up(self):
        half = math.sqrt(0.5)
        self.assertTrue(self.twist(gestures.TWO_HAND_ROTATE, rotation=(0.0, half, 0.0, half)))
        self.assertAlmostEqual(self.cube.rotation_euler.z, math.radians(90), places=4)
        self.assertEqual(tuple(round(c, 5) for c in self.cube.location), (1.0, 2.0, 0.0))

    def test_a_canceled_twist_puts_the_object_back(self):
        self.assertFalse(
            self.twist(gestures.TWO_HAND_SCALE, last_phase=pointing.CANCELED, translation=(0.3, 0.0, 0.0))
        )
        self.assertEqual(tuple(round(c, 5) for c in self.cube.scale), (1.0, 1.0, 1.0))


def unit_cube_mesh(name):
    mesh = bpy.data.meshes.new(name)
    corners = [(x, y, z) for z in (-0.5, 0.5) for y in (-0.5, 0.5) for x in (-0.5, 0.5)]
    faces = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
    mesh.from_pydata(corners, [], faces)
    return mesh


def drag(obj, delta_blender):
    """One drag of `obj` by a Blender-axes delta, the way Controls runs it."""
    state = pointing.Pointing()
    state.clicked("peer", obj.name)
    state.drag_started("peer", 1, tuple(obj.matrix_world.translation), obj.name)
    name, target = state.drag_moved("peer", 1, content(delta_blender))
    fake = controls.Controls.__new__(controls.Controls)  # only move(): no panel, no timer
    fake.pointing = state
    fake.gestures = None
    fake.move(name, target)
    bpy.context.view_layer.update()


class HierarchyTests(unittest.TestCase):
    """A child under a rotated, scaled parent, and a collection instance."""

    def setUp(self):
        scene = bpy.context.scene
        self.parent = bpy.data.objects["Cube"]
        self.parent.location = (0.0, 0.0, 0.0)
        self.parent.rotation_euler = (0.0, 0.0, math.radians(90))
        self.parent.scale = (2.0, 2.0, 2.0)
        self.child = bpy.data.objects.new("Child", unit_cube_mesh("ChildMesh"))
        scene.collection.objects.link(self.child)
        self.child.parent = self.parent
        self.child.location = (2.0, 0.0, 0.0)  # parent space: world (0, 4, 0)
        self.props = bpy.data.collections.new("Props")
        scene.collection.children.link(self.props)
        self.proto = bpy.data.objects.new("Proto", unit_cube_mesh("ProtoMesh"))
        self.props.objects.link(self.proto)
        bpy.context.view_layer.layer_collection.children["Props"].exclude = True
        self.instancer = bpy.data.objects.new("Instancer", None)
        self.instancer.instance_type = "COLLECTION"
        self.instancer.instance_collection = self.props
        self.instancer.location = (0.0, 10.0, 0.0)
        scene.collection.objects.link(self.instancer)
        bpy.context.view_layer.update()

    def tearDown(self):
        for obj in (self.child, self.instancer, self.proto):
            bpy.data.objects.remove(obj)
        bpy.data.collections.remove(self.props)
        self.parent.rotation_euler = (0.0, 0.0, 0.0)
        self.parent.scale = (1.0, 1.0, 1.0)
        bpy.context.view_layer.update()

    def test_a_click_on_a_child_picks_the_child(self):
        ray = pointing.click_ray({"start": content((0.0, 4.0, -5.0)), "action": content((0.0, 4.0, 0.0))})
        self.assertEqual(controls.pick(*ray), "Child")

    def test_a_drag_moves_a_child_in_world_space_and_leaves_its_parent(self):
        drag(self.child, (1.0, 0.0, 0.5))
        self.assertEqual(tuple(round(v, 5) for v in self.child.matrix_world.translation), (1.0, 4.0, 0.5))
        self.assertEqual(tuple(self.parent.location), (0.0, 0.0, 0.0))
        self.assertIs(self.child.parent, self.parent)

    def test_a_click_on_a_collection_instance_picks_the_object_that_instances_it(self):
        ray = pointing.click_ray({"start": content((0.0, 10.0, -5.0)), "action": content((0.0, 10.0, 0.0))})
        self.assertEqual(controls.pick(*ray), "Instancer")

    def test_a_drag_moves_the_instance_not_its_prototype(self):
        drag(self.instancer, (2.0, 0.0, 0.0))
        self.assertEqual(tuple(round(v, 5) for v in self.instancer.location), (2.0, 10.0, 0.0))
        self.assertEqual(tuple(self.proto.location), (0.0, 0.0, 0.0))


if __name__ == "__main__":
    result = unittest.main(argv=[sys.argv[0]], exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
