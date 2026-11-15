# SPDX-License-Identifier: MIT
"""The add-on's served GUI panel: a page shipped in `panel/`, published over the stream's own QAR
session and shown beside the app volume to every user.

The qaros device owns the session. It publishes `qar.session`, `qar.appVolumeId` and
`qar.runtimeLibrary` once it streams; loading the runtime by that path gives this process the module
the device loaded, so the session pointer is valid here. Everything is closed before the device is
released. Structures and signatures follow qar_streaming_c/v0 (types.h, detail/gui_panels.h)."""
import ctypes
import math
import os
from pathlib import Path

PAGE_FOLDER = Path(__file__).with_name("panel")
BUNDLE_NAME = "blender"
ENTRY = "index.html"
PANEL_SIZE = (0.5, 0.62)  # metres, the whole window: a page of about 263 x 325 CSS pixels
PANEL_GAP = 0.05  # metres between the volume's side and the panel

ID_LENGTH = 16
BUNDLE_NAME_MAX = 64
STRUCTURE_TYPE_GUI_PANEL_INIT = 0x5001
STRUCTURE_TYPE_GUI_PANEL_SERVED_CONTENT_EXT = 0x5004


class StructureHeader(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int32), ("next", ctypes.c_void_p)]


class Id(ctypes.Structure):
    _fields_ = [("data", ctypes.c_uint8 * ID_LENGTH)]


class Quaternion(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float), ("z", ctypes.c_float), ("w", ctypes.c_float)]


class Vector3(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float), ("z", ctypes.c_float)]


class Pose(ctypes.Structure):
    _fields_ = [("orientation", Quaternion), ("position", Vector3)]


class PanelSize(ctypes.Structure):
    _fields_ = [("width_meters", ctypes.c_float), ("height_meters", ctypes.c_float), ("content_scale", ctypes.c_float)]


class VolumeSize(ctypes.Structure):
    _fields_ = [("width_meters", ctypes.c_float), ("length_meters", ctypes.c_float), ("height_meters", ctypes.c_float)]


class PanelInit(ctypes.Structure):
    _fields_ = [
        ("header", StructureHeader),
        ("common_name", ctypes.c_char_p),
        ("display_name", ctypes.c_char_p),
        ("pose", Pose),
        ("size", PanelSize),
        ("visible_to_peers", ctypes.c_void_p),
        ("visible_to_peer_count", ctypes.c_size_t),
    ]


class BundleId(ctypes.Structure):
    _fields_ = [("publisher", Id), ("name", ctypes.c_char * (BUNDLE_NAME_MAX + 1))]


class ServedContentExt(ctypes.Structure):
    _fields_ = [("header", StructureHeader), ("bundle", BundleId), ("entry", ctypes.c_char_p)]


class Result(ctypes.Structure):
    _fields_ = [("code", ctypes.c_int32), ("error_handle", ctypes.c_uint32)]


class Message(ctypes.Structure):
    _fields_ = [
        ("panel", Id),
        ("user", Id),
        ("sequence", ctypes.c_uint64),
        ("json", ctypes.c_char_p),
        ("json_size", ctypes.c_size_t),
    ]


MESSAGE_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.POINTER(Message), ctypes.c_void_p)
# Every message callback handed to QAR, kept for the life of the process: a library thread that was
# already inside the callback when the subscription was cancelled must never find it freed.
_CALLBACKS = []


class RayStyle(ctypes.Structure):
    _fields_ = [("line_style", ctypes.c_int32), ("color", ctypes.c_uint8 * 4)]


class MappingRule(ctypes.Structure):
    _fields_ = [
        ("header", StructureHeader),
        ("gesture_kind", ctypes.c_int32),
        ("enabled", ctypes.c_bool),
        ("precision", ctypes.c_float),
        ("app_scale_sensitivity_mode", ctypes.c_int32),
        ("translation_axes", ctypes.c_uint32),
        ("rotation_axes", ctypes.c_uint32),
        ("enable_gestures_from_outside_of_volume", ctypes.c_bool),
        ("controller_ray_style", RayStyle),
        ("app_transform_mapping", ctypes.c_int32),
    ]


MAX_MAPPING_RULES = 16


class GestureConfiguration(ctypes.Structure):
    _fields_ = [
        ("header", StructureHeader),
        ("mapping_rules", MappingRule * MAX_MAPPING_RULES),
        ("mapping_rule_count", ctypes.c_size_t),
    ]


