# SPDX-License-Identifier: MIT
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / "tools" / "prepare_blender_addon.py"
spec = importlib.util.spec_from_file_location("prepare_blender_addon", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AddonTests(unittest.TestCase):
    def test_append(self):
        self.assertEqual(module.prepare("librarynames = ['helide', 'visrtx']\nx = 2\n"),
                         "librarynames = ['helide', 'visrtx', 'qaros']\nx = 2\n")

    def test_already_present(self):
        source = "librarynames=['qaros']\n"
        self.assertEqual(module.prepare(source), source)

    def test_multiline(self):
        source = "librarynames = [\n 'visrtx',\n]\n"
        self.assertEqual(module.prepare(source), "librarynames = ['visrtx', 'qaros']\n")

    def test_reject_dynamic(self):
        with self.assertRaises(ValueError):
            module.prepare("librarynames = get_libraries()\n")

    def test_reject_missing(self):
        with self.assertRaises(ValueError):
            module.prepare("names = []\n")


if __name__ == "__main__":
    unittest.main()
