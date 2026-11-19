# SPDX-License-Identifier: MIT
"""Streams the open Blender scene to every QAROS user through the `qaros` ANARI device.

The add-on owns one ANARI device for as long as streaming is on, independent of any viewport: the
scene is mirrored from depsgraph handlers, so Solid shading, other workspaces and closed 3D views do
not stop the stream (docs/DEVELOPING.md)."""
import atexit
import os
import sys
import threading
import time

import bpy
from bpy.app.handlers import persistent

from . import anari, blender_panels, blender_support, shading, targets, usd_import
from .hub_launch import (
    AutoStartOnce,
    StopRequestWatcher,
    auto_start_requested,
    preference_defaults,
    started_by_hub,
    stdin_is_pipe,
)
from .controls import Controls, outlined_names, selected_name
from .controls import _window_override as controls_window_override
from .gestures import TOOL_IDS, TOOL_NAVIGATE, TOOLS
from .protocol import panel_names, toggled_panel
from .materials import material_descriptions, principled_shader
from .panel import QarError, Runtime, ServedPanel, VolumeGestures
from .sync import GEOMETRY_TYPES, SceneCapture, SceneMirror, SyncWorker, scene_bounds, streams
from .sync import streaming_renderer as sync_streaming_renderer
from .volume_name import choose_volume, conflict_message, streaming_note

bl_info = {
    "name": "QAROS ANARI Streaming",
    "author": "Quaternar",
    "version": (0, 6, 0),
    "blender": (4, 5, 0),
    "location": "3D View > Sidebar > QAROS",
    "description": "Streams the scene to QAROS headsets through the qaros ANARI device",
    "category": "3D View",
}

LIBRARY_NAME = "qaros"
PUBLISH_FRAME_SIZE = (16, 16)
# How often Blender's thread looks for a stop request from QAROS. The launcher terminates the app one
# second after asking, so the quit has to start well inside that.
STOP_POLL_INTERVAL = 0.1
# Seconds after the add-on registered before the automatic Start: Blender's event loop runs timers
# only once the startup file and any file named on the command line are loaded.
AUTO_START_DELAY = 0.5
# How often Blender's thread looks whether the stream runs, to open the controls panel beside it.
PANEL_POLL_INTERVAL = 0.5
# How long the session's device list read from the device is reused by panel redraws.
TARGETS_CACHE_SECONDS = 0.5
# How often Blender's thread compares the 3D viewport's shading with the stream's while streaming.
SHADING_POLL_INTERVAL = 0.25


def log(text):
    print(f"[qaros-anari] {text}", flush=True)


# The Backend preference's "no backend named" value: qaros then chooses (QAR_ANARI_BACKEND unset),
# in the order the add-on gives it in QAR_ANARI_BACKEND_ORDER: VisGL first, for its physically based
# materials without grain. A qaros older than that variable ignores it and starts with VisRTX.
BACKEND_AUTOMATIC = "auto"
AUTOMATIC_ORDER = "visgl,visrtx,ospray,helide"
# The renderers qaros forwards to. Fixed numbers: Blender saves the number, so a saved choice keeps
# its backend when items are added. An empty identifier would make Automatic a heading that cannot
# be chosen. There is no Vulkan ANARI backend: VisGL renders with OpenGL into the stream's Vulkan
# images.
BACKEND_ITEMS = (
    (BACKEND_AUTOMATIC, "Automatic", "The first that works on this PC: VisGL, VisRTX, OSPRay, then Helide", 0),
    ("visgl", "VisGL",
     "GPU rasterizer (OpenGL 4.3) writing straight into the stream's Vulkan images. Fast and smooth; "
     "no World lighting, no selection outline", 2),
    ("visrtx", "VisRTX",
     "NVIDIA RTX ray tracer: physically based materials and World lighting. Needs an RTX GPU and "
     "QAROS's opt-in VisRTX component", 1),
    ("ospray", "OSPRay", "Intel CPU ray tracer: matte materials, half resolution by default", 4),
    ("helide", "Helide", "CPU reference renderer: matte materials, the slowest", 3),
)
BACKEND_LABELS = {item[0]: item[1] for item in BACKEND_ITEMS}
# QAR_ANARI_BACKEND as Blender started with it (tests, a developer): Automatic keeps it.
STARTUP_BACKEND = os.environ.get("QAR_ANARI_BACKEND")


def backend_environment(choice, environ, startup=None):
    """Points qaros at `choice` through the variables it reads when its library loads:
    QAR_ANARI_BACKEND names one backend; Automatic leaves the one Blender started with (else none)
    and gives the add-on's order, unless one was set before Blender started."""
    if choice != BACKEND_AUTOMATIC:
        environ["QAR_ANARI_BACKEND"] = choice
        return
    if startup:
        environ["QAR_ANARI_BACKEND"] = startup
    else:
        environ.pop("QAR_ANARI_BACKEND", None)
    environ.setdefault("QAR_ANARI_BACKEND_ORDER", AUTOMATIC_ORDER)
# The VisRTX materials preference: physically based (metal, highlights, grain in a moving view) or
# matte (smooth).
VISRTX_PHYSICAL = "physical"
VISRTX_MATTE = "matte"


# qar.renderRate: frames per second per user, in [RENDER_RATE_MIN, RENDER_RATE_MAX]; the device's
# own default is RENDER_RATE_DEFAULT.
RENDER_RATE_DEFAULT = 30
RENDER_RATE_MIN = 1
RENDER_RATE_MAX = 120


class Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    anari_folder: bpy.props.StringProperty(
        name="ANARI folder",
        description="Folder with anari.dll and anari_library_qaros.dll. Empty: the ANARI device of the QAROS that started Blender, else the QAROS setup's ANARI component on the system path",
        subtype="DIR_PATH",
    )
    backend: bpy.props.EnumProperty(
        name="Renderer",
        description=(
            "ANARI back end that renders the stream. Applies when streaming starts; the QAROS tab "
            "restarts a running stream on it"
        ),
        items=BACKEND_ITEMS,
        default=BACKEND_AUTOMATIC,
    )
    max_users: bpy.props.IntProperty(
        name="Users", description="Most devices served at once. Applies when streaming starts", default=2, min=1, max=32
    )
    render_rate: bpy.props.IntProperty(
        name="Render rate",
        description=(
            "Frames per second each user's headset gets. Lower leaves the renderer time for more users; "
            "scene changes then show later, head motion stays smooth. Applies at once, also while streaming"
        ),
        default=RENDER_RATE_DEFAULT,
        min=RENDER_RATE_MIN,
        max=RENDER_RATE_MAX,
        update=lambda prefs, context: STREAMER.apply_render_rate(prefs.render_rate),
    )
    app_scale: bpy.props.FloatProperty(
        name="Scale", description="Room metres per Blender unit", default=0.25, min=0.001, max=100.0
    )
    render_scale: bpy.props.FloatProperty(
        name="Render scale",
        description=(
            "Share of the headset's resolution each eye renders at, upscaled for the headset. "
            "0: automatic, half on the CPU backends (OSPRay, Helide), full on the GPU backends"
        ),
        default=0.0,
        min=0.0,
        max=1.0,
        step=5,
        subtype="FACTOR",
    )
    near: bpy.props.FloatProperty(name="Near", description="Depth range in Blender units", default=0.1, min=0.0001)
    far: bpy.props.FloatProperty(name="Far", description="Depth range in Blender units", default=100.0, min=0.01)
    ambient: bpy.props.FloatProperty(
        name="Ambient light",
        description="Light from every direction, added to the scene's lights. Applies when streaming starts",
        default=0.3, min=0.0, max=10.0,
    )
    visrtx_materials: bpy.props.EnumProperty(
        name="VisRTX materials",
        description="How VisRTX shades Blender materials. Applies when streaming starts",
        items=(
            (VISRTX_MATTE, "Matte (smooth)", "Colour only: no grain, no metal or highlights", 0),
            (VISRTX_PHYSICAL, "Physically based (metal, grain)",
             "Metallic, roughness and emission, with grain on surfaces lit by the ambient light", 1),
        ),
        default=VISRTX_PHYSICAL,
    )
    tool: bpy.props.EnumProperty(
        name="Tool",
        description=(
            "What hand gestures in the app volume do, for every user; also chosen on the Tools tab of "
            "the controls panel in the headset. A double tap selects with either tool"
        ),
        items=tuple((tool, name, description, index) for index, (tool, name, description) in enumerate(TOOLS)),
        default=TOOL_NAVIGATE,
    )
    # What the controls panel in the headset shows (docs/DEVELOPING.md).
    show_tools_tab: bpy.props.BoolProperty(name="Tools", description="Navigation or Object selection tab", default=True)
    show_object_tab: bpy.props.BoolProperty(name="Object", description="Location, rotation and scale tab", default=True)
    show_material_tab: bpy.props.BoolProperty(name="Material", description="Base colour, roughness and metallic tab", default=True)
    show_frame_tab: bpy.props.BoolProperty(name="Frame", description="Frame and Play tab", default=True)
    show_panels_tab: bpy.props.BoolProperty(name="Panels", description="Blender's own panels, chosen below", default=True)
    controls_panels: bpy.props.StringProperty(
        name="Panels",
        description="Blender panel classes the Panels tab shows, in order",
        default=",".join(blender_panels.DEFAULT_PANELS),
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "anari_folder")
        layout.prop(self, "backend")
        row = layout.row()
        row.prop(self, "max_users")
        row.prop(self, "render_rate")
        row.prop(self, "render_scale")
        row = layout.row()
        row.prop(self, "app_scale")
        row.prop(self, "near")
        row.prop(self, "far")
        layout.prop(self, "ambient")
        layout.prop(self, "visrtx_materials")
        layout.prop(self, "tool")
        self.draw_controls_panel(layout.box())

    def draw_controls_panel(self, box):
        box.label(text="Controls panel in the headset")
        row = box.row(align=True)
        for name in ("show_tools_tab", "show_object_tab", "show_material_tab", "show_frame_tab", "show_panels_tab"):
            row.prop(self, name, toggle=True)
        if not self.show_panels_tab:
            return
        chosen = panel_names(self.controls_panels)
        order = ", ".join(blender_panels.panel_title(name) for name in chosen) or "none"
        row = box.row()
        row.label(text=f"Panels tab shows: {order}")
        row.operator(QAROS_OT_reset_controls_panels.bl_idname, text="", icon="LOOP_BACK")
        for heading, panels in blender_panels.available_panels():
            if not panels:
                continue
            column = box.column()
            column.label(text=heading)
            flow = column.grid_flow(row_major=True, columns=3, even_columns=True, align=True)
            for name, title in panels:
                button = flow.operator(
                    QAROS_OT_toggle_controls_panel.bl_idname, text=title, depress=name in chosen
                )
                button.panel = name