# QarGestureKind, QarGesturePhase and QarGestureAppTransformMapping values (types.h).
GESTURE_CLICK, GESTURE_HOVER, GESTURE_ONE_HAND_6DOF = 0, 1, 2
GESTURE_TWO_HAND_SCALE, GESTURE_TWO_HAND_ROTATE = 3, 4
PHASE_STARTED, PHASE_UPDATED, PHASE_ENDED, PHASE_INSTANT, PHASE_CANCELED = 0, 1, 2, 3, 4
MAPPING_NONE, MAPPING_APP_POSE = 0, 1
SENSITIVITY_CONSTANT = 0
AXIS_NONE, AXIS_Y = 0, 2

GESTURE_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)


class QarError(RuntimeError):
    pass


_P = ctypes.c_void_p
_FUNCTIONS = {
    "qar_impl_result_is_success": (ctypes.c_bool, [Result]),
    "qar_impl_result_message": (None, [Result, ctypes.c_char_p, ctypes.c_size_t]),
    "qar_impl_panel_content_publish_directory": (Result, [_P, ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(BundleId)]),
    "qar_impl_panel_content_remove": (Result, [_P, ctypes.POINTER(BundleId)]),
    "qar_impl_gui_panels_get_or_create": (Result, [_P, ctypes.POINTER(PanelInit), ctypes.POINTER(Id)]),
    "qar_impl_gui_panels_close_panel": (Result, [_P, ctypes.POINTER(Id)]),
    "qar_impl_app_volume_get_latest_pose": (Result, [_P, _P, ctypes.POINTER(Pose)]),
    "qar_impl_app_volume_get_latest_size": (Result, [_P, _P, ctypes.POINTER(VolumeSize)]),
    "qar_impl_panel_content_subscribe_messages": (
        Result,
        [_P, ctypes.POINTER(BundleId), MESSAGE_CALLBACK, ctypes.c_void_p, _P],
    ),
    "qar_impl_panel_content_post_message": (
        Result,
        [_P, ctypes.POINTER(BundleId), ctypes.POINTER(Id), ctypes.POINTER(Id), ctypes.c_char_p],
    ),
    "qar_impl_app_volumes_subscribe_all_gesture_events": (Result, [_P, _P, GESTURE_CALLBACK, _P, _P]),
    "qar_impl_app_volume_gesture_get_lifecycle": (Result, [_P, ctypes.POINTER(Id), ctypes.POINTER(Id)]),
    "qar_impl_app_volume_gesture_get_classification": (
        Result,
        [_P, ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32)],
    ),
    "qar_impl_app_volume_gesture_get_points": (
        Result,
        [_P, ctypes.POINTER(Vector3), ctypes.POINTER(ctypes.c_bool), ctypes.POINTER(Vector3), ctypes.POINTER(ctypes.c_bool)],
    ),
    "qar_impl_app_volume_gesture_get_gesture_id": (Result, [_P, ctypes.POINTER(ctypes.c_uint64)]),
    "qar_impl_app_volume_gesture_get_mode": (Result, [_P, ctypes.POINTER(ctypes.c_uint32)]),
    "qar_impl_app_volume_gesture_get_handedness": (Result, [_P, ctypes.POINTER(ctypes.c_int32)]),
    "qar_impl_app_volume_gesture_get_deltas": (
        Result,
        [_P, ctypes.POINTER(Vector3), ctypes.POINTER(Quaternion), ctypes.POINTER(ctypes.c_bool)],
    ),
    "qar_impl_app_volume_get_latest_app_pose": (Result, [_P, _P, ctypes.POINTER(Pose)]),
    "qar_impl_app_volume_get_latest_app_scale": (Result, [_P, _P, ctypes.POINTER(ctypes.c_float)]),
    "qar_impl_query_app_volumes_count": (Result, [_P, ctypes.POINTER(ctypes.c_size_t)]),
    "qar_impl_query_app_volumes": (Result, [_P, ctypes.POINTER(_P), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]),
    "qar_impl_app_volume_get_id": (Result, [_P, ctypes.POINTER(Id)]),
    "qar_impl_app_volume_handle_destroy": (None, [_P]),
    "qar_impl_app_volume_get_gesture_configuration": (Result, [_P, ctypes.POINTER(GestureConfiguration)]),
    "qar_impl_app_volumes_change_gesture_configuration": (Result, [_P, _P, ctypes.POINTER(GestureConfiguration)]),
    "qar_impl_app_volumes_change_app_pose": (Result, [_P, _P, ctypes.POINTER(Pose)]),
    "qar_impl_app_volumes_change_app_scale": (Result, [_P, _P, ctypes.c_float]),
    "qar_impl_app_volumes_change_gesture_mode": (Result, [_P, _P, ctypes.c_uint32, ctypes.POINTER(GestureConfiguration)]),
    "qar_impl_app_volumes_remove_gesture_mode": (Result, [_P, _P, ctypes.c_uint32]),
    "qar_impl_app_volumes_select_gesture_mode": (Result, [_P, _P, ctypes.POINTER(Id), ctypes.c_uint32]),
    "qar_impl_app_volumes_clear_peer_gesture_mode": (Result, [_P, _P, ctypes.POINTER(Id)]),
    "qar_impl_app_volumes_clear_all_peer_gesture_modes": (Result, [_P, _P]),
    "qar_impl_cancel_token_create": (Result, [ctypes.POINTER(_P)]),
    "qar_impl_cancel_token_cancel": (Result, [_P]),
    "qar_impl_cancel_token_handle_destroy": (None, [_P]),
}


