# SPDX-License-Identifier: MIT
"""How a Blender that QAROS started learns that QAROS stops it, whether to start streaming by
itself, and which preferences apply when its preferences do not list the add-on. No bpy here, so it
is tested with plain Python.

A hub's launcher stops an app by closing its stdin, asking it to close and, one second later,
terminating it (docs/DEVELOPING.md). Blender treats neither the closed stdin nor the
close request as a quit within that second, so without this the stream ended on a kill and the
session's unsaved work was gone."""
import os
import stat
import sys
import threading
from types import SimpleNamespace


def stdin_is_pipe(stream=None):
    """Whether stdin is a pipe, as a launcher's is. A Blender started from Explorer has none, one
    started in a terminal has the console."""
    stream = sys.stdin if stream is None else stream
    if stream is None:
        return False
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    if sys.platform == "win32":
        import ctypes
        import msvcrt

        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError:
            return False
        FILE_TYPE_PIPE = 0x0003
        return ctypes.windll.kernel32.GetFileType(handle) == FILE_TYPE_PIPE
    try:
        return stat.S_ISFIFO(os.fstat(fd).st_mode)
    except OSError:
        return False


def started_by_hub(environ, stdin_pipe):
    """A hub's launcher names its instance (QAR_INSTANCE) in every app it starts and gives it a
    piped stdin. Both are required, so a Blender started by hand never quits on its own."""
    return bool(environ.get("QAR_INSTANCE")) and stdin_pipe


class StopRequestWatcher:
    """Reads stdin to its end on a thread of its own. The end of stdin is the launcher's stop
    request. What it reads (an onboarding code for an app given one) is discarded, never logged."""

    def __init__(self, stream):
        self._stream = stream
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._read, name="qaros-anari-stdin", daemon=True)

    def start(self):
        self._thread.start()

    def requested(self):
        return self._stopped.is_set()

    def wait(self, timeout):
        return self._stopped.wait(timeout)

    def _read(self):
        try:
            while self._stream.readline():
                pass
        except (OSError, ValueError):
            pass
        self._stopped.set()


# Set to 1 by the QAROS Visualizer's Blender tile when it starts Blender: the
# add-on starts streaming once by itself, without the user pressing Start.
AUTO_START_ENVIRONMENT = "QAR_BLENDER_AUTO_START"
# The words QAR_PANEL_AUTO_CONNECT of the ROS 2 Rviz plugin takes, in any case.
AUTO_START_ON = frozenset(("1", "true", "on", "yes"))
AUTO_START_OFF = frozenset(("0", "false", "off", "no"))


def auto_start_requested(environ, warn):
    """Whether QAROS asked this Blender to start streaming by itself. Unset or empty is off; any
    other word is off too, with a warning through ``warn``, so a typo never shares a scene the user
    did not mean to share."""
    value = (environ.get(AUTO_START_ENVIRONMENT) or "").strip()
    if not value:
        return False
    word = value.lower()
    if word in AUTO_START_ON:
        return True
    if word not in AUTO_START_OFF:
        warn(
            f"{AUTO_START_ENVIRONMENT}={value!r} is none of 1/true/on/yes or 0/false/off/no: "
            "not starting by itself"
        )
    return False


class AutoStartOnce:
    """The automatic Start of one Blender process: at most one attempt, and none once streaming is
    on already (the user pressed Start first). A failed attempt is not repeated: the user presses
    Start, as without auto-start."""

    def __init__(self, requested):
        self._pending = bool(requested)

    @property
    def pending(self):
        return self._pending

    def take(self, running):
        """True exactly once, when an automatic Start is still due and streaming is off."""
        if not self._pending:
            return False
        self._pending = False
        return not running


# Blender's own value for a property declared without a default, by property function.
PROPERTY_ZERO = {"StringProperty": "", "BoolProperty": False, "IntProperty": 0, "FloatProperty": 0.0}


def preference_defaults(annotations):
    """The add-on's preferences as the user would find them unchanged: each declared property's
    `default`, else Blender's zero for its type. ``annotations`` maps a name to the deferred
    property of the AddonPreferences class (``function`` and ``keywords``).

    QAROS starts Blender with ``--addons qaros_anari``, which enables the add-on for the session
    without adding it to the preferences (``addon_utils.enable`` without ``default_set``). A Blender
    whose preferences never enabled it, a portable or never-started one among them, then has no
    preferences entry for the add-on, and these values apply."""
    values = {}
    for name, prop in annotations.items():
        keywords = getattr(prop, "keywords", None) or {}
        function = getattr(getattr(prop, "function", None), "__name__", "")
        values[name] = keywords["default"] if "default" in keywords else PROPERTY_ZERO.get(function)
    return SimpleNamespace(**values)
