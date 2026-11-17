# SPDX-License-Identifier: MIT
"""The supported Blender range, 4.5 to 5.2: Blender 5.3 crashes with the add-on, so it refuses to register.

    python tests/test_blender_support.py"""
import ast
import importlib.util
from pathlib import Path
import unittest

ADDON = Path(__file__).resolve().parents[1] / "blender" / "qaros_anari"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BlenderSupportTest(unittest.TestCase):
    def setUp(self):
        self.support = load("qaros_blender_support_test", ADDON / "blender_support.py")

    def test_range(self):
        for version in ((4, 5, 0), (4, 5, 16), (4, 6, 0), (5, 0, 0), (5, 2, 0), (5, 2, 4)):
            self.assertTrue(self.support.supported(version), version)
        for version in ((4, 4, 3), (5, 3, 0), (5, 3, 1), (5, 4, 0), (6, 0, 0)):
            self.assertFalse(self.support.supported(version), version)

    def test_message_names_version_and_range(self):
        message = self.support.unsupported_message((5, 3, 0, 1))
        self.assertIn("Blender 5.3.0", message)
        self.assertIn("4.5–5.2", message)

    def test_register_refuses_unsupported_blender_before_registering_anything(self):
        """register() needs bpy, so read its source: the guard must be its first statement."""
        tree = ast.parse((ADDON / "__init__.py").read_text(encoding="utf-8"))
        register = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "register")
        guard = register.body[0]
        self.assertIsInstance(guard, ast.If)
        self.assertIn("blender_support.supported", ast.unparse(guard.test))
        self.assertTrue(any(isinstance(n, ast.Raise) for n in guard.body))


if __name__ == "__main__":
    unittest.main()
