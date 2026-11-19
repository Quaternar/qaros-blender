# SPDX-License-Identifier: MIT
"""The supported Blender range, 4.5 to 5.2: Blender 5.3 crashes with the add-on, so it refuses to register.

    python tests/test_blender_support.py"""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
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


    def test_use_nodes_is_read_only_before_blender_5(self):
        """Blender 5 shades every material and World by its nodes and warns on each `use_nodes`
        read; Blender 6.0 removes it."""
        class Deprecated:
            node_tree = None

            @property
            def use_nodes(self):
                raise AssertionError("use_nodes read on Blender 5")

        for version in ((5, 0, 0), (5, 2, 3), (6, 0, 0)):
            self.assertTrue(self.support.uses_nodes(Deprecated(), version), version)
        flat = SimpleNamespace(use_nodes=False)
        self.assertFalse(self.support.uses_nodes(flat, (4, 5, 0)))
        self.assertTrue(self.support.uses_nodes(SimpleNamespace(use_nodes=True), (4, 5, 0)))
        self.assertFalse(self.support.uses_nodes(flat), "plain Python reads the flag")

    def test_enable_nodes_sets_the_flag_only_before_blender_5(self):
        old = SimpleNamespace(use_nodes=False)
        self.support.enable_nodes(old, (4, 5, 0))
        self.assertTrue(old.use_nodes)
        new = SimpleNamespace()
        self.support.enable_nodes(new, (5, 0, 0))
        self.assertFalse(hasattr(new, "use_nodes"))


if __name__ == "__main__":
    unittest.main()