def controls_settings():
    """(switched-on tabs, chosen panel class names) from the preferences: read every tick."""
    prefs = preferences()
    switches = {
        "tools": prefs.show_tools_tab,
        "object": prefs.show_object_tab,
        "material": prefs.show_material_tab,
        "frame": prefs.show_frame_tab,
        "panels": prefs.show_panels_tab,
    }
    return tuple(tab for tab, on in switches.items() if on), panel_names(prefs.controls_panels)


def gesture_tool():
    """The Tool preference: read every tick. An id this add-on does not know reads as Navigation."""
    tool = preferences().tool
    return tool if tool in TOOL_IDS else TOOL_NAVIGATE


def set_gesture_tool(tool):
    """The Tools tab in the headset chose `tool`: the preference, so Blender's UI shows it too."""
    prefs = preferences()
    if prefs.tool == tool:
        return
    prefs.tool = tool
    if bpy.context.preferences.addons.get(__package__) is not None:
        bpy.context.preferences.is_dirty = True
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in ("VIEW_3D", "PREFERENCES"):
                area.tag_redraw()


class QAROS_OT_toggle_controls_panel(bpy.types.Operator):
    bl_idname = "qaros.toggle_controls_panel"
    bl_label = "Show in the headset"
    bl_description = "Show or hide this Blender panel in the headset's controls panel (shown last when added)"
    bl_options = {"INTERNAL"}

    panel: bpy.props.StringProperty()

    def execute(self, context):
        prefs = preferences()
        prefs.controls_panels = toggled_panel(prefs.controls_panels, self.panel)
        context.preferences.is_dirty = True
        return {"FINISHED"}


class QAROS_OT_set_tool(bpy.types.Operator):
    bl_idname = "qaros.set_tool"
    bl_label = "Tool"
    bl_description = "What hand gestures in the app volume do, for every user (also the Tools tab in the headset)"
    bl_options = {"INTERNAL"}

    tool: bpy.props.EnumProperty(
        items=tuple((tool, name, description, index) for index, (tool, name, description) in enumerate(TOOLS))
    )

    def execute(self, context):
        set_gesture_tool(self.tool)
        return {"FINISHED"}


class QAROS_OT_reset_controls_panels(bpy.types.Operator):
    bl_idname = "qaros.reset_controls_panels"
    bl_label = "Default panels"
    bl_description = "Show Transform, Relations and Visibility again"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        preferences().controls_panels = ",".join(blender_panels.DEFAULT_PANELS)
        context.preferences.is_dirty = True
        return {"FINISHED"}


# qar.renderScale takes 0 (automatic) or [RENDER_SCALE_MIN, 1]; a slider value below the minimum
# other than 0 is raised to it rather than failing the stream.
RENDER_SCALE_MIN = 0.25


def render_scale_parameter(value):
    if value <= 0.0:
        return 0.0
    return min(1.0, max(RENDER_SCALE_MIN, value))


# The preferences of a session whose Blender preferences do not list the add-on: built once, so the
# controls panel choices made in that session (toggle, reset) hold until Blender quits.
SESSION_PREFERENCES = None


def preferences():
    """The add-on's preferences, or their defaults when Blender's preferences do not list the add-on:
    loaded by ``--addons`` alone (a Blender QAROS started whose preferences never enabled it), it
    has no entry there (hub_launch.preference_defaults)."""
    global SESSION_PREFERENCES
    entry = bpy.context.preferences.addons.get(__package__)
    if entry is not None:
        return entry.preferences
    if SESSION_PREFERENCES is None:
        SESSION_PREFERENCES = preference_defaults(Preferences.__annotations__)
    return SESSION_PREFERENCES


def scene_targets(scene):
    """The scene's Streams to choice (saved with the .blend; docs/user-guide.md)."""
    return targets.TargetChoice.from_scene(
        getattr(scene, "qaros_stream_to_all", True), getattr(scene, "qaros_stream_targets", "")
    )


def store_scene_targets(scene, choice):
    scene.qaros_stream_to_all = choice.stream_to_all
    text = choice.selected_text()
    if scene.qaros_stream_targets != text:
        scene.qaros_stream_targets = text


def tag_redraw():
    """Redraws every area: the tab's status changes with no input event."""
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    except Exception:  # noqa: BLE001 - background mode has no windows
        pass


# Back ends that compile GPU programs on their first device: VisRTX builds its OptiX modules, which takes
# about two minutes while the OptiX disk cache is empty (a new PC, a new driver, a cleared cache) and a
# fraction of a second once it is filled. Building them on Blender's UI thread froze Blender for that long.
WARM_BACKENDS = frozenset({"visrtx"})
WARMUP_TIMEOUT_S = 600.0
SWITCH_POLL_INTERVAL = 0.25
LIBRARY_LOCK = threading.Lock()


class Warmup:
    """One throwaway device on a back end, built and rendered once on a thread of its own, streaming
    off: it fills the back end's compile cache so the real device starts at once. Never touches bpy."""

    def __init__(self, key, folder, on_status):
        self.key = key
        self.error = None
        self.started = time.monotonic()
        self.thread = threading.Thread(
            target=self.run, args=(folder, on_status), name=f"qaros-anari-warm-{key}", daemon=True
        )
        self.thread.start()

    @property
    def alive(self):
        return self.thread.is_alive()

    def run(self, folder, on_status):
        try:
            library = Streamer.library_for(self.key, folder, on_status)
            device = library.new_device()
            try:
                device.commit()
                subtype = sync_streaming_renderer(device.property_string("qar.backend") or self.key)[0]
                camera = device.new("Camera", "perspective")
                renderer = device.new("Renderer", subtype)
                world = device.new("World")
                frame = device.new("Frame")
                for handle in (camera, renderer, world):
                    device.commit(handle)
                device.set(frame, "world", anari.WORLD, world)
                device.set(frame, "renderer", anari.RENDERER, renderer)
                device.set(frame, "camera", anari.CAMERA, camera)
                device.set(frame, "size", anari.UINT32_VEC2, (64, 64))
                device.set(frame, "channel.color", anari.DATA_TYPE, anari.UFIXED8_RGBA_SRGB)
                device.commit(frame)
                device.render(frame)
                for handle in (frame, world, renderer, camera):
                    device.release(handle)
            finally:
                device.close()
        except Exception as error:  # noqa: BLE001 - shown in the tab by the switch
            self.error = str(error) or type(error).__name__


