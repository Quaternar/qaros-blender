# SPDX-License-Identifier: MIT
"""Where the Blender add-on loads anari.dll from (qaros_anari/anari.py find_folder), with plain
Python: the folder the user named, else the one QAROS names, else the PATH entry with the device.
Python's ctypes never searches PATH for a bare name, so an empty preference must not mean
`CDLL("anari.dll")`."""
import importlib.util
import os
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / "blender" / "qaros_anari" / "anari.py"
spec = importlib.util.spec_from_file_location("anari_binding", path)
anari = importlib.util.module_from_spec(spec)
spec.loader.exec_module(anari)

DEVICE = ("anari.dll", "anari_library_qaros.dll")


def files_in(*folders):
    present = {os.path.join(folder, name) for folder in folders for name in DEVICE}
    return lambda candidate: candidate in present


class FindFolderTests(unittest.TestCase):
    def test_the_folder_the_user_named_wins(self):
        environ = {anari.FOLDER_ENVIRONMENT: "qaros", "PATH": "setup"}
        self.assertEqual(anari.find_folder("mine", "qaros", environ, files_in("qaros", "setup")), "mine")

    def test_the_qaros_that_started_blender_comes_before_the_path(self):
        environ = {anari.FOLDER_ENVIRONMENT: "qaros", "PATH": os.pathsep.join(("other", "setup"))}
        self.assertEqual(anari.find_folder("", "qaros", environ, files_in("qaros", "setup")), "qaros")

    def test_the_first_path_entry_with_anari_and_the_device(self):
        environ = {"PATH": os.pathsep.join(("", "only-anari", '"setup"', "later"))}
        is_file = files_in("setup", "later")
        only_anari = os.path.join("only-anari", "anari.dll")
        self.assertEqual(
            anari.find_folder("", "qaros", environ, lambda c: is_file(c) or c == only_anari), "setup"
        )

    def test_a_named_folder_of_qaros_without_the_device_falls_back_to_the_path(self):
        environ = {anari.FOLDER_ENVIRONMENT: "gone", "PATH": "setup"}
        self.assertEqual(anari.find_folder("", "qaros", environ, files_in("setup")), "setup")

    def test_empty_when_no_folder_has_the_device(self):
        self.assertEqual(anari.find_folder("", "qaros", {"PATH": "a"}, files_in()), "")
        self.assertEqual(anari.find_folder("", "qaros", {}, files_in()), "")


if __name__ == "__main__":
    unittest.main()
