# SPDX-License-Identifier: MIT
"""The Blender add-on's USD import fix-up (qaros_anari/usd_import.py) in Blender itself (background
mode, its bundled pxr): primitive shapes get the materials bound to them, which Blender's own USD
import drops. Needs no ANARI device:

    blender -b --factory-startup --python-exit-code 1 --python test_blender_usd_import.py"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import bpy
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdShade

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))
import qaros_anari  # noqa: E402
from qaros_anari import usd_import  # noqa: E402

SHAPES = {
    "Sphere": UsdGeom.Sphere, "Cube": UsdGeom.Cube, "Cylinder": UsdGeom.Cylinder,
    "Cone": UsdGeom.Cone, "Capsule": UsdGeom.Capsule, "Plane": UsdGeom.Plane,
}


def define_material(stage, name, opacity=None, threshold=None):
    material = UsdShade.Material.Define(stage, f"/Root/Looks/{name}")
    shader = UsdShade.Shader.Define(stage, f"/Root/Looks/{name}/Preview")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(1.0, 0.0, 0.0))
    if opacity is not None:
        shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(opacity)
    if threshold is not None:
        shader.CreateInput("opacityThreshold", Sdf.ValueTypeNames.Float).Set(threshold)
    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    return material


def principled_inputs(material_name):
    node = next(n for n in bpy.data.materials[material_name].node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    return (round(node.inputs["Alpha"].default_value, 4), round(node.inputs["Transmission Weight"].default_value, 4),
            node.inputs["Alpha"].is_linked)


def define_triangle(stage, path):
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    return mesh


def bind(prim, material, purpose=UsdShade.Tokens.allPurpose):
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(material, materialPurpose=purpose)


def material_names(object_name):
    return [slot.material.name if slot.material else None for slot in bpy.data.objects[object_name].material_slots]


class UsdShapeMaterialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        qaros_anari.register()

    @classmethod
    def tearDownClass(cls):
        qaros_anari.unregister()

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def stage(self, build):
        """A stage under /Root that `build(stage)` fills; returns its file."""
        path = Path(self.folder.name) / "scene.usda"
        stage = Usd.Stage.CreateNew(str(path))
        stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, "/Root").GetPrim())
        build(stage)
        stage.GetRootLayer().Save()
        return str(path)

    def test_every_shape_type_gets_its_bound_material(self):
        def build(stage):
            for name, schema in SHAPES.items():
                bind(schema.Define(stage, f"/Root/{name}").GetPrim(), define_material(stage, f"Mat{name}"))

        bpy.ops.wm.usd_import(filepath=self.stage(build), import_all_materials=True)
        for name in SHAPES:
            with self.subTest(shape=name):
                self.assertEqual(material_names(name), [f"Mat{name}"])

    def test_inherited_and_preview_bindings_resolve(self):
        def build(stage):
            UsdGeom.Xform.Define(stage, "/Root/Parent")
            bind(stage.GetPrimAtPath("/Root/Parent"), define_material(stage, "Inherited"))
            UsdGeom.Sphere.Define(stage, "/Root/Parent/Child")
            # Only a preview-purpose binding: Blender's default purpose (Full) falls back to it.
            bind(UsdGeom.Cube.Define(stage, "/Root/PreviewOnly").GetPrim(), define_material(stage, "Preview"),
                 UsdShade.Tokens.preview)

        bpy.ops.wm.usd_import(filepath=self.stage(build), import_all_materials=True)
        self.assertEqual(material_names("Child"), ["Inherited"])
        self.assertEqual(material_names("PreviewOnly"), ["Preview"])

    def test_shapes_with_one_name_keep_their_own_materials(self):
        def build(stage):
            for parent, x in (("Left", -2.0), ("Right", 2.0)):
                UsdGeom.Xform.Define(stage, f"/Root/{parent}").AddTranslateOp().Set(Gf.Vec3d(x, 0, 0))
                bind(UsdGeom.Sphere.Define(stage, f"/Root/{parent}/Ball").GetPrim(), define_material(stage, parent))

        bpy.ops.wm.usd_import(filepath=self.stage(build), import_all_materials=True)
        # Blender names the second "Ball.001" (and merges each one-child Xform into it): tell them
        # apart by where they are.
        by_side = {
            "Left" if bpy.data.objects[name].matrix_world.translation.x < 0 else "Right": material_names(name)
            for name in ("Ball", "Ball.001")
        }
        self.assertEqual(by_side, {"Left": ["Left"], "Right": ["Right"]})

    def test_meshes_and_unbound_shapes_are_left_alone(self):
        def build(stage):
            bind(define_triangle(stage, "/Root/Triangle").GetPrim(), define_material(stage, "MeshMat"))
            UsdGeom.Sphere.Define(stage, "/Root/Bare")

        bpy.ops.wm.usd_import(filepath=self.stage(build), import_all_materials=True)
        self.assertEqual(material_names("Triangle"), ["MeshMat"])
        self.assertEqual(material_names("Bare"), [])

    def test_default_import_names_the_materials_it_did_not_create(self):
        def build(stage):
            bind(UsdGeom.Sphere.Define(stage, "/Root/Ball").GetPrim(), define_material(stage, "Red"))

        with patch.object(usd_import, "log") as log:
            bpy.ops.wm.usd_import(filepath=self.stage(build))
        self.assertEqual(material_names("Ball"), [])
        messages = " ".join(call.args[0] for call in log.call_args_list)
        self.assertIn("/Root/Looks/Red", messages)
        self.assertIn("USD for QAROS", messages)

    def test_preview_surface_opacity_streams_as_alpha_not_transmission(self):
        def build(stage):
            for name, opacity in (("Glass", 0.4), ("Opaque", 1.0)):
                bind(define_triangle(stage, f"/Root/{name}").GetPrim(), define_material(stage, name, opacity))
            # A shape: its material only exists with Import All Materials, and gets both fixes.
            bind(UsdGeom.Sphere.Define(stage, "/Root/Bubble").GetPrim(), define_material(stage, "Bubble", 0.25))

        bpy.ops.wm.usd_import(filepath=self.stage(build), import_all_materials=True)
        # (Alpha, Transmission Weight, Alpha linked); Blender alone gives Glass (1.0, 0.6, False).
        self.assertEqual(principled_inputs("Glass"), (0.4, 0.0, False))
        self.assertEqual(principled_inputs("Opaque"), (1.0, 0.0, False))
        self.assertEqual(material_names("Bubble"), ["Bubble"])
        self.assertEqual(principled_inputs("Bubble"), (0.25, 0.0, False))

    def test_opacity_with_a_threshold_is_left_to_blender(self):
        def build(stage):
            bind(define_triangle(stage, "/Root/Cutout").GetPrim(), define_material(stage, "Cutout", 0.4, 0.5))

        with patch.object(usd_import, "log") as log:
            bpy.ops.wm.usd_import(filepath=self.stage(build), import_all_materials=True)
        # Blender already reads it as an Alpha cutout; the hook changes nothing.
        self.assertEqual(principled_inputs("Cutout")[1], 0.0)
        self.assertFalse(any("Transmission" in call.args[0] for call in log.call_args_list))

    def test_import_operator_gives_sphere_lights_their_world_radius(self):
        def build(stage):
            UsdGeom.SetStageMetersPerUnit(stage, 0.01)
            UsdLux.SphereLight.Define(stage, "/Root/Bulb").CreateRadiusAttr(5.0)  # 5 cm

        path = self.stage(build)
        bpy.ops.wm.usd_import(filepath=path)
        # Blender alone: 5, read as metres by Cycles, which ignores the 0.01 object scale here.
        self.assertAlmostEqual(bpy.data.objects["Bulb"].data.shadow_soft_size, 5.0, places=5)
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.assertEqual(bpy.ops.qaros.import_usd(filepath=path), {"FINISHED"})
        self.assertAlmostEqual(bpy.data.objects["Bulb"].data.shadow_soft_size, 0.05, places=5)

    def test_import_operator_binds_shapes_and_drops_only_its_own_unused_materials(self):
        def build(stage):
            bind(UsdGeom.Sphere.Define(stage, "/Root/Ball").GetPrim(), define_material(stage, "Red"))
            define_material(stage, "Unused")

        kept = bpy.data.materials.new("Already here, unused")
        self.assertEqual(bpy.ops.qaros.import_usd(filepath=self.stage(build)), {"FINISHED"})
        self.assertEqual(material_names("Ball"), ["Red"])
        # Bound by the hook, so no longer held by the fake user Import All Materials gave it.
        self.assertFalse(bpy.data.materials["Red"].use_fake_user)
        self.assertNotIn("Unused", bpy.data.materials)
        self.assertIn(kept.name, bpy.data.materials)


if __name__ == "__main__":
    test_arguments = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    result = unittest.main(argv=[sys.argv[0], *test_arguments], exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