class Runtime:
    """The QAR runtime the device loaded, opened again by its path. Missing functions mean a
    runtime without served GUI panels: the caller gets a QarError naming them."""

    def __init__(self, path):
        self.dll = ctypes.CDLL(path)
        missing = []
        for name, (restype, argtypes) in _FUNCTIONS.items():
            function = getattr(self.dll, name, None)
            if function is None:
                missing.append(name)
                continue
            function.restype = restype
            function.argtypes = argtypes
        if missing:
            self.close()
            raise QarError("this QAROS runtime has no served GUI panels (missing " + ", ".join(missing) + ")")

    def call(self, name, *args):
        result = getattr(self.dll, name)(*args)
        if self.dll.qar_impl_result_is_success(result):
            return
        buffer = ctypes.create_string_buffer(1024)
        self.dll.qar_impl_result_message(result, buffer, len(buffer))
        raise QarError(f"{name[len('qar_impl_'):]}: {buffer.value.decode('utf-8', 'replace')} (status {result.code})")

    def close(self):
        """Drops this process's reference, so the device's unload really unloads the runtime."""
        if self.dll is None:
            return
        import _ctypes

        handle = self.dll._handle
        self.dll = None
        if os.name == "nt":
            _ctypes.FreeLibrary(handle)
        else:
            _ctypes.dlclose(handle)


def rotate(q, v):
    """v rotated by the unit quaternion q = (x, y, z, w)."""
    x, y, z, w = q
    tx = 2.0 * (y * v[2] - z * v[1])
    ty = 2.0 * (z * v[0] - x * v[2])
    tz = 2.0 * (x * v[1] - y * v[0])
    return (
        v[0] + w * tx + (y * tz - z * ty),
        v[1] + w * ty + (z * tx - x * tz),
        v[2] + w * tz + (x * ty - y * tx),
    )


def beside_volume(center, orientation, size, panel_width=PANEL_SIZE[0], gap=PANEL_GAP):
    """The panel's top-edge midpoint to the right of the volume, level with its top, in the
    volume's orientation. `size` is (width, length, height) in metres."""
    width, _, height = size
    offset = rotate(orientation, (width / 2.0 + gap + panel_width / 2.0, height / 2.0, 0.0))
    return tuple(c + o for c, o in zip(center, offset))


def common_name(volume_name):
    """The panel's identity key: ASCII, one per app volume, so a second Blender has its own."""
    ascii_name = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_.") else "-" for ch in volume_name)
    return f"{ascii_name or 'blender'}-controls"


