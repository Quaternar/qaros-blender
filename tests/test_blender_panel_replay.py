# SPDX-License-Identifier: MIT
"""Blender's own Python panels replayed for the controls page (qaros_anari/blender_panels.py), in
Blender itself (background mode, factory scene, no device):

    blender -b --factory-startup --python-exit-code 1 --python test_blender_panel_replay.py"""
import json
import math
from pathlib import Path
import sys
import unittest

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))
from qaros_anari import blender_panels, controls, protocol  # noqa: E402


def fields(items):
    return {item["id"]: item for item in items if item.get("kind") == "field"}


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.cube = bpy.data.objects["Cube"]
        bpy.context.view_layer.objects.active = self.cube
        self.cube.location = (0.0, 0.0, 0.0)
        self.cube.rotation_mode = "XYZ"
        self.cube.rotation_euler = (0.0, 0.0, 0.0)
        self.cube.hide_render = False
        self.cube.parent = None
        self.panels = {panel["name"]: panel for panel in blender_panels.panels_state()}

    def test_the_name_then_the_three_panels_in_order(self):
        names = [panel["name"] for panel in blender_panels.panels_state()]
        self.assertEqual(names, ["OBJECT_name"] + [name for name, _ in blender_panels.PANELS])

    def test_the_preferences_offer_blenders_python_properties_panels_by_tab(self):
        offered = dict(blender_panels.available_panels())
        self.assertEqual(list(offered), [heading for _, heading in blender_panels.PANEL_CONTEXTS])
        object_panels = dict(offered["Object"])
        for name in blender_panels.DEFAULT_PANELS:
            self.assertIn(name, object_panels)
        self.assertEqual(object_panels["OBJECT_PT_transform"], "Transform")
        self.assertIn("Delta Transform", object_panels["OBJECT_PT_delta_transform"], "a sub-panel names its parent")
        self.assertTrue(offered["Physics"], "physics panels are offered")
        for _, panels in offered.items():
            for name, _ in panels:
                self.assertNotEqual(name, "OBJECT_PT_context_object", "a C-only header panel")

    def test_chosen_panels_replay_in_their_order_and_an_unknown_one_says_so(self):
        chosen = ("OBJECT_PT_visibility", "OBJECT_PT_delta_transform", "NO_PT_such")
        panels = blender_panels.panels_state(chosen)
        self.assertEqual([p["name"] for p in panels], ["OBJECT_name", *chosen])
        self.assertEqual(panels[2]["title"], "Delta Transform")
        self.assertIn("objects:Cube:delta_location", fields(panels[2]["items"]))
        self.assertEqual(panels[3]["items"], [{"kind": "note", "text": "Not in this Blender"}])

    def test_every_offered_panel_replays_without_raising(self):
        for _, panels in blender_panels.available_panels():
            for name, _ in panels:
                with self.subTest(panel=name):
                    blender_panels.replay(name, self.cube)

    def test_transform_yields_typed_fields_with_their_values(self):
        transform = fields(self.panels["OBJECT_PT_transform"]["items"])
        location = transform["objects:Cube:location"]
        self.assertEqual((location["type"], location["length"], location["components"]), ("float", 3, "XYZ"))
        self.assertEqual(location["value"], [0.0, 0.0, 0.0])
        rotation = transform["objects:Cube:rotation_euler"]
        self.assertEqual(rotation["unit"], "deg")
        mode = transform["objects:Cube:rotation_mode"]
        self.assertEqual(mode["type"], "enum")
        self.assertIn("XYZ", [item[0] for item in mode["items"]])
        locks = transform["objects:Cube:lock_location"]
        self.assertEqual((locks["type"], locks["value"]), ("boolean", [False, False, False]))

    def test_visibility_replays_with_the_scene_engine(self):
        visibility = fields(self.panels["OBJECT_PT_visibility"]["items"])
        self.assertIn("objects:Cube:hide_render", visibility)
        stopped = [item for item in self.panels["OBJECT_PT_visibility"]["items"] if item.get("text", "").startswith("Stopped")]
        self.assertEqual(stopped, [], "the replay must not stop part-way")

    def test_inverted_checkboxes_are_marked(self):
        visibility = fields(self.panels["OBJECT_PT_visibility"]["items"])
        self.assertTrue(visibility["objects:Cube:hide_select"]["invert"], "Blender draws it as Selectable")
        self.assertTrue(visibility["objects:Cube:hide_render"]["invert"], "drawn as Renders")
        self.assertFalse(visibility["objects:Cube:visible_camera"]["invert"])

    def test_relations_shows_the_parent_as_a_link_to_other_objects(self):
        parent = fields(self.panels["OBJECT_PT_relations"]["items"])["objects:Cube:parent"]
        self.assertEqual((parent["type"], parent["value"], parent["collection"]), ("pointer", "", "objects"))
        self.assertFalse(parent["readonly"])
        self.assertIn("Camera", parent["choices"])
        self.assertNotIn("Cube", parent["choices"], "an object cannot be its own parent")

    def test_the_name_is_an_editable_text_field(self):
        name = fields(self.panels["OBJECT_name"]["items"])["objects:Cube:name"]
        self.assertEqual((name["type"], name["value"], name["readonly"]), ("string", "Cube", False))
        self.assertGreaterEqual(name["maxlen"], 63)

    def edit(self, identity, index, value):
        exposed = blender_panels.exposed_fields(blender_panels.panels_state())
        field = exposed[identity]
        return blender_panels.apply_prop(field, index, protocol.coerce_prop(field, index, value))

    def test_an_edit_writes_through_the_field(self):
        self.assertTrue(self.edit("objects:Cube:location", 1, 2.5))
        self.assertAlmostEqual(self.cube.location[1], 2.5)

    def test_a_rotation_is_edited_in_degrees(self):
        self.assertTrue(self.edit("objects:Cube:rotation_euler", 2, 90))
        self.assertAlmostEqual(self.cube.rotation_euler[2], math.pi / 2, places=5)

    def test_enums_and_booleans(self):
        self.assertTrue(self.edit("objects:Cube:rotation_mode", -1, "XZY"))
        self.assertEqual(self.cube.rotation_mode, "XZY")
        self.assertTrue(self.edit("objects:Cube:hide_render", -1, True))
        self.assertTrue(self.cube.hide_render)

    def test_values_outside_the_field_are_refused(self):
        exposed = blender_panels.exposed_fields(blender_panels.panels_state())
        with self.assertRaises(protocol.Refused):
            protocol.coerce_prop(exposed["objects:Cube:rotation_mode"], -1, "SIDEWAYS")
        with self.assertRaises(protocol.Refused):
            protocol.coerce_prop(exposed["objects:Cube:hide_render"], -1, 1)
        with self.assertRaises(protocol.Refused):
            protocol.coerce_prop(exposed["objects:Cube:location"], 3, 1.0)

    def test_typing_a_name_renames_the_object(self):
        try:
            self.assertTrue(self.edit("objects:Cube:name", -1, "Kocka ✓"))
            self.assertIn("Kocka ✓", bpy.data.objects)
        finally:
            bpy.data.objects["Kocka ✓"].name = "Cube"

    def test_a_typed_parent_links_and_an_empty_one_unlinks(self):
        self.assertTrue(self.edit("objects:Cube:parent", -1, "Camera"))
        self.assertEqual(self.cube.parent, bpy.data.objects["Camera"])
        self.assertTrue(self.edit("objects:Cube:parent", -1, ""))
        self.assertIsNone(self.cube.parent)

    def test_a_parent_that_is_no_object_or_a_loop_is_refused(self):
        exposed = blender_panels.exposed_fields(blender_panels.panels_state())
        with self.assertRaises(protocol.Refused):
            protocol.coerce_prop(exposed["objects:Cube:parent"], -1, "Nothing")
        camera = bpy.data.objects["Camera"]
        camera.parent = self.cube
        try:
            self.assertFalse(self.edit("objects:Cube:parent", -1, "Camera"), "a parent loop")
            self.assertIsNone(self.cube.parent)
        finally:
            camera.parent = None

    def test_a_bone_parent_offers_the_armatures_bones(self):
        armature = bpy.data.objects.new("Rig", bpy.data.armatures.new("Rig"))
        bpy.context.scene.collection.objects.link(armature)
        bpy.context.view_layer.objects.active = armature
        bpy.ops.object.mode_set(mode="EDIT")
        bone = armature.data.edit_bones.new("Arm")
        bone.tail = (0.0, 0.0, 1.0)
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.view_layer.objects.active = self.cube
        self.cube.parent = armature
        self.cube.parent_type = "BONE"
        try:
            panels = {panel["name"]: panel for panel in blender_panels.panels_state()}
            bone_field = fields(panels["OBJECT_PT_relations"]["items"])["objects:Cube:parent_bone"]
            self.assertEqual((bone_field["type"], bone_field["choices"]), ("string", ["Arm"]))
            with self.assertRaises(protocol.Refused):
                protocol.coerce_prop(bone_field, -1, "Leg")
            self.assertTrue(self.edit("objects:Cube:parent_bone", -1, "Arm"))
            self.assertEqual(self.cube.parent_bone, "Arm")
        finally:
            self.cube.parent = None
            self.cube.parent_type = "OBJECT"
            bpy.data.objects.remove(armature)

    def test_file_paths_stay_read_only(self):
        image = bpy.data.images.new("Probe", 4, 4)
        try:
            field = blender_panels.describe(image, "filepath", None, -1)
            self.assertTrue(field["readonly"])
        finally:
            bpy.data.images.remove(image)

    def test_a_renamed_object_leaves_its_old_field_unresolved(self):
        exposed = blender_panels.exposed_fields(blender_panels.panels_state())
        field = exposed["objects:Cube:location"]
        self.cube.name = "Box"
        try:
            self.assertFalse(blender_panels.apply_prop(field, 0, 1.0))
        finally:
            self.cube.name = "Cube"


