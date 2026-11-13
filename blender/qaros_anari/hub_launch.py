# SPDX-License-Identifier: MIT
"""How a Blender that QAROS started learns that QAROS stops it. No bpy here, so it is tested with
plain Python.

A hub's launcher stops an app by closing its stdin, asking it to close and, one second later,
terminating it (docs/DEVELOPING.md). Blender treats neither the closed stdin nor the
close request as a quit within that second, so without this the stream ended on a kill and the
session's unsaved work was gone."""
import os
import stat
import sys
import threading


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