class ServedPanel:
    """Publishes the page folder and shows it on one panel beside the app volume."""

    def __init__(self, runtime, session, volume_id, volume_name, display_name="Blender"):
        self.runtime = runtime
        self.session = ctypes.c_void_p(session)
        self.volume_id = ctypes.c_void_p(volume_id)
        self.bundle = None
        self.panel = None
        self.subscription = None
        try:
            bundle = BundleId()
            runtime.call(
                "qar_impl_panel_content_publish_directory",
                self.session,
                BUNDLE_NAME.encode(),
                str(PAGE_FOLDER).encode("utf-8"),
                ctypes.byref(bundle),
            )
            self.bundle = bundle
            self.panel = self._create(common_name(volume_name), display_name)
        except Exception:
            self.close()
            raise

    def _placement(self):
        pose = Pose()
        size = VolumeSize()
        self.runtime.call("qar_impl_app_volume_get_latest_pose", self.session, self.volume_id, ctypes.byref(pose))
        self.runtime.call("qar_impl_app_volume_get_latest_size", self.session, self.volume_id, ctypes.byref(size))
        q = (pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w)
        if not math.isclose(sum(c * c for c in q), 1.0, abs_tol=1e-3):
            q = (0.0, 0.0, 0.0, 1.0)
        center = (pose.position.x, pose.position.y, pose.position.z)
        return q, beside_volume(center, q, (size.width_meters, size.length_meters, size.height_meters))

    def _create(self, name, display_name):
        orientation, position = self._placement()
        served = ServedContentExt()
        served.header.type = STRUCTURE_TYPE_GUI_PANEL_SERVED_CONTENT_EXT
        served.bundle = self.bundle
        served.entry = ENTRY.encode()
        init = PanelInit()
        init.header.type = STRUCTURE_TYPE_GUI_PANEL_INIT
        init.header.next = ctypes.cast(ctypes.pointer(served), ctypes.c_void_p)
        init.common_name = name.encode("ascii")
        init.display_name = display_name.encode("utf-8")
        init.pose.orientation = Quaternion(*orientation)
        init.pose.position = Vector3(*position)
        init.size = PanelSize(PANEL_SIZE[0], PANEL_SIZE[1], 1.0)
        panel = Id()
        # A panel an earlier run left (a crash, or the hub restarted under Blender) is taken over:
        # the runtime keeps its place and points it at this run's bundle, named in `served`.
        self.runtime.call("qar_impl_gui_panels_get_or_create", self.session, ctypes.byref(init), ctypes.byref(panel))
        return panel

    def subscribe(self, handler):
        """Calls `handler(json_text, user_hex)` on a QAR library thread for every message a panel
        showing the page sends. The handler must not touch Blender: it hands the message on."""

        def receive(message, _state):
            try:
                text = ctypes.string_at(message.contents.json, message.contents.json_size)
                handler(text.decode("utf-8", "replace"), bytes(message.contents.user.data).hex())
            except Exception as error:  # noqa: BLE001 - nothing may escape into the C caller
                print(f"[qaros-anari] panel message dropped: {error!r}", flush=True)

        token = ctypes.c_void_p()
        self.runtime.call("qar_impl_cancel_token_create", ctypes.byref(token))
        callback = MESSAGE_CALLBACK(receive)
        _CALLBACKS.append(callback)
        try:
            self.runtime.call(
                "qar_impl_panel_content_subscribe_messages",
                self.session,
                ctypes.byref(self.bundle),
                callback,
                None,
                token,
            )
        except QarError:
            self.runtime.dll.qar_impl_cancel_token_handle_destroy(token)
            raise
        self.subscription = token

    def post(self, text):
        """Posts one JSON value to every user's copy of the page."""
        self.runtime.call(
            "qar_impl_panel_content_post_message",
            self.session,
            ctypes.byref(self.bundle),
            None,
            None,
            text.encode("utf-8"),
        )

    def close(self):
        """Closes the panel for every user and stops serving the page. Errors are collected, not
        raised: a closing stream must still release its device."""
        errors = []
        if self.subscription is not None:
            try:
                self.runtime.call("qar_impl_cancel_token_cancel", self.subscription)
            except QarError as error:
                errors.append(str(error))
            self.runtime.dll.qar_impl_cancel_token_handle_destroy(self.subscription)
            self.subscription = None
        if self.panel is not None:
            try:
                self.runtime.call("qar_impl_gui_panels_close_panel", self.session, ctypes.byref(self.panel))
            except QarError as error:
                errors.append(str(error))
            self.panel = None
        if self.bundle is not None:
            try:
                self.runtime.call("qar_impl_panel_content_remove", self.session, ctypes.byref(self.bundle))
            except QarError as error:
                errors.append(str(error))
            self.bundle = None
        return errors


MANIPULATION_KINDS = (GESTURE_ONE_HAND_6DOF, GESTURE_TWO_HAND_SCALE, GESTURE_TWO_HAND_ROTATE)


def _copy_rule(target, source):
    ctypes.memmove(ctypes.byref(target), ctypes.byref(source), ctypes.sizeof(MappingRule))


