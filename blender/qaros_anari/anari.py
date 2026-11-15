# SPDX-License-Identifier: MIT
"""The part of the ANARI C API the add-on needs, through ctypes: Blender's Python has no cffi, so
the Khronos bindings do not load there. Values and signatures follow anari/anari.h (SDK 0.16)."""
import ctypes
import os
from pathlib import Path

DATA_TYPE = 100
STRING = 101
VOID_POINTER = 102
BOOL = 103
STRING_LIST = 150
PARAMETER_LIST = 152
ARRAY1D = 504
CAMERA = 507
FRAME = 508
GEOMETRY = 509
GROUP = 510
INSTANCE = 511
LIGHT = 512
MATERIAL = 513
RENDERER = 514
SURFACE = 515
WORLD = 519
INT32 = 1016
UINT32 = 1020
UINT32_VEC2 = 1021
UINT32_VEC3 = 1022
FLOAT32 = 1068
FLOAT32_VEC3 = 1070
FLOAT32_VEC4 = 1071
UFIXED8_RGBA_SRGB = 2003
FLOAT32_BOX3 = 2010
FLOAT32_MAT4 = 2014

WAIT = 1
SEVERITY_NAMES = {1: "fatal", 2: "error", 3: "warning", 4: "performance", 5: "info", 6: "debug"}

_handle = ctypes.c_void_p


class Parameter(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("type", ctypes.c_int)]


STATUS_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, _handle, _handle, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p
)

_SIGNATURES = {
    "anariLoadLibrary": (_handle, [ctypes.c_char_p, STATUS_CALLBACK, ctypes.c_void_p]),
    "anariUnloadLibrary": (None, [_handle]),
    "anariNewDevice": (_handle, [_handle, ctypes.c_char_p]),
    "anariNewArray1D": (
        _handle, [_handle, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint64]
    ),
    "anariNewObject": (_handle, [_handle, ctypes.c_char_p, ctypes.c_char_p]),
    "anariNewWorld": (_handle, [_handle]),
    "anariNewGroup": (_handle, [_handle]),
    "anariNewSurface": (_handle, [_handle]),
    "anariNewFrame": (_handle, [_handle]),
    "anariNewInstance": (_handle, [_handle, ctypes.c_char_p]),
    "anariNewGeometry": (_handle, [_handle, ctypes.c_char_p]),
    "anariNewMaterial": (_handle, [_handle, ctypes.c_char_p]),
    "anariNewLight": (_handle, [_handle, ctypes.c_char_p]),
    "anariNewCamera": (_handle, [_handle, ctypes.c_char_p]),
    "anariNewRenderer": (_handle, [_handle, ctypes.c_char_p]),
    "anariGetObjectSubtypes": (ctypes.POINTER(ctypes.c_char_p), [_handle, ctypes.c_int]),
    "anariGetObjectInfo": (
        ctypes.c_void_p, [_handle, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    ),
    "anariGetParameterInfo": (
        ctypes.c_void_p,
        [_handle, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int],
    ),
    "anariMapArray": (ctypes.c_void_p, [_handle, _handle]),
    "anariUnmapArray": (None, [_handle, _handle]),
    "anariSetParameter": (None, [_handle, _handle, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]),
    "anariUnsetParameter": (None, [_handle, _handle, ctypes.c_char_p]),
    "anariCommitParameters": (None, [_handle, _handle]),
    "anariRelease": (None, [_handle, _handle]),
    "anariGetProperty": (
        ctypes.c_int, [_handle, _handle, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint]
    ),
    "anariRenderFrame": (None, [_handle, _handle]),
    "anariFrameReady": (ctypes.c_int, [_handle, _handle, ctypes.c_uint]),
    "anariMapFrame": (
        ctypes.c_void_p,
        [_handle, _handle, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32),
         ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_int)],
    ),
    "anariUnmapFrame": (None, [_handle, _handle, ctypes.c_char_p]),
}


class AnariError(RuntimeError):
    pass


def _bind(dll):
    for name, (result, arguments) in _SIGNATURES.items():
        function = getattr(dll, name)
        function.restype = result
        function.argtypes = arguments


def _strings(pointer):
    """Copy a borrowed, null-terminated string list before another backend query."""
    if not pointer:
        return ()
    values = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_char_p))
    result = []
    for value in values:
        if value is None:
            break
        result.append(value.decode("utf-8", "replace"))
    return tuple(result)


