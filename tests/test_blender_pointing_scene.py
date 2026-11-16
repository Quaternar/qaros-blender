# SPDX-License-Identifier: MIT
"""Pointing into the app volume against a real Blender scene (background mode, factory scene, no
device): the click ray picks, the tool decides what clicks, double taps and hand gestures do, the
selection and the controls panel follow, and a drag moves the picked object.

    blender -b --factory-startup --python-exit-code 1 --python test_blender_pointing_scene.py"""
import json
import math
from pathlib import Path
import sys
import time
import unittest

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))
from qaros_anari import controls, gestures, pointing, protocol  # noqa: E402


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
        state.selection = picked
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


def fake_controls(tool=gestures.TOOL_SELECT):
    """A Controls without a panel or a timer: gestures, hovers, routing and pointing only."""
    import threading

    fake = controls.Controls.__new__(controls.Controls)
    fake.pointing = pointing.Pointing()
    fake.taps = pointing.Taps()
    fake.gestures = FakeGestures()
    fake.view = FakeView()
    fake.queue = FakeQueue()
    fake.hovers = {}
    fake.hovers_lock = threading.Lock()
    fake.hovers_followed = {}
    fake.twists = {}
    fake.routing_lock = threading.Lock()
    fake.routing = {}
    fake.selected = False
    fake.tool = tool
    fake.decided = {}
    return fake


def select(fake, name):
    """`name` selected in Blender, as the UI tick sees it."""
    controls.select_only(name)
    fake.follow_selection()


class ViewNotModelTests(unittest.TestCase):
    """A drag that did not grab the selection (a GUI panel's title bar, empty space, another object)
    must leave Blender's scene as it is."""

    HAND = (0.0, -5.0, 0.0)

    def setUp(self):
        self.cube = bpy.data.objects["Cube"]
        self.cube.location = (0.0, 0.0, 0.0)
        bpy.context.view_layer.update()
        self.controls = fake_controls()
        select(self.controls, "Cube")
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
        now = time.monotonic()
        self.hover((0.0, 0.0, 3.0), now)  # past the cube
        self.controls.follow_hovers()
        self.assertFalse(self.controls.routing["peer"])
        self.hover((0.0, 0.0, 0.0), now + 0.1)  # on the cube
        self.controls.follow_hovers()
        self.assertTrue(self.controls.routing["peer"])

    def test_a_user_moving_the_object_keeps_the_aim(self):
        self.controls.pointing.drag_started("peer", 1, (0.0, 0.0, 0.0), "Cube")
        self.hover((0.0, 0.0, 3.0), time.monotonic())
        self.controls.follow_hovers()
        self.assertTrue(self.controls.routing["peer"])


def manipulation(kind, phase, gesture_id=4, translation=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0, 1.0), peer="peer"):
    return {"peer": peer, "kind": kind, "phase": phase, "gesture_id": gesture_id, "mode": 0, "hand": 0,
            "start": None, "action": None, "translation": translation, "rotation": rotation}


