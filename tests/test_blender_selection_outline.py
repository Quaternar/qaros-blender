# SPDX-License-Identifier: MIT
"""The selection outline in Blender itself (background mode, factory scene, no device): every
instance carries its object's ANARI ``id``, and a selection change, in Blender's UI or the
headset, sets ``qar.selection`` to the selected object's id. The device is a stand-in that records
what it is given:

    blender -b --factory-startup --python-exit-code 1 --python test_blender_selection_outline.py"""
from pathlib import Path
import sys
import unittest

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))
import qaros_anari  # noqa: E402
from qaros_anari import anari, controls, sync  # noqa: E402


class RecordingDevice:
    """Records every object and parameter, and the order of commits and device parameters."""

    handle = 1

    def __init__(self):
        self.support = anari.MaterialSupport("matte", [("color", anari.FLOAT32_VEC3)], ())
        self.objects = {self.handle: {"kind": "Device", "parameters": {}}}
        self.events = []

    def material_support(self):
        return self.support

    def environment_support(self, require_visible=True):
        return False

    def new(self, kind, subtype=None):
        handle = len(self.objects) + 1
        self.objects[handle] = {"kind": kind, "subtype": subtype, "parameters": {}}
        return handle

    def array(self, _data_type, _count, data=None, _item_bytes=None):
        handle = self.new("Array")
        self.objects[handle]["parameters"]["data"] = data
        return handle

    def array2d(self, _data_type, _width, _height, _data, _item_bytes):
        return self.new("Array2D")

    def set(self, handle, name, data_type, value):
        self.objects[handle]["parameters"][name] = value
        if handle == self.handle:
            self.events.append(("device", name, value))

    def unset(self, handle, name):
        self.objects[handle]["parameters"].pop(name, None)

    def commit(self, handle=None):
        self.events.append(("commit", handle or self.handle))

    def release(self, handle):
        pass

    def close(self):
        pass

    @property
    def selection(self):
        return self.objects[self.handle]["parameters"].get("qar.selection")

    def instance_id(self, handle):
        return self.objects[handle]["parameters"].get("id")


class StreamerSelectionTests(unittest.TestCase):
    """The add-on's own Streamer, depsgraph handlers and sync worker, on the stand-in device."""

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=False)
        self.device = RecordingDevice()
        streamer = qaros_anari.STREAMER
        streamer.device = self.device
        streamer.mirror = sync.SceneMirror(self.device)
        streamer.capture = sync.SceneCapture(material_support=streamer.mirror.material_support)
        streamer.scene_name = bpy.context.scene.name
        streamer.mirror.apply(streamer.capture.capture(bpy.context.evaluated_depsgraph_get(), (), True))
        self.failures = []
        streamer.worker = sync.SyncWorker(streamer.mirror, self.failures.append)
        streamer.selection = None
        self.streamer = streamer
        qaros_anari.add_handlers()

    def tearDown(self):
        qaros_anari.remove_handlers()
        self.streamer.worker.close()
        streamer = self.streamer
        streamer.worker = streamer.mirror = streamer.capture = streamer.device = None
        streamer.selection = None

    def settle(self):
        bpy.context.view_layer.update()
        self.assertTrue(self.streamer.worker.wait_idle(30), "the sync worker did not finish")
        self.assertEqual(self.failures, [])

    def instance_of(self, name):
        return self.streamer.mirror.instance_slots[(name, 0)][0]

    def select(self, name):
        controls.select_only(name)
        self.settle()

    def deselect_all(self):
        for obj in bpy.context.view_layer.objects:
            obj.select_set(False)
        self.settle()

    def test_the_selection_at_start_is_outlined(self):
        self.assertEqual(controls.selected_name(), "Cube", "the factory scene selects its cube")
        self.streamer.follow_selection()
        self.settle()
        cube_id = self.device.instance_id(self.instance_of("Cube"))
        self.assertIsInstance(cube_id, int)
        self.assertEqual(self.device.selection, str(cube_id))

    def test_a_selection_change_in_blender_sets_the_parameter(self):
        self.streamer.follow_selection()
        self.settle()
        cube_id = self.device.instance_id(self.instance_of("Cube"))
        bpy.ops.mesh.primitive_uv_sphere_add(location=(3.0, 0.0, 0.0))
        sphere = bpy.context.active_object.name
        self.settle()
        sphere_id = self.device.instance_id(self.instance_of(sphere))
        self.assertNotEqual(sphere_id, cube_id, "every object has its own id")
        self.assertEqual(self.device.selection, str(sphere_id), "adding selects the new object")

        self.select("Cube")
        self.assertEqual(self.device.selection, str(cube_id))
        self.assertEqual(self.device.instance_id(self.instance_of("Cube")), cube_id, "ids are stable")

        self.deselect_all()
        self.assertEqual(self.device.selection, "", "nothing selected outlines nothing")

        self.select(sphere)
        self.assertEqual(self.device.selection, str(sphere_id))
        # An object without faces is selected but has no instance: nothing to outline there.
        self.select("Light")
        self.assertNotIn(("Light", 0), self.streamer.mirror.instance_slots)
        self.assertNotEqual(self.device.selection, str(sphere_id))

    def test_an_unchanged_selection_sets_nothing(self):
        self.streamer.follow_selection()
        self.settle()
        before = [event for event in self.device.events if event[0] == "device"]
        bpy.context.view_layer.objects["Cube"].location.x += 1.0
        self.settle()
        after = [event for event in self.device.events if event[0] == "device"]
        self.assertEqual(after, before, "a move of the selected object keeps the parameter")

    def test_a_models_root_outlines_every_part_below_it(self):
        """A root empty has no geometry: selecting it names the ids of its parts (children and
        grandchildren), so the stream outlines the whole model."""
        root = bpy.data.objects.new("ModelRoot", None)
        bpy.context.scene.collection.objects.link(root)
        parts = []
        for index in range(2):
            bpy.ops.mesh.primitive_cube_add(location=(5.0 + 3.0 * index, 0.0, 0.0))
            part = bpy.context.active_object
            part.parent = root
            parts.append(part)
        deep = bpy.data.objects.new("DeepPart", parts[1].data)
        bpy.context.scene.collection.objects.link(deep)
        deep.parent = parts[1]
        self.settle()
        controls.select_only("ModelRoot", with_children=True)
        self.settle()
        ids = {str(self.device.instance_id(self.instance_of(part.name))) for part in (*parts, deep)}
        self.assertEqual(len(ids), 3, "every part has its own id")
        root_id = str(self.streamer.mirror.object_id("ModelRoot"))  # names no instance: nothing to draw
        self.assertEqual(set(self.device.selection.split(",")), ids | {root_id})
        self.assertNotIn(("ModelRoot", 0), self.streamer.mirror.instance_slots, "the root draws nothing")
        self.select("Cube")
        self.assertEqual(len(self.device.selection.split(",")), 1, "a part-free object is outlined alone")
        self.assertTrue(controls.outlined_names("ModelRoot")[0] == "ModelRoot")
        self.assertEqual(set(controls.outlined_names("ModelRoot")), {"ModelRoot", *(p.name for p in parts), "DeepPart"})
        self.assertEqual(controls.outlined_names(None), ())


