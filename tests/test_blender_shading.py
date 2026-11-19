# SPDX-License-Identifier: MIT
"""The stream's shading after the 3D viewport's (qaros_anari/shading.py), with plain Python."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest

folder = Path(__file__).resolve().parents[1] / "blender" / "qaros_anari"


def load(name):
    """`qaros_anari.<name>` without the package's __init__ (it needs bpy)."""
    if "qaros_anari" not in sys.modules:
        package = ModuleType("qaros_anari")
        package.__path__ = [str(folder)]
        sys.modules["qaros_anari"] = package
    spec = importlib.util.spec_from_file_location(f"qaros_anari.{name}", folder / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


materials = load("materials")
shading = load("shading")


def view(mode, **values):
    defaults = dict(
        type=mode, color_type="MATERIAL", single_color=(0.8, 0.8, 0.8), use_scene_lights=False,
        use_scene_world=False, use_scene_lights_render=True, use_scene_world_render=True,
    )
    defaults.update(values)
    return SimpleNamespace(**defaults)


PRINCIPLED = materials.MaterialDescription(
    base_color=(1.0, 0.0, 0.0), metallic=1.0, roughness=0.2, is_principled=True, viewport_color=(0.0, 0.0, 1.0)
)


def studio_view(mode, **values):
    values.setdefault("selected_studio_light", SimpleNamespace(path="C:/Blender/studiolights/world/forest.exr"))
    values.setdefault("studiolight_intensity", 1.25)
    values.setdefault("studiolight_rotate_z", 0.5)
    return view(mode, **values)


class StudioLightTests(unittest.TestCase):
    def test_material_preview_without_the_world_takes_the_studio_hdri(self):
        stream = shading.from_view(studio_view(shading.MATERIAL))
        self.assertEqual(stream.studio, ("C:/Blender/studiolights/world/forest.exr", 1.25, 0.5))
        self.assertFalse(stream.scene_world)

    def test_rendered_without_the_world_takes_it_too(self):
        stream = shading.from_view(studio_view(shading.RENDERED, use_scene_world_render=False))
        self.assertEqual(stream.studio_light, "C:/Blender/studiolights/world/forest.exr")

    def test_the_scene_world_replaces_the_studio_hdri(self):
        for stream in (
            shading.from_view(studio_view(shading.MATERIAL, use_scene_world=True)),
            shading.from_view(studio_view(shading.RENDERED)),
            shading.from_view(studio_view(shading.SOLID)),
        ):
            self.assertEqual(stream.studio_light, "")

    def test_intensity_and_rotation_do_not_count_as_another_shading(self):
        first = shading.from_view(studio_view(shading.MATERIAL))
        turned = shading.from_view(studio_view(shading.MATERIAL, studiolight_rotate_z=1.0, studiolight_intensity=2.0))
        other = shading.from_view(studio_view(
            shading.MATERIAL, selected_studio_light=SimpleNamespace(path="C:/Blender/studiolights/world/city.exr")
        ))
        self.assertEqual(first, turned)
        self.assertNotEqual(first.studio, turned.studio)
        self.assertNotEqual(first, other)


class FromViewTests(unittest.TestCase):
    def test_without_a_viewport_the_stream_renders_as_before(self):
        self.assertEqual(shading.from_view(None), shading.Shading())
        self.assertEqual(shading.from_view(None).mode, shading.RENDERED)

    def test_each_viewport_mode_maps_to_its_stream_mode(self):
        for mode in shading.MODES:
            self.assertEqual(shading.from_view(view(mode)).mode, mode)

    def test_solid_and_wireframe_use_the_studio_light_only(self):
        for mode in (shading.SOLID, shading.WIREFRAME):
            stream = shading.from_view(view(mode, use_scene_lights=True, use_scene_world=True))
            self.assertFalse(stream.scene_lights)
            self.assertFalse(stream.scene_world)

    def test_material_preview_and_rendered_follow_their_own_scene_options(self):
        preview = shading.from_view(view(shading.MATERIAL, use_scene_lights=True))
        self.assertEqual((preview.scene_lights, preview.scene_world), (True, False))
        rendered = shading.from_view(view(shading.RENDERED, use_scene_world_render=False))
        self.assertEqual((rendered.scene_lights, rendered.scene_world), (True, False))

    def test_a_fixed_choice_overrides_the_viewport_mode(self):
        self.assertEqual(shading.from_view(view(shading.SOLID), shading.WIREFRAME).mode, shading.WIREFRAME)
        self.assertEqual(shading.from_view(None, shading.SOLID).mode, shading.SOLID)

    def test_solid_colour_settings_only_change_solid(self):
        self.assertEqual(
            shading.from_view(view(shading.RENDERED, color_type="SINGLE", single_color=(1, 0, 0))),
            shading.from_view(view(shading.RENDERED)),
            "a setting the mode ignores must not resync the scene",
        )
        self.assertNotEqual(
            shading.from_view(view(shading.SOLID, color_type="SINGLE", single_color=(1, 0, 0))),
            shading.from_view(view(shading.SOLID)),
        )


class DescriptionTests(unittest.TestCase):
    def test_rendered_and_material_preview_keep_the_exported_material(self):
        for mode in (shading.RENDERED, shading.MATERIAL):
            self.assertIs(shading.shaded_description("Red", PRINCIPLED, shading.Shading(mode)), PRINCIPLED)

    def test_solid_is_matte_in_the_material_viewport_colour(self):
        solid = shading.shaded_description("Red", PRINCIPLED, shading.Shading(shading.SOLID))
        self.assertEqual(solid.base_color, (0.0, 0.0, 1.0))
        self.assertFalse(solid.is_principled, "matte: no metal, no grain")
        self.assertEqual(solid.metallic, 0.0)

    def test_solid_single_colour_and_faces_without_material(self):
        single = shading.Shading(shading.SOLID, color_type="SINGLE", single_color=(0.1, 0.2, 0.3))
        self.assertEqual(shading.shaded_description("Red", PRINCIPLED, single).base_color, (0.1, 0.2, 0.3))
        plain = shading.Shading(shading.SOLID)
        self.assertEqual(shading.shaded_description("", PRINCIPLED, plain).base_color, shading.SOLID_GREY)

    def test_solid_attribute_colours_show_in_the_attribute_mode_only(self):
        key = materials.COLOR_KEY_PREFIX + "fff000000"
        colour = materials.MaterialDescription(base_color=(1.0, 0.0, 0.0))
        attribute = shading.Shading(shading.SOLID, color_type="VERTEX")
        self.assertEqual(shading.shaded_description(key, colour, attribute).base_color, (1.0, 0.0, 0.0))
        self.assertEqual(
            shading.shaded_description(key, colour, shading.Shading(shading.SOLID)).base_color, shading.SOLID_GREY
        )

    def test_wireframe_is_one_light_matte_material(self):
        wire = shading.shaded_description("Red", PRINCIPLED, shading.Shading(shading.WIREFRAME))
        self.assertEqual(wire, shading.WIRE_MATERIAL)
        self.assertFalse(wire.is_principled)


class WireRadiusTests(unittest.TestCase):
    def test_the_radius_follows_the_scene_size_with_a_floor(self):
        self.assertAlmostEqual(shading.wire_radius((0, 0, 0), (3, 4, 0)), 5 * shading.WIRE_RADIUS_SHARE)
        self.assertEqual(shading.wire_radius((0, 0, 0), (0, 0, 0)), shading.WIRE_RADIUS_MIN)
        self.assertGreater(shading.wire_radius(None, None), 0.0)


if __name__ == "__main__":
    unittest.main()
