# SPDX-License-Identifier: MIT
"""The controls panel's Blender side: reads what the page shows and applies what users change.

Messages arrive on a QAR library thread and only enter the EditQueue there. A timer on Blender's UI
thread drains it: edits apply latest-first per control, a drag that ends records one undo step, and
the page's state is posted again whenever it changed. Every bpy access happens on that timer."""
import math
import threading
import time

import bpy
from mathutils import Vector

from . import blender_panels, pointing
from .materials import principled_shader
from .panel import OBJECT_MODE, QarError
from .protocol import (
    Call,
    EditQueue,
    Hello,
    PropSet,
    TABS,
    Refused,
    coerce_prop,
    panels_message,
    parse,
    result_message,
    shown,
    state_message,
)

TICK = 0.05  # seconds between drains: a drag applies at most 20 values a second
# Ticks between two replays of Blender's panels when nothing was edited (200 ms).
PANELS_EVERY = 4
UNDO_LABEL = "QAROS panel edit"
EULER_MODES = {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}


def log(text):
    print(f"[qaros-anari] {text}", flush=True)


def _window_override():
    """What an operator run from a timer needs: a window and its screen."""
    window = bpy.context.window_manager.windows[0] if bpy.context.window_manager.windows else None
    if window is None:
        return None
    return {"window": window, "screen": window.screen}


def _principled(obj):
    material = obj.active_material if obj is not None else None
    if material is None:
        return None, None, "No material"
    if not material.use_nodes or material.node_tree is None:
        return material, None, "Material does not use nodes"
    node, reason = principled_shader(material.node_tree)
    return material, node, reason


def _input_value(node, name):
    socket = node.inputs.get(name) if node is not None else None
    if socket is None or socket.is_linked:
        return None
    return socket.default_value


def snapshot():
    """What the page shows: the active object, its material, the frame."""
    scene = bpy.context.scene
    obj = bpy.context.view_layer.objects.active
    state = {"object": None, "frame": None, "material": None}
    override = _window_override()
    playing = override["screen"].is_animation_playing if override else False
    state["frame"] = {
        "current": scene.frame_current,
        "start": scene.frame_start,
        "end": scene.frame_end,
        "playing": bool(playing),
    }
    if obj is None:
        return state
    euler = obj.rotation_mode in EULER_MODES
    state["object"] = {
        "name": obj.name,
        "location": list(obj.location),
        "rotation": [math.degrees(a) for a in obj.rotation_euler] if euler else None,
        "scale": list(obj.scale),
    }
    material, node, reason = _principled(obj)
    if node is None:
        state["material"] = {"name": material.name if material else None, "reason": reason}
        return state
    color = _input_value(node, "Base Color")
    state["material"] = {
        "name": material.name,
        "color": list(color)[:3] if color is not None else None,
        "roughness": _input_value(node, "Roughness"),
        "metallic": _input_value(node, "Metallic"),
    }
    return state


def apply_set(edit):
    """Applies one Set to the active object, its material or the scene. False when it does not
    apply now (no object, a linked input, a rotation mode other than Euler)."""
    scene = bpy.context.scene
    if edit.key == "frame":
        scene.frame_set(int(round(edit.value)))
        return True
    obj = bpy.context.view_layer.objects.active
    if obj is None:
        return False
    if edit.key == "location":
        obj.location[edit.index] = edit.value
        return True
    if edit.key == "scale":
        obj.scale[edit.index] = edit.value
        return True
    if edit.key == "rotation":
        if obj.rotation_mode not in EULER_MODES:
            return False
        obj.rotation_euler[edit.index] = math.radians(edit.value)
        return True
    _, node, _ = _principled(obj)
    name = {"color": "Base Color", "roughness": "Roughness", "metallic": "Metallic"}[edit.key]
    socket = node.inputs.get(name) if node is not None else None
    if socket is None or socket.is_linked:
        return False
    if edit.key == "color":
        socket.default_value[edit.index] = edit.value
    else:
        socket.default_value = edit.value
    return True