class MirrorSelectionTests(unittest.TestCase):
    """SceneMirror.select on a scene of merged objects."""

    def setUp(self):
        self.device = RecordingDevice()
        self.mirror = sync.SceneMirror(self.device)
        snapshot = sync.Snapshot(True)
        snapshot.materials = {}
        part = (
            np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32),
            None,
            np.array([[0, 1, 2]], dtype=np.uint32),
            None,
        )
        self.names = [f"Object {i}" for i in range(sync.BATCH_MIN_OBJECTS + 2)]
        snapshot.meshes = {name: [part] for name in self.names}
        snapshot.instances = [
            (name, np.eye(4, dtype=np.float32).flatten().tolist()) for name in self.names
        ]
        self.mirror.apply(snapshot)

    def tearDown(self):
        self.mirror.close()

    def test_a_selected_object_leaves_its_chunk_for_an_instance_with_its_id(self):
        self.assertIn("Object 3", self.mirror.batched, "static objects of one material merge")
        self.device.events.clear()
        self.assertTrue(self.mirror.select("Object 3", now=100.0))
        self.assertNotIn("Object 3", self.mirror.batched)
        instance = self.mirror.instance_slots[("Object 3", 0)][0]
        object_id = self.device.instance_id(instance)
        self.assertEqual(self.device.selection, str(object_id))
        self.assertIn(instance, self.mirror.instances)
        # The world lists the new instance before the device names its id.
        world_commit = self.device.events.index(("commit", self.mirror.world))
        selection_set = self.device.events.index(("device", "qar.selection", str(object_id)))
        self.assertLess(world_commit, selection_set)
        self.assertEqual(self.device.events[-1], ("commit", self.device.handle))

    def test_several_selected_objects_are_named_together_and_all_leave_their_chunks(self):
        self.assertTrue(self.mirror.select(("Object 3", "Object 4"), now=100.0))
        ids = [str(self.device.instance_id(self.mirror.instance_slots[(name, 0)][0]))
               for name in ("Object 3", "Object 4")]
        self.assertEqual(self.device.selection, ",".join(ids))
        self.assertNotIn("Object 3", self.mirror.batched)
        self.assertNotIn("Object 4", self.mirror.batched)
        self.assertFalse(self.mirror.select(("Object 3", "Object 4"), now=100.0))
        self.assertTrue(self.mirror.select("Object 3", now=100.0), "a smaller selection is a change")
        self.assertEqual(self.device.selection, ids[0])

    def test_a_deselected_object_merges_again(self):
        self.mirror.select("Object 3", now=100.0)
        self.assertFalse(self.mirror.select("Object 3", now=100.0), "the same selection changes nothing")
        self.mirror.select(None, now=101.0)
        self.assertEqual(self.device.selection, "")
        self.assertIn("Object 3", self.mirror.batched)

    def test_the_worker_selects_after_the_snapshots_before(self):
        failures = []
        worker = sync.SyncWorker(self.mirror, failures.append)
        try:
            worker.select("Object 5")
            self.assertTrue(worker.wait_idle(30))
        finally:
            worker.close()
        self.assertEqual(failures, [])
        instance = self.mirror.instance_slots[("Object 5", 0)][0]
        self.assertEqual(self.device.selection, str(self.device.instance_id(instance)))


if __name__ == "__main__":
    result = unittest.main(argv=[sys.argv[0]], exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