class MaterialSupport:
    """A device's copied material metadata; safe to read without calling ANARI during a render."""

    def __init__(self, subtype, parameters, alpha_modes, fallback_reason=None, matte=None):
        self.subtype = subtype
        self.parameters = frozenset(parameters)
        self.alpha_modes = frozenset(alpha_modes)
        self.fallback_reason = fallback_reason
        self.matte = matte

    def supports(self, name, data_type):
        return (name, data_type) in self.parameters

    @property
    def color_parameter(self):
        return "baseColor" if self.subtype == "physicallyBased" else "color"

    @property
    def supports_blend(self):
        return (
            self.supports("opacity", FLOAT32)
            and self.supports("alphaMode", STRING)
            and "blend" in self.alpha_modes
        )

    @property
    def status(self):
        if self.subtype == "matte":
            return "Materials: Matte fallback"
        if self.matte is not None:
            return "Materials: Physically based and matte"
        return "Materials: Physically based"

    @property
    def missing_parameters(self):
        expected = [("opacity", FLOAT32), ("alphaMode", STRING)]
        if self.subtype == "physicallyBased":
            expected.extend((("metallic", FLOAT32), ("roughness", FLOAT32), ("emissive", FLOAT32_VEC3)))
        return tuple(name for name, data_type in expected if not self.supports(name, data_type))


# Set by QAROS in a Blender it starts (the Blender tile of the Visualizer): the folder of that QAROS's
# ANARI device, so the add-on loads the device of the QAROS that started it.
FOLDER_ENVIRONMENT = "QAR_ANARI_FOLDER"


def find_folder(folder, name, environ=None, is_file=None):
    """The folder anari.dll and anari_library_<name>.dll load from: the one the user named, else the
    one QAROS names (QAR_ANARI_FOLDER), else the first PATH entry holding both (the QAROS setup puts
    its `anari` folder there). Python's ctypes never searches PATH for a bare name, so the PATH
    lookup is done here. Empty when no folder has them."""
    if folder:
        return str(folder)
    environ = os.environ if environ is None else environ
    is_file = os.path.isfile if is_file is None else is_file
    files = ("anari.dll", f"anari_library_{name}.dll")
    candidates = [environ.get(FOLDER_ENVIRONMENT, "")]
    candidates.extend(environ.get("PATH", "").split(os.pathsep))
    for candidate in candidates:
        candidate = candidate.strip().strip('"')
        if candidate and all(is_file(os.path.join(candidate, file)) for file in files):
            return candidate
    return ""


class Library:
    """anari.dll and one loaded ANARI library (`qaros`). `folder` holds anari.dll and
    anari_library_qaros.dll; empty means QAR_ANARI_FOLDER, then the system search path
    (find_folder)."""

    def __init__(self, folder, name, on_status):
        self._dll_directory = None
        named = bool(folder)
        folder = find_folder(folder, name)
        if not folder:
            raise AnariError(
                "No anari.dll found: install QAROS with its ANARI device, or set ANARI folder in the "
                "add-on's preferences"
            )
        folder = Path(folder)
        if not (folder / "anari.dll").is_file():
            raise AnariError(f"No anari.dll in {folder}")
        if not named:
            print(f"[qaros-anari] ANARI folder: {folder}", flush=True)
        self._dll_directory = os.add_dll_directory(str(folder))
        # ANARI's loader takes the library by its bare name; loading it first by full path
        # makes that name resolve to this copy, not another one on the search path.
        ctypes.WinDLL(str(folder / f"anari_library_{name}.dll"))
        self.api = ctypes.CDLL(str(folder / "anari.dll"))
        _bind(self.api)
        # Called from any backend or stream thread; the reference keeps the thunk alive.
        self._callback = STATUS_CALLBACK(
            lambda _user, _device, _source, _type, severity, _code, message: on_status(
                SEVERITY_NAMES.get(severity, str(severity)), (message or b"").decode("utf-8", "replace")
            )
        )
        self.handle = self.api.anariLoadLibrary(name.encode(), self._callback, None)
        if not self.handle:
            raise AnariError(f"anariLoadLibrary('{name}') failed; see the status messages")

    def new_device(self, subtype="default"):
        device = self.api.anariNewDevice(self.handle, subtype.encode())
        if not device:
            raise AnariError("anariNewDevice failed; see the status messages")
        return Device(self.api, device)

    def close(self):
        # The library stays loaded: qaros never unloads the QAR runtime, whose crash hooks would
        # then point into freed memory.
        self.handle = None


