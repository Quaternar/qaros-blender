# SPDX-License-Identifier: MIT
"""The Blender add-on's scene sync in Blender itself (background mode): the add-on's depsgraph
handlers, SceneCapture on Blender's thread and SceneMirror on the SyncWorker thread, against the real
qaros device and the selected backend with streaming off. Run from the folder that holds anari.dll and
anari_library_qaros.dll:

    blender -b --factory-startup --python-exit-code 1 --python test_blender_addon_sync.py"""
import os
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
import math
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bpy
from mathutils import Matrix
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))
import qaros_anari  # noqa: E402
from qaros_anari import anari, materials, sync, world  # noqa: E402

FOLDER = os.getcwd()


def principled_material(name):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    return material, material.node_tree.nodes.get("Principled BSDF")


class BackendPreferenceTests(unittest.TestCase):
    """The Backend preference: every backend qaros knows, Automatic selectable, and fixed numbers,
    because Blender saves an enum preference as its number."""

    def setUp(self):
        self.items = qaros_anari.Preferences.__annotations__["backend"].keywords["items"]

    def test_lists_automatic_and_every_backend(self):
        self.assertEqual([item[0] for item in self.items], ["auto", "visrtx", "visgl", "ospray", "helide"])
        self.assertEqual(qaros_anari.Preferences.__annotations__["backend"].keywords["default"], "auto")
        # An empty identifier would make Automatic a heading that cannot be chosen again.
        self.assertTrue(all(item[0] for item in self.items))

    def test_saved_numbers_stay_with_their_backend(self):
        numbers = {item[0]: item[3] for item in self.items}
        self.assertEqual(numbers, {"auto": 0, "visrtx": 1, "visgl": 2, "helide": 3, "ospray": 4})

    def test_automatic_puts_visgl_first(self):
        self.assertEqual(self.items[0][2], "VisGL, VisRTX, OSPRay, then Helide")
        self.assertEqual(qaros_anari.AUTOMATIC_ORDER, "visgl,visrtx,ospray,helide")

    def test_visrtx_materials_default_to_physically_based_with_fixed_numbers(self):
        keywords = qaros_anari.Preferences.__annotations__["visrtx_materials"].keywords
        self.assertEqual(keywords["default"], "physical")
        self.assertEqual({item[0]: item[3] for item in keywords["items"]}, {"matte": 0, "physical": 1})


class MaterialExtractionTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.material, self.shader = principled_material("Extraction")
        self.tree = self.material.node_tree
        self.output = next(node for node in self.tree.nodes if node.type == "OUTPUT_MATERIAL")
        self.material.diffuse_color = (0.25, 0.5, 0.75, 0.5)

    def test_connected_shader_wins_over_disconnected_and_selected_nodes(self):
        self.shader.inputs["Base Color"].default_value = (1.0, 0.0, 0.0, 1.0)
        connected = self.tree.nodes.new("ShaderNodeBsdfPrincipled")
        connected.inputs["Base Color"].default_value = (0.0, 0.0, 1.0, 1.0)
        self.tree.links.new(connected.outputs["BSDF"], self.output.inputs["Surface"])
        self.tree.nodes.active = self.shader
        description = materials.material_description(self.material)
        self.assertEqual(description.base_color, (0.0, 0.0, 1.0))
        self.assertTrue(description.is_principled)
        self.assertFalse(description.warnings)

    def test_follows_reroutes_on_the_surface_connection(self):
        previous = self.shader.outputs["BSDF"]
        for _ in range(3):
            reroute = self.tree.nodes.new("NodeReroute")
            self.tree.links.new(previous, reroute.inputs[0])
            previous = reroute.outputs[0]
        self.tree.links.new(previous, self.output.inputs["Surface"])
        self.shader.inputs["Metallic"].default_value = 0.75
        self.assertEqual(materials.material_description(self.material).metallic, 0.75)

    def test_uses_the_active_all_output_and_ignores_renderer_specific_outputs(self):
        other_output = self.tree.nodes.new("ShaderNodeOutputMaterial")
        other_output.target = "CYCLES"
        other_output.is_active_output = True
        other_shader = self.tree.nodes.new("ShaderNodeBsdfPrincipled")
        other_shader.inputs["Base Color"].default_value = (1.0, 0.0, 0.0, 1.0)
        self.tree.links.new(other_shader.outputs["BSDF"], other_output.inputs["Surface"])
        self.output.is_active_output = True
        self.shader.inputs["Base Color"].default_value = (0.0, 1.0, 0.0, 1.0)
        self.assertEqual(materials.material_description(self.material).base_color, (0.0, 1.0, 0.0))
        self.output.target = "CYCLES"
        description = materials.material_description(self.material)
        self.assertFalse(description.is_principled)
        self.assertIn("Material Output for All", description.warnings[0])

    def test_unsupported_shaders_and_mixes_use_viewport_colour(self):
        for kind in ("ShaderNodeBsdfDiffuse", "ShaderNodeMixShader"):
            with self.subTest(kind=kind):
                shader = self.tree.nodes.new(kind)
                self.tree.links.new(shader.outputs[0], self.output.inputs["Surface"])
                description = materials.material_description(self.material)
                self.assertEqual(description.base_color, (0.25, 0.5, 0.75))
                self.assertEqual(description.opacity, 0.5)
                self.assertFalse(description.is_principled)
                self.assertIn("Unsupported surface shader", description.warnings[0])

    def test_shader_groups_use_viewport_colour(self):
        group_tree = bpy.data.node_groups.new("Unsupported shader group", "ShaderNodeTree")
        group_tree.interface.new_socket(name="Shader", in_out="OUTPUT", socket_type="NodeSocketShader")
        group = self.tree.nodes.new("ShaderNodeGroup")
        group.node_tree = group_tree
        self.tree.links.new(group.outputs[0], self.output.inputs["Surface"])
        description = materials.material_description(self.material)
        self.assertFalse(description.is_principled)
        self.assertEqual(description.base_color, (0.25, 0.5, 0.75))
        self.assertIn("Unsupported surface shader", description.warnings[0])

    def test_unconnected_and_muted_paths_do_not_select_an_unrelated_shader(self):
        self.shader.mute = True
        self.assertIn("muted", materials.material_description(self.material).warnings[0])
        self.shader.mute = False
        for link in list(self.output.inputs["Surface"].links):
            self.tree.links.remove(link)
        self.assertFalse(materials.material_description(self.material).is_principled)

    def test_linked_inputs_use_explicit_defaults_not_inactive_socket_values(self):
        value = self.tree.nodes.new("ShaderNodeValue")
        colour = self.tree.nodes.new("ShaderNodeRGB")
        for name in ("Alpha", "Metallic", "Roughness", "Emission Strength"):
            self.shader.inputs[name].default_value = 0.875
            self.tree.links.new(value.outputs[0], self.shader.inputs[name])
        self.shader.inputs["Base Color"].default_value = (1.0, 0.0, 0.0, 0.25)
        self.shader.inputs["Emission Color"].default_value = (4.0, 2.0, 1.0, 1.0)
        self.tree.links.new(colour.outputs[0], self.shader.inputs["Base Color"])
        description = materials.material_description(self.material)
        self.assertEqual(description.base_color, (0.25, 0.5, 0.75))
        self.assertEqual((description.opacity, description.metallic, description.roughness), (1.0, 0.0, 0.5))
        self.assertEqual(description.emissive, (0.0, 0.0, 0.0))
        self.assertEqual(len(description.warnings), 5)

    def test_emission_preserves_hdr_colour_and_combines_strength(self):
        self.shader.inputs["Emission Color"].default_value = (3.0, 1.5, 0.5, 1.0)
        self.shader.inputs["Emission Strength"].default_value = 2.0
        self.shader.inputs["Base Color"].default_value = (0.25, 0.5, 0.75, 0.125)
        description = materials.material_description(self.material)
        self.assertEqual(description.emissive, (6.0, 3.0, 1.0))
        self.assertEqual(description.opacity, 1.0, "base-colour alpha replaced Principled Alpha")
        colour = self.tree.nodes.new("ShaderNodeRGB")
        self.tree.links.new(colour.outputs[0], self.shader.inputs["Emission Color"])
        self.assertEqual(materials.material_description(self.material).emissive, (0.0, 0.0, 0.0))

    def test_absent_inputs_and_scalar_ranges_have_explicit_defaults(self):
        node = SimpleNamespace(inputs={})
        viewport = SimpleNamespace(use_nodes=False, diffuse_color=(0.25, 0.5, 0.75, 0.5))
        self.assertEqual(materials.material_description(viewport).opacity, 0.5)
        self.assertEqual(materials.material_description(None), materials.FALLBACK_MATERIAL)
        with patch.object(materials, "principled_shader", return_value=(node, None)):
            description = materials.material_description(self.material)
            self.assertEqual((description.opacity, description.metallic, description.roughness), (1.0, 0.0, 0.5))
            self.assertEqual(description.emissive, (0.0, 0.0, 0.0))
            node.inputs = {
                name: SimpleNamespace(is_linked=False, default_value=value)
                for name, value in (("Alpha", 2.0), ("Metallic", -1.0), ("Roughness", float("nan")),
                                    ("Emission Strength", -4.0))
            }
            bounded = materials.material_description(self.material)
            self.assertEqual((bounded.opacity, bounded.metallic, bounded.roughness), (1.0, 0.0, 0.5))
            self.assertEqual(bounded.emission_strength, 0.0)
            self.assertIn("Roughness is not finite", bounded.warnings[0])


class MaterialDevice:
    """Records typed writes and ownership so replacement tests can catch stale surface bindings."""

    def __init__(self, support, hdri=False):
        self.support = support
        self.hdri = hdri
        self.objects = {}
        self.events = []
        self.fail_subtype = None

    def material_support(self):
        return self.support

    def environment_support(self):
        return self.hdri

    def new(self, kind, subtype=None):
        if kind == "Material" and subtype == self.fail_subtype:
            raise anari.AnariError("Injected material creation failure")
        handle = len(self.objects) + 1
        self.objects[handle] = {"kind": kind, "subtype": subtype, "parameters": {}, "released": False}
        self.events.append(("new", handle, kind, subtype))
        return handle

    def array(self, _data_type, _count, _data=None, _item_bytes=None):
        return self.new("Array")

    def array2d(self, _data_type, width, height, data, _item_bytes):
        handle = self.new("Array2D")
        self.objects[handle]["parameters"]["size"] = (width, height, len(memoryview(data).cast("B")))
        return handle

    def set(self, handle, name, data_type, value):
        obj = self.objects[handle]
        if obj["released"]:
            raise AssertionError("Wrote through a released owner")
        if obj["kind"] == "Material":
            support = self.support if self.support.subtype == obj["subtype"] else self.support.matte
            if not support.supports(name, data_type):
                raise AssertionError(f"Unsupported material write: {obj['subtype']} {name} {data_type}")
            if name == "alphaMode" and value not in support.alpha_modes:
                raise AssertionError(f"Unadvertised alpha mode: {value}")
        obj["parameters"][name] = value
        self.events.append(("set", handle, name, data_type, value))

    def unset(self, handle, name):
        self.objects[handle]["parameters"].pop(name, None)

    def commit(self, handle):
        self.events.append(("commit", handle))

    def release(self, handle):
        obj = self.objects[handle]
        if obj["released"]:
            raise AssertionError("Released the same owner twice")
        if obj["kind"] == "Material" and any(
            surface["kind"] == "Surface" and not surface["released"]
            and surface["parameters"].get("material") == handle
            for surface in self.objects.values()
        ):
            raise AssertionError("Old material owner released before every surface was rebound")
        obj["released"] = True
        self.events.append(("release", handle))


