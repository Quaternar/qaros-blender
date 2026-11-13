# SPDX-License-Identifier: MIT
"""The Blender add-on's app volume naming (qaros_anari/volume_name.py), with plain Python."""
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / "blender" / "qaros_anari" / "volume_name.py"
spec = importlib.util.spec_from_file_location("volume_name", path)
volume_name = importlib.util.module_from_spec(spec)
spec.loader.exec_module(volume_name)


class ChooseVolumeTests(unittest.TestCase):
    def test_no_name_is_the_automatic_blender_volume(self):
        for typed in ("", None, "   "):
            choice = volume_name.choose_volume(typed)
            self.assertEqual((choice.name, choice.display_name), ("blender", "Blender"))
            self.assertTrue(choice.numbering, "a second Blender is numbered, not hidden")

    def test_a_typed_name_is_its_own_volume_without_numbering(self):
        choice = volume_name.choose_volume(" Turbine review ")
        self.assertEqual(choice.display_name, "Turbine review")
        self.assertFalse(choice.numbering, "a chosen name is never renamed behind the user's back")
        self.assertTrue(choice.name.startswith("blender-"))
        self.assertTrue(choice.name.isascii())

    def test_the_same_name_is_the_same_volume_and_others_differ(self):
        a = volume_name.choose_volume("Turbine review").name
        self.assertEqual(a, volume_name.choose_volume("Turbine review").name)
        self.assertNotEqual(a, volume_name.choose_volume("Turbine design").name)

    def test_any_text_gives_an_ascii_name(self):
        choice = volume_name.choose_volume("Turbína 🚀")
        self.assertTrue(choice.name.isascii())
        self.assertEqual(choice.display_name, "Turbína 🚀")


class MessageTests(unittest.TestCase):
    def test_a_numbered_volume_is_explained(self):
        auto = volume_name.choose_volume("")
        self.assertIsNone(volume_name.streaming_note(auto, "Blender"))
        self.assertEqual(
            volume_name.streaming_note(auto, "Blender 2"),
            "Blender is already shown by another Blender: streaming as Blender 2",
        )

    def test_a_taken_chosen_name_asks_for_another(self):
        chosen = volume_name.choose_volume("Turbine")
        error = "'Turbine' is already shown by another application on this PC. Choose another app volume name."
        self.assertEqual(
            volume_name.conflict_message(chosen, error),
            "'Turbine' is already shown by another Blender. Choose another name.",
        )
        self.assertIsNone(volume_name.conflict_message(chosen, "Joining QAROS timed out"))
        self.assertIsNone(volume_name.conflict_message(chosen, None))


if __name__ == "__main__":
    unittest.main()