def app_decides_from(default):
    """A copy of the volume's default gesture configuration in which no gesture moves the app: QAROS
    reports each one and draws its ray as before, and the add-on decides what it does.
    Manipulations report hand metres (precision 1, constant sensitivity). A two-hand turn about the
    vertical is added after the two-hand spread when the default has none (QAROS's does not)."""
    config = GestureConfiguration()
    ctypes.memmove(ctypes.byref(config), ctypes.byref(default), ctypes.sizeof(GestureConfiguration))
    count = min(config.mapping_rule_count, MAX_MAPPING_RULES)
    kinds = [config.mapping_rules[index].gesture_kind for index in range(count)]
    for index in range(count):
        rule = config.mapping_rules[index]
        rule.app_transform_mapping = MAPPING_NONE
        if rule.gesture_kind in MANIPULATION_KINDS:
            rule.precision = 1.0
            rule.app_scale_sensitivity_mode = SENSITIVITY_CONSTANT
    if GESTURE_TWO_HAND_ROTATE in kinds or count >= MAX_MAPPING_RULES:
        return config
    at = kinds.index(GESTURE_TWO_HAND_SCALE) + 1 if GESTURE_TWO_HAND_SCALE in kinds else 0
    for index in range(count, at, -1):
        _copy_rule(config.mapping_rules[index], config.mapping_rules[index - 1])
    turn = config.mapping_rules[at]
    if at > 0:
        _copy_rule(turn, config.mapping_rules[at - 1])  # the spread's ray style and reach
    turn.gesture_kind = GESTURE_TWO_HAND_ROTATE
    turn.enabled = True
    turn.precision = 1.0
    turn.app_scale_sensitivity_mode = SENSITIVITY_CONSTANT
    turn.translation_axes = AXIS_NONE
    turn.rotation_axes = AXIS_Y
    turn.app_transform_mapping = MAPPING_NONE
    config.mapping_rule_count = count + 1
    return config