class FakePanel:
    """A ServedPanel without a runtime: keeps what Controls posts, hands it messages."""

    def __init__(self):
        self.posted = []
        self.receive = None

    def subscribe(self, callback):
        self.receive = callback

    def post(self, text):
        self.posted.append(json.loads(text))

    def latest(self, key):
        return next(m["value"] for m in reversed(self.posted) if m.get("t") == "state" and m.get("key") == key)


class ControlsSettingsTests(unittest.TestCase):
    """What the add-on's preferences choose reaches the page, and a hidden tab's edits are refused."""

    def setUp(self):
        self.cube = bpy.data.objects["Cube"]
        bpy.context.view_layer.objects.active = self.cube
        self.cube.location = (0.0, 0.0, 0.0)
        self.settings = (("material", "panels"), ("OBJECT_PT_visibility",))
        self.panel = FakePanel()
        self.controls = controls.Controls(self.panel, lambda: self.settings)

    def tearDown(self):
        self.controls.closed = True

    def send(self, **message):
        self.panel.receive(json.dumps(message), "user-1")
        self.controls.step()

    def test_the_page_gets_the_switched_on_tabs_and_the_chosen_panels(self):
        self.send(t="hello")
        self.assertEqual(self.panel.latest("scene")["tabs"], ["material", "panels"])
        self.assertEqual([p["name"] for p in self.panel.latest("panels")], ["OBJECT_name", "OBJECT_PT_visibility"])

    def test_an_edit_for_a_hidden_tab_or_panel_is_refused(self):
        self.send(t="hello")
        self.send(t="set", key="location", index=0, value=3.0, final=True)
        self.assertEqual(self.cube.location[0], 0.0, "the Object tab is off")
        self.send(t="prop", id="objects:Cube:location", index=0, value=3.0, final=True)
        self.assertEqual(self.cube.location[0], 0.0, "Transform is not a chosen panel")
        self.send(t="prop", id="objects:Cube:hide_render", index=-1, value=True, final=True)
        self.assertTrue(self.cube.hide_render, "Visibility is chosen")
        self.cube.hide_render = False

    def test_switching_the_panels_tab_off_empties_it(self):
        self.settings = (("object",), ("OBJECT_PT_visibility",))
        self.send(t="hello")
        self.assertEqual(self.panel.latest("panels"), [])
        self.send(t="prop", id="objects:Cube:hide_render", index=-1, value=True, final=True)
        self.assertFalse(self.cube.hide_render)


if __name__ == "__main__":
    result = unittest.main(argv=[sys.argv[0]], exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
