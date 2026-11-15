# SPDX-License-Identifier: MIT
"""The release ZIP as a user installs it and QAROS starts it, with a real Blender and plain Python
driving it:

    python tests/test_blender_addon_install.py --blender <blender.exe>

1. Blender's **Install from Disk** (`preferences.addon_install`) takes the ZIP that
   tools/build_zip.py writes and puts `qaros_anari` into the user's add-ons folder.
2. A second Blender started as the QAROS Visualizer's Blender tile starts it (`--addons
   qaros_anari`, QAR_BLENDER_AUTO_START=1, QAR_ANARI_FOLDER) loads the add-on from that folder,
   reads its preferences although Blender's preferences do not list it, finds the ANARI device in
   QAR_ANARI_FOLDER and schedules the automatic Start.

Both Blenders run in background mode with their own empty user folder (BLENDER_USER_RESOURCES),
like a Blender that was never started, so the user's own Blender setup is neither read nor
changed. The ANARI device is two empty files: only the lookup is checked, nothing is loaded."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_zip  # noqa: E402

TIMEOUT_S = 120
BLENDER = None

INSTALL = """
import sys, bpy
result = bpy.ops.preferences.addon_install(filepath=sys.argv[-1], overwrite=True)
sys.exit(0 if result == {"FINISHED"} else 1)
"""

PROBE = """
import json, os, sys, bpy
import qaros_anari
from qaros_anari import anari
prefs = qaros_anari.preferences()
json.dump({
    "module": qaros_anari.__file__,
    "in_preferences": bpy.context.preferences.addons.get("qaros_anari") is not None,
    "anari_folder_preference": prefs.anari_folder,
    "backend": prefs.backend,
    "anari_folder": anari.find_folder(prefs.anari_folder, qaros_anari.LIBRARY_NAME),
    "auto_start_scheduled": bpy.app.timers.is_registered(qaros_anari.auto_start_streaming),
    "version": list(qaros_anari.bl_info["version"]),
}, open(sys.argv[-1], "w"))
"""


def clean_environment(user_folder, **extra):
    """This process's environment without anything QAROS or Blender set, with a user folder of its own."""
    environ = {
        name: value for name, value in os.environ.items()
        if not name.startswith(("QAR_", "BLENDER_"))
    }
    environ["BLENDER_USER_RESOURCES"] = str(user_folder)
    environ.update(extra)
    return environ


def run_blender(arguments, environ):
    completed = subprocess.run(
        [str(BLENDER), "-b", *arguments], env=environ, timeout=TIMEOUT_S,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
    )
    return completed.returncode, completed.stdout


class InstalledAddonTests(unittest.TestCase):
    def test_install_from_disk_then_start_as_the_blender_tile_does(self):
        with tempfile.TemporaryDirectory(prefix="qaros-blender-install-") as temp:
            temp = Path(temp)
            archive = temp / "qaros_blender_anari.zip"
            build_zip.build(archive)
            user = temp / "user"
            user.mkdir()

            code, output = run_blender(
                ["--factory-startup", "--python-exit-code", "1", "--python-expr", INSTALL, "--", str(archive)],
                clean_environment(user),
            )
            self.assertEqual(code, 0, output)
            installed = sorted(user.rglob("qaros_anari/__init__.py"))
            self.assertEqual(len(installed), 1, f"one installed add-on in {user}: {installed}\n{output}")
            self.assertEqual(installed[0].parent.parent.name, "addons")

            device = temp / "qaros" / "anari"
            device.mkdir(parents=True)
            for name in ("anari.dll", "anari_library_qaros.dll"):
                (device / name).write_bytes(b"")
            report = temp / "probe.json"
            code, output = run_blender(
                ["--addons", "qaros_anari", "--python-exit-code", "1", "--python-expr", PROBE, "--", str(report)],
                clean_environment(user, QAR_BLENDER_AUTO_START="1", QAR_ANARI_FOLDER=str(device)),
            )
            self.assertEqual(code, 0, output)
            probe = json.loads(report.read_text())
            self.assertEqual(Path(probe["module"]).resolve(), installed[0].resolve(), "loaded from the user's add-ons")
            self.assertFalse(probe["in_preferences"], "--addons alone leaves Blender's preferences as they were")
            self.assertEqual(probe["anari_folder_preference"], "")
            self.assertEqual(probe["backend"], "auto")
            self.assertEqual(Path(probe["anari_folder"]), device, "the device of the QAROS that started Blender")
            self.assertTrue(probe["auto_start_scheduled"], output)
            self.assertNotIn("Traceback", output)


def main():
    global BLENDER
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blender", type=Path, required=True)
    args, rest = parser.parse_known_args()
    BLENDER = args.blender
    result = unittest.main(argv=[sys.argv[0], *rest], exit=False).result
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
