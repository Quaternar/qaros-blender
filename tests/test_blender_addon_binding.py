# SPDX-License-Identifier: MIT
"""The Blender add-on's ctypes binding (qaros_anari/anari.py) against the real qaros device and a
real backend, streaming off: the same qar.* parameters and scene objects the add-on uses. Runs from
the folder that holds anari.dll and anari_library_qaros.dll."""
import array
import ctypes
import importlib.util
import os
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / "blender" / "qaros_anari" / "anari.py"
spec = importlib.util.spec_from_file_location("qaros_anari_binding", path)
anari = importlib.util.module_from_spec(spec)
spec.loader.exec_module(anari)


class BindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = []
        cls.library = anari.Library(os.getcwd(), "qaros", lambda severity, text: cls.messages.append((severity, text)))

    def setUp(self):
        self.device = self.library.new_device()
        d = self.device
        d.set(d.handle, "qar.stream", anari.BOOL, 0)
        d.set(d.handle, "qar.maxUsers", anari.INT32, 2)
        d.set(d.handle, "qar.fps", anari.INT32, 30)
        d.set(d.handle, "qar.appScale", anari.FLOAT32, 0.25)
        d.set(d.handle, "qar.near", anari.FLOAT32, 0.1)
        d.set(d.handle, "qar.far", anari.FLOAT32, 100.0)
        d.set(d.handle, "qar.volume", anari.STRING, "blender")
        d.set(d.handle, "qar.volumeDisplayName", anari.STRING, "Blender")
        d.commit()

    def tearDown(self):
        self.device.close()

    def triangle_world(self):
        d = self.device
        positions = d.array(anari.FLOAT32_VEC3, 3, array.array("f", [0, 0, 0, 1, 0, 0, 0, 1, 0]), 12)
        indices = d.array(anari.UINT32_VEC3, 1, array.array("I", [0, 1, 2]), 12)
        geometry = d.new("Geometry", "triangle")
        d.set(geometry, "vertex.position", anari.ARRAY1D, positions)
        d.set(geometry, "primitive.index", anari.ARRAY1D, indices)
        d.commit(geometry)
        material = d.new("Material", "matte")
        d.set(material, "color", anari.FLOAT32_VEC3, (0.8, 0.2, 0.2))
        d.commit(material)
        surface = d.new("Surface")
        d.set(surface, "geometry", anari.GEOMETRY, geometry)
        d.set(surface, "material", anari.MATERIAL, material)
        d.commit(surface)
        surfaces = d.array(anari.SURFACE, 1, [surface])
        group = d.new("Group")
        d.set(group, "surface", anari.ARRAY1D, surfaces)
        d.commit(group)
        instance = d.new("Instance", "transform")
        d.set(instance, "group", anari.GROUP, group)
        d.commit(instance)
        light = d.new("Light", "directional")
        d.set(light, "direction", anari.FLOAT32_VEC3, (0, 0, -1))
        d.commit(light)
        world = d.new("World")
        d.set(world, "instance", anari.ARRAY1D, d.array(anari.INSTANCE, 1, [instance]))
        d.set(world, "light", anari.ARRAY1D, d.array(anari.LIGHT, 1, [light]))
        d.commit(world)
        for handle in (positions, indices, geometry, material, surface, surfaces, group, instance, light):
            d.release(handle)
        return world

    def test_renders_the_publish_frame(self):
        d = self.device
        world = self.triangle_world()
        camera = d.new("Camera", "perspective")
        d.set(camera, "position", anari.FLOAT32_VEC3, (0.3, 0.3, 2))
        d.commit(camera)
        renderer = d.new("Renderer", "default")
        d.set(renderer, "background", anari.FLOAT32_VEC4, (0, 0, 0, 0))
        d.commit(renderer)
        frame = d.new("Frame")
        d.set(frame, "size", anari.UINT32_VEC2, (16, 16))
        d.set(frame, "channel.color", anari.DATA_TYPE, anari.UFIXED8_RGBA_SRGB)
        d.set(frame, "world", anari.WORLD, world)
        d.set(frame, "camera", anari.CAMERA, camera)
        d.set(frame, "renderer", anari.RENDERER, renderer)
        d.commit(frame)
        self.assertTrue(d.render(frame))
        for handle in (frame, renderer, camera, world):
            d.release(handle)

    def test_streaming_off_reports_not_streaming(self):
        self.assertEqual(self.device.property_int("qar.streaming", anari.BOOL), 0)
        self.assertEqual(self.device.property_int("qar.activeUsers", anari.UINT32), 0)

    def test_render_scale_is_unanswered_until_streaming(self):
        # The resolved scale (automatic included) exists only once streaming runs.
        self.assertIsNone(self.device.property_float("qar.renderScale"))

    def test_reports_the_backend_it_forwards_to(self):
        backend = self.device.property_string("qar.backend")
        self.assertIn(backend, ("visrtx", "ospray", "visgl", "helide"))
        if os.environ.get("QAR_ANARI_BACKEND"):
            self.assertEqual(backend, os.environ["QAR_ANARI_BACKEND"])

    def test_material_introspection_through_the_proxy(self):
        subtypes = self.device.object_subtypes(anari.MATERIAL)
        support = self.device.material_support()
        self.assertIn(support.subtype, subtypes)
        self.assertTrue(support.supports(support.color_parameter, anari.FLOAT32_VEC3))
        self.assertEqual(support.parameters, self.device.object_parameters(anari.MATERIAL, support.subtype))
        if support.supports("alphaMode", anari.STRING):
            self.assertIn("opaque", support.alpha_modes)
        handle = self.device.new("Material", support.subtype)
        try:
            self.device.set(handle, support.color_parameter, anari.FLOAT32_VEC3, (0.8, 0.2, 0.2))
            self.device.commit(handle)
        finally:
            self.device.release(handle)
        print(f"Material capabilities: {support.subtype}; alpha modes: {sorted(support.alpha_modes)}")

    def test_environment_light_through_the_proxy(self):
        supported = self.device.environment_support()
        self.assertEqual(supported and "hdri" in self.device.object_subtypes(anari.LIGHT), supported)
        if not supported:
            print(f"Environment light: none ({self.device.property_string('qar.backend')})")
            return
        light = self.device.new("Light", "hdri")
        try:
            radiance = self.device.array2d(anari.FLOAT32_VEC3, 4, 2, array.array("f", [1.0] * 24), 12)
            self.device.set(light, "radiance", anari.ARRAY2D, radiance)
            self.device.release(radiance)
            self.device.set(light, "visible", anari.BOOL, 0)
            self.device.commit(light)
        finally:
            self.device.release(light)
        with self.assertRaises(anari.AnariError):
            self.device.array2d(anari.FLOAT32_VEC3, 4, 2, array.array("f", [1.0] * 3), 12)

    def test_array_size_mismatch_is_rejected(self):
        with self.assertRaises(anari.AnariError):
            self.device.array(anari.FLOAT32_VEC3, 2, array.array("f", [0, 0, 0]), 12)

    def test_no_errors_reported(self):
        self.device.release(self.triangle_world())
        errors = [text for severity, text in self.messages if severity in ("fatal", "error")]
        self.assertEqual(errors, [])

    def target_errors(self):
        found = [text for severity, text in self.messages if severity == "error" and "qar.targets" in text]
        self.messages[:] = [m for m in self.messages if "qar.targets" not in m[1]]
        return found

    def test_targets_takes_peer_ids_and_unset_and_reports_a_bad_id(self):
        d = self.device
        d.set(d.handle, "qar.targets", anari.STRING, "0123456789abcdef0123456789abcdef,fedcba98-7654-3210-fedc-ba9876543210")
        d.commit()
        d.set(d.handle, "qar.targets", anari.STRING, "")
        d.commit()
        d.unset(d.handle, "qar.targets")
        d.commit()
        self.assertEqual(self.target_errors(), [], "ids, an empty list and unset are taken")
        d.set(d.handle, "qar.targets", anari.STRING, "receiver-1")
        d.commit()
        errors = self.target_errors()
        self.assertEqual(len(errors), 1, "a malformed id is reported once")
        self.assertIn("receiver-1", errors[0])

    def test_session_targets_is_unanswered_until_streaming(self):
        # Streaming is off here: no session, so no list (the add-on then shows only its saved choice).
        self.assertIsNone(self.device.property_string("qar.sessionTargets"))