class StreamingRendererTests(unittest.TestCase):
    """OSPRay streams with SciVis without ambient occlusion and matte materials; VisRTX keeps its
    default renderer with physically based materials, or matte ones when its preference says so;
    VisGL and Helide keep physically based materials."""

    def support(self):
        matte = anari.MaterialSupport("matte", [("color", anari.FLOAT32_VEC3)], ())
        return anari.MaterialSupport("physicallyBased", [("baseColor", anari.FLOAT32_VEC3)], (), matte=matte)

    def mirror(self, backend, visrtx_matte=False, hdri=False):
        device = MaterialDevice(self.support(), hdri)
        mirror = sync.SceneMirror(device, backend, visrtx_matte=visrtx_matte)
        mirror.configure_renderer(0.3, (0.0, 0.0, 0.0, 0.0))
        self.addCleanup(mirror.close)
        renderer = device.objects[mirror.renderer]
        return mirror, renderer

    def test_ospray_uses_scivis_without_ambient_occlusion_and_matte(self):
        mirror, renderer = self.mirror("ospray")
        self.assertEqual(renderer["subtype"], "scivis")
        self.assertEqual(renderer["parameters"]["aoSamples"], 0)
        self.assertEqual(mirror.material_support.subtype, "matte")
        self.assertEqual(mirror.material_support.fallback_reason, sync.SCIVIS_MATTE_ONLY)
        self.assertEqual(mirror.device.objects[mirror.material("Plain")]["subtype"], "matte")

    def test_visrtx_streams_physically_based_by_default(self):
        mirror, renderer = self.mirror("visrtx")
        self.assertEqual(renderer["subtype"], "default")
        self.assertEqual(renderer["parameters"]["ambientSamples"], 0)
        self.assertEqual(mirror.material_support.subtype, "physicallyBased")
        self.assertIsNone(mirror.material_support.fallback_reason)

    def test_visrtx_streams_matte_when_its_preference_says_so(self):
        mirror, renderer = self.mirror("visrtx", visrtx_matte=True)
        self.assertEqual(renderer["subtype"], "default")
        self.assertEqual(mirror.material_support.subtype, "matte")
        self.assertEqual(mirror.material_support.fallback_reason, sync.VISRTX_MATTE_ONLY)

    def test_the_visrtx_preference_leaves_other_backends_alone(self):
        for backend in ("visgl", "helide"):
            with self.subTest(backend=backend):
                mirror, _renderer = self.mirror(backend, visrtx_matte=True)
                self.assertEqual(mirror.material_support.subtype, "physicallyBased")

    def test_the_world_is_streamed_only_where_an_hdri_light_can_light_it(self):
        self.assertTrue(self.mirror("visrtx", hdri=True)[0].environment_support)
        self.assertFalse(self.mirror("visgl", hdri=False)[0].environment_support)
        # OSPRay has an hdri light, but its SciVis renderer ignores it.
        self.assertFalse(self.mirror("ospray", hdri=True)[0].environment_support)

    def test_other_backends_keep_the_default_renderer_and_physically_based(self):
        for backend in ("visgl", "helide", None):
            with self.subTest(backend=backend):
                mirror, renderer = self.mirror(backend)
                self.assertEqual(renderer["subtype"], "default")
                self.assertNotIn("aoSamples", renderer["parameters"])
                self.assertEqual(mirror.material_support.subtype, "physicallyBased")

    def test_matte_only_without_a_matte_model_keeps_what_the_backend_offers(self):
        support = anari.MaterialSupport("physicallyBased", [("baseColor", anari.FLOAT32_VEC3)], ())
        self.assertIs(sync.renderer_material_support(sync.SCIVIS_MATTE_ONLY, support), support)


def environment_world(name, image=None, color=(0.2, 0.4, 0.6, 1.0), strength=1.0, rotation=None,
                      vector_type="POINT"):
    """A World: Background (strength) fed by `image` through an Environment Texture (and a Mapping
    with `rotation`), or by the constant `color`."""
    data = bpy.data.worlds.new(name)
    data.use_nodes = True
    tree = data.node_tree
    background = next(node for node in tree.nodes if node.type == "BACKGROUND")
    background.inputs["Strength"].default_value = strength
    background.inputs["Color"].default_value = color
    if image is not None:
        texture = tree.nodes.new("ShaderNodeTexEnvironment")
        texture.image = image
        tree.links.new(texture.outputs["Color"], background.inputs["Color"])
        if rotation is not None:
            mapping = tree.nodes.new("ShaderNodeMapping")
            mapping.vector_type = vector_type
            mapping.inputs["Rotation"].default_value = rotation
            coordinates = tree.nodes.new("ShaderNodeTexCoord")
            tree.links.new(coordinates.outputs["Generated"], mapping.inputs["Vector"])
            tree.links.new(mapping.outputs["Vector"], texture.inputs["Vector"])
    return data


def float_image(name, width, height, rgba):
    image = bpy.data.images.new(name, width, height, float_buffer=True)
    pixels = np.empty((height, width, 4), dtype=np.float32)
    pixels[:, :] = rgba
    image.pixels.foreach_set(pixels.ravel())
    return image


class WorldEnvironmentTests(unittest.TestCase):
    """Blender's World as a plain environment: an equirectangular image with its strength and
    rotation, or a plain colour; unsupported setups fall back to the World's viewport colour."""

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_no_world_black_world_and_zero_strength_send_nothing(self):
        self.assertIsNone(world.world_environment(None))
        self.assertIsNone(world.world_environment(environment_world("Black", color=(0, 0, 0, 1))))
        self.assertIsNone(world.world_environment(environment_world("Off", strength=0.0)))

    def test_a_plain_colour_is_a_uniform_environment(self):
        environment = world.world_environment(environment_world("Plain", strength=2.0))
        self.assertEqual(environment.strength, 2.0)
        self.assertEqual(environment.pixels.shape, (2, 4, 3))
        np.testing.assert_allclose(environment.pixels[0, 0], (0.2, 0.4, 0.6), atol=1e-6)
        self.assertEqual(environment.direction, (1.0, 0.0, 0.0))
        self.assertEqual(environment.up, (0.0, 1.0, 0.0))

    def test_an_environment_image_keeps_its_pixels_rows_and_strength(self):
        image = float_image("Sky", 8, 4, (0.5, 1.0, 2.0, 1.0))
        image.pixels[0:4] = (9.0, 0.0, 0.0, 1.0)  # bottom-left pixel
        cache = {}
        environment = world.world_environment(environment_world("Image", image, strength=3.0), cache)
        self.assertEqual(environment.pixels.shape, (4, 8, 3))
        np.testing.assert_allclose(environment.pixels[0, 0], (9.0, 0.0, 0.0))
        np.testing.assert_allclose(environment.pixels[3, 7], (0.5, 1.0, 2.0))
        self.assertEqual(environment.strength, 3.0)
        self.assertEqual(environment.warnings, ())
        # Read once: a second capture of the same image reuses the pixels.
        again = world.world_environment(bpy.data.worlds["Image"], cache)
        self.assertIs(again.pixels, environment.pixels)
        self.assertEqual(again, environment)

    def test_a_mapping_rotation_turns_the_image(self):
        image = float_image("Turned", 8, 4, (1.0, 1.0, 1.0, 1.0))
        quarter = (0.0, 0.0, math.pi / 2)
        environment = world.world_environment(environment_world("Point", image, rotation=quarter))
        # The texture looks up R @ d, so its centre (Blender +X) shows at R^T @ X = -Y in Blender,
        # which is +Z in the app volume (Y up).
        np.testing.assert_allclose(environment.direction, (0.0, 0.0, 1.0), atol=1e-6)
        np.testing.assert_allclose(environment.up, (0.0, 1.0, 0.0), atol=1e-6)
        inverse = world.world_environment(environment_world("Texture", image, rotation=quarter, vector_type="TEXTURE"))
        np.testing.assert_allclose(inverse.direction, (0.0, 0.0, -1.0), atol=1e-6)

    def test_strength_and_rotation_changes_keep_the_same_source(self):
        image = float_image("Shared", 8, 4, (1.0, 1.0, 1.0, 1.0))
        first = world.world_environment(environment_world("A", image, strength=1.0))
        second = world.world_environment(environment_world("B", image, strength=2.0))
        self.assertEqual(first.source, second.source)
        self.assertNotEqual(first, second)

    def test_wide_images_are_averaged_down(self):
        pixels = np.arange(2 * 4 * 3, dtype=np.float32).reshape(2, 4, 3)
        small = world.downsample(pixels, max_width=2)
        self.assertEqual(small.shape, (1, 2, 3))
        np.testing.assert_allclose(small[0, 0], pixels[:2, :2].mean(axis=(0, 1)))

    def test_unsupported_setups_use_the_viewport_colour_with_a_reason(self):
        data = environment_world("Sky texture")
        data.color = (0.1, 0.2, 0.3)
        tree = data.node_tree
        sky = tree.nodes.new("ShaderNodeTexNoise")
        background = next(node for node in tree.nodes if node.type == "BACKGROUND")
        tree.links.new(sky.outputs["Color"], background.inputs["Color"])
        environment = world.world_environment(data)
        np.testing.assert_allclose(environment.pixels[0, 0], (0.1, 0.2, 0.3), atol=1e-6)
        self.assertIn("is not supported", environment.warnings[0])
        missing = environment_world("Missing image", image=bpy.data.images.new("Empty", 4, 2))
        missing.node_tree.nodes["Environment Texture"].image = None
        self.assertIn("has no image", world.world_environment(missing).warnings[0])

    def test_capture_reports_each_world_reason_once(self):
        warnings = []
        capture = sync.SceneCapture(warnings.append)
        data = environment_world("Reported")
        background = next(node for node in data.node_tree.nodes if node.type == "BACKGROUND")
        value = data.node_tree.nodes.new("ShaderNodeValue")
        data.node_tree.links.new(value.outputs[0], background.inputs["Strength"])
        scene = SimpleNamespace(world=data)
        capture.capture_environment(scene)
        capture.capture_environment(scene)
        self.assertEqual(warnings, ["World: World strength is linked; using strength 1."])


class EnvironmentLightTests(unittest.TestCase):
    """The World as an hdri light on backends with one: drawn nowhere, the studio light dropped, a
    new strength or rotation updated in place, a new image sent as a new light."""

    def setUp(self):
        matte = anari.MaterialSupport("matte", [("color", anari.FLOAT32_VEC3)], ())
        support = anari.MaterialSupport("physicallyBased", [("baseColor", anari.FLOAT32_VEC3)], (), matte=matte)
        self.device = MaterialDevice(support, hdri=True)
        self.mirror = sync.SceneMirror(self.device, "visrtx")
        self.addCleanup(self.mirror.close)

    def environment(self, source="a", strength=1.0, direction=(1.0, 0.0, 0.0)):
        pixels = np.ones((2, 4, 3), dtype=np.float32)
        return world.Environment((source,), pixels, strength, direction, (0.0, 1.0, 0.0))

    def apply(self, environment, lights=()):
        snapshot = sync.Snapshot(False)
        snapshot.lights = list(lights)
        snapshot.environment = environment
        self.mirror.apply(snapshot)

    def light(self, handle):
        return self.device.objects[handle]

    def test_the_environment_replaces_the_studio_light_and_stays_out_of_view(self):
        self.apply(self.environment(strength=2.0))
        self.assertEqual(len(self.mirror.lights), 1)
        hdri = self.light(self.mirror.lights[0])
        self.assertEqual(hdri["subtype"], "hdri")
        self.assertEqual(hdri["parameters"]["visible"], 0)
        self.assertEqual(hdri["parameters"]["scale"], 2.0)
        radiance = self.light(hdri["parameters"]["radiance"])
        self.assertEqual(radiance["parameters"]["size"], (4, 2, 2 * 4 * 3 * 4))
        self.assertTrue(radiance["released"])  # the light holds it now

    def test_lamps_and_the_environment_light_together(self):
        self.apply(self.environment(), lights=[sync.STUDIO_LIGHT])
        self.assertEqual([self.light(h)["subtype"] for h in self.mirror.lights], ["directional", "hdri"])

    def test_a_new_strength_or_rotation_updates_the_light_in_place(self):
        self.apply(self.environment())
        hdri = self.mirror.environment
        self.apply(self.environment(strength=4.0, direction=(0.0, 0.0, 1.0)))
        self.assertIs(self.mirror.environment, hdri)
        self.assertEqual(self.light(hdri)["parameters"]["scale"], 4.0)
        self.assertEqual(self.light(hdri)["parameters"]["direction"], (0.0, 0.0, 1.0))
        self.assertFalse(self.light(hdri)["released"])
        self.assertEqual(sum(1 for o in self.device.objects.values() if o["kind"] == "Array2D"), 1)

    def test_a_new_image_is_a_new_light_and_the_old_one_is_released(self):
        self.apply(self.environment("a"))
        old = self.mirror.environment
        self.apply(self.environment("b"))
        self.assertNotEqual(self.mirror.environment, old)
        self.assertTrue(self.light(old)["released"])

    def test_no_environment_brings_the_studio_light_back(self):
        self.apply(self.environment())
        old = self.mirror.environment
        self.apply(None)
        self.assertIsNone(self.mirror.environment)
        self.assertTrue(self.light(old)["released"])
        self.assertEqual([self.light(h)["subtype"] for h in self.mirror.lights], ["directional"])

    def test_a_backend_without_an_hdri_light_ignores_the_world(self):
        self.device.hdri = False
        mirror = sync.SceneMirror(self.device, "visgl")
        self.addCleanup(mirror.close)
        snapshot = sync.Snapshot(False)
        snapshot.environment = self.environment()
        mirror.apply(snapshot)
        self.assertEqual([self.light(h)["subtype"] for h in mirror.lights], ["directional"])


