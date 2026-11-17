#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run the add-on's tests.

    python tools/run_tests.py [--blender EXE] [--anari-folder DIR] [--backend helide] [--group ...]

Groups (all by default; a group whose requirement is missing is skipped and reported):

| Group   | Needs                                   | Tests                                              |
|---------|-----------------------------------------|----------------------------------------------------|
| unit    | Python 3.11+                            | protocol, panel, pointing, volume names, targets,  |
|         |                                         | auto-start, ANARI folder lookup, release assets,   |
|         |                                         | stream shading                                     |
| blender | Blender                                 | panel replay, pointing in a scene, USD import,     |
|         |                                         | add-on started by --addons alone, Streams to,      |
|         |                                         | selection outline ids, QAROS tab panels,           |
|         |                                         | ZIP installed                                      |
|         |                                         | from disk and started as the Blender tile does     |
| device  | Blender and a qaros ANARI device folder | ctypes binding, scene sync, USD scene rendered     |

The device folder holds anari.dll and anari_library_qaros.dll: the installed QAROS's `anari`
folder (the default), or a build's output folder. Device tests run with streaming off."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
TIMEOUT_S = 120
SKIPPED = 77

UNIT = ["test_blender_helper.py", "test_blender_hub_launch.py", "test_blender_anari_folder.py",
        "test_blender_volume_name.py", "test_blender_targets.py",
        "test_blender_panel.py", "test_blender_controls_protocol.py", "test_blender_pointing.py", "test_blender_gestures.py",
        "test_blender_shading.py",
        "test_release_package.py"]
BLENDER = ["test_blender_panel_replay.py", "test_blender_pointing_scene.py", "test_blender_usd_import.py",
           "test_blender_addon_launch.py", "test_blender_splats.py",
           "test_blender_targets_scene.py", "test_blender_selection_outline.py", "test_blender_tab.py",
           "test_blender_timer_operators.py"]
# Plain Python driving Blender processes of its own (--blender).
BLENDER_DRIVER = ["test_blender_addon_install.py"]
DEVICE_PYTHON = ["test_blender_addon_binding.py"]
DEVICE_BLENDER = ["test_blender_addon_sync.py", "test_blender_usd_scene.py",
                  "test_blender_renderer_switch.py"]


def installed_anari_folder():
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Quaternar\QAROS") as key:
            folder = Path(winreg.QueryValueEx(key, "InstallFolder")[0]) / "anari"
    except OSError:
        return None
    return folder if (folder / "anari_library_qaros.dll").is_file() else None


def find_blender():
    named = os.environ.get("QAR_TEST_BLENDER_EXECUTABLE")
    if named:
        return Path(named)
    found = shutil.which("blender")
    if found:
        return Path(found)
    if sys.platform != "win32":
        return None
    installs = sorted(Path(os.environ.get("ProgramFiles", r"C:\Program Files")).glob(
        "Blender Foundation/Blender */blender.exe"), reverse=True)
    return installs[0] if installs else None


def blender_command(blender, script, *extra):
    return [str(blender), "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(TESTS / script),
            "--", *extra]


def run(name, command, cwd=None, env=None):
    print(f"--- {name}", flush=True)
    try:
        code = subprocess.run(command, cwd=cwd, env=env, timeout=TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        print(f"{name}: timed out after {TIMEOUT_S} s")
        return "failed"
    if code == SKIPPED:
        return "skipped"
    return "passed" if code == 0 else "failed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--blender", type=Path,
        help="blender executable (default: QAR_TEST_BLENDER_EXECUTABLE, then PATH, then Program Files)",
    )
    parser.add_argument("--anari-folder", type=Path, help="folder with anari.dll and anari_library_qaros.dll")
    parser.add_argument("--backend", default="helide", help="backend for the device tests (default: helide)")
    parser.add_argument("--group", action="append", choices=("unit", "blender", "device"))
    args = parser.parse_args()
    groups = set(args.group or ("unit", "blender", "device"))
    blender = args.blender or find_blender()
    anari_folder = args.anari_folder or installed_anari_folder()

    results = {}
    if "unit" in groups:
        for script in UNIT:
            results[script] = run(script, [sys.executable, "-I", str(TESTS / script)])
    if "blender" in groups:
        if blender is None:
            print("blender group skipped: no Blender found (--blender)")
        else:
            for script in BLENDER:
                results[script] = run(script, blender_command(blender, script))
            for script in BLENDER_DRIVER:
                results[script] = run(script, [sys.executable, "-I", str(TESTS / script), "--blender", str(blender)])
    if "device" in groups:
        if blender is None or anari_folder is None:
            print("device group skipped: needs Blender and an ANARI device folder (--blender, --anari-folder)")
        else:
            env = dict(os.environ, QAR_ANARI_STREAM="0", QAR_ANARI_BACKEND=args.backend)
            cwd = anari_folder.resolve()
            for script in DEVICE_PYTHON:
                results[script] = run(script, [sys.executable, "-I", str(TESTS / script)], cwd, env)
            for script in DEVICE_BLENDER:
                results[script] = run(script, blender_command(blender, script, "-v"), cwd, env)

    print()
    for name, result in results.items():
        print(f"{result:8} {name}")
    failed = [name for name, result in results.items() if result == "failed"]
    return 1 if failed or not results else 0


if __name__ == "__main__":
    sys.exit(main())
