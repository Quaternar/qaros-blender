# SPDX-License-Identifier: MIT
"""The release assets (tools/package_release.py), with plain Python: the ZIP holds qaros_anari/ as
Blender's Install from Disk takes it, and manifest.json carries what the QAROS Visualizer's Blender
tile reads (the asset format of the other QAROS app releases)."""
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import package_release  # noqa: E402


class VersionTests(unittest.TestCase):
    def test_version_file_equals_bl_info(self):
        planned = package_release.check_versions()
        self.assertRegex(planned, r"^\d+\.\d+\.\d+$")

    def test_release_versions_must_match_version_file(self):
        planned = package_release.planned_version()
        self.assertEqual(package_release.release_version(""), planned)
        self.assertEqual(package_release.release_version(f"v{planned}"), planned)
        self.assertEqual(package_release.release_version(f"{planned}-rc.2"), f"{planned}-rc.2")
        self.assertEqual(package_release.release_version(f"{planned}-pr.7"), f"{planned}-pr.7")
        for wrong in ("99.0.0", f"{planned}-beta", f"{planned}.1", "latest"):
            with self.assertRaises(package_release.ReleaseError, msg=wrong):
                package_release.release_version(wrong)

    def test_bl_info_out_of_step_with_version_file_fails(self):
        info = dict(package_release.bl_info(), version=(99, 0, 0))
        with patch.object(package_release, "bl_info", return_value=info):
            with self.assertRaises(package_release.ReleaseError):
                package_release.check_versions()


class PackageTests(unittest.TestCase):
    def test_zip_and_manifest(self):
        version = f"{package_release.planned_version()}-pr.1"
        with tempfile.TemporaryDirectory() as out:
            archive, manifest_path = package_release.package(version, Path(out))
            self.assertEqual(archive.name, f"qaros_blender_anari-{version}.zip")
            with zipfile.ZipFile(archive) as opened:
                names = opened.namelist()
            self.assertIn("qaros_anari/__init__.py", names)
            self.assertTrue(all(name.startswith("qaros_anari/") for name in names), names)
            self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {key: manifest[key] for key in ("version", "release", "package")},
                {"version": 1, "release": version, "package": "qaros-blender"},
            )
            (asset,) = manifest["assets"]
            self.assertEqual(asset["name"], archive.name)
            self.assertEqual(asset["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertRegex(asset["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(asset["size"], archive.stat().st_size)
            self.assertEqual(asset["platform"], "any")
            self.assertEqual(asset["app_version"], version)
            self.assertTrue(re.match(r"^\d+\.\d+\.\d+$", asset["min_qaros_version"]))
            self.assertEqual(asset["min_blender_version"], ".".join(map(str, package_release.bl_info()["blender"])))
            self.assertEqual(asset["addon_module"], "qaros_anari")


if __name__ == "__main__":
    unittest.main()