class Switch:
    """A running stream waiting for the new Renderer's back end to be ready."""

    def __init__(self, key, previous, warmup):
        self.key = key
        self.previous = previous
        self.warmup = warmup


class Streamer:
    """The one stream of this Blender process. An ANARI library is loaded once per Renderer and
    kept: qaros keeps the QAR runtime loaded for the life of the process, and it chooses its backend
    when its library loads, so another Renderer loads `qaros` again beside the first (each load is
    a library of its own with its own backend). Every load comes from the ANARI folder of the first:
    another anari.dll in the same process would be a second ANARI runtime."""

    library = None  # the library the running or last stream used
    libraries = {}  # Renderer preference -> anari.Library
    folder = None  # the ANARI folder preference the first library loaded from
    warmed = set()  # Renderer preferences whose first device has been built (compile cache filled)
    warmups = {}  # Renderer preference -> the Warmup in flight or failed

    def __init__(self):
        self.device = None
        self.backend = None
        self.library_key = None  # the Renderer preference of the running stream
        self.visrtx_matte = False  # the VisRTX materials choice of the running stream
        self.mirror = None
        self.capture = None
        self.worker = None
        self.camera = None
        self.frame = None
        self.joining = None
        self.scene_name = None
        self.error = None
        self.switching = None  # the Switch waiting for the new back end; the old one keeps streaming
        self.switch_error = None  # why the last Renderer switch fell back (the old one still streams)
        self.stopping = False
        self.volume = None  # the VolumeChoice of the running device
        self.runtime = None  # the QAR runtime the device loaded, for the served panel
        self.panel = None  # the served controls panel beside the app volume
        self.controls = None  # applies what users change on the panel
        self.panel_error = None
        self.targets_parameter = None  # the qar.targets value last given to the device
        self.targets_cache = (0.0, None)  # (read at, SessionTargets or None)
        self.selection = None  # the object the stream outlines (qar.selection), as last handed over

    @property
    def running(self):
        return self.device is not None

    def volume_known(self):
        """Whether the device knows where its app volume is (qar.volumeCube answers): before that, a
        fit has nothing to change. Answered without the backend gate, so panel redraws never wait."""
        return self.device is not None and self.device.property_matrix("qar.volumeCube") is not None

    def status(self):
        return self.state()[1]

    def state(self):
        """(icon, text, alert) of the stream, for the QAROS tab's status row and header."""
        if self.error:
            return "ERROR", self.error, True
        if not self.running:
            return "UNLINKED", "Off", False
        if self.switching is not None:
            waited = round(time.monotonic() - self.switching.warmup.started)
            label = BACKEND_LABELS.get(self.switching.key, self.switching.key)
            previous = BACKEND_LABELS.get(self.switching.previous, self.switching.previous)
            text = f"Starting {label}... the first start can take a few minutes ({waited} s). Still on {previous}"
            return "SORTTIME", text, False
        if self.switch_error:
            return "ERROR", self.switch_error, True
        if self.stopping:
            return "SORTTIME", "Stopping (waiting for QAROS to answer)", False
        if self.joining is not None and self.joining.is_alive():
            return "SORTTIME", "Joining QAROS (first run: allow Blender in QAROS)", False
        if not self.device.property_int("qar.streaming", anari.BOOL):
            reason = self.device.property_string("qar.streamError")
            conflict = conflict_message(self.volume, reason)
            if conflict:
                return "ERROR", conflict, True
            text = f"Not streaming: {reason}" if reason else "Not streaming: joining QAROS failed (see the console)"
            return "ERROR", text, True
        users = self.device.property_int("qar.activeUsers", anari.UINT32) or 0
        text = f"Streaming to {users} user{'s' if users != 1 else ''}"
        scale = self.device.property_float("qar.renderScale")
        if scale is not None and scale < 1.0:
            text += f" at {round(scale * 100)} % resolution"
        return "LINKED", text, False

    @classmethod
    def library_for(cls, key, folder, on_status):
        """The `qaros` library for the Renderer preference `key`, loaded on first use (any thread)."""
        with LIBRARY_LOCK:
            library = cls.libraries.get(key)
            if library is None:
                folder = folder if cls.folder is None else cls.folder
                backend_environment(key, os.environ, STARTUP_BACKEND)
                library = anari.Library(folder, LIBRARY_NAME, on_status)
                cls.libraries[key] = library
                cls.folder = folder
            return library

    def load_library(self, prefs):
        """The `qaros` library for the Renderer preference, loaded on first use."""
        key = prefs.backend
        library = Streamer.library_for(key, prefs.anari_folder, self.on_status)
        Streamer.library = library
        self.library_key = key
        return library

    def restart_reasons(self):
        """The preferences that apply when streaming starts and differ from the running stream's."""
        if not self.running or self.switching is not None:
            return ()
        prefs = preferences()
        reasons = []
        if prefs.backend != self.library_key:
            reasons.append("Renderer")
        if self.backend == "visrtx" and (prefs.visrtx_materials == VISRTX_MATTE) != self.visrtx_matte:
            reasons.append("VisRTX materials")
        return tuple(reasons)

    def can_restart(self):
        """A stream that has joined QAROS can stop at once; one still joining cannot."""
        return (
            self.running and not self.stopping and self.switching is None
            and not (self.joining is not None and self.joining.is_alive())
        )

    def restart(self, context):
        """Stops and starts again with the current preferences (a new device on the chosen
        renderer, into the same app volume). False when it cannot stop at once."""
        if not self.can_restart():
            return False
        self.switch_error = None
        key = preferences().backend
        if key != self.library_key and key in WARM_BACKENDS and key not in Streamer.warmed:
            # The new back end's first start is slow: build it beside the running stream, off
            # Blender's UI thread, and switch when it is ready.
            self.begin_switch(key)
            return True
        self.restart_now(context)
        return True

    def restart_now(self, context):
        log("restarting the stream")
        self.stop(wait=True)
        self.start(context)

    def prewarm(self, key):
        """Starts building `key`'s first device in the background (nothing streams on it): its
        first Start is then quick. None for a back end without a slow first start."""
        if key not in WARM_BACKENDS or key in Streamer.warmed:
            return None
        warmup = Streamer.warmups.get(key)
        if warmup is None or (not warmup.alive and warmup.error):
            log(f"preparing {key} in the background")
            warmup = Streamer.warmups[key] = Warmup(key, preferences().anari_folder, self.on_status)
        return warmup

    def begin_switch(self, key):
        warmup = self.prewarm(key)
        if warmup is None:  # warmed in the meantime
            self.restart_now(bpy.context)
            return
        self.switching = Switch(key, self.library_key, warmup)
        log(f"switching to {key}: it starts in the background; {self.library_key} keeps streaming")
        bpy.app.timers.register(self.poll_switch, first_interval=SWITCH_POLL_INTERVAL)

    def abandon_switch(self, reason):
        """The new back end did not come up: the Renderer preference returns to the one still
        streaming, and the tab says why."""
        switch, self.switching = self.switching, None
        preferences().backend = switch.previous
        label = BACKEND_LABELS.get(switch.key, switch.key)
        previous = BACKEND_LABELS.get(switch.previous, switch.previous)
        self.switch_error = f"{label} did not start: {reason}. Still on {previous}"
        log(self.switch_error)

    def poll_switch(self):
        """Timer: finishes or abandons the switch. The stream restarts on the new back end only once
        its first device exists, so the restart takes the time of a cached start."""
        switch = self.switching
        if switch is None:
            return None
        if not self.running or self.stopping:
            self.switching = None
            return None
        warmup = switch.warmup
        if warmup.alive:
            if time.monotonic() - warmup.started > WARMUP_TIMEOUT_S:
                self.abandon_switch(f"no answer after {round(WARMUP_TIMEOUT_S)} s")
                tag_redraw()
                return None
            tag_redraw()
            return SWITCH_POLL_INTERVAL
        if warmup.error:
            Streamer.warmups.pop(switch.key, None)
            self.abandon_switch(warmup.error)
            tag_redraw()
            return None
        Streamer.warmed.add(switch.key)
        self.switching = None
        try:
            self.restart_now(bpy.context)
        except Exception as error:  # noqa: BLE001 - back to the renderer that worked
            log(f"restart on {switch.key} failed: {error!r}")
            if self.running:
                self.stop(wait=True)
            preferences().backend = switch.previous
            try:
                self.start(bpy.context)
                label = BACKEND_LABELS.get(switch.key, switch.key)
                previous = BACKEND_LABELS.get(switch.previous, switch.previous)
                self.switch_error = f"{label} did not start: {error}. Back on {previous}"
            except Exception as again:  # noqa: BLE001 - shown in the tab
                self.error = f"Cannot start: {again}"
        tag_redraw()
        return None

    def follow_shading(self):
        """Gives the stream the shading the QAROS tab asks for (the 3D viewport's by default): a
        change rebuilds the whole scene in the new shading."""
        if not self.running or self.stopping or self.capture is None or self.worker is None:
            return
        wanted = wanted_shading()
        current = self.capture.shading
        if wanted == current and wanted.studio == current.studio:
            return
        self.capture.shading = wanted
        if wanted == current:
            # Only the studio HDRI's intensity or rotation: the lights again, no geometry.
            depsgraph = bpy.context.evaluated_depsgraph_get()
            self.worker.submit(self.capture.capture(depsgraph, (), False))
            return
        log(f"shading: {shading.LABELS[wanted.mode]}")
        self.resync()

    def start(self, context):
        prefs = preferences()
        self.error = None
        self.switch_error = None
        device = self.load_library(prefs).new_device()
        # qar.* parameters are read when streaming starts; with no onboarding set, qaros joins the
        # QAROS on this PC by local enrollment.
        device.set(device.handle, "qar.stream", anari.BOOL, 1)
        device.set(device.handle, "qar.maxUsers", anari.INT32, prefs.max_users)
        # A qaros older than qar.renderRate reports it as unknown and renders at its own 30.
        device.set(device.handle, "qar.renderRate", anari.FLOAT32, float(prefs.render_rate))
        device.set(device.handle, "qar.renderScale", anari.FLOAT32, render_scale_parameter(prefs.render_scale))
        device.set(device.handle, "qar.appScale", anari.FLOAT32, prefs.app_scale)
        device.set(device.handle, "qar.near", anari.FLOAT32, prefs.near)
        device.set(device.handle, "qar.far", anari.FLOAT32, prefs.far)
        self.volume = choose_volume(getattr(context.scene, "qaros_volume_name", ""))
        device.set(device.handle, "qar.volume", anari.STRING, self.volume.name)
        device.set(device.handle, "qar.volumeDisplayName", anari.STRING, self.volume.display_name)
        device.set(device.handle, "qar.volumeNumbering", anari.BOOL, 1 if self.volume.numbering else 0)
        # Unset (the default) streams to every device; a QAROS without target choice never sees it.
        self.targets_parameter = scene_targets(context.scene).parameter()
        if self.targets_parameter is not None:
            device.set(device.handle, "qar.targets", anari.STRING, self.targets_parameter)
        device.commit()
        self.device = device
        self.targets_cache = (0.0, None)
        # The backend the library chose when it loaded (Renderer preference).
        self.backend = device.property_string("qar.backend")
        log(f"backend: {self.backend or 'unknown'}")
        self.visrtx_matte = prefs.visrtx_materials == VISRTX_MATTE
        # Objects outside the app volume are not rendered (qar.volumeCube, polled by the worker).
        self.mirror = SceneMirror(
            device, self.backend, lambda: device.property_matrix("qar.volumeCube"),
            visrtx_matte=self.visrtx_matte,
        )
        log(f"renderer: {self.mirror.renderer_subtype}")
        log("world: streamed as an hdri light" if self.mirror.environment_support
            else "world: not streamed (the renderer has no hdri light)")
        support = self.mirror.material_support
        log(support.status)
        if support.fallback_reason:
            log(f"material fallback: {support.fallback_reason}")
        if support.missing_parameters:
            log(f"material model lacks parameters: {', '.join(support.missing_parameters)}")
        if not support.supports_blend:
            log("material model does not advertise alpha blending")
        self.mirror.configure_renderer(prefs.ambient, (0.0, 0.0, 0.0, 0.0))
        self.capture = SceneCapture(lambda warning: log(warning), support)
        self.capture.shading = wanted_shading()
        log(f"shading: {shading.LABELS[self.capture.shading.mode]}")
        self.scene_name = context.scene.name
        # Applied here: nothing renders yet, so nothing holds the scene lock. Later syncs go
        # through the worker, so the UI thread never waits for a target's render.
        self.mirror.apply(self.capture.capture(context.evaluated_depsgraph_get(), (), True))
        self.worker = SyncWorker(self.mirror, lambda error: log(f"sync failed: {error!r}"))
        self.selection = None
        self.follow_selection()
        self.publish()
        add_handlers()
        bpy.app.timers.register(self.open_panel, first_interval=PANEL_POLL_INTERVAL)
        if not bpy.app.timers.is_registered(watch_shading):
            bpy.app.timers.register(watch_shading, first_interval=SHADING_POLL_INTERVAL)
        log(f"streaming '{self.scene_name}': {self.mirror.objects} objects, {self.mirror.triangles} triangles")

    def apply_render_rate(self, rate):
        """Hands a changed Render rate preference to the running device: every user's next frame
        renders at it."""
        if not self.running or self.stopping or self.device is None:
            return
        self.device.set(self.device.handle, "qar.renderRate", anari.FLOAT32, float(rate))
        self.device.commit()
        log(f"render rate {rate} frames per second per user")

    def apply_targets(self, choice):
        """Hands a changed Streams to choice to the running device: its next commit stops the
        devices no longer chosen and starts the newly chosen ones."""
        if not self.running or self.stopping:
            return
        parameter = choice.parameter()
        if parameter == self.targets_parameter:
            return
        if parameter is None:
            self.device.unset(self.device.handle, "qar.targets")
            log("streaming to every device")
        else:
            self.device.set(self.device.handle, "qar.targets", anari.STRING, parameter)
            log(f"streaming to {len(choice.selected)} selected device(s)")
        self.device.commit()
        self.targets_parameter = parameter
        self.targets_cache = (0.0, None)

    def session_targets(self):
        """The session's devices as the device reports them, or None (not streaming yet, or a
        QAROS without target choice). Read at most every TARGETS_CACHE_SECONDS: panels redraw on
        every mouse move."""
        if not self.running or self.stopping:
            return None
        read_at, cached = self.targets_cache
        now = time.monotonic()
        if now - read_at < TARGETS_CACHE_SECONDS:
            return cached
        cached = targets.parse_session_targets(self.device.property_string("qar.sessionTargets"))
        self.targets_cache = (now, cached)
        return cached

    def publish(self):
        """qaros streams the world and renderer of the application's last rendered frame, and
        starts its stream service on the first one. Rendering once is enough: later edits commit
        on the same world. The first render joins QAROS, which can wait for the user's approval,
        so it runs off Blender's UI thread (qaros onboards outside its scene lock)."""
        device = self.device
        self.camera = device.new("Camera", "perspective")
        device.set(self.camera, "position", anari.FLOAT32_VEC3, (0.0, 0.0, 5.0))
        device.commit(self.camera)
        self.frame = device.new("Frame")
        device.set(self.frame, "world", anari.WORLD, self.mirror.world)
        device.set(self.frame, "renderer", anari.RENDERER, self.mirror.renderer)
        device.set(self.frame, "camera", anari.CAMERA, self.camera)
        device.set(self.frame, "size", anari.UINT32_VEC2, PUBLISH_FRAME_SIZE)
        device.set(self.frame, "channel.color", anari.DATA_TYPE, anari.UFIXED8_RGBA_SRGB)
        device.commit(self.frame)
        self.joining = threading.Thread(target=device.render, args=(self.frame,), name="qaros-anari-join", daemon=True)
        self.joining.start()

    def open_panel(self):
        """Opens the controls panel once the device streams: the panel needs the stream's session
        and app volume. A failure leaves the stream running and is reported once."""
        if not self.running or self.stopping or self.panel is not None or self.panel_error:
            return None
        session = self.device.property_pointer("qar.session")
        volume_id = self.device.property_pointer("qar.appVolumeId")
        library = self.device.property_string("qar.runtimeLibrary")
        if not (session and volume_id and library):
            return PANEL_POLL_INTERVAL
        try:
            self.runtime = Runtime(library)
            self.panel = ServedPanel(self.runtime, session, volume_id, self.volume.name, self.volume.display_name)
            self.controls = Controls(self.panel, controls_settings, gesture_tool, set_gesture_tool)
            log("controls panel opened beside the app volume")
            try:
                self.controls.attach_gestures(
                    VolumeGestures(self.runtime, session, volume_id, self.controls.receive_gesture)
                )
                log("hand gestures in the app volume: Blender moves the view or the selected object, by the tool")
            except (QarError, OSError) as error:
                # The panel works without it: only picking in the volume is missing.
                log(f"pointing into the app volume not available: {error}")
        except (QarError, OSError) as error:
            self.panel_error = str(error)
            log(f"controls panel not opened: {error}")
            self.close_panel()
        return None

    def close_panel(self):
        if self.controls is not None:
            self.controls.close()
            self.controls = None
        if self.panel is not None:
            for error in self.panel.close():
                log(f"closing the controls panel: {error}")
            self.panel = None
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None

    def stop(self, wait=False):
        """Stops syncing at once. Joining cannot be cancelled and releasing the device under it
        would free what it uses, so while it still waits for QAROS (an unanswered approval lasts
        a minute) the release finishes from a timer instead of freezing Blender."""
        self.switching = None
        remove_handlers()
        if self.joining is not None and self.joining.is_alive() and not wait:
            self.stopping = True
            bpy.app.timers.register(self.finish_stop, first_interval=0.5)
            log("stopping once QAROS answers the join")
            return
        self.release()

    def finish_stop(self):
        if self.joining is not None and self.joining.is_alive():
            return 0.5
        self.release()
        return None

    def release(self):
        if self.worker is not None:
            self.worker.close()
            self.worker = None
        if self.joining is not None:
            self.joining.join()
            self.joining = None
        self.stopping = False
        self.capture = None
        if self.mirror is not None:
            self.mirror.close()
            self.mirror = None
        # The panel lives on the device's session: it is closed while that session still exists.
        self.close_panel()
        self.panel_error = None
        if self.device is not None:
            self.device.release(self.frame)
            self.device.release(self.camera)
            self.frame = self.camera = None
            # Releasing the device ends the stream service: users stop receiving this scene.
            self.device.close()
            self.device = None
        log("stopped")

    def sync(self, scene, depsgraph):
        if not self.running:
            return
        dirty = set()
        materials = False
        for update in depsgraph.updates:
            datablock = update.id
            if isinstance(datablock, bpy.types.Object) and update.is_updated_geometry:
                dirty.add(datablock.original.name)
            elif isinstance(datablock, (bpy.types.Material, bpy.types.NodeTree)):
                materials = True
            elif isinstance(datablock, (bpy.types.Mesh, bpy.types.Curve, bpy.types.MetaBall)):
                data = datablock.original
                dirty.update(obj.name for obj in bpy.data.objects if obj.data == data)
        if scene.name != self.scene_name:
            self.apply_targets(scene_targets(scene))
        full = scene.name != self.scene_name or self.worker.take_failure()
        self.scene_name = scene.name
        descriptions = material_descriptions(bpy.data.materials) if materials else None
        self.worker.submit(self.capture.capture(depsgraph, dirty, full, descriptions))
        self.follow_selection()

    def follow_selection(self):
        """Outlines Blender's selection in the stream, as Blender's viewport outlines it: the
        selected active object, picked in the headset or in Blender's UI (controls.selected_name).
        A selection change updates the depsgraph, so every change passes through sync()."""
        names = outlined_names(selected_name())
        if names == self.selection:
            return
        self.selection = names
        self.worker.select(names)

    def resync(self):
        if self.running:
            self.scene_name = bpy.context.scene.name
            # A loaded .blend or an undo brings its own Streams to choice.
            self.apply_targets(scene_targets(bpy.context.scene))
            depsgraph = bpy.context.evaluated_depsgraph_get()
            self.worker.submit(self.capture.capture(depsgraph, (), True, material_descriptions(bpy.data.materials)))
            self.follow_selection()

    def on_status(self, severity, message):
        if severity in ("fatal", "error", "warning"):
            log(f"{severity}: {message}")


