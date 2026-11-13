# SPDX-License-Identifier: MIT
"""A USD file through Blender to the stream, end to end: tests/usd/usd_scene.usda imported with the
add-on's File > Import > USD for QAROS, mirrored by the add-on into the real qaros device (streaming
off) on QAR_ANARI_BACKEND, and rendered from above. Each object of the scene is checked by the
colour where it lands in the image, so a lost material, opacity, display colour, instance, frame
or light shows as a wrong pixel (docs/DEVELOPING.md, Tests). Run from the folder
that holds anari.dll and anari_library_qaros.dll:

    blender -b --factory-startup --python-exit-code 1 --python test_blender_usd_scene.py"""
import math
import os
from pathlib import Path
import sys
import unittest

import bpy
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "blender"))
import qaros_anari  # noqa: E402
from qaros_anari import anari, sync  # noqa: E402

SCENE = HERE / "usd" / "usd_scene.usda"
SIZE = 256
EYE_HEIGHT = 3.5  # metres above the floor, looking straight down
FOVY = math.radians(60.0)


def pixel(point):
    """Column and row (from the bottom, as ANARI frames are) where a point of the device's Y-up
    space lands for the camera above the floor; USD's metres after the import are that space."""
    x, y, z = point
    spread = (EYE_HEIGHT - y) * math.tan(FOVY / 2.0)
    u, v = x / spread, -z / spread  # the camera's up is -z
    return int((u + 1.0) / 2.0 * SIZE), int((v + 1.0) / 2.0 * SIZE)


class UsdSceneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        qaros_anari.register()
        bpy.ops.wm.read_factory_settings(use_empty=True)
        cls.result = bpy.ops.qaros.import_usd(filepath=str(SCENE))
        cls.messages = []
        cls.library = anari.Library(os.getcwd(), "qaros", lambda severity, text: cls.messages.append((severity, text)))
        cls.device = cls.library.new_device()
        cls.device.set(cls.device.handle, "qar.stream", anari.BOOL, 0)
        cls.device.commit()
        cls.mirror = sync.SceneMirror(cls.device, os.environ.get("QAR_ANARI_BACKEND"))
        cls.mirror.configure_renderer(0.3, (0.0, 0.0, 0.0, 0.0))
        cls.capture = sync.SceneCapture(material_support=cls.mirror.material_support)
        cls.frames = {number: cls.render(number) for number in (1, 13)}

    @classmethod
    def tearDownClass(cls):
        cls.mirror.close()
        cls.device.close()
        qaros_anari.unregister()

    @classmethod
    def render(cls, number):
        """RGBA8 sRGB of the scene at animation frame `number`, rows from the bottom."""
        bpy.context.scene.frame_set(number)
        names = {obj.name for obj in bpy.data.objects}
        cls.mirror.apply(cls.capture.capture(bpy.context.evaluated_depsgraph_get(), names, number == 1))
        device = cls.device
        camera = device.new("Camera", "perspective")
        device.set(camera, "position", anari.FLOAT32_VEC3, (0.0, EYE_HEIGHT, 0.0))
        device.set(camera, "direction", anari.FLOAT32_VEC3, (0.0, -1.0, 0.0))
        device.set(camera, "up", anari.FLOAT32_VEC3, (0.0, 0.0, -1.0))
        device.set(camera, "fovy", anari.FLOAT32, FOVY)
        device.set(camera, "aspect", anari.FLOAT32, 1.0)
        device.commit(camera)
        frame = device.new("Frame")
        device.set(frame, "world", anari.WORLD, cls.mirror.world)
        device.set(frame, "renderer", anari.RENDERER, cls.mirror.renderer)
        device.set(frame, "camera", anari.CAMERA, camera)
        device.set(frame, "size", anari.UINT32_VEC2, (SIZE, SIZE))
        device.set(frame, "channel.color", anari.DATA_TYPE, anari.UFIXED8_RGBA_SRGB)
        device.set(frame, "channel.depth", anari.DATA_TYPE, anari.FLOAT32)
        device.commit(frame)
        if not device.render(frame):
            raise anari.AnariError("render failed")
        width, height, rgba = device.frame_color(frame)
        device.release(frame)
        device.release(camera)
        return np.frombuffer(rgba, dtype=np.uint8).reshape(height, width, 4).astype(np.int32)

    def colour(self, point, number=1):
        """Mean RGBA of the 5 x 5 pixels around where `point` lands."""
        column, row = pixel(point)
        return self.frames[number][row - 2:row + 3, column - 2:column + 3].reshape(-1, 4).mean(axis=0)

    def test_the_import_succeeds_without_device_errors(self):
        self.assertEqual(self.result, {"FINISHED"})
        errors = [text for severity, text in self.messages if severity in ("fatal", "error")]
        self.assertEqual(errors, [])

    def test_a_shape_streams_its_bound_material(self):
        # Beside the top: the top of a metal sphere under an overhead sun is the sun's white highlight.
        r, g, b, _ = self.colour((-0.42, 0.26, -0.5))
        self.assertGreater(r - b, 40, f"gold sphere not gold: {(r, g, b)}")
        self.assertGreater(g - b, 20, f"gold sphere not gold: {(r, g, b)}")

    def test_a_shape_from_a_referenced_layer_streams_its_material(self):
        r, g, b, _ = self.colour((0.5, 0.3, -0.5))
        self.assertGreater(g - r, 40, f"cylinder not green: {(r, g, b)}")
        self.assertGreater(g - b, 40, f"cylinder not green: {(r, g, b)}")

    def test_display_color_streams_without_a_material(self):
        r, g, b, _ = self.colour((-0.5, 0.3, 0.5))
        self.assertGreater(r - b, 80, f"cube not orange: {(r, g, b)}")
        self.assertGreater(g - b, 30, f"cube not orange: {(r, g, b)}")
        self.assertGreater(r - g, 10, f"cube not orange: {(r, g, b)}")

    def test_opacity_shows_the_floor_through_the_glass(self):
        if not self.mirror.material_support.supports_blend:
            self.skipTest("backend lacks advertised alpha blending")
        r, g, b, a = self.colour((0.5, 0.3, 0.5))
        self.assertGreater(b - r, 20, f"glass not blue: {(r, g, b)}")
        # Opaque, this blue lit from above saturates its blue channel (VisGL: 84, 108, 255); at
        # opacity 0.4 the grey floor behind pulls it down (99, 108, 186).
        self.assertLess(b, 225, f"glass streams opaque: {(r, g, b)}")
        self.assertEqual(a, 255)

    def test_point_instances_stream(self):
        for x in (-0.2, 0.0, 0.2):
            r, g, b, _ = self.colour((x, 0.1, 0.0))
            self.assertGreater(r - g, 60, f"instance at x={x} not red: {(r, g, b)}")

    def test_the_animation_moves_the_blade(self):
        tip = (0.25, 0.04, -0.8)
        first, later = self.colour(tip, 1), self.colour(tip, 13)
        floor = self.colour((0.8, 0.0, 0.0))
        self.assertGreater(first[:3].mean(), floor[:3].mean() + 10, f"no blade at frame 1: {first}, floor {floor}")
        self.assertLess(abs(later[:3].mean() - floor[:3].mean()), 10, f"blade still there at frame 13: {later}")

    def test_the_sun_lights_the_floor_without_washing_it_out(self):
        r, g, b, _ = self.colour((0.8, 0.0, 0.0))
        self.assertLess(max(r, g, b) - min(r, g, b), 12, f"floor not grey: {(r, g, b)}")
        self.assertTrue(110 < g < 245, f"floor brightness {g} (lit grey, not black, not white)")


if __name__ == "__main__":
    test_arguments = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    result = unittest.main(argv=[sys.argv[0], *test_arguments], exit=False).result
    if result.wasSuccessful() and result.testsRun and result.testsRun == len(result.skipped):
        sys.exit(77)
    sys.exit(0 if result.wasSuccessful() else 1)