class RoutingTests(unittest.TestCase):
    """QAR's thread decides each manipulation once, when it starts, by the tool: view gestures to the
    view worker, object ones to the UI thread, and with Object selection never the view."""

    def setUp(self):
        bpy.data.objects["Cube"].select_set(False)
        self.controls = fake_controls(gestures.TOOL_NAVIGATE)

    def run_gesture(self, kind):
        for phase in (pointing.STARTED, pointing.UPDATED, pointing.ENDED):
            self.controls.receive_gesture(manipulation(kind, phase))

    def test_navigation_moves_the_view_with_or_without_a_selection(self):
        for kind in gestures.MANIPULATIONS:
            self.run_gesture(kind)
        select(self.controls, "Cube")
        self.controls.set_aim("peer", True)
        for kind in gestures.MANIPULATIONS:
            self.run_gesture(kind)
        self.assertEqual(len(self.controls.view.events), 18)
        self.assertEqual(self.controls.queue.items, [])

    def test_object_selection_moves_only_the_selection(self):
        self.controls.tool = gestures.TOOL_SELECT
        select(self.controls, "Cube")
        self.controls.set_aim("peer", False)
        self.run_gesture(gestures.TWO_HAND_ROTATE)
        self.run_gesture(gestures.TWO_HAND_SCALE)
        self.run_gesture(pointing.ONE_HAND)  # not aimed at the cube: nothing
        self.controls.set_aim("peer", True)
        self.run_gesture(pointing.ONE_HAND)
        self.assertEqual(
            [item[1]["kind"] for item in self.controls.queue.items],
            [gestures.TWO_HAND_ROTATE] * 3 + [gestures.TWO_HAND_SCALE] * 3 + [pointing.ONE_HAND] * 3,
        )
        self.assertEqual(self.controls.view.events, [])

    def test_object_selection_with_nothing_selected_does_nothing(self):
        self.controls.tool = gestures.TOOL_SELECT
        self.controls.follow_selection()
        for kind in gestures.MANIPULATIONS:
            self.run_gesture(kind)
        self.assertEqual(self.controls.view.events, [])
        self.assertEqual(self.controls.queue.items, [])
        self.assertEqual(self.controls.decided, {})

    def test_a_gesture_keeps_its_target_when_the_tool_changes(self):
        self.controls.receive_gesture(manipulation(pointing.ONE_HAND, pointing.STARTED))
        self.controls.tool = gestures.TOOL_SELECT
        select(self.controls, "Cube")
        self.controls.set_aim("peer", True)
        self.controls.receive_gesture(manipulation(pointing.ONE_HAND, pointing.UPDATED))
        self.controls.receive_gesture(manipulation(pointing.ONE_HAND, pointing.ENDED))
        self.assertEqual(len(self.controls.view.events), 3)
        self.assertEqual(self.controls.decided, {})


def click(peer="peer", hand=1, when=None, target=(0.0, 0.0, 0.0), start=(0.0, -5.0, 0.0)):
    """A click whose ray runs from `start` to `target` (Blender world), as QAR's thread queues it."""
    return {"peer": peer, "kind": pointing.CLICK, "phase": pointing.INSTANT, "gesture_id": 1, "mode": 0,
            "hand": hand, "start": content(start), "action": content(target), "translation": (0.0, 0.0, 0.0),
            "rotation": (0.0, 0.0, 0.0, 1.0), "time": time.monotonic() if when is None else when}


class ClickTests(unittest.TestCase):
    """A click selects by Blender's ray cast with Object selection; a double tap with any tool."""

    def setUp(self):
        layer = bpy.context.view_layer
        for obj in layer.objects:
            obj.select_set(False)
        layer.objects.active = None
        self.controls = fake_controls(gestures.TOOL_NAVIGATE)

    def selection(self):
        layer = bpy.context.view_layer
        active = layer.objects.active
        return active.name if active else None, [o.name for o in layer.objects if o.select_get()]

    def test_navigation_does_not_select_on_a_click(self):
        self.controls.gesture(click(when=10.0))
        self.assertEqual(self.selection(), (None, []))

    def test_a_double_tap_selects_what_the_ray_hits_in_navigation(self):
        self.controls.gesture(click(when=10.0))
        self.controls.gesture(click(when=10.3))
        self.assertEqual(self.selection(), ("Cube", ["Cube"]))
        self.assertEqual(self.controls.pointing.selection, "Cube")
        self.assertTrue(self.controls.selected)
        self.assertTrue(self.controls.routing["peer"], "the ray that selected it is on it")

    def test_two_slow_clicks_or_two_hands_are_no_double_tap(self):
        self.controls.gesture(click(when=10.0))
        self.controls.gesture(click(when=10.0 + pointing.DOUBLE_TAP_SECONDS + 0.1))
        self.controls.gesture(click(hand=0, when=20.0))
        self.controls.gesture(click(hand=1, when=20.1))
        self.assertEqual(self.selection(), (None, []))

    def test_object_selection_selects_on_a_click(self):
        self.controls.tool = gestures.TOOL_SELECT
        bpy.data.objects["Camera"].select_set(True)
        self.controls.gesture(click(when=10.0))
        self.assertEqual(self.selection(), ("Cube", ["Cube"]))

    def test_a_click_on_nothing_keeps_the_selection(self):
        self.controls.tool = gestures.TOOL_SELECT
        self.controls.gesture(click(when=10.0))
        self.controls.gesture(click(when=20.0, target=(0.0, 0.0, 3.0), start=(0.0, -5.0, 3.0)))
        self.assertEqual(self.selection(), ("Cube", ["Cube"]))