STREAMER = Streamer()


def guarded(function):
    """A handler that fails must not take Blender's handler chain or the stream down with it."""

    def run(*args):
        try:
            function(*args)
        except Exception as error:  # noqa: BLE001 - reported, the next edit retries
            log(f"sync failed: {error!r}")

    return persistent(run)


on_depsgraph = guarded(lambda scene, depsgraph: STREAMER.sync(scene, depsgraph))
on_frame = guarded(lambda scene, depsgraph: STREAMER.sync(scene, depsgraph))
on_load = guarded(lambda *_args: STREAMER.resync())
on_undo = guarded(lambda *_args: STREAMER.resync())

HANDLERS = (
    (bpy.app.handlers.depsgraph_update_post, on_depsgraph),
    (bpy.app.handlers.frame_change_post, on_frame),
    (bpy.app.handlers.load_post, on_load),
    (bpy.app.handlers.undo_post, on_undo),
    (bpy.app.handlers.redo_post, on_undo),
)


def add_handlers():
    for chain, handler in HANDLERS:
        if handler not in chain:
            chain.append(handler)


def remove_handlers():
    for chain, handler in HANDLERS:
        if handler in chain:
            chain.remove(handler)


def wanted_shading():
    """The shading the stream should have: the QAROS tab's choice, by default the 3D viewport's."""
    choice = getattr(bpy.context.window_manager, "qaros_shading", shading.FOLLOW)
    return shading.from_view(viewport_shading(), None if choice == shading.FOLLOW else choice)