def run_call(name):
    override = _window_override()
    if override is None:
        raise RuntimeError("no Blender window")
    with bpy.context.temp_override(**override):
        if name == "undo":
            bpy.ops.ed.undo()
        elif name == "redo":
            bpy.ops.ed.redo()
        elif name == "play":
            bpy.ops.screen.animation_play()


def push_undo():
    override = _window_override()
    if override is None:
        return
    with bpy.context.temp_override(**override):
        bpy.ops.ed.undo_push(message=UNDO_LABEL)


def pick(origin, direction):
    """The object Blender's ray cast hits first, by name, or None. A hit on an instance (a
    collection instance, instancing on faces or vertices) picks the object that instances it, the one
    a user can select and move: the ray cast names the instanced prototype, which may not even be in
    the view layer, and moving it would move it in every instance."""
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    hit, _location, _normal, _index, obj, matrix = scene.ray_cast(depsgraph, Vector(origin), Vector(direction))
    if not hit or obj is None:
        return None
    instancer = instancer_of(depsgraph, obj, matrix)
    return (instancer or obj).original.name


INSTANCE_MATRIX_TOLERANCE = 1.0e-5


def instancer_of(depsgraph, obj, matrix):
    """The object whose instancing put `obj` at `matrix`, or None when the hit is `obj` itself."""
    if obj.original.name in bpy.context.view_layer.objects and _same_matrix(obj.matrix_world, matrix):
        return None
    for instance in depsgraph.object_instances:
        if not instance.is_instance or instance.object.original != obj.original:
            continue
        if _same_matrix(instance.matrix_world, matrix):
            return instance.parent
    return None


def _same_matrix(a, b):
    return all(abs(x - y) <= INSTANCE_MATRIX_TOLERANCE for row_a, row_b in zip(a, b) for x, y in zip(row_a, row_b))


def select_only(name):
    """Makes `name` the one selected and active object: the panel then shows it."""
    layer = bpy.context.view_layer
    obj = layer.objects.get(name)
    if obj is None:
        return
    for other in layer.objects:
        if other.select_get():
            other.select_set(False)
    obj.select_set(True)
    layer.objects.active = obj


