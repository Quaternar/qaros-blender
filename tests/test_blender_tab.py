# SPDX-License-Identifier: MIT
"""The QAROS tab in Blender itself (background mode): every panel draws against Blender's own
properties and icons, the Active Object panel follows the selection, the Renderer choice reaches the
variables qaros reads, and the stream's capture follows the shading and the object's Stream setting:

    blender -b --factory-startup --python-exit-code 1 --python test_blender_tab.py"""
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest

import bpy
from _bpy_internal.addons.cli import set_from_cli

ADDON = Path(__file__).resolve().parents[1] / "blender"
sys.path.insert(0, str(ADDON))
os.environ.pop("QAR_BLENDER_AUTO_START", None)
set_from_cli("qaros_anari")
import qaros_anari  # noqa: E402
from qaros_anari import shading, sync  # noqa: E402

ICONS = set(bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"].enum_items.keys())


def parameters(function):
    return set(bpy.types.UILayout.bl_rna.functions[function].parameters.keys())


class Layout:
    """Records what a panel draws and checks it against Blender: every property exists on its data,
    every keyword is one UILayout takes, every icon is one of Blender's."""

    def __init__(self, test, drawn):
        self.test = test
        self.drawn = drawn
        self.enabled = True
        self.alert = False
        self.scale_y = 1.0
        self.use_property_split = False
        self.use_property_decorate = True

    def check(self, function, keywords):
        unknown = set(keywords) - parameters(function)
        self.test.assertFalse(unknown, f"UILayout.{function} has no {unknown}")
        icon = keywords.get("icon")
        if icon is not None:
            self.test.assertIn(icon, ICONS, f"{icon} is not a Blender icon")

    def nested(self, **keywords):
        return Layout(self.test, self.drawn)

    row = column = box = nested

    def grid_flow(self, **keywords):
        return self.nested()

    def label(self, **keywords):
        self.check("label", keywords)
        self.drawn.append(("label", keywords.get("text", ""), keywords.get("icon")))

    def prop(self, data, name, **keywords):
        self.check("prop", keywords)
        self.test.assertIn(name, data.bl_rna.properties.keys(), f"{data!r} has no {name}")
        self.drawn.append(("prop", name, keywords.get("icon")))

    def operator(self, idname, **keywords):
        self.check("operator", keywords)
        category, name = idname.split(".")
        self.test.assertTrue(hasattr(getattr(bpy.ops, category), name), idname)
        self.drawn.append(("operator", idname, keywords.get("icon")))
        return SimpleNamespace()

    def operator_menu_enum(self, idname, property, **keywords):
        self.check("operator_menu_enum", keywords)
        self.drawn.append(("menu", idname, keywords.get("text")))


def reset_scene():
    """The factory scene as the tests change it. Not read_factory_settings: it would drop the
    add-on, which --addons alone enabled."""
    for obj in bpy.data.objects:
        obj.qaros_stream = True
    bpy.data.materials["Material"].diffuse_color = (0.8, 0.8, 0.8, 1.0)
    bpy.context.window_manager.qaros_shading = shading.FOLLOW


def panels():
    return [cls for cls in qaros_anari.CLASSES if issubclass(cls, bpy.types.Panel)]


class TabTests(unittest.TestCase):
    def setUp(self):
        reset_scene()
        qaros_anari.STREAMER.error = None

    def draw(self, panel, obj=None):
        drawn = []
        context = SimpleNamespace(
            scene=bpy.context.scene, window_manager=bpy.context.window_manager,
            preferences=bpy.context.preferences, active_object=obj, object=obj,
        )
        if hasattr(panel, "poll") and not panel.poll(context):
            return None
        instance = SimpleNamespace(layout=Layout(self, drawn))
        if hasattr(panel, "draw_header"):
            panel.draw_header(instance, context)
        panel.draw(instance, context)
        return drawn

    def test_every_panel_draws_with_blenders_properties_and_icons(self):
        cube = bpy.data.objects["Cube"]
        for panel in panels():
            for obj in (None, cube, bpy.data.objects["Light"]):
                with self.subTest(panel=panel.__name__, obj=obj and obj.name):
                    self.draw(panel, obj)

    def test_every_icon_named_in_the_source_is_a_blender_icon(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in (ADDON / "qaros_anari").glob("*.py"))
        named = set(re.findall(r'icon="([A-Z_0-9]+)"', source))
        named |= set(shading.ICONS.values()) | set(qaros_anari.TOOL_ICONS.values())
        named |= set(qaros_anari.OBJECT_ICONS.values())
        self.assertFalse(named - ICONS, "not Blender icons")

    def test_the_active_object_panel_follows_the_selection(self):
        drawn = self.draw(qaros_anari.QAROS_PT_object, None)
        self.assertIn(("label", "Select an object", "RESTRICT_SELECT_OFF"), drawn)
        cube = bpy.data.objects["Cube"]
        drawn = self.draw(qaros_anari.QAROS_PT_object, cube)
        self.assertIn(("label", "Cube", "OUTLINER_OB_MESH"), drawn)
        self.assertIn(("prop", "qaros_stream", "HIDE_OFF"), drawn)
        self.assertIn(("label", "Streams when streaming starts", "CHECKMARK"), drawn)
        self.assertIn(("prop", "diffuse_color", None), drawn, "the factory viewport's Solid: the viewport colour")
        bpy.context.window_manager.qaros_shading = shading.RENDERED
        drawn = self.draw(qaros_anari.QAROS_PT_object, cube)
        self.assertIn(("prop", "default_value", None), drawn, "the Principled inputs the stream uses")
        cube.qaros_stream = False
        drawn = self.draw(qaros_anari.QAROS_PT_object, cube)
        self.assertIn(("prop", "qaros_stream", "HIDE_ON"), drawn)
        self.assertIn(("label", "Left out of the stream", "HIDE_ON"), drawn)

    def test_every_outlined_object_reads_outlined_while_streaming(self):
        streamer = qaros_anari.STREAMER
        saved = streamer.device, streamer.capture, streamer.selection
        try:
            streamer.device, streamer.capture = object(), None
            streamer.selection = ("Root", "Cube")  # a model root selected: it and its parts
            outlined = ("RESTRICT_SELECT_OFF", "Streamed, outlined in the headset", False)
            self.assertEqual(qaros_anari.object_status(bpy.data.objects["Cube"]), outlined)
            self.assertEqual(
                qaros_anari.object_status(bpy.data.objects["Light"]), ("CHECKMARK", "Streamed", False)
            )
            streamer.selection = ()
            self.assertEqual(
                qaros_anari.object_status(bpy.data.objects["Cube"]), ("CHECKMARK", "Streamed", False)
            )
        finally:
            streamer.device, streamer.capture, streamer.selection = saved

    def test_the_object_properties_tab_has_a_qaros_panel(self):
        panel = qaros_anari.QAROS_PT_object_properties
        self.assertEqual((panel.bl_space_type, panel.bl_context), ("PROPERTIES", "object"))
        drawn = self.draw(panel, bpy.data.objects["Cube"])
        self.assertEqual(drawn[0], ("prop", "qaros_stream", None), "the header's checkbox")

    def test_status_shows_as_an_icon(self):
        self.assertEqual(qaros_anari.STREAMER.state(), ("UNLINKED", "Off", False))
        qaros_anari.STREAMER.error = "Cannot start: no QAROS"
        drawn = self.draw(qaros_anari.QAROS_PT_panel)
        self.assertEqual(drawn[0], ("label", "", "ERROR"), "the header")
        self.assertIn(("label", "Cannot start: no QAROS", "ERROR"), drawn)
        self.assertIn(("operator", "qaros.start_streaming", "PLAY"), drawn)

    def test_the_shading_row_is_icons_with_follow_first(self):
        drawn = self.draw(qaros_anari.QAROS_PT_view)
        self.assertIn(("prop", "qaros_shading", None), drawn)
        viewport = shading.from_view(qaros_anari.viewport_shading()).mode
        self.assertIn(("label", shading.LABELS[viewport] + " (viewport)", shading.ICONS[viewport]), drawn)
        bpy.context.window_manager.qaros_shading = shading.WIREFRAME
        try:
            self.assertEqual(qaros_anari.wanted_shading().mode, shading.WIREFRAME)
            drawn = self.draw(qaros_anari.QAROS_PT_view)
            self.assertIn(("label", "Wireframe", "SHADING_WIRE"), drawn)
        finally:
            bpy.context.window_manager.qaros_shading = shading.FOLLOW

    def test_every_operator_and_property_has_a_tooltip(self):
        for cls in qaros_anari.CLASSES:
            if issubclass(cls, bpy.types.Operator):
                self.assertTrue(getattr(cls, "bl_description", ""), cls.__name__)
        for name, prop in qaros_anari.Preferences.__annotations__.items():
            self.assertTrue(prop.keywords.get("description"), name)
        for item in qaros_anari.BACKEND_ITEMS:
            self.assertTrue(item[2], item[0])


class RendererTests(unittest.TestCase):
    def test_a_named_renderer_is_the_only_backend(self):
        environ = {}
        qaros_anari.backend_environment("visrtx", environ)
        self.assertEqual(environ, {"QAR_ANARI_BACKEND": "visrtx"})

    def test_automatic_after_a_named_renderer_lets_qaros_choose_again(self):
        environ = {"QAR_ANARI_BACKEND": "visrtx"}
        qaros_anari.backend_environment("auto", environ)
        self.assertEqual(environ, {"QAR_ANARI_BACKEND_ORDER": qaros_anari.AUTOMATIC_ORDER})

    def test_automatic_keeps_what_blender_started_with(self):
        environ = {"QAR_ANARI_BACKEND": "visgl", "QAR_ANARI_BACKEND_ORDER": "helide"}
        qaros_anari.backend_environment("auto", environ, startup="helide")
        self.assertEqual(environ, {"QAR_ANARI_BACKEND": "helide", "QAR_ANARI_BACKEND_ORDER": "helide"})

    def test_choosing_a_renderer_while_stopped_sets_the_preference_only(self):
        prefs = qaros_anari.preferences()
        before = prefs.backend
        try:
            self.assertEqual(bpy.ops.qaros.set_backend(backend="helide"), {"FINISHED"})
            self.assertEqual(prefs.backend, "helide")
            self.assertFalse(qaros_anari.STREAMER.running)
            self.assertEqual(qaros_anari.STREAMER.restart_reasons(), ())
        finally:
            prefs.backend = before

    def test_each_renderer_loads_its_own_library_once(self):
        loaded = []

        class FakeLibrary:
            def __init__(self, folder, name, on_status):
                loaded.append((folder, os.environ.get("QAR_ANARI_BACKEND")))

        saved = (
            dict(qaros_anari.Streamer.libraries), os.environ.get("QAR_ANARI_BACKEND"), qaros_anari.Streamer.folder
        )
        original = qaros_anari.anari.Library
        qaros_anari.anari.Library = FakeLibrary
        try:
            qaros_anari.Streamer.libraries.clear()
            qaros_anari.Streamer.folder = None
            streamer = qaros_anari.Streamer()
            for backend in ("helide", "visgl", "helide"):
                streamer.load_library(SimpleNamespace(anari_folder="", backend=backend))
            streamer.load_library(SimpleNamespace(anari_folder="elsewhere", backend="ospray"))
            self.assertEqual(loaded, [("", "helide"), ("", "visgl"), ("", "ospray")], "the first ANARI folder")
            self.assertEqual(streamer.library_key, "ospray")
        finally:
            qaros_anari.anari.Library = original
            qaros_anari.Streamer.libraries.clear()
            qaros_anari.Streamer.libraries.update(saved[0])
            qaros_anari.Streamer.folder = saved[2]
            if saved[1] is None:
                os.environ.pop("QAR_ANARI_BACKEND", None)
            else:
                os.environ["QAR_ANARI_BACKEND"] = saved[1]


class CaptureTests(unittest.TestCase):
    def setUp(self):
        reset_scene()
        self.capture = sync.SceneCapture()

    def snapshot(self, mode=shading.RENDERED):
        self.capture.shading = shading.from_view(None, mode)
        return self.capture.capture(bpy.context.evaluated_depsgraph_get(), (), True,
                                    qaros_anari.material_descriptions(bpy.data.materials))

    def test_an_object_left_out_of_the_stream_is_not_captured(self):
        self.assertIn("Cube", [name for name, _transform in self.snapshot().instances])
        bpy.data.objects["Cube"].qaros_stream = False
        self.assertNotIn("Cube", [name for name, _transform in self.snapshot().instances])
        bpy.data.objects["Light"].qaros_stream = False
        self.assertEqual(self.snapshot().lights, [])

    def test_wireframe_captures_the_cube_edges_not_its_triangles(self):
        snapshot = self.snapshot(shading.WIREFRAME)
        (positions, normals, segments, key), = snapshot.meshes["Cube"]
        self.assertEqual((len(positions), len(segments)), (8, 12), "a quad's four sides, no diagonals")
        self.assertIsNone(normals)
        self.assertEqual(key, shading.WIRE_KEY)
        self.assertTrue(sync.is_wire(snapshot.meshes["Cube"][0]))
        self.assertEqual(snapshot.materials[shading.WIRE_KEY], shading.WIRE_MATERIAL)
        self.assertGreater(snapshot.wire_radius, 0.0)
        self.assertEqual(snapshot.lights, [], "the studio light, as Blender's wireframe")

    def test_solid_is_matte_in_the_viewport_colour_and_material_keeps_the_principled(self):
        material = bpy.data.materials["Material"]
        material.diffuse_color = (0.0, 0.0, 1.0, 1.0)
        solid = self.snapshot(shading.SOLID).materials["Material"]
        self.assertEqual(solid.base_color, (0.0, 0.0, 1.0))
        self.assertFalse(solid.is_principled)
        rendered = self.snapshot(shading.RENDERED).materials["Material"]
        self.assertTrue(rendered.is_principled)
        self.assertEqual(len(self.snapshot(shading.RENDERED).lights), 1)
        self.assertEqual(self.snapshot(shading.MATERIAL).lights, [], "Material Preview's own scene-lights option")


if __name__ == "__main__":
    result = unittest.main(argv=[sys.argv[0]], exit=False).result
    if not result.wasSuccessful():
        sys.exit(1)