class FakePanel:
    """The served panel: records what the add-on posts."""

    def __init__(self):
        self.posted = []

    def subscribe(self, handler):
        self.handler = handler

    def post(self, text):
        self.posted.append(json.loads(text))

    def latest(self, key):
        return next(m["value"] for m in reversed(self.posted) if m.get("t") == "state" and m.get("key") == key)


class PanelFollowsSelectionTests(unittest.TestCase):
    """The controls panel shows the selected object, selected in the headset or in Blender, and its
    Tools tab and Blender's preference show one tool."""

    def setUp(self):
        layer = bpy.context.view_layer
        for obj in layer.objects:
            obj.select_set(False)
        layer.objects.active = bpy.data.objects["Light"]
        bpy.data.objects["Light"].select_set(True)
        self.tool = gestures.TOOL_NAVIGATE
        self.panel = FakePanel()

        def set_tool(tool):
            self.tool = tool

        self.controls = controls.Controls(self.panel, tool=lambda: self.tool, set_tool=set_tool)
        self.controls.step()

    def tearDown(self):
        self.controls.close()

    def test_a_double_tap_in_the_headset_shows_its_object(self):
        self.assertEqual(self.panel.latest("scene")["object"]["name"], "Light")
        self.controls.queue.put(("gesture", click(when=10.0)))
        self.controls.queue.put(("gesture", click(when=10.2)))
        self.controls.step()
        self.assertEqual(self.panel.latest("scene")["object"]["name"], "Cube")
        fields = [item.get("id") for panel in self.panel.latest("panels") for item in panel["items"]]
        self.assertIn("objects:Cube:location", fields)
        self.assertNotIn("objects:Light:location", fields)

    def test_a_selection_in_blender_shows_its_object_and_moves_with_object_selection(self):
        controls.select_only("Cube")
        self.controls.step()
        self.assertEqual(self.panel.latest("scene")["object"]["name"], "Cube")
        self.assertEqual(self.controls.pointing.selection, "Cube")
        self.assertTrue(self.controls.selected)

    def test_the_tools_tab_chooses_the_tool_for_blender_too(self):
        self.assertEqual(self.panel.latest("scene")["tool"], gestures.TOOL_NAVIGATE)
        self.assertEqual([tool[0] for tool in self.panel.latest("scene")["tools"]], list(gestures.TOOL_IDS))
        self.controls.receive(json.dumps({"t": "tool", "value": gestures.TOOL_SELECT}), "user")
        self.controls.step()
        self.assertEqual(self.tool, gestures.TOOL_SELECT, "the preference")
        self.assertEqual(self.controls.tool, gestures.TOOL_SELECT, "the gestures")
        self.assertEqual(self.panel.latest("scene")["tool"], gestures.TOOL_SELECT, "the page")

    def test_a_tool_chosen_in_blender_shows_on_the_page(self):
        self.tool = gestures.TOOL_SELECT
        self.controls.step()
        self.assertEqual(self.panel.latest("scene")["tool"], gestures.TOOL_SELECT)
        self.assertEqual(self.panel.latest("scene")["tabs"][0], "tools")

    def test_a_hidden_tools_tab_refuses_a_tool(self):
        self.controls.settings = lambda: (tuple(tab for tab in protocol.TABS if tab != "tools"), ())
        self.controls.receive(json.dumps({"t": "tool", "value": gestures.TOOL_SELECT}), "user")
        self.controls.step()
        self.assertEqual(self.tool, gestures.TOOL_NAVIGATE)


class TwistTests(unittest.TestCase):
    """Two hands scale and turn the selected object about its own origin."""

    def setUp(self):
        self.cube = bpy.data.objects["Cube"]
        self.cube.location = (1.0, 2.0, 0.0)
        self.cube.rotation_euler = (0.0, 0.0, 0.0)
        self.cube.scale = (1.0, 1.0, 1.0)
        bpy.context.view_layer.update()
        self.controls = fake_controls()
        select(self.controls, "Cube")

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
    state.selection = obj.name
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