class Controls:
    """Connects one ServedPanel to Blender until `close`."""

    def __init__(self, panel, settings=None):
        self.panel = panel
        # () -> (switched-on tabs, chosen panel class names): the add-on's preferences, read each
        # tick so a change shows without reopening the panel.
        self.settings = settings or (lambda: (TABS, blender_panels.DEFAULT_PANELS))
        self.queue = EditQueue()
        self.last_state = None
        self.last_panels = None
        self.exposed = {}  # field id -> field of the last replay: what a prop edit is checked against
        self.ticks = 0
        self.closed = False
        self.gestures = None  # VolumeGestures, once attached
        self.hovers = {}  # (peer, hand) -> (monotonic time, action point): written on QAR's thread
        self.hovers_lock = threading.Lock()
        self.pointing = pointing.Pointing()
        self.modes = {}  # peer -> the gesture mode last switched to ('object' or 'default')
        self.hovers_followed = {}  # peer -> time of the hover its mode was last decided by
        panel.subscribe(self.receive)
        bpy.app.timers.register(self.tick, first_interval=TICK)

    def attach_gestures(self, gestures):
        self.gestures = gestures

    def receive_gesture(self, event):
        """QAR library thread: queue the app volume gesture, nothing else. A hover only updates the
        hand's latest hit (hovers come every frame and change nothing in Blender)."""
        if event["kind"] == pointing.HOVER and pointing.has_hit(event):
            with self.hovers_lock:
                kept = self.hovers.setdefault((event["peer"], event.get("hand")), [])
                kept.append((time.monotonic(), (event["start"], event["action"])))
                del kept[: -pointing.HOVERS_KEPT]
            return
        if event["kind"] in (pointing.CLICK, pointing.ONE_HAND):
            self.queue.put(("gesture", event))

    def receive(self, text, user):
        """QAR library thread: parse and queue, nothing else."""
        try:
            self.queue.put(parse(text, user))
        except Refused as error:
            log(f"panel message refused: {error}")

    def tick(self):
        if self.closed:
            return None
        try:
            self.step()
        except Exception as error:  # noqa: BLE001 - a failing tick must not stop the next one
            log(f"panel step failed: {error!r}")
        return TICK

    def step(self):
        sets, others = self.queue.drain()
        tabs, chosen = self.settings()
        refused = [item for item in sets + others if not shown(item, tabs)]
        for item in refused:
            log(f"panel message for a hidden tab refused: {item}")
        sets = [edit for edit in sets if shown(edit, tabs)]
        others = [item for item in others if shown(item, tabs)]
        repost = bool(refused)
        final = False
        for edit in sets:
            applied = self.apply_prop(edit) if isinstance(edit, PropSet) else apply_set(edit)
            if not applied:
                repost = True  # the page shows a value Blender did not take: show it Blender's
            final = final or edit.final
        if final:
            push_undo()
        self.follow_hovers()
        for item in others:
            if isinstance(item, tuple) and item[0] == "gesture":
                if self.gesture(item[1]):
                    push_undo()  # a released drag of an object is one undo step
                repost = True
            elif isinstance(item, Hello):
                repost = True
            elif isinstance(item, Call):
                self.call(item)
        self.post_state(tabs, force=repost)
        self.ticks += 1
        if sets or others or refused or self.ticks % PANELS_EVERY == 0:
            self.post_panels(tabs, chosen, force=repost)

    def gesture(self, event):
        """One app volume gesture on the UI thread. True when it ended a drag that moved an object."""
        kind, phase, peer = event["kind"], event["phase"], event["peer"]
        if kind == pointing.CLICK and phase in (pointing.INSTANT, pointing.ENDED):
            self.click(peer, event)
            return False
        # A drag in the default mode moves the view (the app transform): the mixer already did it,
        # and Blender's scene stays as it is.
        if kind != pointing.ONE_HAND or event["mode"] != OBJECT_MODE:
            return False
        key = event["gesture_id"]
        if phase == pointing.STARTED:
            obj = bpy.data.objects.get(self.pointing.selected.get(peer, ""))
            if obj is not None:
                grabbed = self.aimed(peer, event)
                if self.pointing.drag_started(peer, key, tuple(obj.matrix_world.translation), grabbed) is None:
                    log(f"drag by {peer[:8]} grabbed {grabbed or 'nothing'}, not {obj.name}: nothing moves")
            return False
        if phase == pointing.UPDATED:
            moved = self.pointing.drag_moved(peer, key, self.content_delta(event["translation"]))
            if moved is not None:
                self.move(*moved)
            return False
        if phase in (pointing.ENDED, pointing.CANCELED):
            finished = self.pointing.drag_finished(peer, key, phase == pointing.CANCELED)
            if finished is None:
                return False
            name, restore, moved = finished
            if restore is not None:
                self.move(name, restore)
                return False
            return moved
        return False

    def content_delta(self, translation):
        """A drag's accumulated translation (app volume space) in content space."""
        try:
            orientation, scale = self.gestures.app_transform()
        except QarError as error:
            log(f"app transform not read: {error}")
            return None
        return pointing.volume_to_content_delta(translation, orientation, scale)

    def recent_hover(self, peer, hand, start):
        with self.hovers_lock:
            kept = list(self.hovers.get((peer, hand), ()))
        if start is None:
            return None
        return pointing.closest_hover(kept, start, time.monotonic())

    def aimed(self, peer, event):
        """What a click or a drag's pinch aimed at, by Blender's ray cast: its own hit, else the same
        hand's latest hover. None when nothing."""
        hover = self.recent_hover(peer, event.get("hand"), event.get("start"))
        ray = pointing.click_ray(event, hover)
        return pick(*ray) if ray is not None else None

    def follow_hovers(self):
        """Each user with a selection is in the object mode only while their latest hover is on it:
        a one-hand drag then moves the object, anywhere else it moves the view. A user moving an
        object keeps the mode until the drag ends (a running gesture keeps its mode anyway)."""
        latest = {}
        for (peer, _hand), kept in self.hovers_snapshot():
            if kept and (peer not in latest or kept[-1][0] > latest[peer][0]):
                latest[peer] = kept[-1]
        for peer, (seen, (start, action)) in latest.items():
            if peer not in self.pointing.selected or self.pointing.dragging(peer):
                continue
            if self.hovers_followed.get(peer) == seen:
                continue
            self.hovers_followed[peer] = seen
            ray = pointing.click_ray({"start": start, "action": action})
            aimed = pick(*ray) if ray is not None else None
            self.set_mode(peer, self.pointing.wanted_mode(peer, aimed))

    def click(self, peer, event):
        hover = self.recent_hover(peer, event.get("hand"), event.get("start"))
        ray = pointing.click_ray(event, hover)
        name = pick(*ray) if ray is not None else None
        source = "its own hit" if pointing.has_hit(event) else ("the hover hit" if hover is not None else "no hit")
        with self.hovers_lock:
            kept = self.hovers.get((peer, event.get("hand")))
        age = f"{time.monotonic() - kept[-1][0]:.2f} s" if kept else "none"
        aim = "" if ray is None else " from ({:.2f}, {:.2f}, {:.2f}) along ({:.2f}, {:.2f}, {:.2f})".format(*ray[0], *ray[1])
        log(f"click by {peer[:8]} aimed by {source} (last hover {age}){aim}: {name or 'nothing'}")
        mode = self.pointing.clicked(peer, name)
        if name is not None:
            select_only(name)
        self.set_mode(peer, mode)
        # The click decided the mode: only a hover after it moves the ray off the selection.
        latest = [kept[-1][0] for (who, _hand), kept in self.hovers_snapshot() if who == peer and kept]
        if latest:
            self.hovers_followed[peer] = max(latest)

    def hovers_snapshot(self):
        with self.hovers_lock:
            return [(key, list(kept)) for key, kept in self.hovers.items()]

    def move(self, name, translation):
        obj = bpy.data.objects.get(name)
        if obj is None:
            for gone in self.pointing.forget_object(name):
                self.set_mode(gone, "default")
            return
        obj.matrix_world.translation = Vector(translation)

    def set_mode(self, peer, mode):
        if self.gestures is None or self.modes.get(peer) == mode:
            return
        try:
            if mode == "object":
                self.gestures.select_object_mode(peer)
            else:
                self.gestures.select_default_mode(peer)
        except QarError as error:
            log(f"gesture mode for {peer[:8]} not changed: {error}")
            return
        self.modes[peer] = mode

    def apply_prop(self, edit):
        field = self.exposed.get(edit.id)
        if field is None:
            log(f"panel edit of a field not shown now: {edit.id}")
            return False
        try:
            value = coerce_prop(field, edit.index, edit.value)
        except Refused as error:
            log(f"panel edit of {edit.id} refused: {error}")
            return False
        return blender_panels.apply_prop(field, edit.index, value)

    def post_panels(self, tabs, chosen, force=False):
        panels = blender_panels.panels_state(chosen) if "panels" in tabs else []
        text, kept = panels_message(panels)
        self.exposed = blender_panels.exposed_fields(kept)
        if text == self.last_panels and not force:
            return
        self.panel.post(text)
        self.last_panels = text

    def call(self, item):
        try:
            run_call(item.name)
            self.panel.post(result_message(item, True))
        except Exception as error:  # noqa: BLE001 - the page learns why, Blender keeps running
            log(f"panel call {item.name} failed: {error}")
            self.panel.post(result_message(item, False, error))

    def post_state(self, tabs, force=False):
        state = snapshot()
        state["tabs"] = [tab for tab in TABS if tab in tabs]
        text = state_message(state)
        if text == self.last_state and not force:
            return
        self.panel.post(text)
        self.last_state = text

    def close(self):
        self.closed = True
        if self.gestures is not None:
            for error in self.gestures.close():
                log(f"closing the volume gestures: {error}")
            self.gestures = None