def viewport_shading():
    """The shading of the largest 3D viewport in Blender's windows, or None without one (background
    mode, a workspace without a 3D view)."""
    found, largest = None, -1
    window_manager = bpy.context.window_manager
    for window in window_manager.windows if window_manager is not None else ():
        for area in window.screen.areas:
            if area.type == "VIEW_3D" and area.width * area.height > largest:
                found, largest = area.spaces.active.shading, area.width * area.height
    return found


def watch_shading():
    """Timer: the viewport's shading changes without a depsgraph update, so it is compared here."""
    if not STREAMER.running:
        return None
    try:
        STREAMER.follow_shading()
    except Exception as error:  # noqa: BLE001 - reported, the next tick retries
        log(f"following the viewport shading failed: {error!r}")
    return SHADING_POLL_INTERVAL


def start_reporting(operator, context, start):
    """Runs `start` (a Start or a restart); a failure is shown in the tab and reported."""
    try:
        start(context)
    except Exception as error:  # noqa: BLE001 - shown in the panel
        STREAMER.error = f"Cannot start: {error}"
        if STREAMER.running:
            STREAMER.stop(wait=True)
        operator.report({"ERROR"}, STREAMER.error)
        return {"CANCELLED"}
    return {"FINISHED"}


class QAROS_OT_start(bpy.types.Operator):
    bl_idname = "qaros.start_streaming"
    bl_label = "Start Streaming"
    bl_description = (
        "Join the QAROS on this PC and stream this scene to its users. The first time, QAROS asks "
        "you to allow Blender"
    )

    @classmethod
    def poll(cls, context):
        return not STREAMER.running

    def execute(self, context):
        return start_reporting(self, context, STREAMER.start)


class QAROS_OT_stop(bpy.types.Operator):
    bl_idname = "qaros.stop_streaming"
    bl_label = "Stop Streaming"
    bl_description = "Stop streaming: users no longer see this scene. Blender keeps running"

    @classmethod
    def poll(cls, context):
        return STREAMER.running and not STREAMER.stopping

    def execute(self, context):
        STREAMER.stop()
        return {"FINISHED"}


class QAROS_OT_restart(bpy.types.Operator):
    bl_idname = "qaros.restart_streaming"
    bl_label = "Restart Streaming"
    bl_description = (
        "Stop and start the stream again, into the same app volume: applies the Renderer and the "
        "VisRTX materials. Not while QAROS is still answering the join"
    )

    @classmethod
    def poll(cls, context):
        return STREAMER.can_restart()

    def execute(self, context):
        return start_reporting(self, context, STREAMER.restart)


