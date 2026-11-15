#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Write the installable add-on ZIP: qaros_anari/ with every source file, no caches.

    python tools/build_zip.py [--output dist/qaros_blender_anari.zip]"""
import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "blender" / "qaros_anari"
SKIPPED_PARTS = {"__pycache__"}
SKIPPED_SUFFIXES = {".pyc", ".pyo"}


def addon_files():
    for path in sorted(ADDON.rglob("*")):
        if not path.is_file():
            continue
        if SKIPPED_PARTS.intersection(path.relative_to(ADDON).parts) or path.suffix in SKIPPED_SUFFIXES:
            continue
        yield path


def build(output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in addon_files():
            archive.write(path, Path("qaros_anari") / path.relative_to(ADDON))
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "qaros_blender_anari.zip")
    args = parser.parse_args()
    count = build(args.output.resolve())
    print(f"Wrote {args.output} ({count} files)")


if __name__ == "__main__":
    main()