class MetadataAPI:
    """Native-layout query results, including missing metadata and repeated parameter names."""

    def __init__(self, materials, alpha_modes=("opaque", "blend", "mask")):
        self.subtypes = (ctypes.c_char_p * (len(materials) + 1))(
            *(name.encode() for name in materials), None
        )
        self.parameters = {
            name.encode(): (anari.Parameter * (len(parameters) + 1))(
                *(anari.Parameter(parameter.encode(), data_type) for parameter, data_type in parameters),
                anari.Parameter(None, 0),
            ) if parameters is not None else None
            for name, parameters in materials.items()
        }
        self.alpha_modes = (ctypes.c_char_p * (len(alpha_modes) + 1))(
            *(mode.encode() for mode in alpha_modes), None
        ) if alpha_modes is not None else None
        self.queries = []

    def anariGetObjectSubtypes(self, device, object_type):
        self.queries.append((device, object_type))
        return self.subtypes

    def anariGetObjectInfo(self, device, object_type, subtype, info, info_type):
        self.queries.append((device, object_type, subtype, info, info_type))
        return self.parameters[subtype]

    def anariGetParameterInfo(self, device, object_type, subtype, parameter, parameter_type, info, info_type):
        self.queries.append((device, object_type, subtype, parameter, parameter_type, info, info_type))
        return self.alpha_modes