class QAROS_OT_set_backend(bpy.types.Operator):
    bl_idname = "qaros.set_backend"
    bl_label = "Renderer"
    bl_description = "ANARI back end that renders the stream. A running stream restarts on the new one"
    bl_options = {"INTERNAL"}

    # Each item's description is its tooltip in the menu.
    backend: bpy.props.EnumProperty(name="Renderer", items=BACKEND_ITEMS)

    def execute(self, context):
        prefs = preferences()
        if prefs.backend != self.backend:
            prefs.backend = self.backend
            if context.preferences.addons.get(__package__) is not None:
                context.preferences.is_dirty = True
        if not STREAMER.running:
            STREAMER.prewarm(self.backend)
        if not STREAMER.restart_reasons():
            return {"FINISHED"}
        if not STREAMER.can_restart():
            self.report({"INFO"}, f"{BACKEND_LABELS[self.backend]} renders from the next Start")
            return {"FINISHED"}
        return start_reporting(self, context, STREAMER.restart)


class QAROS_OT_fit_volume(bpy.types.Operator):
    bl_idname = "qaros.fit_volume"
    bl_label = "Fit Scene to Volume"
    bl_description = (
        "Scale and move the streamed scene so every visible object fits its QAROS app volume. "
        "Changes the volume's content placement for every user; a gesture in the headset moves it on"
    )

    @classmethod
    def poll(cls, context):
        return STREAMER.running and not STREAMER.stopping and STREAMER.volume_known()

    def execute(self, context):
        bounds = scene_bounds(context.evaluated_depsgraph_get())
        if bounds is None:
            self.report({"WARNING"}, "Nothing visible to fit")
            return {"CANCELLED"}
        lower, upper = bounds
        # Applied by the device's commit: it changes the volume's app scale and app position.
        STREAMER.device.set(STREAMER.device.handle, "qar.fitBox", anari.FLOAT32_BOX3, (*lower, *upper))
        STREAMER.device.commit()
        log(f"fit {lower} .. {upper} into the app volume")
        return {"FINISHED"}


class QAROS_OT_stream_to_all(bpy.types.Operator):
    bl_idname = "qaros.stream_to_all"
    bl_label = "All devices"
    bl_description = (
        "Stream to every QAROS device, also those that join later. Untick to choose devices: "
        "every connected device stays ticked until you untick it"
    )
    bl_options = {"INTERNAL", "UNDO"}

    on: bpy.props.BoolProperty()

    def execute(self, context):
        choice = targets.set_all(scene_targets(context.scene), self.on, STREAMER.session_targets())
        store_scene_targets(context.scene, choice)
        STREAMER.apply_targets(choice)
        return {"FINISHED"}


class QAROS_OT_stream_to_device(bpy.types.Operator):
    bl_idname = "qaros.stream_to_device"
    bl_label = "Stream to this device"
    bl_description = "Stream to this QAROS device or stop streaming to it. A dot: it receives the stream now"
    bl_options = {"INTERNAL", "UNDO"}

    peer_id: bpy.props.StringProperty()
    on: bpy.props.BoolProperty()

    def execute(self, context):
        choice = targets.set_target(
            scene_targets(context.scene), self.peer_id, self.on, STREAMER.session_targets()
        )
        store_scene_targets(context.scene, choice)
        STREAMER.apply_targets(choice)
        return {"FINISHED"}


def checkbox_icon(ticked):
    return "CHECKBOX_HLT" if ticked else "CHECKBOX_DEHLT"


def alert_label(layout, text, icon="ERROR"):
    row = layout.row()
    row.alert = True
    row.label(text=text, icon=icon)


def draw_streams_to(layout, scene):
    """Streams to: All devices, or one checkbox per connected device and per chosen device that is
    not connected. The list comes from the device once streaming runs."""
    choice = scene_targets(scene)
    session = STREAMER.session_targets()
    if STREAMER.running and not STREAMER.stopping and session is None and STREAMER.volume_known():
        # Streaming runs, but the device does not list its targets: an older QAROS.
        layout.label(text="Every device (this QAROS cannot choose devices)", icon="INFO")
        return
    column = layout.column(align=True)
    line = column.row()
    line.alignment = "LEFT"
    button = line.operator(
        QAROS_OT_stream_to_all.bl_idname, text="All devices", icon=checkbox_icon(choice.stream_to_all), emboss=False
    )
    button.on = not choice.stream_to_all
    for row in targets.rows(choice, session):
        line = column.row()
        line.alignment = "LEFT"
        line.enabled = row.online or not choice.stream_to_all
        button = line.operator(
            QAROS_OT_stream_to_device.bl_idname, text=row.label, icon=checkbox_icon(row.ticked), emboss=False
        )
        button.peer_id = row.peer_id
        button.on = not row.ticked
        if row.streaming:
            line.label(text="", icon="RADIOBUT_ON")
    if not choice.stream_to_all:
        layout.label(text=targets.summary(choice, session), icon="COMMUNITY")
    if session is None and STREAMER.running:
        layout.label(text="Devices are listed once streaming runs", icon="SORTTIME")
    warning = targets.over_limit(choice, session, preferences().max_users)
    if warning:
        alert_label(layout, warning)


TOOL_ICONS = {TOOL_NAVIGATE: "VIEW_PAN", "SELECT": "RESTRICT_SELECT_OFF"}


def draw_tool(layout):
    """The tool hand gestures use, as the Tools tab in the headset shows it. Buttons of an operator,
    not the preference itself: a Blender QAROS started with --addons alone has no preferences entry
    to draw (preferences())."""
    current = gesture_tool()
    row = layout.row(align=True)
    for tool, name, _description in TOOLS:
        row.operator(
            QAROS_OT_set_tool.bl_idname, text=name, icon=TOOL_ICONS.get(tool, "HAND"), depress=tool == current
        ).tool = tool


def addon_preferences(context):
    """The add-on's own preferences, or None when Blender's preferences do not list it (--addons)."""
    entry = context.preferences.addons.get(__package__)
    return entry.preferences if entry is not None else None


class QarosSidebar:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "QAROS"


class QAROS_PT_panel(QarosSidebar, bpy.types.Panel):
    """Start and stop, the stream's status and its app volume."""

    bl_label = "QAROS"

    def draw_header(self, context):
        icon, _text, _alert = STREAMER.state()
        self.layout.label(text="", icon=icon)

    def draw(self, context):
        layout = self.layout
        icon, text, alert = STREAMER.state()
        row = layout.row()
        row.alert = alert
        row.label(text=text, icon=icon)
        row = layout.row(align=True)
        row.scale_y = 1.4
        if STREAMER.running:
            row.operator(QAROS_OT_stop.bl_idname, text="Stop", icon="PAUSE")
            row.operator(QAROS_OT_restart.bl_idname, text="", icon="FILE_REFRESH")
        else:
            row.operator(QAROS_OT_start.bl_idname, icon="PLAY")
        row = layout.row()
        # Read when streaming starts: changing it takes Stop, then Start.
        row.enabled = not STREAMER.running
        row.prop(context.scene, "qaros_volume_name", text="", icon="MESH_CUBE", placeholder="Blender (automatic)")
        if STREAMER.running and STREAMER.volume is not None and STREAMER.device is not None:
            note = streaming_note(STREAMER.volume, STREAMER.device.property_string("qar.volumeDisplayName"))
            if note:
                layout.label(text=note, icon="INFO")


OBJECT_ICONS = {
    "MESH": "OUTLINER_OB_MESH", "CURVE": "OUTLINER_OB_CURVE", "SURFACE": "OUTLINER_OB_SURFACE",
    "META": "OUTLINER_OB_META", "FONT": "OUTLINER_OB_FONT", "LIGHT": "OUTLINER_OB_LIGHT",
    "CAMERA": "OUTLINER_OB_CAMERA", "EMPTY": "OUTLINER_OB_EMPTY", "ARMATURE": "OUTLINER_OB_ARMATURE",
    "POINTCLOUD": "OUTLINER_OB_POINTCLOUD", "VOLUME": "OUTLINER_OB_VOLUME",
}


