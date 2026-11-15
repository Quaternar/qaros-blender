# SPDX-License-Identifier: MIT
"""Streams to in Blender itself (background mode): the QAROS tab's device choice, saved with the
.blend and handed to the device as ``qar.targets``. The device is a stand-in that records what it is
given and answers ``qar.sessionTargets`` like a session with two Visualizer receivers:

    blender -b --factory-startup --python-exit-code 1 --python test_blender_targets_scene.py"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import bpy
from _bpy_internal.addons.cli import set_from_cli

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))
os.environ.pop("QAR_BLENDER_AUTO_START", None)
set_from_cli("qaros_anari")
import qaros_anari  # noqa: E402

R1 = "0123456789abcdef0123456789abcdef"
R2 = "fedcba9876543210fedcba9876543210"
SESSION = json.dumps([
    {"id": R1, "name": "Display Receiver 1", "online": True, "selected": True, "streaming": True},
    {"id": R2, "name": "Display Receiver 2", "online": True, "selected": True, "streaming": True},
])


class FakeDevice:
    handle = 1

    def __init__(self, session=SESSION):
        self.calls = []
        self.session = session

    def set(self, obj, name, data_type, value):
        self.calls.append(("set", name, value))

    def unset(self, obj, name):
        self.calls.append(("unset", name))

    def commit(self, obj=None):
        self.calls.append(("commit",))

    def property_string(self, name):
        return self.session if name == "qar.sessionTargets" else None

    def property_matrix(self, name):
        return (1.0,) * 16 if name == "qar.volumeCube" else None


class FakeCapture:
    def capture(self, *args):
        return None


class FakeWorker:
    def submit(self, snapshot):
        pass


class Layout:
    """Records the labels and buttons a draw function puts on the panel."""

    def __init__(self, texts):
        self.texts = texts
        self.enabled = True
        self.alert = False

    def box(self):
        return Layout(self.texts)

    def row(self, **_kwargs):
        return Layout(self.texts)

    def label(self, text="", icon="NONE"):
        self.texts.append(text)

    def operator(self, idname, text="", icon="NONE", emboss=True):
        self.texts.append(f"[{icon}] {text}")
        return type("Button", (), {})()


def drawn():
    texts = []
    qaros_anari.draw_streams_to(Layout(texts), bpy.context.scene)
    return texts


class StreamsToTests(unittest.TestCase):
    def setUp(self):
        scene = bpy.context.scene
        scene.qaros_stream_to_all = True
        scene.qaros_stream_targets = ""
        self.device = FakeDevice()
        self.streamer = qaros_anari.STREAMER
        self.streamer.device = self.device
        self.streamer.capture = FakeCapture()
        self.streamer.worker = FakeWorker()
        self.streamer.targets_parameter = None
        self.streamer.targets_cache = (0.0, None)

    def tearDown(self):
        self.streamer.device = None
        self.streamer.capture = None
        self.streamer.worker = None

    def test_1_every_device_by_default_with_nothing_set(self):
        scene = bpy.context.scene
        self.assertTrue(scene.qaros_stream_to_all)
        self.assertIsNone(qaros_anari.scene_targets(scene).parameter())
        texts = drawn()
        self.assertIn("[CHECKBOX_HLT] All devices", texts)
        self.assertIn("[CHECKBOX_HLT] Display Receiver 1", texts)
        self.assertIn("[CHECKBOX_HLT] Display Receiver 2", texts)
        self.assertEqual(self.device.calls, [], "drawing changes nothing on the device")

    def test_2_unticking_receiver_2_streams_to_receiver_1_only(self):
        self.assertEqual(bpy.ops.qaros.stream_to_device(peer_id=R2, on=False), {"FINISHED"})
        self.assertEqual(self.device.calls, [("set", "qar.targets", R1), ("commit",)])
        scene = bpy.context.scene
        self.assertFalse(scene.qaros_stream_to_all)
        self.assertEqual(json.loads(scene.qaros_stream_targets), {R1: "Display Receiver 1"})
        self.device.calls.clear()
        texts = drawn()
        self.assertIn("[CHECKBOX_DEHLT] All devices", texts)
        self.assertIn("[CHECKBOX_DEHLT] Display Receiver 2", texts)
        self.assertIn("1 selected device, 1 connected", texts)
        bpy.ops.qaros.stream_to_device(peer_id=R2, on=True)
        self.assertEqual(self.device.calls, [("set", "qar.targets", f"{R1},{R2}"), ("commit",)])

    def test_3_ticking_all_again_unsets_the_parameter(self):
        bpy.ops.qaros.stream_to_all(on=False)
        self.assertEqual(self.device.calls, [("set", "qar.targets", f"{R1},{R2}"), ("commit",)])
        self.device.calls.clear()
        bpy.ops.qaros.stream_to_all(on=True)
        self.assertEqual(self.device.calls, [("unset", "qar.targets"), ("commit",)])
        self.device.calls.clear()
        bpy.ops.qaros.stream_to_all(on=True)
        self.assertEqual(self.device.calls, [], "an unchanged choice is not sent again")

    def test_4_the_choice_is_saved_with_the_blend_and_applied_when_loaded(self):
        bpy.ops.qaros.stream_to_device(peer_id=R2, on=False)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "targets.blend")
            bpy.ops.wm.save_as_mainfile(filepath=path)
            bpy.context.scene.qaros_stream_to_all = True
            bpy.context.scene.qaros_stream_targets = ""
            bpy.ops.wm.open_mainfile(filepath=path)
        scene = bpy.context.scene
        self.assertFalse(scene.qaros_stream_to_all)
        self.assertEqual(json.loads(scene.qaros_stream_targets), {R1: "Display Receiver 1"})
        # What the add-on's load handler runs; the device already has this choice.
        self.device.calls.clear()
        self.streamer.targets_parameter = None
        self.streamer.resync()
        self.assertEqual(self.device.calls, [("set", "qar.targets", R1), ("commit",)])

    def test_5_a_chosen_device_that_left_stays_listed_by_name(self):
        bpy.ops.qaros.stream_to_device(peer_id=R2, on=False)
        self.device.session = json.dumps([
            {"id": R2, "name": "Display Receiver 2", "online": True, "selected": False, "streaming": False},
        ])
        self.streamer.targets_cache = (0.0, None)
        texts = drawn()
        self.assertIn("[CHECKBOX_HLT] Display Receiver 1 (not connected)", texts)
        self.assertIn("1 selected device, 0 connected", texts)

    def test_6_a_qaros_without_target_choice_says_so(self):
        self.device.session = None
        texts = drawn()
        self.assertIn("Every device (this QAROS cannot choose devices)", texts)

    def test_7_before_streaming_only_the_saved_choice_is_shown(self):
        bpy.ops.qaros.stream_to_device(peer_id=R2, on=False)
        self.streamer.device = None
        texts = drawn()
        self.assertIn("[CHECKBOX_HLT] Display Receiver 1 (not connected)", texts)
        self.assertNotIn("[CHECKBOX_DEHLT] Display Receiver 2", texts)


if __name__ == "__main__":
    result = unittest.main(argv=[sys.argv[0]], exit=False).result
    if not result.wasSuccessful():
        sys.exit(1)