class MaterialSupportTests(unittest.TestCase):
    def test_prefers_physically_based_independent_of_subtype_order(self):
        api = MetadataAPI({
            "matte": [("color", anari.FLOAT32_VEC3)],
            "physicallyBased": [
                ("baseColor", anari.STRING), ("baseColor", anari.FLOAT32_VEC3),
                ("metallic", anari.FLOAT32), ("roughness", anari.FLOAT32),
                ("opacity", anari.FLOAT32), ("alphaMode", anari.STRING),
            ],
        })
        support = anari.Device(api, 123).material_support()
        self.assertEqual(support.subtype, "physicallyBased")
        self.assertEqual(support.matte.subtype, "matte")
        self.assertIsNone(support.fallback_reason)
        self.assertTrue(support.supports("baseColor", anari.STRING))
        self.assertTrue(support.supports("baseColor", anari.FLOAT32_VEC3))
        self.assertEqual(support.missing_parameters, ("emissive",))
        self.assertEqual(support.alpha_modes, {"opaque", "blend", "mask"})
        self.assertTrue(support.supports_blend)
        self.assertEqual(api.queries, [
            (123, anari.MATERIAL),
            (123, anari.MATERIAL, b"physicallyBased", b"parameter", anari.PARAMETER_LIST),
            (123, anari.MATERIAL, b"physicallyBased", b"alphaMode", anari.STRING, b"value", anari.STRING_LIST),
            (123, anari.MATERIAL, b"matte", b"parameter", anari.PARAMETER_LIST),
        ])
        # The cached result owns its metadata even if native buffers change or disappear.
        api.parameters[b"physicallyBased"][1].name = b"changed"
        api.alpha_modes[0] = b"changed"
        self.assertTrue(support.supports("baseColor", anari.FLOAT32_VEC3))
        self.assertIn("opaque", support.alpha_modes)

    def test_matte_fallback_explains_an_unusable_physically_based_model(self):
        for parameters in (None, [("baseColor", anari.STRING)]):
            with self.subTest(parameters=parameters):
                api = MetadataAPI({
                    "physicallyBased": parameters,
                    "matte": [("color", anari.FLOAT32_VEC3), ("opacity", anari.FLOAT32)],
                })
                support = anari.Device(api, 123).material_support()
                self.assertEqual(support.subtype, "matte")
                self.assertIn("constant RGB baseColor", support.fallback_reason)
                self.assertIn("fallback", support.status)
                self.assertEqual(support.missing_parameters, ("alphaMode",))
                self.assertFalse(support.alpha_modes)

    def test_missing_physically_based_model_is_a_visible_matte_fallback(self):
        api = MetadataAPI({"matte": [("color", anari.FLOAT32_VEC3)]})
        support = anari.Device(api, 123).material_support()
        self.assertIn("physicallyBased is not advertised", support.fallback_reason)

    def test_missing_string_enumeration_does_not_invent_blend_support(self):
        api = MetadataAPI({
            "matte": [("color", anari.FLOAT32_VEC3), ("alphaMode", anari.STRING)]
        }, alpha_modes=None)
        support = anari.Device(api, 123).material_support()
        self.assertTrue(support.supports("alphaMode", anari.STRING))
        self.assertFalse(support.alpha_modes)
        self.assertFalse(support.supports_blend)

    def test_no_usable_model_has_an_actionable_error(self):
        for materials in ({}, {"physicallyBased": None}, {"matte": [("color", anari.STRING)]}):
            with self.subTest(materials=materials):
                with self.assertRaisesRegex(anari.AnariError, "No usable ANARI material:.*matte"):
                    anari.Device(MetadataAPI(materials), 123).material_support()

    def test_null_subtype_list_is_rejected(self):
        api = MetadataAPI({})
        api.subtypes = None
        with self.assertRaisesRegex(anari.AnariError, "physicallyBased is not advertised"):
            anari.Device(api, 123).material_support()


class EnvironmentSupportTests(unittest.TestCase):
    """The World is streamed only to an hdri light advertising radiance and visible: an environment
    the camera does not see."""

    def support(self, lights):
        return anari.Device(MetadataAPI(lights), 123).environment_support()

    def test_an_hdri_light_with_radiance_and_visible(self):
        self.assertTrue(self.support({
            "point": [("intensity", anari.FLOAT32)],
            "hdri": [("radiance", anari.ARRAY2D), ("visible", anari.BOOL), ("scale", anari.FLOAT32)],
        }))

    def test_no_hdri_light_or_one_that_cannot_hide(self):
        self.assertFalse(self.support({"directional": [("irradiance", anari.FLOAT32)]}))
        # VisRTX 26.09: reads visible without advertising it, and lights nothing with it off.
        self.assertFalse(self.support({"hdri": [("radiance", anari.ARRAY2D), ("scale", anari.FLOAT32)]}))
        self.assertFalse(self.support({"hdri": [("radiance", anari.ARRAY1D), ("visible", anari.BOOL)]}))


if __name__ == "__main__":
    unittest.main()
