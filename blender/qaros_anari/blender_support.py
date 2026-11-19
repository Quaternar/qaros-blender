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


# From Blender 5 every material and World is shaded by its node tree: `use_nodes` is deprecated there
# (each read or write warns) and goes away in Blender 6.0.
NODES_ALWAYS_FROM = (5, 0, 0)


def running_version():
    """bpy.app.version inside Blender; None in plain Python (tools, unit tests)."""
    try:
        import bpy
    except ImportError:
        return None
    return tuple(bpy.app.version)


def uses_nodes(datablock, version=None):
    """Whether Blender shades `datablock` (a Material or World) by its node tree; `use_nodes` is read
    only before Blender 5. `version` defaults to the running Blender."""
    version = running_version() if version is None else tuple(version)
    if version is not None and version[:3] >= NODES_ALWAYS_FROM:
        return True
    return bool(getattr(datablock, "use_nodes", True))


def enable_nodes(datablock, version=None):
    """Gives `datablock` its node tree: sets `use_nodes` before Blender 5, where it is not yet on."""
    version = running_version() if version is None else tuple(version)
    if version is None or version[:3] < NODES_ALWAYS_FROM:
        datablock.use_nodes = True
