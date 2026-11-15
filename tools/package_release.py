#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Write the release assets of the add-on: qaros_blender_anari-<version>.zip and manifest.json.

    python tools/package_release.py --check                       # VERSION equals bl_info["version"]
    python tools/package_release.py [--version 0.4.0] [--out-dir dist/release]

The ZIP is the one Blender's Install from Disk takes (tools/build_zip.py: the folder qaros_anari/ at
its root). manifest.json describes it for the QAROS Visualizer's Blender tile, which downloads the
latest release, in the format of the other QAROS app releases (qaros-rerun):

    {"version": 1, "release": "0.4.0", "package": "qaros-blender",
     "assets": [{"name": "qaros_blender_anari-0.4.0.zip", "sha256": "...", "size": 123, "platform": "any",
                 "app_version": "0.4.0", "min_qaros_version": "0.9.0",
                 "min_blender_version": "4.5.0", "addon_module": "qaros_anari"}]}

`platform` is `any`: the add-on is pure Python and loads QAROS's own ANARI device. The version is
the file VERSION (`X.Y.Z`, the next planned release), or --version: `X.Y.Z`, `X.Y.Z-rc.N` or
`X.Y.Z-pr.N`, whose `X.Y.Z` must equal VERSION. VERSION must equal bl_info["version"]."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import sys

import build_zip

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
ADDON_INIT = build_zip.ADDON / "__init__.py"

MANIFEST_FORMAT = 1
PACKAGE = "qaros-blender"
ADDON_MODULE = "qaros_anari"
# The release ZIP is named after the repository, the folder inside it after the add-on module.
ARCHIVE_STEM = "qaros_blender_anari"
PLATFORM = "any"
# The oldest QAROS whose ANARI device the add-on streams with. Newer features (the controls panel,
# pointing) are reported and left out on an older runtime, so this moves only when streaming itself
# needs a newer QAROS.
MIN_QAROS_VERSION = "0.9.0"

CORE = re.compile(r"^\d+\.\d+\.\d+$")
RELEASE = re.compile(r"^(\d+\.\d+\.\d+)(-(rc|pr)\.\d+)?$")


class ReleaseError(Exception):
    pass


def planned_version():
    text = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not CORE.match(text):
        raise ReleaseError(f"VERSION must hold X.Y.Z, found {text!r}")
    return text


def bl_info():
    """bl_info of the add-on, read without importing it (it imports bpy)."""
    tree = ast.parse(ADDON_INIT.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "bl_info" for t in node.targets):
            return ast.literal_eval(node.value)
    raise ReleaseError(f"no bl_info in {ADDON_INIT}")


def dotted(numbers):
    return ".".join(str(number) for number in numbers)


def check_versions():
    """VERSION, equal to bl_info["version"]."""
    planned = planned_version()
    addon = dotted(bl_info()["version"])
    if addon != planned:
        raise ReleaseError(f'bl_info["version"] is {addon}, VERSION is {planned}: keep them equal')
    return planned


def release_version(requested):
    planned = check_versions()
    version = (requested or planned).strip()
    version = version[1:] if version.startswith("v") else version
    match = RELEASE.match(version)
    if not match:
        raise ReleaseError(f"{version!r} is not X.Y.Z, X.Y.Z-rc.N or X.Y.Z-pr.N")
    if match.group(1) != planned:
        raise ReleaseError(f"version {version} does not match VERSION {planned}: fix VERSION or the tag")
    return version


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def package(version, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{ARCHIVE_STEM}-{version}.zip"
    build_zip.build(archive)
    asset = {
        "name": archive.name,
        "sha256": sha256(archive),
        "size": archive.stat().st_size,
        "platform": PLATFORM,
        "app_version": version,
        "min_qaros_version": MIN_QAROS_VERSION,
        "min_blender_version": dotted(bl_info()["blender"]),
        "addon_module": ADDON_MODULE,
    }
    manifest = {"version": MANIFEST_FORMAT, "release": version, "package": PACKAGE, "assets": [asset]}
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return archive, manifest_path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only check that VERSION equals bl_info['version']")
    parser.add_argument("--version", default="", help="X.Y.Z, X.Y.Z-rc.N or X.Y.Z-pr.N (default: VERSION)")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist" / "release")
    args = parser.parse_args()
    try:
        if args.check:
            print(f"VERSION {check_versions()} equals bl_info['version']")
            return 0
        archive, manifest_path = package(release_version(args.version), args.out_dir.resolve())
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {archive}")
    print(manifest_path.read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
