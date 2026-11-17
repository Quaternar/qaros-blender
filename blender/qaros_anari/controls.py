# SPDX-License-Identifier: MIT
"""The controls panel's Blender side: reads what the page shows and applies what users change.

Messages arrive on a QAR library thread and only enter the EditQueue there. A timer on Blender's UI
thread drains it: edits apply latest-first per control, a drag that ends records one undo step, and
the page's state is posted again whenever it changed. Every bpy access happens on that timer."""
import math
from dataclasses import dataclass
import threading
import time

import bpy
from mathutils import Matrix, Quaternion, Vector

from . import blender_panels, gestures, pointing
from .materials import principled_shader
from .panel import QarError
from .protocol import (
    Call,
    EditQueue,
    Hello,
    PropSet,
    TABS,
    Refused,
    ToolSet,
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


def _usable_window(window):
    """Whether operators can run in `window` now: a real window with a screen of areas, not a
    temporary one (a file browser, a preferences window that is opening or closing) and with no
    modal operator (an import, a transform) in it."""
    screen = window.screen
    if screen is None or screen.is_temporary or len(screen.areas) == 0:
        return False
    return len(getattr(window, "modal_operators", ())) == 0


def _window_override():
    """What an operator run from a timer needs, built fresh on every call (never kept: a window,
    area or region the user closes or that Blender replaces would leave the override pointing at
    freed memory, and an operator that reads it crashes Blender): a usable window, its screen and,
    where it has one, a 3D view area with its main region. None when no window is usable now."""
    manager = bpy.context.window_manager
    windows = [window for window in manager.windows if _usable_window(window)]
    if not windows:
        return None
    window = windows[0]
    override = {"window": window, "screen": window.screen}
    for area in window.screen.areas:
        if area.type != "VIEW_3D":
            continue
        for region in area.regions:
            if region.type == "WINDOW":
                override.update(area=area, region=region)
                return override
    return override


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
        raise RuntimeError("no Blender window ready (an import or a dialog is open)")
    with bpy.context.temp_override(**override):
        if name == "undo":
            bpy.ops.ed.undo()
        elif name == "redo":
            bpy.ops.ed.redo()
        elif name == "play":
            bpy.ops.screen.animation_play()


def push_undo():
    """One undo step for what the panel or a gesture changed. False when no window can take it now
    (a modal operator such as a viewport move runs, a file browser is open): the caller tries again on
    its next tick, because an undo push made then reads a context that is being replaced."""
    override = _window_override()
    if override is None:
        return False
    with bpy.context.temp_override(**override):
        bpy.ops.ed.undo_push(message=UNDO_LABEL)
    return True


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


def ancestors(obj):
    """The parent chain of `obj`, nearest first (a parent loop, which Blender forbids, ends it)."""
    chain = []
    while obj.parent is not None and obj.parent not in chain and len(chain) < 1000:
        obj = obj.parent
        chain.append(obj)
    return chain


def root_of(name):
    """The top-most object of the parent chain of `name` (`name` itself with no parent), or `name`
    when the object is gone. A model imported as a root with child parts is moved as a whole by its
    root."""
    obj = bpy.data.objects.get(name)
    if obj is None:
        return name
    chain = ancestors(obj)
    return chain[-1].name if chain else name


def within(name, top):
    """Whether object `name` is `top` or below it in the parent chain."""
    obj = bpy.data.objects.get(name)
    return obj is not None and (name == top or any(parent.name == top for parent in ancestors(obj)))


def select_only(name, with_children=False):
    """Makes `name` the one selected and active object, as a click in the viewport does: the
    controls panel then shows it, the gestures of Object selection move it, and Blender's own UI
    shows it selected at once. `with_children` also highlights everything below it in Blender's
    viewport and outliner; the active object, which moves, stays `name`. Data API only: an operator
    run from a timer reads a context that may be changing."""
    layer = bpy.context.view_layer
    obj = layer.objects.get(name)
    if obj is None:
        return
    for other in layer.objects:
        if other.select_get():
            other.select_set(False)
    obj.select_set(True)
    if with_children:
        for child in obj.children_recursive:
            if child.name in layer.objects:
                child.select_set(True)
    layer.objects.active = obj
    redraw_desktop()


# The editors that show the selection: redrawn after a selection made in the headset, which no
# mouse event in them would otherwise redraw.
SELECTION_EDITORS = {"VIEW_3D", "OUTLINER", "PROPERTIES"}


def redraw_desktop():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in SELECTION_EDITORS:
                area.tag_redraw()


def selected_name():
    """The selected object: Blender's active object while it is selected, else None. The one
    selection every user's gestures and the controls panel follow, made in the headset or in
    Blender's UI."""
    obj = bpy.context.view_layer.objects.active
    return obj.name if obj is not None and obj.select_get() else None


@dataclass
class Twist:
    """One user's two-hand scale or turn of their selected object."""

    object_name: str
    start: Matrix  # the object's world matrix when the gesture began
    app_orientation: tuple  # the app orientation then: turns arrive in app volume space
    moved: bool = False


def twisted(kind, start, translation, rotation, app_orientation):
    """The world matrix a two-hand gesture gives an object that started at `start`: scaled or turned
    about its own origin, the turn about the world's axes."""
    if kind == gestures.TWO_HAND_SCALE:
        return start @ Matrix.Scale(gestures.scale_factor(translation), 4)
    x, y, z, w = gestures.object_rotation(rotation, app_orientation)
    origin = Matrix.Translation(start.translation)
    return origin @ Quaternion((w, x, y, z)).to_matrix().to_4x4() @ origin.inverted() @ start


class Controls:
    """Connects one ServedPanel to Blender until `close`."""

    def __init__(self, panel, settings=None, tool=None, set_tool=None):
        self.panel = panel
        # () -> (switched-on tabs, chosen panel class names): the add-on's preferences, read each
        # tick so a change shows without reopening the panel.
        self.settings = settings or (lambda: (TABS, blender_panels.DEFAULT_PANELS))
        # () -> a gestures.TOOL_IDS id, and (id) -> None: the add-on's Tool preference, read each
        # tick and written by the Tools tab, so the page and Blender's UI show one tool.
        self.read_tool = tool or (lambda: gestures.TOOL_NAVIGATE)
        self.write_tool = set_tool or (lambda _tool: None)
        self.queue = EditQueue()
        self.last_state = None
        self.last_panels = None
        self.exposed = {}  # field id -> field of the last replay: what a prop edit is checked against
        self.ticks = 0
        self.undo_pending = False  # an undo step wanted, waiting for a window that can take it
        self.closed = False
        self.gestures = None  # VolumeGestures, once attached
        self.hovers = {}  # (peer, hand) -> (monotonic time, action point): written on QAR's thread
        self.hovers_lock = threading.Lock()
        self.pointing = pointing.Pointing()
        self.taps = pointing.Taps()
        self.hovers_followed = {}  # peer -> time of the hover their aim was last decided by
        self.twists = {}  # (peer, gesture id) -> Twist
        self.routing_lock = threading.Lock()
        # Written on the UI thread, read on QAR's when a manipulation starts:
        self.routing = {}  # peer -> their ray is on the selection
        self.selected = False  # Blender has a selected object
        self.tool = gestures.TOOL_NAVIGATE
        self.decided = {}  # (peer, gesture id) -> gestures.VIEW, OBJECT or NOTHING
        self.view = None  # gestures.ViewWorker, once gestures are attached
        panel.subscribe(self.receive)
        bpy.app.timers.register(self.tick, first_interval=TICK)

    def attach_gestures(self, volume_gestures):
        """From now on Blender decides what every manipulation does: the view moves through
        `volume_gestures.change_app_transform`, objects on the UI thread."""

        def read():
            position, orientation, scale = volume_gestures.app_pose()
            return gestures.AppTransform(position, orientation, scale)

        def write(transform):
            volume_gestures.change_app_transform(transform.position, transform.orientation, transform.scale)

        self.gestures = volume_gestures
        self.view = gestures.ViewWorker(read, write, log)

    def receive_gesture(self, event):
        """QAR library thread: route the app volume gesture, nothing else. A hover only updates the
        hand's latest hit (hovers come every frame and change nothing in Blender). A manipulation
        is decided once, when it starts: view gestures go to the view worker, object ones to the
        UI thread. A click is stamped with its arrival, which double taps are counted by."""
        kind = event["kind"]
        if kind == pointing.HOVER and pointing.has_hit(event):
            with self.hovers_lock:
                kept = self.hovers.setdefault((event["peer"], event.get("hand")), [])
                kept.append((time.monotonic(), (event["start"], event["action"])))
                del kept[: -pointing.HOVERS_KEPT]
            return
        if kind == pointing.CLICK:
            self.queue.put(("gesture", {**event, "time": time.monotonic()}))
            return
        if kind not in gestures.MANIPULATIONS:
            return
        decided = self.decide(event)
        if decided == gestures.VIEW and self.view is not None:
            self.view.put(event)
        elif decided == gestures.OBJECT:
            self.queue.put(("gesture", event))

    def decide(self, event):
        """The target of the manipulation `event` belongs to, fixed when it started; None for one
        whose start this add-on did not see. One that moves nothing is logged when it starts."""
        key = (event["peer"], event["gesture_id"])
        with self.routing_lock:
            if event["phase"] == pointing.STARTED:
                on_selection = self.routing.get(event["peer"], False)
                decided = gestures.target(event["kind"], self.tool, self.selected, on_selection)
                self.decided[key] = decided
            elif event["phase"] in (pointing.ENDED, pointing.CANCELED):
                return self.decided.pop(key, None)
            else:
                return self.decided.get(key)
        if decided == gestures.NOTHING:
            why = "the ray is not on the selected object" if self.selected else "nothing is selected"
            log(f"gesture by {event['peer'][:8]} moves nothing: Object selection, and {why}")
        return decided

    def publish_selection(self):
        """Publishes whether Blender has a selection to QAR's thread. A changed selection drops
        every user's aim: it was on the object selected before."""
        with self.routing_lock:
            self.selected = self.pointing.selection is not None
            self.routing.clear()
        self.hovers_followed.clear()

    def follow_selection(self):
        """Takes Blender's selection, made in the headset or in Blender's UI, as the gestures'."""
        name = selected_name()
        if name == self.pointing.selection:
            return False
        self.pointing.selection = name
        self.publish_selection()
        return True

    def set_aim(self, peer, on_selection):
        with self.routing_lock:
            self.routing[peer] = bool(on_selection) and self.pointing.selection is not None

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
            self.undo_pending = True
        for item in others:
            if isinstance(item, ToolSet):
                self.write_tool(item.tool)
                log(f"tool chosen in the headset: {item.tool}")
                repost = True
        tool = self.read_tool()
        with self.routing_lock:
            self.tool = tool
        if self.follow_selection():
            repost = True  # the panel shows the newly selected object at once
        self.follow_hovers()
        for item in others:
            if isinstance(item, tuple) and item[0] == "gesture":
                if self.gesture(item[1]):
                    self.undo_pending = True  # a released drag of an object is one undo step
                repost = True
            elif isinstance(item, Hello):
                repost = True
            elif isinstance(item, Call):
                self.call(item)
        if self.undo_pending and push_undo():
            self.undo_pending = False
        self.post_state(tabs, force=repost)
        self.ticks += 1
        if sets or others or repost or self.ticks % PANELS_EVERY == 0:
            self.post_panels(tabs, chosen, force=repost)

    def gesture(self, event):
        """One app volume gesture on the UI thread: a click, or a manipulation decided for the
        selected object (view ones never come here). True when it ended a manipulation that changed
        an object."""
        kind, phase, peer = event["kind"], event["phase"], event["peer"]
        if kind == pointing.CLICK and phase in (pointing.INSTANT, pointing.ENDED):
            double_tap = self.taps.tap(peer, event.get("hand"), event.get("time", time.monotonic()))
            self.click(peer, event, double_tap)
            return False
        if kind in (gestures.TWO_HAND_SCALE, gestures.TWO_HAND_ROTATE):
            return self.twist(event)
        if kind != pointing.ONE_HAND:
            return False
        key = event["gesture_id"]
        if phase == pointing.STARTED:
            obj = bpy.data.objects.get(self.pointing.selection or "")
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

    def twist(self, event):
        """A two-hand scale or turn of the selected object. True when it ended having changed the
        object; a canceled one puts the object back."""
        key, phase = (event["peer"], event["gesture_id"]), event["phase"]
        if phase == pointing.STARTED:
            obj = bpy.data.objects.get(self.pointing.selection or "")
            if obj is None:
                return False
            orientation = (0.0, 0.0, 0.0, 1.0)
            if self.gestures is not None:
                try:
                    orientation, _scale = self.gestures.app_transform()
                except QarError as error:
                    log(f"app transform not read: {error}")
            self.twists[key] = Twist(obj.name, obj.matrix_world.copy(), orientation)
            return False
        twist = self.twists.get(key)
        if twist is None:
            return False
        obj = bpy.data.objects.get(twist.object_name)
        if obj is None:
            self.twists.pop(key, None)
            return False
        if phase == pointing.UPDATED:
            obj.matrix_world = twisted(
                event["kind"], twist.start, event["translation"], event.get("rotation", (0.0, 0.0, 0.0, 1.0)), twist.app_orientation
            )
            twist.moved = True
            return False
        if phase in (pointing.ENDED, pointing.CANCELED):
            del self.twists[key]
            if phase == pointing.CANCELED:
                obj.matrix_world = twist.start
                return False
            return twist.moved
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
        return self.grab_hit(pick(*ray)) if ray is not None else None

    def grab_hit(self, name):
        """A hit on a part of the selected model counts as a hit on the selection, which is the
        root of the model: dragging a part moves the whole model."""
        selection = self.pointing.selection
        return selection if name is not None and selection and within(name, selection) else name

    def follow_hovers(self):
        """Whether each user points at the selection, by their latest hover: a one-hand drag
        starting then grabs the object (Object selection). A drag in progress keeps what it was
        decided as."""
        if self.pointing.selection is None:
            return
        latest = {}
        for (peer, _hand), kept in self.hovers_snapshot():
            if kept and (peer not in latest or kept[-1][0] > latest[peer][0]):
                latest[peer] = kept[-1]
        for peer, (seen, (start, action)) in latest.items():
            if self.pointing.dragging(peer) or self.hovers_followed.get(peer) == seen:
                continue
            self.hovers_followed[peer] = seen
            ray = pointing.click_ray({"start": start, "action": action})
            aimed = self.grab_hit(pick(*ray)) if ray is not None else None
            self.set_aim(peer, self.pointing.aims_at_selection(aimed))

    def current_tool(self):
        with self.routing_lock:
            return self.tool

    def click(self, peer, event, double_tap=False):
        """A click, or the second click of a double tap: selects what its ray hits when the tool
        says so (gestures.click_selects). A click on nothing keeps the selection: a pinch on a GUI
        panel reaches the volume too and must not take the panel's object away."""
        tap = "double tap" if double_tap else "click"
        if not gestures.click_selects(self.current_tool(), double_tap):
            log(f"{tap} by {peer[:8]}: Navigation selects on a double tap")
            return
        hover = self.recent_hover(peer, event.get("hand"), event.get("start"))
        ray = pointing.click_ray(event, hover)
        name = pick(*ray) if ray is not None else None
        source = "its own hit" if pointing.has_hit(event) else ("the hover hit" if hover is not None else "no hit")
        with self.hovers_lock:
            kept = self.hovers.get((peer, event.get("hand")))
        age = f"{time.monotonic() - kept[-1][0]:.2f} s" if kept else "none"
        aim = "" if ray is None else " from ({:.2f}, {:.2f}, {:.2f}) along ({:.2f}, {:.2f}, {:.2f})".format(*ray[0], *ray[1])
        log(f"{tap} by {peer[:8]} aimed by {source} (last hover {age}){aim}: {name or 'nothing'}")
        if name is None:
            return
        if double_tap:
            # A double tap takes the whole model: the top of the parent chain of the hit part moves it
            # with all its children. A click of Object selection still takes the part it hits.
            name = root_of(name)
        select_only(name, with_children=double_tap)
        if name != self.pointing.selection:
            self.pointing.selection = name
            self.publish_selection()
        self.set_aim(peer, True)
        # The click decided the aim: only a hover after it moves the ray off the selection.
        latest = [kept[-1][0] for (who, _hand), kept in self.hovers_snapshot() if who == peer and kept]
        if latest:
            self.hovers_followed[peer] = max(latest)

    def hovers_snapshot(self):
        with self.hovers_lock:
            return [(key, list(kept)) for key, kept in self.hovers.items()]

    def move(self, name, translation):
        obj = bpy.data.objects.get(name)
        if obj is None:
            if self.pointing.forget_object(name):
                self.publish_selection()
            return
        obj.matrix_world.translation = Vector(translation)

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
        state["tool"] = self.read_tool()
        state["tools"] = [[tool, name, description] for tool, name, description in gestures.TOOLS]
        text = state_message(state)
        if text == self.last_state and not force:
            return
        self.panel.post(text)
        self.last_state = text

    def close(self):
        self.closed = True
        if self.view is not None:
            self.view.close()
            self.view = None
        if self.gestures is not None:
            for error in self.gestures.close():
                log(f"closing the volume gestures: {error}")
            self.gestures = None
