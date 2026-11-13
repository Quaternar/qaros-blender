# SPDX-License-Identifier: MIT
"""Streams the open Blender scene to every QAROS user through the `qaros` ANARI device.

The add-on owns one ANARI device for as long as streaming is on, independent of any viewport: the
scene is mirrored from depsgraph handlers, so Solid shading, other workspaces and closed 3D views do
not stop the stream (docs/DEVELOPING.md)."""
import atexit
import os
import sys
import threading

import bpy
from bpy.app.handlers import persistent

from . import anari, blender_panels, usd_import
from .hub_launch import StopRequestWatcher, started_by_hub, stdin_is_pipe
from .controls import Controls
from .protocol import panel_names, toggled_panel
from .materials import material_descriptions
from .panel import QarError, Runtime, ServedPanel, VolumeGestures
from .sync import SceneCapture, SceneMirror, SyncWorker, scene_bounds
from .volume_name import choose_volume, conflict_message, streaming_note

bl_info = {
    "name": "QAROS ANARI Streaming",
    "author": "Quaternar",
    "version": (0, 1, 0),
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
# How often Blender's thread looks whether the stream runs, to open the controls panel beside it.
PANEL_POLL_INTERVAL = 0.5


def log(text):
    print(f"[qaros-anari] {text}", flush=True)


# The Backend preference's "no backend named" value: qaros then chooses (QAR_ANARI_BACKEND unset).
BACKEND_AUTOMATIC = "auto"


class Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    anari_folder: bpy.props.StringProperty(
        name="ANARI folder",
        description="Folder with anari.dll and anari_library_qaros.dll. Empty: the QAROS setup's ANARI component on the system path",
        subtype="DIR_PATH",
    )
    backend: bpy.props.EnumProperty(
        name="Backend",
        description="Renderer qaros forwards to. Read once per Blender session, when streaming first starts",
        # Fixed numbers: Blender saves the number, so a saved choice keeps its backend when items
        # are added. An empty identifier would make Automatic a heading that cannot be chosen.
        items=(
            (BACKEND_AUTOMATIC, "Automatic", "VisRTX, VisGL, OSPRay, then Helide", 0),
            ("visrtx", "VisRTX", "NVIDIA RTX", 1),
            ("visgl", "VisGL", "OpenGL 4.3", 2),
            ("ospray", "OSPRay", "Intel, CPU path tracer", 4),
            ("helide", "Helide", "CPU, reference", 3),
        ),
        default=BACKEND_AUTOMATIC,
    )
    max_users: bpy.props.IntProperty(name="Users", default=2, min=1, max=32)
    fps: bpy.props.IntProperty(name="Frame rate", description="Stereo pairs per second per user", default=30, min=1, max=90)
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
    ambient: bpy.props.FloatProperty(name="Ambient light", default=0.3, min=0.0, max=10.0)
    # What the controls panel in the headset shows (docs/DEVELOPING.md).
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
        row.prop(self, "fps")
        row.prop(self, "render_scale")
        row = layout.row()
        row.prop(self, "app_scale")
        row.prop(self, "near")
        row.prop(self, "far")
        layout.prop(self, "ambient")
        self.draw_controls_panel(layout.box())

    def draw_controls_panel(self, box):
        box.label(text="Controls panel in the headset")
        row = box.row(align=True)
        for name in ("show_object_tab", "show_material_tab", "show_frame_tab", "show_panels_tab"):
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
        "object": prefs.show_object_tab,
        "material": prefs.show_material_tab,
        "frame": prefs.show_frame_tab,
        "panels": prefs.show_panels_tab,
    }
    return tuple(tab for tab, on in switches.items() if on), panel_names(prefs.controls_panels)


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


def preferences():
    return bpy.context.preferences.addons[__package__].preferences


