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
        last = package_release.check_versions()
        self.assertRegex(last, r"^\d+\.\d+\.\d+$")

    def test_release_must_match_version_file_and_builds_the_next_patch(self):
        last = package_release.last_release()
        following = package_release.next_patch(last)
        self.assertEqual(package_release.release_version(last), last)
        self.assertEqual(package_release.release_version(f"v{last}"), last)
        self.assertEqual(package_release.release_version(f"{following}-pr.7"), f"{following}-pr.7")
        self.assertEqual(package_release.release_version(f"{following}-dev.3+1a2b3c4d"), f"{following}-dev.3+1a2b3c4d")
        for wrong in ("99.0.0", following, f"{last}-pr.7", f"{last}-dev.3", f"{following}-rc.1",
                      f"{last}-beta", f"{last}.1", "latest"):
            with self.assertRaises(package_release.ReleaseError, msg=wrong):
                package_release.release_version(wrong)

    def test_next_patch(self):
        self.assertEqual(package_release.next_patch("0.4.1"), "0.4.2")
        self.assertEqual(package_release.next_patch("1.9.9"), "1.9.10")

    def test_default_version_is_the_release_or_the_next_patch(self):
        last = package_release.last_release()
        version = package_release.default_version()
        if version != last:
            following = re.escape(package_release.next_patch(last))
            self.assertRegex(version, rf"^{following}-dev\.\d+(\+[0-9a-f]+)?$")
        self.assertEqual(package_release.release_version(""), version)

    def test_bl_info_out_of_step_with_version_file_fails(self):
        info = dict(package_release.bl_info(), version=(99, 0, 0))
        with patch.object(package_release, "bl_info", return_value=info):
            with self.assertRaises(package_release.ReleaseError):
                package_release.check_versions()


class PackageTests(unittest.TestCase):
    def test_zip_and_manifest(self):
        version = f"{package_release.next_patch(package_release.last_release())}-pr.1"
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
            self.assertEqual(asset["max_blender_version"], "5.2.99")
            self.assertEqual(asset["addon_module"], "qaros_anari")


if __name__ == "__main__":
    unittest.main()
