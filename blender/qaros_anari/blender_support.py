# SPDX-License-Identifier: MIT
"""The Blender versions the add-on supports. Plain Python: tools and tests read it without bpy."""

MIN_BLENDER = (4, 5, 0)
# Blender 5.3 crashes with the add-on. Raise this once a Blender above it is tested.
MAX_BLENDER = (5, 2, 99)
SUPPORTED_RANGE = "4.5–5.2"


def supported(version):
    """True when the Blender version tuple (bpy.app.version) is in the supported range."""
    return MIN_BLENDER <= tuple(version)[:3] <= MAX_BLENDER


def unsupported_message(version):
    return f"QAROS ANARI Streaming does not support Blender {'.'.join(str(n) for n in version[:3])} yet " \
           f"(supported: Blender {SUPPORTED_RANGE}). The add-on stays disabled."