class Device:
    """Thin wrapper; every object is a raw handle the caller releases."""

    def __init__(self, api, handle):
        self.api = api
        self.handle = handle

    def object_subtypes(self, object_type):
        return _strings(self.api.anariGetObjectSubtypes(self.handle, object_type))

    def object_parameters(self, object_type, subtype):
        pointer = self.api.anariGetObjectInfo(
            self.handle, object_type, subtype.encode(), b"parameter", PARAMETER_LIST
        )
        if not pointer:
            return frozenset()
        parameters = ctypes.cast(pointer, ctypes.POINTER(Parameter))
        result = set()
        for parameter in parameters:
            if parameter.name is None:
                break
            result.add((parameter.name.decode("utf-8", "replace"), parameter.type))
        return frozenset(result)

    def parameter_string_values(self, object_type, subtype, name):
        pointer = self.api.anariGetParameterInfo(
            self.handle, object_type, subtype.encode(), name.encode(), STRING, b"value", STRING_LIST
        )
        return _strings(pointer)

    def material_support(self):
        """Select once before publishing a scene. Only constant RGB colour is mandatory; other
        parameters remain individually detectable so reduced backends still have a usable model."""
        subtypes = self.object_subtypes(MATERIAL)
        rejected = []
        models = {}
        for subtype, color in (("physicallyBased", "baseColor"), ("matte", "color")):
            if subtype not in subtypes:
                rejected.append(f"{subtype} is not advertised")
                continue
            parameters = self.object_parameters(MATERIAL, subtype)
            if (color, FLOAT32_VEC3) not in parameters:
                rejected.append(f"{subtype} does not advertise constant RGB {color}")
                continue
            alpha_modes = (
                self.parameter_string_values(MATERIAL, subtype, "alphaMode")
                if ("alphaMode", STRING) in parameters else ()
            )
            models[subtype] = MaterialSupport(subtype, parameters, alpha_modes)
        if "physicallyBased" in models:
            preferred = models["physicallyBased"]
            preferred.matte = models.get("matte")
            return preferred
        if "matte" in models:
            preferred = models["matte"]
            preferred.fallback_reason = "; ".join(rejected) or None
            return preferred
        raise AnariError("No usable ANARI material: " + "; ".join(rejected))

    def new(self, kind, subtype=None):
        constructor = getattr(self.api, f"anariNew{kind}")
        handle = constructor(self.handle, subtype.encode()) if subtype is not None else constructor(self.handle)
        if not handle:
            raise AnariError(f"anariNew{kind}({subtype or ''}) failed")
        return handle

    def array(self, data_type, count, data=None, item_bytes=None):
        """A device-owned 1D array; `data` (a buffer of count * item_bytes bytes, or a list of
        handles) is copied in. Device-owned, so the add-on never keeps memory alive for it."""
        array = self.api.anariNewArray1D(self.handle, None, None, None, data_type, count)
        if not array:
            raise AnariError("anariNewArray1D failed")
        if count:
            pointer = self.api.anariMapArray(self.handle, array)
            if not pointer:
                self.release(array)
                raise AnariError("anariMapArray failed")
            try:
                if isinstance(data, list):
                    (ctypes.c_void_p * count).from_address(pointer)[:] = data
                else:
                    view = memoryview(data).cast("B")
                    if item_bytes is not None and len(view) != count * item_bytes:
                        raise AnariError("Array data has the wrong size")
                    ctypes.memmove(pointer, (ctypes.c_char * len(view)).from_buffer_copy(view), len(view))
            finally:
                self.api.anariUnmapArray(self.handle, array)
        return array

    def set(self, obj, name, data_type, value):
        name = name.encode()
        if data_type == STRING:
            self.api.anariSetParameter(self.handle, obj, name, data_type, ctypes.c_char_p(value.encode()))
            return
        if data_type in (ARRAY1D, CAMERA, GEOMETRY, GROUP, INSTANCE, LIGHT, MATERIAL, RENDERER, SURFACE, WORLD):
            holder = ctypes.c_void_p(value)
        elif data_type in (BOOL, INT32, DATA_TYPE):
            holder = ctypes.c_int32(int(value))
        elif data_type == UINT32:
            holder = ctypes.c_uint32(int(value))
        elif data_type == FLOAT32:
            holder = ctypes.c_float(value)
        elif data_type in (FLOAT32_VEC3, FLOAT32_VEC4, FLOAT32_BOX3, FLOAT32_MAT4):
            holder = (ctypes.c_float * len(value))(*value)
        elif data_type in (UINT32_VEC2, UINT32_VEC3):
            holder = (ctypes.c_uint32 * len(value))(*value)
        else:
            raise AnariError(f"Unsupported parameter type {data_type}")
        self.api.anariSetParameter(self.handle, obj, name, data_type, ctypes.byref(holder))

    def unset(self, obj, name):
        self.api.anariUnsetParameter(self.handle, obj, name.encode())

    def commit(self, obj=None):
        self.api.anariCommitParameters(self.handle, obj or self.handle)

    def release(self, obj):
        if obj:
            self.api.anariRelease(self.handle, obj)

    def property_int(self, name, data_type=INT32):
        value = ctypes.c_int32() if data_type != UINT32 else ctypes.c_uint32()
        found = self.api.anariGetProperty(
            self.handle, self.handle, name.encode(), data_type, ctypes.byref(value), ctypes.sizeof(value), 0
        )
        return value.value if found else None

    def property_float(self, name):
        value = ctypes.c_float()
        found = self.api.anariGetProperty(
            self.handle, self.handle, name.encode(), FLOAT32, ctypes.byref(value), ctypes.sizeof(value), 0
        )
        return value.value if found else None

    def property_matrix(self, name):
        """A FLOAT32_MAT4 device property as 16 floats, column-major, or None."""
        value = (ctypes.c_float * 16)()
        found = self.api.anariGetProperty(
            self.handle, self.handle, name.encode(), FLOAT32_MAT4, ctypes.byref(value), ctypes.sizeof(value), 0
        )
        return tuple(value) if found else None

    def property_string(self, name):
        value = ctypes.c_char_p()
        found = self.api.anariGetProperty(
            self.handle, self.handle, name.encode(), STRING, ctypes.byref(value), ctypes.sizeof(value), 0
        )
        return value.value.decode("utf-8", "replace") if found and value.value else None

    def property_pointer(self, name):
        """A VOID_POINTER device property as an int address, or None."""
        value = ctypes.c_void_p()
        found = self.api.anariGetProperty(
            self.handle, self.handle, name.encode(), VOID_POINTER, ctypes.byref(value), ctypes.sizeof(value), 0
        )
        return value.value if found and value.value else None

    def render(self, frame):
        self.api.anariRenderFrame(self.handle, frame)
        return bool(self.api.anariFrameReady(self.handle, frame, WAIT))

    def frame_color(self, frame):
        """Copy a completed frame's RGBA8 sRGB channel before unmapping it."""
        return self._frame_channel(frame, "channel.color", UFIXED8_RGBA_SRGB)

    def frame_depth(self, frame):
        """Copy a completed frame's float ray distances before unmapping them."""
        return self._frame_channel(frame, "channel.depth", FLOAT32)

    def _frame_channel(self, frame, channel, expected_type):
        """Copy one supported four-byte channel into owned storage."""
        width, height, data_type = ctypes.c_uint32(), ctypes.c_uint32(), ctypes.c_int()
        pointer = self.api.anariMapFrame(
            self.handle, frame, channel.encode(), ctypes.byref(width), ctypes.byref(height), ctypes.byref(data_type)
        )
        if not pointer:
            raise AnariError(f"Cannot map the ANARI frame {channel}")
        try:
            if data_type.value != expected_type:
                raise AnariError(f"Unexpected data type for ANARI frame {channel}")
            return width.value, height.value, ctypes.string_at(pointer, width.value * height.value * 4)
        finally:
            self.api.anariUnmapFrame(self.handle, frame, channel.encode())

    def close(self):
        if self.handle:
            self.api.anariRelease(self.handle, self.handle)
            self.handle = None