class VolumeCullingTests(unittest.TestCase):
    """Objects outside the app volume are not rendered, also not inside merged chunks, and come back
    when the volume reaches them. Object i is one triangle spanning x = i..i+1, y = 0..1."""

    def setUp(self):
        matte = anari.MaterialSupport("matte", [("color", anari.FLOAT32_VEC3)], ())
        support = anari.MaterialSupport("physicallyBased", [("baseColor", anari.FLOAT32_VEC3)], (), matte=matte)
        self.device = MaterialDevice(support)
        self.box = None
        self.mirror = sync.SceneMirror(self.device, None, lambda: self.box)
        self.addCleanup(self.mirror.close)

    def volume(self, centre_x, half_x):
        """qar.volumeCube for a box around (centre_x, 0.5, 0) with half extents (half_x, 1, 1)."""
        self.box = (1 / half_x, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, -centre_x / half_x, -0.5, 0, 1)

    def scene(self, count):
        snapshot = sync.Snapshot(True)
        snapshot.materials["Shared"] = materials.MaterialDescription(is_principled=True)
        part = (
            np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32),
            np.array([[0, 0, 1]] * 3, dtype=np.float32),
            np.array([[0, 1, 2]], dtype=np.uint32), "Shared",
        )
        snapshot.meshes = {f"Object {index}": [part] for index in range(count)}
        for index in range(count):
            transform = np.eye(4, dtype=np.float32)
            transform[3, 0] = index
            snapshot.instances.append((f"Object {index}", transform.flatten().tolist()))
        self.mirror.refresh_volume()
        self.mirror.apply(snapshot)

    def shown(self):
        names = {name for name, _occurrence in self.mirror.instance_slots}
        for chunk in self.mirror.chunks:
            names |= {name for name, _part in chunk.members}
        return names

    def test_objects_outside_the_volume_are_not_rendered(self):
        self.volume(5, 2.9)  # x 2.1..7.9: objects 2..7
        self.scene(sync.BATCH_MIN_OBJECTS - 4)
        self.assertEqual(self.shown(), {f"Object {index}" for index in range(2, 8)})
        self.assertEqual(self.mirror.culled, sync.BATCH_MIN_OBJECTS - 4 - 6)
        self.assertEqual(len(self.mirror.instances), 6)

    def test_moving_the_volume_brings_culled_objects_back(self):
        self.volume(5, 2.9)
        self.scene(sync.BATCH_MIN_OBJECTS - 2)
        self.volume(10, 0.9)  # x 9.1..10.9: objects 9 and 10
        self.assertTrue(self.mirror.refresh_volume())
        self.assertEqual(self.shown(), {"Object 9", "Object 10"})
        self.assertFalse(self.mirror.refresh_volume(), "an unchanged volume places nothing again")

    def test_merged_chunks_hold_only_objects_inside_the_volume(self):
        count = 3 * sync.BATCH_MIN_OBJECTS
        self.volume(12, 11.5)  # x 0.5..23.5: objects 0..23
        self.scene(count)
        inside = {f"Object {index}" for index in range(24)}
        self.assertTrue(self.mirror.chunks, "enough visible objects of one material merge")
        self.assertEqual(self.shown(), inside)
        merged = {name for chunk in self.mirror.chunks for name, _part in chunk.members}
        self.assertLessEqual(merged, inside)

    def test_without_a_volume_everything_is_rendered(self):
        self.scene(sync.BATCH_MIN_OBJECTS - 4)
        self.assertEqual(self.mirror.culled, 0)
        self.assertEqual(len(self.mirror.instances), sync.BATCH_MIN_OBJECTS - 4)


class LightConversionTests(unittest.TestCase):
    """Blender lights in the units Cycles renders them with (measured against Cycles: a 0.5 white
    plane under each light, Standard view transform; OSPRay and VisRTX matched within 7 %)."""

    def light(self, light_type, energy, **settings):
        data = bpy.data.lights.new(f"Test {light_type}", light_type)
        data.energy = energy
        for name, value in settings.items():
            setattr(data, name, value)
        self.addCleanup(bpy.data.lights.remove, data)
        return data

    @staticmethod
    def values(converted):
        return {name: value for name, _type, value in converted[1]}

    def assert_close(self, actual, expected):
        self.assertAlmostEqual(actual, expected, places=5)

    def test_a_sun_is_irradiance_scaled_by_exposure(self):
        converted = sync.light_parameters(self.light("SUN", 2.0, exposure=1.0), Matrix())
        self.assertEqual(converted[0], "directional")
        self.assert_close(self.values(converted)["irradiance"], 4.0)
        np.testing.assert_allclose(self.values(converted)["direction"], (0.0, -1.0, 0.0), atol=1e-6)

    def test_an_unnormalized_sun_is_scaled_by_its_disk(self):
        converted = sync.light_parameters(self.light("SUN", 100.0, angle=0.2, normalize=False), Matrix())
        self.assert_close(self.values(converted)["irradiance"], 100.0 * math.pi * 0.1 ** 2)

    def test_a_point_light_spreads_its_power_over_the_sphere(self):
        converted = sync.light_parameters(self.light("POINT", 100.0, shadow_soft_size=0.5), Matrix())
        self.assertEqual(converted[0], "point")
        self.assert_close(self.values(converted)["intensity"], 100.0 / (4.0 * math.pi))
        self.assertEqual(self.values(converted)["radius"], 0.0)

    def test_an_unnormalized_point_light_is_scaled_by_its_surface_not_the_object_scale(self):
        light = self.light("POINT", 100.0, shadow_soft_size=0.5, normalize=False)
        scaled = Matrix.Diagonal((0.01, 0.01, 0.01, 1.0))
        for matrix in (Matrix(), scaled):
            self.assert_close(self.values(sync.light_parameters(light, matrix))["intensity"], 100.0 * 0.25)

    def test_a_spot_keeps_its_cone(self):
        converted = sync.light_parameters(self.light("SPOT", 100.0, spot_size=1.0, spot_blend=0.4), Matrix())
        values = self.values(converted)
        self.assertEqual(converted[0], "spot")
        self.assert_close(values["intensity"], 100.0 / (4.0 * math.pi))
        self.assert_close(values["openingAngle"], 1.0)
        self.assert_close(values["falloffAngle"], 0.2)

    def test_an_area_light_is_a_lambertian_hemisphere_along_its_axis(self):
        converted = sync.light_parameters(self.light("AREA", 100.0, size=0.05), Matrix())
        values = self.values(converted)
        self.assertEqual(converted[0], "spot")
        self.assert_close(values["intensity"], 100.0 / math.pi)
        self.assert_close(values["openingAngle"], math.pi)
        np.testing.assert_allclose(values["direction"], (0.0, -1.0, 0.0), atol=1e-6)

    def test_an_unnormalized_area_light_is_scaled_by_its_world_area(self):
        doubled = Matrix.Diagonal((2.0, 2.0, 2.0, 1.0))
        for shape, area in (("RECTANGLE", 0.5 * 2.0 * 0.2 * 2.0), ("DISK", math.pi / 4.0 * (0.5 * 2.0) ** 2),
                            ("SQUARE", (0.5 * 2.0) ** 2)):
            with self.subTest(shape=shape):
                light = self.light("AREA", 100.0, shape=shape, size=0.5, size_y=0.2, normalize=False)
                self.assert_close(self.values(sync.light_parameters(light, doubled))["intensity"], 100.0 * area / math.pi)


class SceneBoundsTests(unittest.TestCase):
    """What "Fit scene to volume" sends as qar.fitBox: visible geometry in the device's Y-up space."""

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=False)

    def bounds(self):
        bpy.context.view_layer.update()
        return sync.scene_bounds(bpy.context.evaluated_depsgraph_get())

    def test_the_factory_cube_in_y_up_space(self):
        cube = bpy.data.objects["Cube"]
        cube.location = (0.0, 5.0, 2.0)  # Blender y is the device's -z, Blender z its y
        lower, upper = self.bounds()
        np.testing.assert_allclose(lower, (-1.0, 1.0, -6.0), atol=1e-5)
        np.testing.assert_allclose(upper, (1.0, 3.0, -4.0), atol=1e-5)

    def test_hidden_objects_lights_and_cameras_do_not_count(self):
        bpy.ops.mesh.primitive_cube_add(location=(100.0, 0.0, 0.0))
        bpy.context.active_object.hide_set(True)
        np.testing.assert_allclose(self.bounds()[1], (1.0, 1.0, 1.0), atol=1e-5)
        bpy.data.objects.remove(bpy.data.objects["Cube"])
        self.assertIsNone(self.bounds())


