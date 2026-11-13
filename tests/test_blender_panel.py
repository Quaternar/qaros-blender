# SPDX-License-Identifier: MIT
"""The Blender add-on's served controls panel (qaros_anari/panel.py), with plain Python: where the
panel is placed, its identity key, the C layout of the structures it passes, and the page folder."""
import ctypes
import importlib.util
import math
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / "blender" / "qaros_anari" / "panel.py"
spec = importlib.util.spec_from_file_location("panel", path)
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)

IDENTITY = (0.0, 0.0, 0.0, 1.0)


def close(a, b):
    return all(math.isclose(x, y, abs_tol=1e-6) for x, y in zip(a, b))


class PlacementTests(unittest.TestCase):
    def test_an_unrotated_volume_gets_the_panel_on_its_right_level_with_its_top(self):
        position = panel.beside_volume((1.0, 0.5, -2.0), IDENTITY, (1.2, 1.2, 0.8), panel_width=0.4, gap=0.05)
        # right edge 1.0 + 0.6, gap 0.05, half the panel 0.2; top 0.5 + 0.4
        self.assertTrue(close(position, (1.85, 0.9, -2.0)), position)

    def test_the_offset_turns_with_the_volume(self):
        half = math.sqrt(0.5)
        quarter_turn_about_y = (0.0, half, 0.0, half)  # +X becomes -Z
        position = panel.beside_volume((0.0, 0.0, 0.0), quarter_turn_about_y, (1.0, 1.0, 1.0), panel_width=0.4, gap=0.1)
        self.assertTrue(close(position, (0.0, 0.5, -0.8)), position)

    def test_rotate_keeps_a_vector_under_the_identity(self):
        self.assertTrue(close(panel.rotate(IDENTITY, (1.0, 2.0, 3.0)), (1.0, 2.0, 3.0)))


class CommonNameTests(unittest.TestCase):
    def test_the_key_is_ascii_and_follows_the_volume(self):
        self.assertEqual(panel.common_name("blender"), "blender-controls")
        self.assertEqual(panel.common_name("blender-2"), "blender-2-controls")
        name = panel.common_name("blender-Turbine ✨")
        self.assertTrue(name.isascii())
        self.assertTrue(name.endswith("-controls"))

    def test_an_empty_volume_name_still_has_a_key(self):
        self.assertEqual(panel.common_name(""), "blender-controls")


@unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "layouts below are the 64-bit ones")
class LayoutTests(unittest.TestCase):
    """Sizes and offsets of qar_streaming_c/v0 types.h on 64-bit Windows, as clang prints them for
    that header: a field added or misordered here shifts every later one and the runtime reads
    garbage."""

    def test_sizes_match_the_c_structures(self):
        self.assertEqual(ctypes.sizeof(panel.StructureHeader), 16)
        self.assertEqual(ctypes.sizeof(panel.Pose), 28)
        self.assertEqual(ctypes.sizeof(panel.PanelSize), 12)
        self.assertEqual(ctypes.sizeof(panel.PanelInit), 88)
        self.assertEqual(ctypes.sizeof(panel.BundleId), 81)
        self.assertEqual(ctypes.sizeof(panel.ServedContentExt), 112)
        self.assertEqual(ctypes.sizeof(panel.Result), 8)
        self.assertEqual(panel.PanelInit.pose.offset, 32)
        self.assertEqual(panel.PanelInit.size.offset, 60)
        self.assertEqual(panel.PanelInit.visible_to_peers.offset, 72)
        self.assertEqual(panel.ServedContentExt.entry.offset, 104)
        self.assertEqual(ctypes.sizeof(panel.Message), 56)
        self.assertEqual(panel.Message.sequence.offset, 32)
        self.assertEqual(panel.Message.json.offset, 40)
        self.assertEqual(panel.Message.json_size.offset, 48)


class PageFolderTests(unittest.TestCase):
    # The extensions a served bundle accepts that the page uses; anything else is refused at publish.
    ALLOWED = {".html", ".css", ".js", ".mjs", ".json", ".svg", ".png", ".jpg"}

    def test_the_entry_exists_and_every_file_is_servable(self):
        folder = panel.PAGE_FOLDER
        self.assertTrue((folder / panel.ENTRY).is_file())
        for file in folder.rglob("*"):
            if file.is_file():
                self.assertIn(file.suffix, self.ALLOWED, file)

    def test_the_bundle_name_is_a_valid_bundle_name(self):
        self.assertRegex(panel.BUNDLE_NAME, r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}$")


if __name__ == "__main__":
    unittest.main()