class VolumeGestures:
    """The app volume's gestures for the add-on: while it runs no gesture moves the app (QAROS only
    reports them, app_decides_from) and the add-on moves the view itself (change_app_transform).
    `handler(event)` runs on a QAR library thread with a plain dict (the gesture handle is only
    valid during the call)."""

    def __init__(self, runtime, session, volume_id, handler):
        self.runtime = runtime
        self.session = ctypes.c_void_p(session)
        self.volume_id = ctypes.c_void_p(volume_id)
        self.subscription = None
        self.replaced = None  # the volume's own default configuration, put back by close()
        try:
            self._take_over_gestures()
            self._subscribe(handler)
        except Exception:
            self.close()
            raise

    def _volume_handle(self):
        count = ctypes.c_size_t()
        self.runtime.call("qar_impl_query_app_volumes_count", self.session, ctypes.byref(count))
        handles = (ctypes.c_void_p * max(count.value, 1))()
        written = ctypes.c_size_t()
        self.runtime.call("qar_impl_query_app_volumes", self.session, handles, len(handles), ctypes.byref(written))
        wanted = bytes(ctypes.string_at(self.volume_id.value, ID_LENGTH))
        found = None
        for index in range(written.value):
            identity = Id()
            self.runtime.call("qar_impl_app_volume_get_id", ctypes.c_void_p(handles[index]), ctypes.byref(identity))
            if found is None and bytes(identity.data) == wanted:
                found = handles[index]
            else:
                self.runtime.dll.qar_impl_app_volume_handle_destroy(ctypes.c_void_p(handles[index]))
        if found is None:
            raise QarError("the stream's app volume is not in the session")
        return ctypes.c_void_p(found)

    def _take_over_gestures(self):
        handle = self._volume_handle()
        try:
            default = GestureConfiguration()
            self.runtime.call("qar_impl_app_volume_get_gesture_configuration", handle, ctypes.byref(default))
        finally:
            self.runtime.dll.qar_impl_app_volume_handle_destroy(handle)
        config = app_decides_from(default)
        self.runtime.call(
            "qar_impl_app_volumes_change_gesture_configuration", self.session, self.volume_id, ctypes.byref(config)
        )
        self.replaced = default

    def _subscribe(self, handler):
        dll = self.runtime.dll

        def receive(gesture, _state):
            try:
                peer, volume = Id(), Id()
                kind, phase = ctypes.c_int32(), ctypes.c_int32()
                action, start = Vector3(), Vector3()
                has_action, has_start = ctypes.c_bool(), ctypes.c_bool()
                gesture_id, mode, hand = ctypes.c_uint64(), ctypes.c_uint32(), ctypes.c_int32()
                translation, rotation, mapped = Vector3(), Quaternion(), ctypes.c_bool()
                dll.qar_impl_app_volume_gesture_get_lifecycle(gesture, ctypes.byref(peer), ctypes.byref(volume))
                dll.qar_impl_app_volume_gesture_get_classification(gesture, ctypes.byref(kind), ctypes.byref(phase))
                dll.qar_impl_app_volume_gesture_get_points(
                    gesture, ctypes.byref(action), ctypes.byref(has_action), ctypes.byref(start), ctypes.byref(has_start)
                )
                dll.qar_impl_app_volume_gesture_get_gesture_id(gesture, ctypes.byref(gesture_id))
                dll.qar_impl_app_volume_gesture_get_mode(gesture, ctypes.byref(mode))
                dll.qar_impl_app_volume_gesture_get_handedness(gesture, ctypes.byref(hand))
                dll.qar_impl_app_volume_gesture_get_deltas(
                    gesture, ctypes.byref(translation), ctypes.byref(rotation), ctypes.byref(mapped)
                )
                handler(
                    {
                        "peer": bytes(peer.data).hex(),
                        "kind": kind.value,
                        "phase": phase.value,
                        "gesture_id": gesture_id.value,
                        "mode": mode.value,
                        "hand": hand.value,
                        "translation": (translation.x, translation.y, translation.z),
                        "rotation": (rotation.x, rotation.y, rotation.z, rotation.w),
                        "mapped": mapped.value,
                        "start": (start.x, start.y, start.z) if has_start.value else None,
                        "action": (action.x, action.y, action.z) if has_action.value else None,
                    }
                )
            except Exception as error:  # noqa: BLE001 - nothing may escape into the C caller
                print(f"[qaros-anari] volume gesture dropped: {error!r}", flush=True)

        token = ctypes.c_void_p()
        self.runtime.call("qar_impl_cancel_token_create", ctypes.byref(token))
        callback = GESTURE_CALLBACK(receive)
        _CALLBACKS.append(callback)
        try:
            self.runtime.call(
                "qar_impl_app_volumes_subscribe_all_gesture_events", self.session, self.volume_id, callback, None, token
            )
        except QarError:
            self.runtime.dll.qar_impl_cancel_token_handle_destroy(token)
            raise
        self.subscription = token

    def app_transform(self):
        """(app orientation as x, y, z, w; app scale): how content sits in the volume now."""
        _position, orientation, scale = self.app_pose()
        return orientation, scale

    def app_pose(self):
        """(position, orientation as x, y, z, w, scale): the app transform now."""
        pose = Pose()
        scale = ctypes.c_float()
        self.runtime.call("qar_impl_app_volume_get_latest_app_pose", self.session, self.volume_id, ctypes.byref(pose))
        self.runtime.call("qar_impl_app_volume_get_latest_app_scale", self.session, self.volume_id, ctypes.byref(scale))
        q, p = pose.orientation, pose.position
        return (p.x, p.y, p.z), (q.x, q.y, q.z, q.w), scale.value

    def change_app_transform(self, position, orientation, scale):
        """Moves the view: every user sees the content at this app pose and scale."""
        pose = Pose()
        pose.orientation = Quaternion(*orientation)
        pose.position = Vector3(*position)
        self.runtime.call("qar_impl_app_volumes_change_app_pose", self.session, self.volume_id, ctypes.byref(pose))
        self.runtime.call("qar_impl_app_volumes_change_app_scale", self.session, self.volume_id, ctypes.c_float(scale))

    def close(self):
        """Stops the gestures and gives them back to QAROS: the volume's own configuration again.
        Errors are collected: a closing stream must still release its device."""
        errors = []
        if self.subscription is not None:
            try:
                self.runtime.call("qar_impl_cancel_token_cancel", self.subscription)
            except QarError as error:
                errors.append(str(error))
            self.runtime.dll.qar_impl_cancel_token_handle_destroy(self.subscription)
            self.subscription = None
        if self.replaced is not None:
            try:
                self.runtime.call(
                    "qar_impl_app_volumes_change_gesture_configuration",
                    self.session,
                    self.volume_id,
                    ctypes.byref(self.replaced),
                )
            except QarError as error:
                errors.append(str(error))
            self.replaced = None
        return errors