class MaterialExportTests(unittest.TestCase):
    def setUp(self):
        matte = anari.MaterialSupport("matte", [
            ("color", anari.FLOAT32_VEC3), ("opacity", anari.FLOAT32), ("alphaMode", anari.STRING),
        ], ("opaque", "blend"))
        self.support = anari.MaterialSupport("physicallyBased", [
            ("baseColor", anari.FLOAT32_VEC3), ("opacity", anari.FLOAT32),
            ("alphaMode", anari.STRING),
            ("metallic", anari.FLOAT32), ("roughness", anari.FLOAT32), ("emissive", anari.FLOAT32_VEC3),
        ], ("opaque", "blend"), matte=matte)
        self.device = MaterialDevice(self.support)
        self.mirror = sync.SceneMirror(self.device)

    def tearDown(self):
        self.mirror.close()

    def scene(self, description):
        snapshot = sync.Snapshot(True)
        snapshot.materials["Shared"] = description
        part = (
            np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32),
            np.array([[0, 0, 1]] * 3, dtype=np.float32),
            np.array([[0, 1, 2]], dtype=np.uint32), "Shared",
        )
        snapshot.meshes = {"First": [part], "Second": [part]}
        identity = np.eye(4, dtype=np.float32).flatten().tolist()
        snapshot.instances = [(name, identity) for name in snapshot.meshes]
        self.mirror.apply(snapshot)

    def test_corners_weld_per_vertex_and_normal(self):
        # Two triangles over a quad's four vertices, six corners: 0 1 2 and 2 1 3.
        corner_vertices = np.array([0, 1, 2, 2, 1, 3], dtype=np.int32)
        triangles = np.array([[0, 1, 2], [3, 4, 5]], dtype=np.uint32)
        smooth = np.array([[0, 0, 1]] * 6, dtype=np.float32)
        used, remapped = sync.weld_corners(triangles, corner_vertices, smooth)
        self.assertEqual(len(used), 4)
        np.testing.assert_array_equal(corner_vertices[used][remapped], corner_vertices[triangles])
        sharp = np.array([[0, 0, 1]] * 3 + [[0, 1, 0]] * 3, dtype=np.float32)
        used, remapped = sync.weld_corners(triangles, corner_vertices, sharp)
        self.assertEqual(len(used), 6)
        np.testing.assert_array_equal(sharp[used][remapped], sharp[triangles])
        # Flat faces carry no normals: corners weld per vertex whatever their normals were.
        used, remapped = sync.weld_corners(triangles, corner_vertices)
        self.assertEqual(len(used), 4)
        np.testing.assert_array_equal(corner_vertices[used][remapped], corner_vertices[triangles])

    def multi_part_scene(self, count, moved=None, odd_one=False):
        """`count` objects with a smooth 'Body' part and a flat 'Cap' part; with `odd_one`, one more
        object whose second part uses a material nobody else has."""
        snapshot = sync.Snapshot(moved is None)
        positions = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
        triangle = np.array([[0, 1, 2]], dtype=np.uint32)
        smooth_normals = np.array([[0, 0, 1]] * 3, dtype=np.float32)
        names = [f"Mixed {i}" for i in range(count)] + (["Odd"] if odd_one else [])
        if moved is None:
            for key in ("Body", "Cap", "Rare"):
                snapshot.materials[key] = materials.MaterialDescription(is_principled=True)
            for name in names:
                snapshot.meshes[name] = [
                    (positions, smooth_normals, triangle, "Body"),
                    (positions, None, triangle, "Rare" if name == "Odd" else "Cap"),
                ]
        for index, name in enumerate(names):
            transform = np.eye(4, dtype=np.float32)
            transform[3, 0] = index
            if name == moved:
                transform[3, 1] = 1.0
            snapshot.instances.append((name, transform.flatten().tolist()))
        self.mirror.apply(snapshot)

    def test_objects_with_several_parts_merge_every_part(self):
        count = sync.BATCH_MIN_OBJECTS
        self.multi_part_scene(count, odd_one=True)
        self.assertEqual(sorted(chunk.key for chunk in self.mirror.chunks), [("Body", True), ("Cap", False)])
        self.assertTrue(all(len(chunk.members) == count for chunk in self.mirror.chunks))
        # The odd object's 'Rare' part has no company, so the whole object stays standalone.
        self.assertEqual(set(self.mirror.groups), {"Odd"})
        self.assertEqual(len(self.mirror.instances), 3)
        self.assertEqual(self.mirror.objects, count + 1)
        self.assertEqual(self.mirror.triangles, 2 * (count + 1))
        self.assertEqual(len(self.mirror.batched["Mixed 0"][0]), 2)

    def test_a_moved_multi_part_object_leaves_every_chunk(self):
        count = sync.BATCH_MIN_OBJECTS + 1
        self.multi_part_scene(count)
        before = {chunk.key: chunk.entry[0] for chunk in self.mirror.chunks}
        self.multi_part_scene(count, moved="Mixed 2")
        self.assertIn("Mixed 2", self.mirror.groups)
        for chunk in self.mirror.chunks:
            self.assertNotIn("Mixed 2", [name for name, _index in chunk.members])
            self.assertNotEqual(chunk.entry[0], before[chunk.key])
        self.assertEqual(len(self.mirror.instances), 3)
        self.assertEqual(self.mirror.triangles, 2 * count)

    def test_flat_and_smooth_objects_use_separate_chunks(self):
        snapshot = sync.Snapshot(True)
        snapshot.materials["Shared"] = materials.MaterialDescription(is_principled=True)
        positions = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
        triangle = np.array([[0, 1, 2]], dtype=np.uint32)
        smooth = (positions, np.array([[0, 0, 1]] * 3, dtype=np.float32), triangle, "Shared")
        flat = (positions, None, triangle, "Shared")
        count = sync.BATCH_MIN_OBJECTS
        snapshot.meshes = {f"Smooth {i}": [smooth] for i in range(count)}
        snapshot.meshes.update({f"Flat {i}": [flat] for i in range(count)})
        identity = np.eye(4, dtype=np.float32).flatten().tolist()
        snapshot.instances = [(name, identity) for name in snapshot.meshes]
        self.mirror.apply(snapshot)
        self.assertEqual(sorted(chunk.key for chunk in self.mirror.chunks), [("Shared", False), ("Shared", True)])
        flat_chunk = next(chunk for chunk in self.mirror.chunks if not chunk.key[1])
        geometry = next(h for h in flat_chunk.entry[1] if self.device.objects[h]["kind"] == "Geometry")
        self.assertNotIn("vertex.normal", self.device.objects[geometry]["parameters"])

    def many(self, count, description=None, moved=None):
        """`count` one-triangle objects sharing one material; `moved` is lifted by one unit."""
        snapshot = sync.Snapshot(moved is None)
        if moved is None:
            snapshot.materials["Shared"] = description or materials.MaterialDescription(is_principled=True)
            part = (
                np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32),
                np.array([[0, 0, 1]] * 3, dtype=np.float32),
                np.array([[0, 1, 2]], dtype=np.uint32), "Shared",
            )
            snapshot.meshes = {f"Object {index}": [part] for index in range(count)}
        for index in range(count):
            transform = np.eye(4, dtype=np.float32)
            transform[3, 0] = index
            if f"Object {index}" == moved:
                transform[3, 1] = 1.0
            snapshot.instances.append((f"Object {index}", transform.flatten().tolist()))
        self.mirror.apply(snapshot)

    def live(self, kind):
        return [h for h, obj in self.device.objects.items() if obj["kind"] == kind and not obj["released"]]

    def test_static_objects_of_one_material_share_chunks(self):
        self.many(sync.BATCH_MAX_OBJECTS + 4)
        self.assertEqual(len(self.mirror.chunks), 2)
        self.assertEqual(len(self.mirror.instances), 2)
        self.assertEqual(self.mirror.groups, {})
        self.assertEqual(self.mirror.objects, sync.BATCH_MAX_OBJECTS + 4)
        self.assertEqual(self.mirror.triangles, sync.BATCH_MAX_OBJECTS + 4)
        self.assertEqual(len(self.live("Surface")), 2)

    def test_few_objects_stay_standalone(self):
        self.many(sync.BATCH_MIN_OBJECTS - 1)
        self.assertEqual(self.mirror.chunks, [])
        self.assertEqual(len(self.mirror.instances), sync.BATCH_MIN_OBJECTS - 1)

    def test_a_moved_object_leaves_its_chunk_and_bakes_the_rest(self):
        count = sync.BATCH_MIN_OBJECTS + 2
        self.many(count)
        chunk_group = self.mirror.chunks[0].entry[0]
        self.many(count, moved="Object 3")
        self.assertIn("Object 3", self.mirror.groups)
        self.assertNotIn("Object 3", self.mirror.batched)
        self.assertTrue(self.device.objects[chunk_group]["released"])
        self.assertEqual(len(self.mirror.instances), 2)
        self.assertEqual(self.mirror.objects, count)
        # It stays standalone, moving in place, instead of rebuilding the chunk on every move.
        standalone = self.mirror.groups["Object 3"]
        rebuilt = self.mirror.chunks[0].entry[0]
        self.mirror.apply(self.moved_snapshot(count, "Object 3", 2.0))
        self.assertEqual(self.mirror.groups["Object 3"], standalone)
        self.assertEqual(self.mirror.chunks[0].entry[0], rebuilt)

    def test_a_still_object_rejoins_its_chunk_after_settling(self):
        count = sync.BATCH_MIN_OBJECTS + 2
        self.many(count)
        self.many(count, moved="Object 3")
        moved_at = self.mirror.dynamic["Object 3"]
        self.assertEqual(self.mirror.settle_deadline(), moved_at + sync.SETTLE_SECONDS)
        # Moving again restarts the wait; before it ends nothing changes.
        self.mirror.apply(self.moved_snapshot(count, "Object 3", 2.0))
        restarted = self.mirror.dynamic["Object 3"]
        self.assertGreaterEqual(restarted, moved_at)
        self.assertFalse(self.mirror.settle(restarted + sync.SETTLE_SECONDS / 2))
        self.assertIn("Object 3", self.mirror.groups)
        standalone = self.mirror.groups["Object 3"][0]
        self.assertTrue(self.mirror.settle(restarted + sync.SETTLE_SECONDS))
        self.assertIn("Object 3", self.mirror.batched)
        self.assertNotIn("Object 3", self.mirror.groups)
        self.assertTrue(self.device.objects[standalone]["released"])
        self.assertEqual(self.mirror.batched["Object 3"][1][13], 2.0)  # baked at its last height
        self.assertEqual(len(self.mirror.instances), 1)
        self.assertEqual(self.mirror.objects, count)
        self.assertIsNone(self.mirror.settle_deadline())

    def test_worker_settles_without_a_snapshot(self):
        settled = threading.Event()

        class SettlingMirror:
            def __init__(self):
                self.deadline = time.monotonic() + 0.05

            def settle_deadline(self):
                return self.deadline

            def refresh_volume(self):
                return False

            def settle(self):
                self.deadline = None
                settled.set()

        errors = []
        worker = sync.SyncWorker(SettlingMirror(), errors.append)
        try:
            self.assertTrue(settled.wait(5), "the worker did not settle on its own")
        finally:
            worker.close()
        self.assertEqual(errors, [])

    def moved_snapshot(self, count, name, height):
        snapshot = sync.Snapshot(False)
        for index in range(count):
            transform = np.eye(4, dtype=np.float32)
            transform[3, 0] = index
            if f"Object {index}" == name:
                transform[3, 1] = height
            snapshot.instances.append((f"Object {index}", transform.flatten().tolist()))
        return snapshot

    def test_chunk_bakes_each_objects_transform(self):
        uploads = []
        array = self.device.array

        def record(data_type, count, data=None, item_bytes=None):
            uploads.append((data_type, data))
            return array(data_type, count, data, item_bytes)

        with patch.object(self.device, "array", side_effect=record):
            self.many(sync.BATCH_MIN_OBJECTS)
        vectors = [data for data_type, data in uploads if data_type == anari.FLOAT32_VEC3]
        positions, normals = vectors
        self.assertEqual(positions.shape, (3 * sync.BATCH_MIN_OBJECTS, 3))
        # Object i is translated by i along x; its triangle keeps its own corners and order.
        np.testing.assert_allclose(positions[0::3, 0], np.arange(sync.BATCH_MIN_OBJECTS))
        np.testing.assert_allclose(positions[1::3, 0], np.arange(sync.BATCH_MIN_OBJECTS) + 1)
        np.testing.assert_allclose(normals, np.tile([0, 0, 1], (3 * sync.BATCH_MIN_OBJECTS, 1)))
        indices = next(data for data_type, data in uploads if data_type == anari.UINT32_VEC3)
        np.testing.assert_array_equal(indices[5], [15, 16, 17])

    def test_material_edits_keep_chunk_geometry_and_rebind_on_subtype_change(self):
        self.many(sync.BATCH_MIN_OBJECTS)
        entry = self.mirror.chunks[0].entry
        self.mirror.update_materials({"Shared": materials.MaterialDescription(metallic=0.5, is_principled=True)})
        self.assertIs(self.mirror.chunks[0].entry, entry)
        self.mirror.update_materials({"Shared": materials.MaterialDescription(base_color=(0.0, 1.0, 0.0))})
        matte = self.mirror.materials["Shared"]
        surface = next(h for h in entry[1] if self.device.objects[h]["kind"] == "Surface")
        self.assertEqual(self.device.objects[surface]["parameters"]["material"], matte)

    def test_exports_all_scoped_parameters_and_clears_previous_emission(self):
        description = materials.MaterialDescription(
            (0.25, 0.5, 0.75), 0.5, 0.75, 0.125, (2.0, 1.0, 0.5), 3.0, is_principled=True
        )
        self.scene(description)
        handle = self.mirror.materials["Shared"]
        self.assertEqual(self.device.objects[handle]["parameters"], {
            "baseColor": (0.25, 0.5, 0.75), "opacity": 0.5, "alphaMode": "blend", "metallic": 0.75,
            "roughness": 0.125, "emissive": (6.0, 3.0, 1.5),
        })
        groups = self.mirror.groups.copy()
        self.mirror.update_materials({"Shared": replace(description, emission_strength=0.0, metallic=0.0)})
        self.assertEqual(self.mirror.materials["Shared"], handle)
        self.assertEqual(self.mirror.groups, groups)
        self.assertEqual(self.device.objects[handle]["parameters"]["emissive"], (0.0, 0.0, 0.0))
        self.assertEqual(self.device.objects[handle]["parameters"]["metallic"], 0.0)

    def test_subtype_changes_rebind_every_shared_surface_before_releasing_old_material(self):
        description = materials.MaterialDescription(metallic=0.75, is_principled=True)
        self.scene(description)
        old = self.mirror.materials["Shared"]
        groups = self.mirror.groups.copy()
        self.mirror.update_materials({"Shared": materials.MaterialDescription(base_color=(0.0, 1.0, 0.0))})
        matte = self.mirror.materials["Shared"]
        self.assertNotEqual(matte, old)
        self.assertEqual(self.device.objects[matte]["subtype"], "matte")
        self.assertEqual(len(self.mirror.surface_materials), 2)
        for surface in self.mirror.surface_materials:
            self.assertEqual(self.device.objects[surface]["parameters"]["material"], matte)
            self.assertLess(self.device.events.index(("commit", matte)),
                            self.device.events.index(("set", surface, "material", anari.MATERIAL, matte)))
        self.assertEqual(self.device.events.count(("release", old)), 1)
        self.mirror.update_materials({"Shared": description})
        self.assertEqual(self.mirror.groups, groups)
        self.assertEqual(self.device.objects[self.mirror.materials["Shared"]]["subtype"], "physicallyBased")
        self.assertEqual(self.device.events.count(("release", matte)), 1)

    def test_opacity_transitions_reset_modes_without_replacing_material_or_geometry(self):
        self.scene(materials.MaterialDescription(is_principled=True))
        groups = self.mirror.groups.copy()
        for principled in (True, False):
            original = None
            for opacity, mode in ((1.0, "opaque"), (0.5, "blend"), (0.0, "blend"), (1.0, "opaque")):
                with self.subTest(principled=principled, opacity=opacity):
                    self.mirror.update_materials({"Shared": materials.MaterialDescription(
                        opacity=opacity, is_principled=principled,
                    )})
                    handle = self.mirror.materials["Shared"]
                    if original is None:
                        original = handle
                    self.assertEqual(handle, original)
                    self.assertEqual(self.mirror.groups, groups)
                    self.assertEqual(self.device.objects[handle]["parameters"]["opacity"], opacity)
                    self.assertEqual(self.device.objects[handle]["parameters"]["alphaMode"], mode)

    def test_incomplete_blend_metadata_uses_opaque_approximation_and_clears_diagnostic(self):
        complete = self.support.matte
        cases = [
            anari.MaterialSupport("matte", complete.parameters - {("opacity", anari.FLOAT32)}, complete.alpha_modes),
            anari.MaterialSupport("matte", complete.parameters - {("alphaMode", anari.STRING)}, complete.alpha_modes),
            anari.MaterialSupport("matte", complete.parameters, ()),
            anari.MaterialSupport("matte", complete.parameters, ("opaque", "mask")),
        ]
        for support in cases:
            with self.subTest(parameters=support.parameters, modes=support.alpha_modes):
                messages = []
                diagnostics = materials.MaterialDiagnostics(messages.append)
                for opacity in (0.5, 0.0, 0.5):
                    description = materials.MaterialDescription(opacity=opacity)
                    chosen, parameters, warnings = materials.material_export(description, support)
                    values = {name: value for name, _type, value in parameters}
                    self.assertIs(chosen, support)
                    self.assertEqual(values.get("opacity", 1.0), 1.0)
                    self.assertNotIn(values.get("alphaMode"), ("blend", "mask"))
                    self.assertEqual(len(warnings), 1)
                    diagnostics.update({"Shared": description}, {"Shared"}, support)
                self.assertEqual(len(messages), 1)
                self.assertIn("opaque approximation", messages[0])
                diagnostics.update({"Shared": materials.MaterialDescription()}, {"Shared"}, support)
                self.assertFalse(diagnostics.active)

    def test_blend_capability_belongs_to_the_selected_material_model(self):
        self.support.matte = anari.MaterialSupport("matte", self.support.matte.parameters, ("opaque",))
        for principled, expected in ((True, 0.5), (False, 1.0)):
            chosen, parameters, warnings = materials.material_export(
                materials.MaterialDescription(opacity=0.5, is_principled=principled), self.support
            )
            values = {name: value for name, _type, value in parameters}
            self.assertEqual(values["opacity"], expected)
            self.assertEqual(values["alphaMode"], "blend" if principled else "opaque")
            self.assertEqual(bool(warnings), not principled)

    def test_blend_only_backend_restores_full_opacity_without_unadvertised_modes(self):
        support = anari.MaterialSupport("matte", self.support.matte.parameters, ("blend",))
        for opacity in (0.5, 1.0):
            _chosen, parameters, warnings = materials.material_export(materials.MaterialDescription(opacity=opacity), support)
            values = {name: value for name, _type, value in parameters}
            self.assertEqual(values["opacity"], opacity)
            self.assertEqual(values["alphaMode"], "blend")
            self.assertFalse(warnings)

    def test_failed_replacement_keeps_the_previous_owner_and_surface_bindings(self):
        self.scene(materials.MaterialDescription(is_principled=True))
        old = self.mirror.materials["Shared"]
        self.device.fail_subtype = "matte"
        with self.assertRaisesRegex(anari.AnariError, "creation failure"):
            self.mirror.update_materials({"Shared": materials.MaterialDescription()})
        self.assertEqual(self.mirror.materials["Shared"], old)
        self.assertFalse(self.device.objects[old]["released"])
        self.assertTrue(all(self.device.objects[s]["parameters"]["material"] == old
                            for s in self.mirror.surface_materials))
        self.device.fail_subtype = None
        self.mirror.update_materials({"Shared": materials.MaterialDescription()})
        self.assertEqual(self.mirror.material_models["Shared"].subtype, "matte")

    def test_reduced_backends_keep_supported_parameters_and_report_degradation(self):
        self.mirror.close()
        support = anari.MaterialSupport("physicallyBased", [
            ("baseColor", anari.FLOAT32_VEC3), ("metallic", anari.STRING),
            ("roughness", anari.FLOAT32), ("emissive", anari.FLOAT32_VEC3),
        ], ())
        self.device = MaterialDevice(support)
        self.mirror = sync.SceneMirror(self.device)
        description = materials.MaterialDescription(metallic=0.75, roughness=0.25, is_principled=True)
        self.scene(description)
        handle = self.mirror.materials["Shared"]
        self.assertNotIn("metallic", self.device.objects[handle]["parameters"])
        self.assertEqual(self.device.objects[handle]["parameters"]["roughness"], 0.25)
        warnings = materials.material_export(description, support)[2]
        self.assertTrue(any("cannot set metallic" in warning for warning in warnings))

    def test_viewport_fallback_is_non_metallic_when_only_physically_based_is_available(self):
        self.support.matte = None
        self.scene(materials.MaterialDescription(base_color=(0.25, 0.5, 0.75)))
        handle = self.mirror.materials["Shared"]
        obj = self.device.objects[handle]
        self.assertEqual(obj["subtype"], "physicallyBased")
        self.assertEqual(obj["parameters"]["metallic"], 0.0)
        self.assertEqual(obj["parameters"]["roughness"], 0.5)
        self.assertEqual(obj["parameters"]["emissive"], (0.0, 0.0, 0.0))

    def test_replacement_does_not_rebind_a_removed_objects_surfaces(self):
        self.scene(materials.MaterialDescription(is_principled=True))
        snapshot = sync.Snapshot(False)
        snapshot.instances = [("First", np.eye(4, dtype=np.float32).flatten().tolist())]
        self.mirror.apply(snapshot)
        self.assertEqual(len(self.mirror.surface_materials), 1)
        self.mirror.update_materials({"Shared": materials.MaterialDescription()})
        self.assertEqual(self.mirror.material_models["Shared"].subtype, "matte")

    def test_matte_fallback_keeps_colour_and_opacity_and_deduplicates_diagnostics(self):
        support = self.support.matte
        description = materials.MaterialDescription(
            opacity=0.5, metallic=0.75, roughness=0.125, emission_color=(2.0, 0.0, 0.0), is_principled=True
        )
        chosen, parameters, warnings = materials.material_export(description, support)
        self.assertIs(chosen, support)
        self.assertEqual({name for name, _data_type, _value in parameters}, {"color", "opacity", "alphaMode"})
        self.assertEqual(len(warnings), 3)
        messages = []
        diagnostics = materials.MaterialDiagnostics(messages.append)
        for _ in range(3):
            diagnostics.update({"Shared": description}, {"Shared"}, support)
        self.assertEqual(len(messages), 3)
        diagnostics.update({"Shared": materials.MaterialDescription(is_principled=True)}, {"Shared"}, support)
        self.assertFalse(diagnostics.active)


class SceneSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = []
        cls.library = anari.Library(FOLDER, "qaros", lambda severity, text: cls.messages.append((severity, text)))

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=False)
        self.message_start = len(self.messages)
        device = self.library.new_device()
        device.set(device.handle, "qar.stream", anari.BOOL, 0)
        device.commit()
        # The add-on's own Streamer and handlers, with a device that does not join QAROS.
        streamer = qaros_anari.STREAMER
        streamer.device = device
        streamer.mirror = sync.SceneMirror(device)
        streamer.mirror.configure_renderer(0.3, (0.0, 0.0, 0.0, 0.0))
        streamer.capture = sync.SceneCapture(material_support=streamer.mirror.material_support)
        streamer.scene_name = bpy.context.scene.name
        streamer.mirror.apply(streamer.capture.capture(bpy.context.evaluated_depsgraph_get(), (), True))
        self.failures = []
        streamer.worker = sync.SyncWorker(streamer.mirror, self.failures.append)
        self.streamer = streamer
        qaros_anari.add_handlers()

    def tearDown(self):
        qaros_anari.remove_handlers()
        self.streamer.release()
        self.assertEqual([text for severity, text in self.messages[self.message_start:]
                          if severity in ("fatal", "error")], [])

    @property
    def mirror(self):
        return self.streamer.mirror

    def settle(self):
        """Let the depsgraph handlers run, then wait for the worker to apply what they sent."""
        bpy.context.view_layer.update()
        self.assertTrue(self.streamer.worker.wait_idle(30), "the sync worker did not finish")
        self.assertEqual(self.failures, [])

    def group(self, name):
        return self.mirror.groups[name][0]

    @contextmanager
    def reference_frame(self, size):
        device = self.streamer.device
        camera = device.new("Camera", "perspective")
        frame = None
        try:
            device.set(camera, "position", anari.FLOAT32_VEC3, (0.0, 0.0, 4.0))
            device.set(camera, "direction", anari.FLOAT32_VEC3, (0.0, 0.0, -1.0))
            device.set(camera, "up", anari.FLOAT32_VEC3, (0.0, 1.0, 0.0))
            device.commit(camera)
            frame = device.new("Frame")
            device.set(frame, "world", anari.WORLD, self.mirror.world)
            device.set(frame, "renderer", anari.RENDERER, self.mirror.renderer)
            device.set(frame, "camera", anari.CAMERA, camera)
            device.set(frame, "size", anari.UINT32_VEC2, (size, size))
            device.set(frame, "channel.color", anari.DATA_TYPE, anari.UFIXED8_RGBA_SRGB)
            device.set(frame, "channel.depth", anari.DATA_TYPE, anari.FLOAT32)
            device.commit(frame)
            yield frame
        finally:
            device.release(frame)
            device.release(camera)

    def render_colour(self, frame, size):
        device = self.streamer.device
        self.assertTrue(device.render(frame))
        width, height, rgba = device.frame_color(frame)
        self.assertEqual((width, height), (size, size))
        return np.frombuffer(rgba, dtype=np.uint8).reshape(height, width, 4)

    def test_queued_edits_update_shared_materials_and_keep_all_geometry(self):
        cube = bpy.data.objects["Cube"]
        first, first_node = principled_material("Queued first")
        second, second_node = principled_material("Queued second")
        cube.data.materials.clear()
        cube.data.materials.append(first)
        cube.data.materials.append(second)
        for index, polygon in enumerate(cube.data.polygons):
            polygon.material_index = index % 2
        copy = bpy.data.objects.new("Shared copy", cube.data)
        bpy.context.scene.collection.objects.link(copy)
        copy.location.x = 3.0
        self.settle()
        groups = self.mirror.groups.copy()
        handles = {key: self.mirror.materials[key] for key in (first.name, second.name)}
        self.assertEqual(list(self.mirror.surface_materials.values()).count(first.name), 2)
        self.assertEqual(list(self.mirror.surface_materials.values()).count(second.name), 2)
        entered, resume = threading.Event(), threading.Event()
        applied = []
        apply = self.mirror.apply

        def blocked(snapshot):
            entered.set()
            if not resume.wait(10):
                raise TimeoutError("test did not release the blocked worker")
            apply(snapshot)
            applied.append(snapshot)

        device = self.streamer.device
        with patch.object(self.mirror, "apply", side_effect=blocked), patch.object(device, "new", wraps=device.new) as new:
            try:
                cube.location.y += 0.25
                bpy.context.view_layer.update()
                self.assertTrue(entered.wait(10), "worker did not take the first snapshot")
                for index in range(16):
                    first_node.inputs["Base Color"].default_value = (index / 16, 0.25, 0.5, 1.0)
                    first_node.inputs["Metallic"].default_value = index / 16
                    first_node.inputs["Roughness"].default_value = 1.0 - index / 16
                    if index == 0:
                        second_node.inputs["Base Color"].default_value = (0.25, 0.5, 0.75, 1.0)
                    bpy.context.view_layer.update()
                expected = materials.material_description(first)
                self.assertFalse(resume.is_set(), "edits waited for the worker")
            finally:
                resume.set()
                self.assertTrue(self.streamer.worker.wait_idle(30))
        self.assertEqual(self.failures, [])
        self.assertEqual(len(applied), 2, "pending UI edits were not coalesced")
        self.assertEqual(applied[-1].materials[first.name], expected)
        self.assertEqual(self.mirror.material_descriptions[first.name], expected)
        self.assertEqual(self.mirror.material_descriptions[second.name].base_color, (0.25, 0.5, 0.75))
        self.assertEqual(self.mirror.groups, groups)
        self.assertEqual({key: self.mirror.materials[key] for key in handles}, handles)
        self.assertFalse(any(call.args[0] in ("Geometry", "Group") for call in new.call_args_list))

    def test_full_resync_rebuilds_surface_bindings_for_replacement_mesh(self):
        old_material, _old_node = principled_material("Old scene")
        bpy.data.objects["Cube"].data.materials[0] = old_material
        self.settle()
        old_mesh = bpy.data.objects["Cube"].data
        replacement_mesh = bpy.data.meshes.new("Replacement mesh")
        replacement_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 0, 1)], [], [(0, 1, 2)])
        replacement, node = principled_material("New scene")
        node.inputs["Base Color"].default_value = (0.0, 0.0, 0.5, 1.0)
        replacement_mesh.materials.append(replacement)
        bpy.data.objects["Cube"].data = replacement_mesh
        bpy.data.meshes.remove(old_mesh)
        self.streamer.resync()
        self.settle()
        self.assertEqual(self.mirror.triangles, 1)
        self.assertEqual(set(self.mirror.surface_materials.values()), {replacement.name})
        self.assertEqual(len(self.mirror.surface_materials), 1)
        self.assertEqual(self.mirror.material_descriptions[replacement.name].base_color, (0.0, 0.0, 0.5))
        self.assertNotIn(old_material.name, self.streamer.capture.diagnostics.active)

    def test_failed_material_write_recovers_on_next_edit_with_full_snapshot(self):
        material, node = principled_material("Retry material")
        cube = bpy.data.objects["Cube"]
        cube.data.materials[0] = material
        self.settle()
        handle = self.mirror.materials[material.name]
        support = self.mirror.material_models[material.name]
        device = self.streamer.device
        set_parameter = device.set

        def fail_colour(obj, name, data_type, value):
            if obj == handle and name == support.color_parameter:
                raise anari.AnariError("injected material write failure")
            set_parameter(obj, name, data_type, value)

        with patch.object(device, "set", side_effect=fail_colour):
            node.inputs["Base Color"].default_value = (0.5, 0.0, 0.0, 1.0)
            bpy.context.view_layer.update()
            self.assertTrue(self.streamer.worker.wait_idle(30))
        self.assertEqual(len(self.failures), 1)
        self.assertIn("injected material write failure", str(self.failures.pop()))
        with patch.object(self.mirror, "apply", wraps=self.mirror.apply) as apply:
            cube.location.x += 0.25
            self.settle()
        self.assertTrue(any(call.args[0].full for call in apply.call_args_list), "retry was not a full scene")
        self.assertEqual(self.mirror.material_descriptions[material.name].base_color, (0.5, 0.0, 0.0))
        self.assertEqual(self.mirror.triangles, 12)
        with self.reference_frame(48) as frame:
            rgba = self.render_colour(frame, 48)
            covered = rgba[:, :, 3] > 128
            self.assertGreater(int(covered.sum()), 16)
            rgb = rgba[covered, :3].mean(axis=0)
            self.assertGreater(float(rgb[0] - max(rgb[1:])), 32, "retry left the stale material in the backend")

    def cube_parts(self):
        cube = bpy.data.objects["Cube"]
        bpy.context.view_layer.update()
        evaluated = cube.evaluated_get(bpy.context.evaluated_depsgraph_get())
        return sync.mesh_parts(evaluated, {})

    def test_flat_faces_share_vertices_and_send_no_normals(self):
        cube = bpy.data.objects["Cube"]
        for polygon in cube.data.polygons:
            polygon.use_smooth = False
        parts = self.cube_parts()
        self.assertEqual(len(parts), 1)
        positions, normals, triangles, _key = parts[0]
        self.assertIsNone(normals)
        self.assertEqual(len(positions), 8)  # one per cube corner, not 24 face corners
        self.assertEqual(len(triangles), 12)
        for polygon in cube.data.polygons:
            polygon.use_smooth = True
        positions, normals, triangles, _key = self.cube_parts()[0]
        self.assertEqual(normals.shape, positions.shape)
        self.assertEqual(len(triangles), 12)
        cube.data.polygons[0].use_smooth = False
        parts = self.cube_parts()
        self.assertEqual([part[1] is None for part in parts], [False, True])
        self.assertEqual(sum(len(part[2]) for part in parts), 12)

    def test_factory_scene_is_mirrored(self):
        self.assertEqual(self.mirror.objects, 1)
        self.assertEqual(self.mirror.triangles, 12)
        self.assertEqual(len(self.mirror.instances), 1)
        # The factory light, and the factory World's grey where the backend has an hdri light.
        self.assertEqual(len(self.mirror.lights), 2 if self.mirror.environment_support else 1)

    def test_moving_an_object_keeps_its_geometry(self):
        before = self.group("Cube")
        bpy.data.objects["Cube"].location.x += 1.0
        self.settle()
        self.assertEqual(self.group("Cube"), before)
        self.assertEqual(len(self.mirror.instances), 1)

    def test_a_modifier_rebuilds_the_object(self):
        before = self.group("Cube")
        modifier = bpy.data.objects["Cube"].modifiers.new("subdivide", "SUBSURF")
        modifier.levels = 1
        self.settle()
        self.assertNotEqual(self.group("Cube"), before)
        self.assertEqual(self.mirror.triangles, 48)

    def test_added_and_deleted_objects(self):
        cube = bpy.data.objects["Cube"]
        copy = bpy.data.objects.new("Copy", cube.data)
        bpy.context.scene.collection.objects.link(copy)
        self.settle()
        self.assertEqual(self.mirror.objects, 2)
        self.assertEqual(len(self.mirror.instances), 2)
        bpy.data.objects.remove(copy)
        self.settle()
        self.assertEqual(self.mirror.objects, 1)
        self.assertEqual(len(self.mirror.instances), 1)

    def test_a_material_colour_reaches_its_material(self):
        material, node = principled_material("Red")
        bpy.data.objects["Cube"].data.materials[0] = material
        self.settle()
        node.inputs["Base Color"].default_value = (1.0, 0.0, 0.0, 1.0)
        self.settle()
        self.assertIn("Red", self.mirror.materials)
        description = self.mirror.material_descriptions["Red"]
        self.assertEqual(description.base_color, (1.0, 0.0, 0.0))
        self.assertEqual(description.opacity, 1.0)

    def paint(self, mesh, colors, domain="CORNER"):
        """A colour attribute like the one Blender's USD import makes from displayColor: `colors`
        holds one RGB per corner (or point), made active."""
        attribute = mesh.color_attributes.new("displayColor", "FLOAT_COLOR", domain)
        rgba = np.concatenate([np.asarray(colors, dtype=np.float32), np.ones((len(colors), 1), np.float32)], axis=1)
        attribute.data.foreach_set("color", rgba.ravel())
        mesh.color_attributes.active_color = attribute
        mesh.update()

    def part_keys(self, name):
        return sorted({part[3] for part in self.mirror.parts[name]})

    def test_faces_without_a_material_stream_their_attribute_colour(self):
        cube = bpy.data.objects["Cube"]
        cube.data.materials.clear()
        self.paint(cube.data, [(1.0, 0.5, 0.0)] * len(cube.data.loops))
        self.settle()
        orange = sync.color_key((sync.COLOR_STEPS, round(0.5 * sync.COLOR_STEPS), 0))
        self.assertEqual(self.part_keys("Cube"), [orange])
        self.assertEqual(materials.material_label(orange), "colour attribute #fff800000")
        np.testing.assert_allclose(self.mirror.material_descriptions[orange].base_color, (1.0, 0.5, 0.0), atol=1e-3)
        self.mirror.configure_renderer(1.0, (0.0, 0.0, 0.0, 0.0))
        with self.reference_frame(32) as frame:
            centre = self.render_colour(frame, 32)[12:20, 12:20, :3].reshape(-1, 3).mean(axis=0)
        self.assertGreater(centre[0], centre[2] + 64, f"not orange: {centre}")
        self.assertGreater(centre[1], centre[2] + 16, f"not orange: {centre}")

    def test_face_colours_become_one_material_each(self):
        cube = bpy.data.objects["Cube"]
        cube.data.materials.clear()
        # displayColor per face: Blender's import writes it to every corner of the face.
        colors = [None] * len(cube.data.loops)
        for polygon in cube.data.polygons:
            for loop in polygon.loop_indices:
                colors[loop] = (1.0, 0.0, 0.0) if polygon.index < 3 else (0.0, 0.0, 1.0)
        self.paint(cube.data, colors)
        self.settle()
        red, blue = sync.color_key((sync.COLOR_STEPS, 0, 0)), sync.color_key((0, 0, sync.COLOR_STEPS))
        self.assertEqual(self.part_keys("Cube"), sorted([red, blue]))

    def test_a_material_wins_over_the_colour_attribute(self):
        cube = bpy.data.objects["Cube"]
        self.paint(cube.data, [(1.0, 0.5, 0.0)] * len(cube.data.loops))
        self.settle()
        self.assertEqual(self.part_keys("Cube"), [cube.data.materials[0].name])

    def test_a_gradient_streams_its_mean_colour_with_a_warning(self):
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=8, y_subdivisions=8, location=(0.0, 0.0, 3.0))
        grid = bpy.context.active_object
        count = len(grid.data.vertices)
        self.paint(grid.data, [(index / count, 0.25, 1.0 - index / count) for index in range(count)], "POINT")
        self.settle()
        keys = self.part_keys(grid.name)
        self.assertEqual(len(keys), 1)
        self.assertIn(sync.MEAN_COLOR_WARNING, self.mirror.material_descriptions[keys[0]].warnings)

    def test_material_updates_use_cached_capabilities(self):
        material, node = principled_material("Cached capabilities")
        bpy.data.objects["Cube"].data.materials[0] = material
        self.settle()
        before = self.mirror.materials[material.name]
        before_group = self.group("Cube")
        support = self.mirror.material_support
        device = self.streamer.device
        with (
            patch.object(device, "object_subtypes", side_effect=AssertionError("query during sync")),
            patch.object(device, "object_parameters", side_effect=AssertionError("query during sync")),
            patch.object(device, "parameter_string_values", side_effect=AssertionError("query during sync")),
            patch.object(device, "new", wraps=device.new) as new_object,
            patch.object(device, "set", wraps=device.set) as set_parameter,
        ):
            node.inputs["Base Color"].default_value = (0.1, 0.2, 0.9, 1.0)
            self.settle()
            self.assertIn("Materials:", support.status)
        self.assertEqual(self.mirror.materials[material.name], before)
        self.assertEqual(self.group("Cube"), before_group)
        self.assertFalse(any(call.args[0] in ("Geometry", "Group") for call in new_object.call_args_list))
        description = self.mirror.material_descriptions[material.name]
        self.assertAlmostEqual(description.base_color[2], 0.9)
        set_parameter.assert_any_call(before, support.color_parameter, anari.FLOAT32_VEC3, description.base_color)

    def test_principled_values_cross_the_worker_as_plain_material_data(self):
        material, node = principled_material("Principled values")
        node.inputs["Base Color"].default_value = (0.25, 0.5, 0.75, 1.0)
        node.inputs["Alpha"].default_value = 0.5
        node.inputs["Metallic"].default_value = 0.75
        node.inputs["Roughness"].default_value = 0.25
        node.inputs["Emission Color"].default_value = (1.5, 0.5, 0.25, 1.0)
        node.inputs["Emission Strength"].default_value = 4.0
        bpy.data.objects["Cube"].data.materials[0] = material
        self.settle()
        expected = materials.MaterialDescription(
            (0.25, 0.5, 0.75), 0.5, 0.75, 0.25, (1.5, 0.5, 0.25), 4.0, is_principled=True
        )
        description = self.mirror.material_descriptions[material.name]
        self.assertEqual(description, expected)
        self.assertIsInstance(description.base_color, tuple)
        self.assertIsInstance(description.emission_color, tuple)
        with self.assertRaises(FrozenInstanceError):
            description.metallic = 0.0
        before = self.group("Cube")
        node.inputs["Metallic"].default_value = 0.125
        self.settle()
        self.assertEqual(description, expected, "the snapshot still referred to mutable Blender data")
        self.assertEqual(self.mirror.material_descriptions[material.name].metallic, 0.125)
        self.assertEqual(self.group("Cube"), before)

    def test_simultaneous_material_and_geometry_changes_still_rebuild_geometry(self):
        material, node = principled_material("Combined edit")
        cube = bpy.data.objects["Cube"]
        cube.data.materials[0] = material
        self.settle()
        before = self.group("Cube")
        node.inputs["Base Color"].default_value = (0.0, 1.0, 0.0, 1.0)
        cube.modifiers.new("subdivide", "SUBSURF").levels = 1
        self.settle()
        self.assertNotEqual(self.group("Cube"), before)
        self.assertEqual(self.mirror.triangles, 48)
        self.assertEqual(self.mirror.material_descriptions[material.name].base_color, (0.0, 1.0, 0.0))

    def test_reassigning_a_material_updates_the_surfaces(self):
        cube = bpy.data.objects["Cube"]
        material, node = principled_material("Replacement")
        node.inputs["Base Color"].default_value = (0.0, 0.0, 1.0, 1.0)
        cube.data.materials[0] = material
        self.settle()
        self.assertIn(material.name, self.mirror.materials)
        parts = self.streamer.capture.meshes["Cube"]
        self.assertEqual({part[3] for part in parts}, {material.name})
        self.assertEqual(self.mirror.material_descriptions[material.name].base_color, (0.0, 0.0, 1.0))

    def test_rewiring_a_material_updates_it_and_clears_fallback_status(self):
        material, original = principled_material("Rewired")
        bpy.data.objects["Cube"].data.materials[0] = material
        self.settle()
        before = self.group("Cube")
        warnings = []
        self.streamer.capture.diagnostics.on_warning = warnings.append
        tree = material.node_tree
        output = next(node for node in tree.nodes if node.type == "OUTPUT_MATERIAL")
        unsupported = tree.nodes.new("ShaderNodeBsdfDiffuse")
        tree.links.new(unsupported.outputs[0], output.inputs["Surface"])
        self.settle()
        self.assertIn(material.name, self.streamer.capture.diagnostics.active)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Rewired", warnings[0])
        material.update_tag()
        self.settle()
        self.assertEqual(len(warnings), 1, "repeated snapshots logged the same warning again")
        original.inputs["Base Color"].default_value = (0.0, 1.0, 0.0, 1.0)
        tree.links.new(original.outputs["BSDF"], output.inputs["Surface"])
        self.settle()
        self.assertNotIn(material.name, self.streamer.capture.diagnostics.active)
        self.assertEqual(self.mirror.material_descriptions[material.name].base_color, (0.0, 1.0, 0.0))
        self.assertEqual(self.group("Cube"), before)

    def test_a_scene_without_lights_gets_the_studio_light(self):
        bpy.data.objects.remove(bpy.data.objects["Light"])
        self.settle()
        self.assertEqual(len(self.mirror.lights), 1)

    def test_physically_based_edits_change_rendered_pixels_and_allow_matte_replacement(self):
        support = self.mirror.material_support
        if support.subtype != "physicallyBased" or not all(
            support.supports(name, data_type) for name, data_type in (
                ("metallic", anari.FLOAT32), ("roughness", anari.FLOAT32), ("emissive", anari.FLOAT32_VEC3)
            )
        ):
            self.skipTest("backend lacks the physically based parameters required by this reference scene")
        for obj in list(bpy.data.objects):
            if obj.type in ("MESH", "LIGHT"):
                bpy.data.objects.remove(obj, do_unlink=True)
        bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=16)
        sphere = bpy.context.object
        for polygon in sphere.data.polygons:
            polygon.use_smooth = True
        material, node = principled_material("Rendered material")
        node.inputs["Base Color"].default_value = (0.25, 0.125, 0.0625, 1.0)
        node.inputs["Metallic"].default_value = 0.0
        node.inputs["Roughness"].default_value = 0.05
        sphere.data.materials.append(material)
        light = bpy.data.lights.new("Reference sun", "SUN")
        light.energy = 2.0
        sun = bpy.data.objects.new("Reference sun", light)
        sun.rotation_euler.x = math.pi / 2
        bpy.context.scene.collection.objects.link(sun)
        self.settle()
        self.mirror.configure_renderer(0.05, (0.0, 0.0, 0.0, 0.0))
        group = self.group(sphere.name)

        def render_colour():
            return self.render_colour(frame, 48).astype(np.int16)

        with self.reference_frame(48) as frame:
            dielectric = render_colour()
            node.inputs["Metallic"].default_value = 1.0
            self.settle()
            metallic = render_colour()
            node.inputs["Roughness"].default_value = 0.8
            self.settle()
            rough = render_colour()
            node.inputs["Emission Color"].default_value = (2.0, 0.0, 0.0, 1.0)
            node.inputs["Emission Strength"].default_value = 2.0
            self.settle()
            emissive = render_colour()
            deltas = [int(np.abs(left[:, :, :3] - right[:, :, :3]).sum())
                      for left, right in ((dielectric, metallic), (metallic, rough), (rough, emissive))]
            print(f"Rendered material RGB deltas (metallic, roughness, emission): {deltas}")
            self.assertGreater(deltas[0], 64, "metallic did not affect the rendered image")
            self.assertGreater(deltas[1], 64, "roughness did not affect the rendered image")
            self.assertGreater(deltas[2], 64, "emission did not affect the rendered image")
            self.assertEqual(self.group(sphere.name), group)
            if self.mirror.material_support.matte is not None:
                previous = self.mirror.materials[material.name]
                material.diffuse_color = (0.0625, 0.75, 0.0625, 1.0)
                diffuse = material.node_tree.nodes.new("ShaderNodeBsdfDiffuse")
                output = next(n for n in material.node_tree.nodes if n.type == "OUTPUT_MATERIAL")
                material.node_tree.links.new(diffuse.outputs[0], output.inputs["Surface"])
                self.settle()
                matte = render_colour()
                self.assertEqual(self.mirror.material_models[material.name].subtype, "matte")
                self.assertNotEqual(self.mirror.materials[material.name], previous)
                self.assertEqual(self.group(sphere.name), group)
                self.assertGreater(int(matte[:, :, 1].sum()), int(matte[:, :, 0].sum()))

    def test_alpha_edits_reveal_overlapping_geometry_and_transparent_background(self):
        preferred = self.mirror.material_support
        models = [(True, preferred)]
        if preferred.matte is not None:
            models.append((False, preferred.matte))
        if not all(support.supports_blend for _principled, support in models):
            self.skipTest("backend lacks advertised alpha blending for the reference materials")
        for obj in list(bpy.data.objects):
            if obj.type in ("MESH", "LIGHT"):
                bpy.data.objects.remove(obj, do_unlink=True)
        front, node = principled_material("Alpha foreground")
        node.inputs["Base Color"].default_value = (0.5, 0.0, 0.0, 1.0)
        front.diffuse_color = (0.5, 0.0, 0.0, 1.0)
        rear = bpy.data.materials.new("Opaque blue background")
        rear.use_nodes = True
        rear_diffuse = rear.node_tree.nodes.new("ShaderNodeBsdfDiffuse")
        rear_output = next(n for n in rear.node_tree.nodes if n.type == "OUTPUT_MATERIAL")
        rear.node_tree.links.new(rear_diffuse.outputs[0], rear_output.inputs["Surface"])
        rear.diffuse_color = (0.0, 0.0, 0.5, 1.0)
        for name, size, y, material in (("Alpha front", 2.0, -0.5, front), ("Alpha rear", 0.8, 0.5, rear)):
            bpy.ops.mesh.primitive_plane_add(size=size, location=(0.0, y, 0.0), rotation=(math.pi / 2, 0.0, 0.0))
            bpy.context.object.name = name
            bpy.context.object.data.materials.append(material)
        self.settle()
        self.mirror.configure_renderer(0.5, (0.0, 0.0, 0.0, 0.0))
        groups = self.mirror.groups.copy()
        device = self.streamer.device

        def render(opacity, principled):
            if principled:
                node.inputs["Alpha"].default_value = opacity
            else:
                front.diffuse_color = (0.5, 0.0, 0.0, opacity)
                front.update_tag()
            self.settle()
            rgba = self.render_colour(frame, 64)
            dw, dh, distances = device.frame_depth(frame)
            self.assertEqual((dw, dh), (64, 64))
            return rgba, np.frombuffer(distances, dtype=np.float32).reshape(dh, dw)

        with self.reference_frame(64) as frame:
            for principled, support in models:
                with self.subTest(model=support.subtype):
                    if not principled:
                        diffuse = front.node_tree.nodes.new("ShaderNodeBsdfDiffuse")
                        output = next(n for n in front.node_tree.nodes if n.type == "OUTPUT_MATERIAL")
                        front.node_tree.links.new(diffuse.outputs[0], output.inputs["Surface"])
                    opaque, near_depth = render(1.0, principled)
                    handle = self.mirror.materials[front.name]
                    partial, _partial_depth = render(0.5, principled)
                    hidden, rear_depth = render(0.0, principled)
                    restored, _restored_depth = render(1.0, principled)
                    self.assertEqual(self.mirror.materials[front.name], handle)
                    self.assertEqual(self.mirror.groups, groups)
                    overlap = (opaque[:, :, 0] > 32) & (hidden[:, :, 2] > 32) & (hidden[:, :, 3] == 255)
                    solo = (opaque[:, :, 3] == 255) & (hidden[:, :, 3] == 0)
                    self.assertGreater(int(overlap.sum()), 16)
                    self.assertGreater(int(solo.sum()), 16)
                    alpha = float(partial[solo, 3].mean())
                    print(f"Rendered {support.subtype} alpha at opacity 0.5: {alpha:.1f}/255")
                    self.assertGreater(alpha, 32)
                    self.assertLess(alpha, 224)
                    self.assertGreater(float(partial[overlap, 0].mean()), 16)
                    self.assertGreater(float(partial[overlap, 2].mean()), 16)
                    self.assertGreater(float(rear_depth[overlap].mean() - near_depth[overlap].mean()), 0.5)
                    self.assertLess(float(np.abs(restored.astype(np.int16) - opaque).mean()), 4)
                    self.assertTrue(np.all(hidden[solo, :3] == 0), "fully transparent foreground retained colour")
                    self.assertEqual(int(opaque[0, 0, 3]), 0, "background was not transparent")

    def test_shared_material_rewiring_changes_both_rendered_objects(self):
        for obj in list(bpy.data.objects):
            if obj.type in ("MESH", "LIGHT"):
                bpy.data.objects.remove(obj, do_unlink=True)
        material, node = principled_material("Shared rendered material")
        node.inputs["Base Color"].default_value = (0.5, 0.0, 0.0, 1.0)
        for x in (-0.7, 0.7):
            bpy.ops.mesh.primitive_plane_add(size=0.9, location=(x, 0.0, 0.0), rotation=(math.pi / 2, 0.0, 0.0))
            bpy.context.object.data.materials.append(material)
        self.settle()
        groups = self.mirror.groups.copy()
        self.assertEqual(list(self.mirror.surface_materials.values()).count(material.name), 2)
        self.mirror.configure_renderer(0.5, (0.0, 0.0, 0.0, 0.0))

        def assert_colour(frame, channel):
            rgba = self.render_colour(frame, 64).astype(np.int16)
            for half in (rgba[:, :32], rgba[:, 32:]):
                covered = half[:, :, 3] > 128
                self.assertGreater(int(covered.sum()), 16)
                mean = half[covered, :3].mean(axis=0)
                self.assertGreater(float(mean[channel]), 32)
                self.assertGreater(float(mean[channel] - np.delete(mean, channel).max()), 24)
            self.assertEqual(self.mirror.groups, groups)

        with self.reference_frame(64) as frame:
            assert_colour(frame, 0)
            node.inputs["Base Color"].default_value = (0.0, 0.0, 0.5, 1.0)
            self.settle()
            assert_colour(frame, 2)
            previous = self.mirror.materials[material.name]
            material.diffuse_color = (0.0, 0.5, 0.0, 1.0)
            tree = material.node_tree
            diffuse = tree.nodes.new("ShaderNodeBsdfDiffuse")
            output = next(n for n in tree.nodes if n.type == "OUTPUT_MATERIAL")
            tree.links.new(diffuse.outputs[0], output.inputs["Surface"])
            self.settle()
            assert_colour(frame, 1)
            fallback = self.mirror.materials[material.name]
            both_models = self.mirror.material_support.matte is not None
            if both_models:
                self.assertNotEqual(fallback, previous)
            self.assertIn(material.name, self.streamer.capture.diagnostics.active)
            node.inputs["Base Color"].default_value = (0.5, 0.0, 0.0, 1.0)
            tree.links.new(node.outputs["BSDF"], output.inputs["Surface"])
            self.settle()
            assert_colour(frame, 0)
            if both_models:
                self.assertNotEqual(self.mirror.materials[material.name], fallback)
            self.assertNotIn(material.name, self.streamer.capture.diagnostics.active)
            colour = tree.nodes.new("ShaderNodeRGB")
            colour.outputs[0].default_value = (0.0, 0.0, 0.5, 1.0)
            link = tree.links.new(colour.outputs[0], node.inputs["Base Color"])
            self.settle()
            assert_colour(frame, 1)
            self.assertTrue(any("Base Color is linked" in warning
                                for warning in self.streamer.capture.diagnostics.active[material.name]))
            tree.links.remove(link)
            self.settle()
            assert_colour(frame, 0)
            self.assertNotIn(material.name, self.streamer.capture.diagnostics.active)

    def test_zero_opacity_matches_removing_shadow_casting_geometry(self):
        preferred = self.mirror.material_support
        models = [(True, preferred)]
        if preferred.matte is not None:
            models.append((False, preferred.matte))
        if not all(support.supports_blend for _principled, support in models):
            self.skipTest("backend lacks advertised alpha blending for the reference materials")
        for obj in list(bpy.data.objects):
            if obj.type in ("MESH", "LIGHT"):
                bpy.data.objects.remove(obj, do_unlink=True)
        front, node = principled_material("Transparent shadow caster")
        node.inputs["Base Color"].default_value = (0.5, 0.0, 0.0, 1.0)
        bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=16, radius=0.6)
        sphere = bpy.context.view_layer.objects.active
        sphere.data.materials.append(front)
        rear, rear_node = principled_material("Shadow receiver")
        rear_node.inputs["Base Color"].default_value = (0.0625, 0.25, 0.5, 1.0)
        bpy.ops.mesh.primitive_plane_add(size=3.0, location=(0.0, 0.8, 0.0), rotation=(math.pi / 2, 0.0, 0.0))
        bpy.context.view_layer.objects.active.data.materials.append(rear)
        # Keep fitted shadow/depth bounds fixed when the test sphere leaves the world.
        bpy.ops.mesh.primitive_cube_add(size=0.1, location=(3.0, -2.0, 0.0))
        light = bpy.data.lights.new("Shadow reference sun", "SUN")
        light.energy = 2.0
        sun = bpy.data.objects.new("Shadow reference sun", light)
        sun.rotation_euler.x = math.pi / 2
        bpy.context.scene.collection.objects.link(sun)
        self.settle()
        self.mirror.configure_renderer(0.05, (0.0, 0.0, 0.0, 0.0))
        with self.reference_frame(64) as frame:
            for principled, support in models:
                with self.subTest(model=support.subtype):
                    if not principled:
                        tree = front.node_tree
                        output = next(n for n in tree.nodes if n.type == "OUTPUT_MATERIAL")
                        diffuse = tree.nodes.new("ShaderNodeBsdfDiffuse")
                        tree.links.new(diffuse.outputs[0], output.inputs["Surface"])
                        self.settle()
                    groups = self.mirror.groups.copy()
                    if principled:
                        node.inputs["Alpha"].default_value = 0.0
                    else:
                        front.diffuse_color = (0.5, 0.0, 0.0, 0.0)
                        front.update_tag()
                    self.settle()
                    zero = self.render_colour(frame, 64).astype(np.int16)
                    self.assertEqual(self.mirror.groups, groups)
                    sphere.hide_viewport = True
                    self.settle()
                    absent = self.render_colour(frame, 64).astype(np.int16)
                    delta = float(np.abs(zero[24:40, 24:40, :3] - absent[24:40, 24:40, :3]).mean())
                    print(f"Rendered {support.subtype} zero-opacity/removal RGB difference: {delta:.2f}")
                    sphere.hide_viewport = False
                    if principled:
                        node.inputs["Alpha"].default_value = 1.0
                    else:
                        front.diffuse_color = (0.5, 0.0, 0.0, 1.0)
                        front.update_tag()
                    self.settle()
                    restored = self.render_colour(frame, 64).astype(np.int16)
                    self.assertLess(delta, 3.0, "fully transparent geometry still occluded the rear surface")
                    self.assertGreater(float(np.abs(restored[24:40, 24:40, :3] - absent[24:40, 24:40, :3]).mean()), 10)

    def test_backend_without_blend_reports_and_resets_opaque_approximation(self):
        support = self.mirror.material_support
        if support.supports_blend:
            self.skipTest("selected material model advertises alpha blending")
        material, node = principled_material("Unsupported alpha")
        bpy.data.objects["Cube"].data.materials[0] = material
        self.settle()
        handle = self.mirror.materials[material.name]
        group = self.group("Cube")
        warnings = []
        self.streamer.capture.diagnostics.on_warning = warnings.append
        device = self.streamer.device
        with patch.object(device, "set", wraps=device.set) as setter:
            for opacity in (0.5, 0.0, 0.5):
                node.inputs["Alpha"].default_value = opacity
                self.settle()
            if support.supports("opacity", anari.FLOAT32):
                setter.assert_any_call(handle, "opacity", anari.FLOAT32, 1.0)
            if support.supports("alphaMode", anari.STRING) and "opaque" in support.alpha_modes:
                setter.assert_any_call(handle, "alphaMode", anari.STRING, "opaque")
            self.assertFalse(any(call.args[1] == "alphaMode" and call.args[3] == "blend"
                                 for call in setter.call_args_list))
        self.assertEqual(self.mirror.materials[material.name], handle)
        self.assertEqual(self.group("Cube"), group)
        self.assertEqual(len(warnings), 1)
        self.assertIn("opaque approximation", warnings[0])
        self.assertIn(material.name, self.streamer.capture.diagnostics.active)
        node.inputs["Alpha"].default_value = 1.0
        self.settle()
        self.assertNotIn(material.name, self.streamer.capture.diagnostics.active)

    def test_repeated_scene_teardown_finishes_pending_array_uploads(self):
        # Release freshly uploaded geometry without rendering or mapping a frame first.
        for cycle in range(20):
            with self.subTest(cycle=cycle):
                values = np.zeros((4096, 3), dtype=np.float32)
                array = self.streamer.device.array(anari.FLOAT32_VEC3, len(values), values, 12)
                self.streamer.device.release(array)
                self.tearDown()
                self.setUp()

    def test_a_fit_without_a_known_volume_is_refused_with_a_warning(self):
        device = self.streamer.device
        # Streaming is off here, so the device never learns where its volume is.
        self.assertFalse(self.streamer.volume_known())
        self.assertFalse(qaros_anari.QAROS_OT_fit_volume.poll(bpy.context))
        device.set(device.handle, "qar.fitBox", anari.FLOAT32_BOX3, (-1.0, -1.0, -1.0, 1.0, 1.0, 1.0))
        device.commit()
        warnings = [text for severity, text in self.messages[self.message_start:] if severity == "warning"]
        self.assertTrue(any("qar.fitBox ignored" in text for text in warnings), warnings)
        # Read once: a later commit does not repeat the request.
        count = len(warnings)
        device.commit()
        later = [text for severity, text in self.messages[self.message_start:] if severity == "warning"]
        self.assertEqual(len(later), count)

    def test_render_scale_preference_maps_to_a_valid_parameter(self):
        self.assertEqual(qaros_anari.render_scale_parameter(0.0), 0.0)
        self.assertEqual(qaros_anari.render_scale_parameter(0.1), qaros_anari.RENDER_SCALE_MIN)
        self.assertEqual(qaros_anari.render_scale_parameter(0.5), 0.5)
        self.assertEqual(qaros_anari.render_scale_parameter(1.0), 1.0)

    def test_repeated_device_lifecycles_wait_for_unmapped_frames(self):
        self.tearDown()
        preferences = SimpleNamespace(anari_folder=FOLDER, backend="", max_users=2, fps=30,
                                      app_scale=0.25, render_scale=0.0, near=0.1, far=100.0,
                                      ambient=0.3, visrtx_materials="physical")
        native_set = anari.Device.set

        def offline_set(device, obj, name, data_type, value):
            native_set(device, obj, name, data_type, 0 if name == "qar.stream" else value)

        try:
            with patch.object(qaros_anari, "preferences", return_value=preferences), \
                    patch.object(qaros_anari.Streamer, "library", self.library), \
                    patch.object(anari.Device, "set", offline_set):
                for cycle in range(20):
                    with self.subTest(cycle=cycle):
                        self.streamer.start(bpy.context)
                        self.streamer.joining.join(10)
                        self.assertFalse(self.streamer.joining.is_alive())
                        with self.reference_frame(16) as frame:
                            self.assertTrue(self.streamer.device.render(frame))
                        # No readback: frameReady(WAIT) must finish the queued render itself.
                        self.streamer.stop(wait=True)
                        self.assertFalse(self.streamer.running)
                self.assertEqual([text for severity, text in self.messages[self.message_start:]
                                  if severity in ("fatal", "error")], [])
        finally:
            if self.streamer.running:
                self.streamer.release()
            self.setUp()

    def test_the_mirrored_world_renders(self):
        device = self.streamer.device
        camera = device.new("Camera", "perspective")
        device.set(camera, "position", anari.FLOAT32_VEC3, (0.0, 0.0, 5.0))
        device.commit(camera)
        frame = device.new("Frame")
        device.set(frame, "world", anari.WORLD, self.mirror.world)
        device.set(frame, "renderer", anari.RENDERER, self.mirror.renderer)
        device.set(frame, "camera", anari.CAMERA, camera)
        device.set(frame, "size", anari.UINT32_VEC2, (16, 16))
        device.set(frame, "channel.color", anari.DATA_TYPE, anari.UFIXED8_RGBA_SRGB)
        device.commit(frame)
        self.assertTrue(device.render(frame))
        device.release(frame)
        device.release(camera)
        errors = [text for severity, text in self.messages if severity in ("fatal", "error")]
        self.assertEqual(errors, [])