class Streamer:
    """The one stream of this Blender process. The ANARI library is loaded once and kept: qaros
    keeps the QAR runtime loaded for the life of the process."""

    library = None

    def __init__(self):
        self.device = None
        self.backend = None
        self.mirror = None
        self.capture = None
        self.worker = None
        self.camera = None
        self.frame = None
        self.joining = None
        self.scene_name = None
        self.error = None
        self.stopping = False
        self.volume = None  # the VolumeChoice of the running device
        self.runtime = None  # the QAR runtime the device loaded, for the served panel
        self.panel = None  # the served controls panel beside the app volume
        self.controls = None  # applies what users change on the panel
        self.panel_error = None

    @property
    def running(self):
        return self.device is not None

    def volume_known(self):
        """Whether the device knows where its app volume is (qar.volumeCube answers): before that, a
        fit has nothing to change. Answered without the backend gate, so panel redraws never wait."""
        return self.device is not None and self.device.property_matrix("qar.volumeCube") is not None

    def status(self):
        if self.error:
            return self.error
        if not self.running:
            return "Off"
        if self.stopping:
            return "Stopping (waiting for QAROS to answer)"
        if self.joining is not None and self.joining.is_alive():
            return "Joining QAROS (first run: allow Blender in QAROS)"
        if not self.device.property_int("qar.streaming", anari.BOOL):
            reason = self.device.property_string("qar.streamError")
            conflict = conflict_message(self.volume, reason)
            if conflict:
                return conflict
            return f"Not streaming: {reason}" if reason else "Not streaming: joining QAROS failed (see the console)"
        users = self.device.property_int("qar.activeUsers", anari.UINT32) or 0
        text = f"Streaming to {users} user{'s' if users != 1 else ''}"
        scale = self.device.property_float("qar.renderScale")
        if scale is not None and scale < 1.0:
            text += f" at {round(scale * 100)} % resolution"
        return text

    def start(self, context):
        prefs = preferences()
        self.error = None
        if Streamer.library is None:
            if prefs.backend != BACKEND_AUTOMATIC:
                import os
                os.environ["QAR_ANARI_BACKEND"] = prefs.backend
            Streamer.library = anari.Library(prefs.anari_folder, LIBRARY_NAME, self.on_status)
        device = Streamer.library.new_device()
        # qar.* parameters are read when streaming starts; with no onboarding set, qaros joins the
        # QAROS on this PC by local enrollment.
        device.set(device.handle, "qar.stream", anari.BOOL, 1)
        device.set(device.handle, "qar.maxUsers", anari.INT32, prefs.max_users)
        device.set(device.handle, "qar.fps", anari.INT32, prefs.fps)
        device.set(device.handle, "qar.renderScale", anari.FLOAT32, render_scale_parameter(prefs.render_scale))
        device.set(device.handle, "qar.appScale", anari.FLOAT32, prefs.app_scale)
        device.set(device.handle, "qar.near", anari.FLOAT32, prefs.near)
        device.set(device.handle, "qar.far", anari.FLOAT32, prefs.far)
        self.volume = choose_volume(getattr(context.scene, "qaros_volume_name", ""))
        device.set(device.handle, "qar.volume", anari.STRING, self.volume.name)
        device.set(device.handle, "qar.volumeDisplayName", anari.STRING, self.volume.display_name)
        device.set(device.handle, "qar.volumeNumbering", anari.BOOL, 1 if self.volume.numbering else 0)
        device.commit()
        self.device = device
        # Fixed for this Blender process: the preference is read only when the library loads.
        self.backend = device.property_string("qar.backend")
        log(f"backend: {self.backend or 'unknown'}")
        # Objects outside the app volume are not rendered (qar.volumeCube, polled by the worker).
        self.mirror = SceneMirror(device, self.backend, lambda: device.property_matrix("qar.volumeCube"))
        log(f"renderer: {self.mirror.renderer_subtype}")
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
        self.scene_name = context.scene.name
        # Applied here: nothing renders yet, so nothing holds the scene lock. Later syncs go
        # through the worker, so the UI thread never waits for a target's render.
        self.mirror.apply(self.capture.capture(context.evaluated_depsgraph_get(), (), True))
        self.worker = SyncWorker(self.mirror, lambda error: log(f"sync failed: {error!r}"))
        self.publish()
        add_handlers()
        bpy.app.timers.register(self.open_panel, first_interval=PANEL_POLL_INTERVAL)
        log(f"streaming '{self.scene_name}': {self.mirror.objects} objects, {self.mirror.triangles} triangles")

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
            self.controls = Controls(self.panel, controls_settings)
            log("controls panel opened beside the app volume")
            try:
                self.controls.attach_gestures(
                    VolumeGestures(self.runtime, session, volume_id, self.controls.receive_gesture)
                )
                log("pointing into the app volume selects and moves objects")
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
        full = scene.name != self.scene_name or self.worker.take_failure()
        self.scene_name = scene.name
        descriptions = material_descriptions(bpy.data.materials) if materials else None
        self.worker.submit(self.capture.capture(depsgraph, dirty, full, descriptions))

    def resync(self):
        if self.running:
            self.scene_name = bpy.context.scene.name
            depsgraph = bpy.context.evaluated_depsgraph_get()
            self.worker.submit(self.capture.capture(depsgraph, (), True, material_descriptions(bpy.data.materials)))

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


class QAROS_OT_start(bpy.types.Operator):
    bl_idname = "qaros.start_streaming"
    bl_label = "Start streaming"
    bl_description = "Join the QAROS on this PC and stream this scene to its users"

    @classmethod
    def poll(cls, context):
        return not STREAMER.running

    def execute(self, context):
        try:
            STREAMER.start(context)
        except Exception as error:  # noqa: BLE001 - shown in the panel
            STREAMER.error = f"Cannot start: {error}"
            if STREAMER.running:
                STREAMER.stop(wait=True)
            self.report({"ERROR"}, STREAMER.error)
            return {"CANCELLED"}
        return {"FINISHED"}