def object_status(obj):
    """(icon, text, alert): what the stream does with `obj`."""
    if not streams(obj):
        return "HIDE_ON", "Left out of the stream", False
    capture = STREAMER.capture
    reason = capture.unstreamed.get(obj.name) if capture is not None else None
    if reason:
        return "ERROR", reason.split(": ", 1)[-1].capitalize(), True
    if not obj.visible_get():
        return "HIDE_ON", "Hidden in the viewport: not streamed", False
    instancer = obj.instance_type == "COLLECTION" and obj.instance_collection is not None
    if obj.type not in GEOMETRY_TYPES and obj.type != "LIGHT" and not instancer:
        return "INFO", "Nothing to stream", False
    if not STREAMER.running:
        return "CHECKMARK", "Streams when streaming starts", False
    if obj.name == STREAMER.selection:
        return "RESTRICT_SELECT_OFF", "Streamed, outlined in the headset", False
    return "CHECKMARK", "Streamed", False


def draw_object_settings(layout, obj):
    """The object's QAROS settings and what the stream does with it: the Object properties' QAROS
    panel and the QAROS tab's Active Object."""
    icon, text, alert = object_status(obj)
    row = layout.row()
    row.alert = alert
    row.label(text=text, icon=icon)


def draw_object_material(layout, obj):
    """The values of the active material the stream uses (shading.py): the Principled BSDF's in
    Material Preview and Rendered, the viewport colour in Solid."""
    if obj.type not in GEOMETRY_TYPES:
        return
    layout.prop(obj, "active_material", text="", icon="MATERIAL")
    material = obj.active_material
    if material is None:
        return
    column = layout.column(align=True)
    column.use_property_split = True
    column.use_property_decorate = False
    stream_shading = STREAMER.capture.shading if STREAMER.capture is not None else wanted_shading()
    if stream_shading.mode == shading.SOLID or not material.use_nodes or material.node_tree is None:
        column.prop(material, "diffuse_color", text="Viewport Color")
        return
    node, _reason = principled_shader(material.node_tree)
    if node is None:
        column.label(text="No Principled BSDF: viewport colour", icon="INFO")
        column.prop(material, "diffuse_color", text="Viewport Color")
        return
    for name in ("Base Color", "Metallic", "Roughness", "Alpha"):
        socket = node.inputs.get(name)
        if socket is None:
            continue
        if socket.is_linked:
            row = column.row()
            row.enabled = False
            row.label(text=f"{name}: linked, not streamed", icon="LINKED")
        else:
            column.prop(socket, "default_value", text=name)


class QAROS_PT_object(QarosSidebar, bpy.types.Panel):
    """The active object, following Blender's selection (in Blender or in the headset)."""

    bl_label = "Active Object"
    bl_parent_id = "QAROS_PT_panel"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if obj is None:
            layout.label(text="Select an object", icon="RESTRICT_SELECT_OFF")
            return
        row = layout.row(align=True)
        row.label(text=obj.name, icon=OBJECT_ICONS.get(obj.type, "OBJECT_DATA"))
        row.prop(obj, "qaros_stream", text="", icon="HIDE_OFF" if streams(obj) else "HIDE_ON", emboss=False)
        draw_object_settings(layout, obj)
        draw_object_material(layout, obj)


class QAROS_PT_object_transform(QarosSidebar, bpy.types.Panel):
    """What the headset's Object tab edits."""

    bl_label = "Transform"
    bl_parent_id = "QAROS_PT_object"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        obj = context.active_object
        column = layout.column()
        column.prop(obj, "location")
        rotation = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}
        column.prop(obj, rotation.get(obj.rotation_mode, "rotation_euler"), text="Rotation")
        column.prop(obj, "scale")


class QAROS_PT_view(QarosSidebar, bpy.types.Panel):
    """How the scene shows in the headsets: shading and placement in the app volume."""

    bl_label = "View"
    bl_parent_id = "QAROS_PT_panel"

    def draw(self, context):
        layout = self.layout
        window_manager = context.window_manager
        row = layout.row(align=True)
        row.prop(window_manager, "qaros_shading", expand=True, icon_only=True)
        stream_shading = STREAMER.capture.shading if STREAMER.capture is not None else wanted_shading()
        mode = stream_shading.mode
        following = window_manager.qaros_shading == shading.FOLLOW
        row = layout.row()
        row.enabled = False
        suffix = " (viewport)" if following else ""
        row.label(text=shading.LABELS[mode] + suffix, icon=shading.ICONS[mode])
        layout.operator(QAROS_OT_fit_volume.bl_idname, icon="FULLSCREEN_ENTER")


class QAROS_PT_renderer(QarosSidebar, bpy.types.Panel):
    """The ANARI back end and the settings read when streaming starts."""

    bl_label = "Renderer"
    bl_parent_id = "QAROS_PT_panel"

    def draw(self, context):
        layout = self.layout
        prefs = preferences()
        layout.operator_menu_enum(
            QAROS_OT_set_backend.bl_idname, "backend", text=BACKEND_LABELS.get(prefs.backend, prefs.backend),
            icon="SHADING_RENDERED",
        )
        if STREAMER.running and STREAMER.mirror is not None:
            in_use = BACKEND_LABELS.get(STREAMER.backend, STREAMER.backend or "unknown")
            materials = STREAMER.mirror.material_support.status.split(": ", 1)[-1]
            layout.label(text=f"{in_use}: {materials}", icon="CHECKMARK")
        if Streamer.folder is not None and prefs.anari_folder != Streamer.folder:
            alert_label(layout, "ANARI folder: restart Blender to apply")
        reasons = STREAMER.restart_reasons()
        if reasons:
            row = layout.row(align=True)
            row.alert = True
            row.label(text=f"{', '.join(reasons)}: restart to apply", icon="ERROR")
            row.operator(QAROS_OT_restart.bl_idname, text="", icon="FILE_REFRESH")
        own = addon_preferences(context)
        if own is None:
            return
        column = layout.column(align=True)
        column.use_property_split = True
        column.use_property_decorate = False
        column.prop(own, "render_rate", text="Frame Rate")
        column.prop(own, "render_scale", text="Resolution")
        if prefs.backend in ("visrtx", BACKEND_AUTOMATIC) or STREAMER.backend == "visrtx":
            column.prop(own, "visrtx_materials", text="VisRTX")


class QAROS_PT_targets(QarosSidebar, bpy.types.Panel):
    """Streams to: every device, or the chosen ones."""

    bl_label = "Streams To"
    bl_parent_id = "QAROS_PT_panel"

    def draw(self, context):
        draw_streams_to(self.layout, context.scene)


class QAROS_PT_headset(QarosSidebar, bpy.types.Panel):
    """What hand gestures do, and the controls panel beside the app volume."""

    bl_label = "Headset"
    bl_parent_id = "QAROS_PT_panel"

    def draw(self, context):
        layout = self.layout
        draw_tool(layout)
        if not STREAMER.running:
            return
        if STREAMER.panel is not None:
            layout.label(text="Controls panel beside the volume", icon="WINDOW")
        elif STREAMER.panel_error:
            alert_label(layout, "No controls panel (see the console)")
        if STREAMER.controls is not None and STREAMER.controls.gestures is None:
            layout.label(text="Hand gestures not available", icon="INFO")


def stream_warnings():
    """Everything the stream approximates or leaves out, for the Diagnostics panel."""
    warnings = []
    if STREAMER.capture is not None:
        if STREAMER.capture.diagnostics.active:
            warnings.append(STREAMER.capture.diagnostics.summary)
        warnings.extend(STREAMER.capture.unstreamed.values())
    if STREAMER.mirror is not None:
        support = STREAMER.mirror.material_support
        if support.fallback_reason:
            warnings.append("Backend uses a matte approximation")
        if support.missing_parameters:
            warnings.append("Backend lacks: " + ", ".join(support.missing_parameters))
    return warnings


class QAROS_PT_diagnostics(QarosSidebar, bpy.types.Panel):
    """What is streamed, and what is approximated or left out (also in the console)."""

    bl_label = "Diagnostics"
    bl_parent_id = "QAROS_PT_panel"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return STREAMER.running and STREAMER.mirror is not None

    def draw_header(self, context):
        if stream_warnings():
            self.layout.label(text="", icon="ERROR")

    def draw(self, context):
        layout = self.layout
        mirror = STREAMER.mirror
        row = layout.row()
        row.label(text=f"{mirror.objects}", icon="OBJECT_DATA")
        row.label(text=f"{mirror.triangles}", icon="MESH_DATA")
        if mirror.culled:
            row.label(text=f"{mirror.culled} outside", icon="HIDE_ON")
        for warning in stream_warnings():
            alert_label(layout, warning)
        if not mirror.material_support.supports_blend:
            layout.label(text="Backend lacks alpha blending", icon="INFO")