class SlowMirror:
    """Applies like a target holding the scene lock for a while."""

    def refresh_volume(self):
        return False

    def __init__(self):
        self.applied = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def settle_deadline(self):
        return None

    def apply(self, snapshot):
        self.entered.set()
        self.release.wait(10)
        self.applied.append(snapshot)


class SyncWorkerTests(unittest.TestCase):
    def snapshot(self, name, full=False):
        snapshot = sync.Snapshot(full)
        snapshot.meshes[name] = None
        snapshot.instances = [(name, None)]
        return snapshot

    def test_submit_does_not_wait_and_folds_pending_snapshots(self):
        mirror = SlowMirror()
        worker = sync.SyncWorker(mirror, lambda error: None)
        try:
            worker.submit(self.snapshot("a"))
            self.assertTrue(mirror.entered.wait(10), "the worker did not start the blocked apply")
            started = time.monotonic()
            for name in ("b", "c", "d"):
                worker.submit(self.snapshot(name))
            self.assertLess(time.monotonic() - started, 0.1, "submit waited for the apply")
            mirror.release.set()
            self.assertTrue(worker.wait_idle(10))
        finally:
            worker.close()
        self.assertEqual(len(mirror.applied), 2)
        folded = mirror.applied[1]
        self.assertEqual(set(folded.meshes), {"b", "c", "d"})
        self.assertFalse(folded.full)
        self.assertEqual(folded.instances, [("d", None)])

    def test_pending_material_edits_keep_the_latest_description(self):
        mirror = SlowMirror()
        worker = sync.SyncWorker(mirror, lambda error: None)
        try:
            worker.submit(self.snapshot("first"))
            self.assertTrue(mirror.entered.wait(10))
            for index in range(20):
                snapshot = self.snapshot("pending")
                snapshot.materials["edited"] = materials.MaterialDescription(metallic=index / 20)
                if index == 0:
                    snapshot.materials["also changed"] = materials.MaterialDescription(roughness=0.125)
                worker.submit(snapshot)
            mirror.release.set()
            self.assertTrue(worker.wait_idle(10))
        finally:
            mirror.release.set()
            worker.close()
        self.assertEqual(len(mirror.applied), 2)
        self.assertEqual(mirror.applied[-1].materials["edited"].metallic, 0.95)
        self.assertEqual(mirror.applied[-1].materials["also changed"].roughness, 0.125)

    def test_a_full_snapshot_replaces_pending_scene_and_material_data(self):
        pending = self.snapshot("old object")
        pending.materials["old material"] = materials.MaterialDescription(metallic=1.0)
        full = self.snapshot("new object", full=True)
        full.materials["new material"] = materials.MaterialDescription(roughness=0.25)
        pending.absorb(full)
        self.assertTrue(pending.full)
        self.assertEqual(pending.meshes, full.meshes)
        self.assertEqual(pending.materials, full.materials)
        latest = self.snapshot("edited object")
        latest.materials["new material"] = materials.MaterialDescription(roughness=0.75)
        pending.absorb(latest)
        self.assertTrue(pending.full)
        self.assertEqual(set(pending.meshes), {"new object", "edited object"})
        self.assertEqual(pending.materials["new material"].roughness, 0.75)

    def test_a_failed_apply_asks_for_a_full_sync(self):
        class Failing:
            def settle_deadline(self):
                return None

            def apply(self, snapshot):
                raise RuntimeError("apply failed")

        errors = []
        worker = sync.SyncWorker(Failing(), errors.append)
        try:
            worker.submit(self.snapshot("a"))
            self.assertTrue(worker.wait_idle(10))
        finally:
            worker.close()
        self.assertEqual(len(errors), 1)
        self.assertTrue(worker.take_failure())
        self.assertFalse(worker.take_failure())


if __name__ == "__main__":
    # Blender owns the arguments before --; optional unittest selections follow it.
    test_arguments = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    result = unittest.main(argv=[sys.argv[0], *test_arguments], exit=False).result
    if result.wasSuccessful() and result.testsRun and result.testsRun == len(result.skipped):
        sys.exit(77)
    sys.exit(0 if result.wasSuccessful() else 1)