class QAROS_OT_stop(bpy.types.Operator):
    bl_idname = "qaros.stop_streaming"
    bl_label = "Stop streaming"

    @classmethod
    def poll(cls, context):
        return STREAMER.running and not STREAMER.stopping

    def execute(self, context):
        STREAMER.stop()
        return {"FINISHED"}


class QAROS_OT_fit_volume(bpy.types.Operator):
    bl_idname = "qaros.fit_volume"
    bl_label = "Fit scene to volume"
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


class QAROS_PT_panel(bpy.types.Panel):
    bl_label = "QAROS"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "QAROS"

    def draw(self, context):
        layout = self.layout
        layout.label(text=STREAMER.status())
        row = layout.row()
        # Read when streaming starts: changing it takes Stop, then Start.
        row.enabled = not STREAMER.running
        row.prop(context.scene, "qaros_volume_name", text="App volume")
        if not getattr(context.scene, "qaros_volume_name", "").strip():
            layout.label(text="Automatic: Blender, numbered if another Blender shows it")
        if STREAMER.running and STREAMER.volume is not None and STREAMER.device is not None:
            note = streaming_note(STREAMER.volume, STREAMER.device.property_string("qar.volumeDisplayName"))
            if note:
                layout.label(text=note, icon="INFO")
        if STREAMER.running and STREAMER.mirror is not None:
            layout.label(text=f"Backend: {STREAMER.backend or 'unknown'}")
            wanted = preferences().backend
            if wanted != BACKEND_AUTOMATIC and STREAMER.backend and wanted != STREAMER.backend:
                row = layout.row()
                row.alert = True
                row.label(text=f"Restart Blender to use {wanted}", icon="ERROR")
            layout.label(text=f"{STREAMER.mirror.objects} objects, {STREAMER.mirror.triangles} triangles")
            support = STREAMER.mirror.material_support
            layout.label(text=support.status, icon="INFO")
            layout.label(text="Exporting supported Principled inputs")
            if STREAMER.capture is not None and STREAMER.capture.diagnostics.active:
                row = layout.row()
                row.alert = True
                row.label(text=STREAMER.capture.diagnostics.summary, icon="ERROR")
            if support.fallback_reason:
                row = layout.row()
                row.alert = True
                row.label(text="Backend uses a matte approximation", icon="ERROR")
            if support.missing_parameters:
                row = layout.row()
                row.alert = True
                row.label(text="Backend lacks: " + ", ".join(support.missing_parameters), icon="ERROR")
            if not support.supports_blend:
                layout.label(text="Backend lacks alpha blending", icon="INFO")
            if STREAMER.panel is not None:
                layout.label(text="Controls panel beside the app volume", icon="WINDOW")
            elif STREAMER.panel_error:
                row = layout.row()
                row.alert = True
                row.label(text="No controls panel (see the console)", icon="ERROR")
            layout.operator(QAROS_OT_fit_volume.bl_idname, icon="FULLSCREEN_ENTER")
            layout.operator(QAROS_OT_stop.bl_idname, icon="PAUSE")
        else:
            layout.operator(QAROS_OT_start.bl_idname, icon="PLAY")


def redraw_panels():
    """The status changes off the UI thread (joining, users connecting)."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    return 1.0


CLASSES = (Preferences, QAROS_OT_toggle_controls_panel, QAROS_OT_reset_controls_panels, QAROS_OT_start, QAROS_OT_stop, QAROS_OT_fit_volume, QAROS_PT_panel)


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


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    usd_import.register()
    # Saved with the .blend; empty (also in a Blender QAROS started with no file) means automatic.
    bpy.types.Scene.qaros_volume_name = bpy.props.StringProperty(
        name="App volume",
        description="QAROS app volume this scene streams into. Empty: Blender, or Blender 2, 3, ... "
        "when another Blender on this PC shows it. A name: that volume only",
        default="",
    )
    bpy.app.timers.register(redraw_panels, first_interval=1.0, persistent=True)
    atexit.register(stop_at_exit)
    watch_for_hub_stop()


def unregister():
    stop_at_exit()
    atexit.unregister(stop_at_exit)
    if bpy.app.timers.is_registered(redraw_panels):
        bpy.app.timers.unregister(redraw_panels)
    if bpy.app.timers.is_registered(quit_when_hub_stops):
        bpy.app.timers.unregister(quit_when_hub_stops)
    del bpy.types.Scene.qaros_volume_name
    usd_import.unregister()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
