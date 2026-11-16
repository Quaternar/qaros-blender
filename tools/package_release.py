#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Write the release assets of the add-on: qaros_blender_anari-<version>.zip and manifest.json.

    python tools/package_release.py --check                       # VERSION equals bl_info["version"]
    python tools/package_release.py --print-version               # the version of this checkout
    python tools/package_release.py [--version 0.4.0] [--out-dir dist/release]

The ZIP is the one Blender's Install from Disk takes (tools/build_zip.py: the folder qaros_anari/ at
its root). manifest.json describes it for the QAROS Visualizer's Blender tile, which downloads the
latest release, in the format of the other QAROS app releases (qaros-rerun):

    {"version": 1, "release": "0.4.0", "package": "qaros-blender",
     "assets": [{"name": "qaros_blender_anari-0.4.0.zip", "sha256": "...", "size": 123, "platform": "any",
                 "app_version": "0.4.0", "min_qaros_version": "0.9.0",
                 "min_blender_version": "4.5.0", "addon_module": "qaros_anari"}]}

`platform` is `any`: the add-on is pure Python and loads QAROS's own ANARI device. The file VERSION
holds the last release `X.Y.Z` and must equal bl_info["version"]. The version is --version, or else
the checkout's own (default_version): `X.Y.Z` on the commit tagged `vX.Y.Z`, otherwise the next
patch `X.Y.(Z+1)-dev.M+<sha>` (M commits since the last `v*` tag). A release `X.Y.Z` must equal
VERSION; a pull request build `X.Y.(Z+1)-pr.N` and a development build `X.Y.(Z+1)-dev.M[+sha]`
carry the next patch (ADR-0002 of Quaternar/qaros)."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
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
RELEASE = re.compile(r"^(\d+\.\d+\.\d+)(-(pr|dev)\.\d+(\+[0-9a-z.]+)?)?$")


class ReleaseError(Exception):
    pass


def last_release():
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


def next_patch(version):
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def check_versions():
    """VERSION (the last release), equal to bl_info["version"]."""
    last = last_release()
    addon = dotted(bl_info()["version"])
    if addon != last:
        raise ReleaseError(f'bl_info["version"] is {addon}, VERSION is {last}: keep them equal')
    return last


def default_version():
    """X.Y.Z on the commit tagged vX.Y.Z, else X.Y.(Z+1)-dev.M+sha (M commits since the last v* tag;
    X.Y.(Z+1)-dev.0 without git or tags)."""
    last = check_versions()
    try:
        described = subprocess.run(
            ["git", "describe", "--tags", "--long", "--abbrev=8", "--match", "v[0-9]*"],
            cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return f"{next_patch(last)}-dev.0"
    match = re.match(r"^v(.+)-(\d+)-g([0-9a-f]+)$", described)
    if not match:
        return f"{next_patch(last)}-dev.0"
    tag, commits, sha = match.groups()
    if commits == "0" and tag == last:
        return last
    return f"{next_patch(last)}-dev.{commits}+{sha}"


def release_version(requested):
    last = check_versions()
    version = requested.strip() if requested else default_version()
    version = version[1:] if version.startswith("v") else version
    match = RELEASE.match(version)
    if not match:
        raise ReleaseError(f"{version!r} is not X.Y.Z, X.Y.Z-pr.N or X.Y.Z-dev.N")
    prerelease = bool(match.group(2))
    expected = next_patch(last) if prerelease else last
    if match.group(1) != expected:
        if prerelease:
            raise ReleaseError(f"version {version} must carry the next patch {expected} (VERSION {last})")
        raise ReleaseError(f"version {version} does not match VERSION {last}: fix VERSION or the tag")
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
    parser.add_argument("--print-version", action="store_true", help="only print the version of this checkout")
    parser.add_argument("--version", default="",
                        help="X.Y.Z, X.Y.Z-pr.N or X.Y.Z-dev.N (default: the version of this checkout)")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist" / "release")
    args = parser.parse_args()
    try:
        if args.check:
            print(f"VERSION {check_versions()} equals bl_info['version']")
            return 0
        if args.print_version:
            print(release_version(""))
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