class QAROS_PT_object_properties(bpy.types.Panel):
    """Object properties > QAROS: the object's QAROS settings, where Blender keeps an object's."""

    bl_label = "QAROS"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def draw_header(self, context):
        self.layout.prop(context.object, "qaros_stream", text="")

    def draw(self, context):
        draw_object_settings(self.layout, context.object)


def remember_target_names():
    """Keeps the names of the chosen devices in the scene, so one that leaves is still listed by
    name. Written from a timer: a panel's draw may not change data."""
    scene = bpy.context.scene
    if scene is None or not STREAMER.running:
        return
    choice = scene_targets(scene)
    named = targets.remember_names(choice, STREAMER.session_targets())
    if named is not choice:
        store_scene_targets(scene, named)


def redraw_panels():
    """The status changes off the UI thread (joining, users connecting, devices coming and
    going)."""
    try:
        remember_target_names()
    except Exception as error:  # noqa: BLE001 - only the names of absent devices are missing
        log(f"remembering device names failed: {error!r}")
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    return 1.0


CLASSES = (
    Preferences,
    QAROS_OT_toggle_controls_panel,
    QAROS_OT_set_tool,
    QAROS_OT_reset_controls_panels,
    QAROS_OT_start,
    QAROS_OT_stop,
    QAROS_OT_restart,
    QAROS_OT_set_backend,
    QAROS_OT_fit_volume,
    QAROS_OT_stream_to_all,
    QAROS_OT_stream_to_device,
    # The sidebar's sub-panels show in this order.
    QAROS_PT_panel,
    QAROS_PT_object,
    QAROS_PT_object_transform,
    QAROS_PT_view,
    QAROS_PT_renderer,
    QAROS_PT_targets,
    QAROS_PT_headset,
    QAROS_PT_diagnostics,
    QAROS_PT_object_properties,
)


def stop_at_exit():
    if STREAMER.running:
        STREAMER.stop(wait=True)


# Set when QAROS started this Blender: tells when QAROS stops it.
STOP_WATCHER = None


def quit_when_hub_stops():
    """QAROS stopped this Blender (Stop in the Visualizer, the hub shutting down). Quit at once
    rather than be terminated a second later: Blender writes its quit.blend first, so File > Recover
    > Last Session brings back unsaved work, and the stream is then released at exit."""
    if not STOP_WATCHER.requested():
        return STOP_POLL_INTERVAL
    log("QAROS stopped Blender: quitting. File > Recover > Last Session restores unsaved work.")
    remove_handlers()
    bpy.ops.wm.quit_blender("EXEC_DEFAULT")
    return None


def watch_for_hub_stop():
    global STOP_WATCHER
    if STOP_WATCHER is not None or not started_by_hub(os.environ, stdin_is_pipe()):
        return
    STOP_WATCHER = StopRequestWatcher(sys.stdin)
    STOP_WATCHER.start()
    bpy.app.timers.register(quit_when_hub_stops, first_interval=STOP_POLL_INTERVAL, persistent=True)


# Set when the Blender tile started this Blender (QAR_BLENDER_AUTO_START): Start is pressed once by
# itself. Kept across a disable and enable of the add-on, so that never starts streaming again.
AUTO_START = None


def auto_start_streaming():
    """The automatic Start, once, through the same operator as the panel's button. A Start that
    fails, or a join that fails later, is left to the user as without auto-start."""
    if not AUTO_START.take(STREAMER.running):
        return None
    log("QAROS started Blender to stream: starting streaming")
    override = controls_window_override()
    try:
        if override is not None:
            with bpy.context.temp_override(**override):
                bpy.ops.qaros.start_streaming("EXEC_DEFAULT")
        else:
            bpy.ops.qaros.start_streaming("EXEC_DEFAULT")
    except RuntimeError as error:
        # The operator's error report, also shown in the panel (STREAMER.error).
        log(f"automatic start failed: {error}")
    return None


def schedule_auto_start():
    global AUTO_START
    if AUTO_START is None:
        AUTO_START = AutoStartOnce(auto_start_requested(os.environ, log))
    if AUTO_START.pending and not bpy.app.timers.is_registered(auto_start_streaming):
        # Persistent: a file named on Blender's command line loads after the add-on registered.
        bpy.app.timers.register(auto_start_streaming, first_interval=AUTO_START_DELAY, persistent=True)


def register_properties():
    """The add-on's properties on Blender's own types."""
    # Saved with the .blend; empty (also in a Blender QAROS started with no file) means automatic.
    bpy.types.Scene.qaros_volume_name = bpy.props.StringProperty(
        name="App volume",
        description="QAROS app volume this scene streams into. Empty: Blender, or Blender 2, 3, ... "
        "when another Blender on this PC shows it. A name: that volume only",
        default="",
    )
    # Streams to, saved with the .blend: every device (the default, also in a Blender QAROS started
    # with no file), or the devices in qaros_stream_targets (JSON: peer id -> last seen name).
    bpy.types.Scene.qaros_stream_to_all = bpy.props.BoolProperty(
        name="All devices", description="Stream to every QAROS device", default=True
    )
    bpy.types.Scene.qaros_stream_targets = bpy.props.StringProperty(
        name="Devices", description="QAROS devices this scene streams to", default="", options={"HIDDEN"}
    )
    # Object > QAROS, saved with the .blend. Tagging the object runs the depsgraph handler, whose
    # capture leaves the object out or brings it back.
    bpy.types.Object.qaros_stream = bpy.props.BoolProperty(
        name="Stream to QAROS",
        description="Stream this object to QAROS headsets. Off: it stays in Blender only",
        default=True,
        update=lambda obj, _context: obj.update_tag(),
    )
    # The stream's shading, for this Blender session (shading.py).
    bpy.types.WindowManager.qaros_shading = bpy.props.EnumProperty(
        name="Stream Shading",
        description="How the headsets see the scene",
        items=(
            (shading.FOLLOW, "Follow Viewport",
             "Shade like the largest 3D viewport: switch its shading (Z) and the headsets follow",
             shading.ICONS[shading.FOLLOW], 0),
            (shading.WIREFRAME, "Wireframe", "Mesh edges as thin light lines", shading.ICONS[shading.WIREFRAME], 1),
            (shading.SOLID, "Solid",
             "Matte, in the viewport's solid colour (material viewport colour or single colour), studio light",
             shading.ICONS[shading.SOLID], 2),
            (shading.MATERIAL, "Material Preview",
             "Exported materials with a studio light; scene lights and World as the viewport's Material Preview "
             "options choose", shading.ICONS[shading.MATERIAL], 3),
            (shading.RENDERED, "Rendered", "Exported materials, scene lights and World",
             shading.ICONS[shading.RENDERED], 4),
        ),
        default=shading.FOLLOW,
        update=lambda _wm, _context: STREAMER.follow_shading(),
    )


def unregister_properties():
    del bpy.types.WindowManager.qaros_shading
    del bpy.types.Object.qaros_stream
    del bpy.types.Scene.qaros_stream_targets
    del bpy.types.Scene.qaros_stream_to_all
    del bpy.types.Scene.qaros_volume_name


def register():
    if not blender_support.supported(bpy.app.version):
        # Blender 5.3 crashes with the add-on: refuse before anything is registered.
        message = blender_support.unsupported_message(bpy.app.version)
        print(message, file=sys.stderr)
        raise RuntimeError(message)
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    usd_import.register()
    register_properties()
    bpy.app.timers.register(redraw_panels, first_interval=1.0, persistent=True)
    atexit.register(stop_at_exit)
    watch_for_hub_stop()
    schedule_auto_start()


def unregister():
    stop_at_exit()
    atexit.unregister(stop_at_exit)
    if bpy.app.timers.is_registered(redraw_panels):
        bpy.app.timers.unregister(redraw_panels)
    if bpy.app.timers.is_registered(quit_when_hub_stops):
        bpy.app.timers.unregister(quit_when_hub_stops)
    if bpy.app.timers.is_registered(auto_start_streaming):
        bpy.app.timers.unregister(auto_start_streaming)
    if bpy.app.timers.is_registered(watch_shading):
        bpy.app.timers.unregister(watch_shading)
    unregister_properties()
    usd_import.unregister()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
