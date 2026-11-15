# SPDX-License-Identifier: MIT
"""Gaussian splats and point clouds are not streamed: the add-on warns once per object and leaves
them out of the scene bounds. Needs no ANARI device:

    blender -b --factory-startup --python-exit-code 1 --python test_blender_splats.py"""
from pathlib import Path
import sys
import unittest

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))
from qaros_anari import sync  # noqa: E402

TORUS = Path(r"C:\lukas-dev\QarBuild\test-apps\models\splats\synthetic-torus\torus_3dgs.ply")


def loose_vertices(name, location, splat):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(-1, -1, -1), (1, 1, 1), (0, 1, 0)], [], [])
    if splat:
        for attribute in sync.SPLAT_ATTRIBUTES:
            mesh.attributes.new(attribute, "FLOAT", "POINT")
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


class UnstreamedTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=False)
        self.warnings = []
        self.capture = sync.SceneCapture(self.warnings.append)

    def depsgraph(self):
        bpy.context.view_layer.update()
        return bpy.context.evaluated_depsgraph_get()

    def capture_scene(self, full=False):
        return self.capture.capture(self.depsgraph(), set(), full)

    def bounds(self):
        return sync.scene_bounds(self.depsgraph())

    def test_splat_mesh_warns_once_and_is_not_streamed(self):
        loose_vertices("Splat", (50, 0, 0), True)
        snapshot = self.capture_scene()
        self.capture_scene(full=True)
        self.assertEqual(self.warnings, ["'Splat': Gaussian splats and point clouds are not streamed"])
        self.assertEqual(self.capture.unstreamed, {"Splat": self.warnings[0]})
        self.assertNotIn("Splat", snapshot.meshes)
        self.assertNotIn("Splat", [name for name, _ in snapshot.instances])
        self.assertIn("Cube", [name for name, _ in snapshot.instances])

    def test_faceless_mesh_without_splat_attributes(self):
        loose_vertices("Dots", (0, 0, 0), False)
        self.capture_scene()
        self.assertEqual(self.warnings, ["'Dots': has no faces: not streamed"])

    def test_factory_scene_has_no_warning(self):
        self.capture_scene()
        self.assertEqual(self.warnings, [])
        self.assertEqual(self.capture.unstreamed, {})

    def test_unstreamed_objects_do_not_count_for_the_bounds(self):
        loose_vertices("Splat", (100, 0, 0), True)
        np.testing.assert_allclose(self.bounds()[1], (1.0, 1.0, 1.0), atol=1e-5)
        bpy.data.objects.remove(bpy.data.objects["Cube"])
        self.assertIsNone(self.bounds())

    def test_hidden_splat_is_not_reported(self):
        loose_vertices("Splat", (0, 0, 0), True).hide_set(True)
        self.capture_scene()
        self.assertEqual(self.warnings, [])

    def test_imported_3dgs_ply(self):
        if not TORUS.is_file() or not hasattr(bpy.ops.wm, "ply_import"):
            self.skipTest("no 3DGS PLY test file or PLY importer")
        bpy.ops.wm.ply_import(filepath=str(TORUS))
        imported = bpy.context.active_object
        self.capture_scene()
        self.assertEqual(self.warnings, [f"'{imported.name}': Gaussian splats and point clouds are not streamed"])
        np.testing.assert_allclose(self.bounds()[1], (1.0, 1.0, 1.0), atol=1e-5)

    def test_point_cloud_object(self):
        if not hasattr(bpy.data, "pointclouds"):
            self.skipTest("this Blender has no point cloud objects")
        cloud = bpy.data.objects.new("Cloud", bpy.data.pointclouds.new("Cloud"))
        bpy.context.scene.collection.objects.link(cloud)
        self.capture_scene()
        self.assertEqual(self.warnings, ["'Cloud': Gaussian splats and point clouds are not streamed"])
        np.testing.assert_allclose(self.bounds()[1], (1.0, 1.0, 1.0), atol=1e-5)


if __name__ == "__main__":
    argv = [sys.argv[0]] + (sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    unittest.main(argv=argv)
