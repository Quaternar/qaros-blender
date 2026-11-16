# SPDX-License-Identifier: MIT
"""Blender decides what a hand gesture does, without Blender: which gestures go to the view and which
to the selected object, by the active tool, and the transforms each produces.

QAROS reports every manipulation gesture and applies none (panel.app_decides_from): the rays and
hands still show, but the app transform stays where the add-on puts it. The tool is chosen on the
controls panel's Tools tab or in the add-on's preferences (one tool for every user). Per gesture, at
its start:

| Gesture               | Navigation                        | Object selection                          |
| --------------------- | --------------------------------- | ----------------------------------------- |
| click                 | nothing                           | selects what the ray hits                 |
| double tap            | selects what the ray hits         | selects what the ray hits                 |
| one-hand drag         | pans the view                     | moves the selection, when it grabs it     |
| two-hand spread/pinch | zooms the view about the volume   | scales the selection about its origin     |
| two-hand turn         | turns the view about the volume   | turns the selection about its origin, up  |

In Object selection a gesture never moves the view: with nothing selected, or a drag that does not
grab the selection, it does nothing (and the add-on logs it).

The view is the app transform (app pose and scale): the device renders each user's eyes in app
content space, so changing it moves the camera over the scene and never touches a Blender object.

Deltas are QAROS's accumulated ones since the gesture started, in app volume space, with the rules
at precision 1 and constant sensitivity: hand metres. A two-hand spread is a vector whose length is
the change of the distance between the hands and whose components share its sign."""
import math
from dataclasses import dataclass

from .pointing import CANCELED, ENDED, ONE_HAND, STARTED, UPDATED, rotate

# QarGestureKind values of the two-hand gestures (qar_streaming_c types.h).
TWO_HAND_SCALE, TWO_HAND_ROTATE = 3, 4
MANIPULATIONS = (ONE_HAND, TWO_HAND_SCALE, TWO_HAND_ROTATE)

# Hands moving this much further apart (metres) scale by e (2.7x); as much closer, by 1/e.
SCALE_DISTANCE = 0.3
MIN_APP_SCALE, MAX_APP_SCALE = 1.0e-4, 1.0e4

VIEW, OBJECT, NOTHING = "view", "object", "nothing"
# The tools, in the order the Tools tab and the preference list them: (id, name, description).
# A tool added here shows on the page (it is drawn from the state message) and in the preferences.
TOOL_NAVIGATE, TOOL_SELECT = "NAVIGATE", "SELECT"
TOOLS = (
    (TOOL_NAVIGATE, "Navigation", "Hand gestures pan, zoom and turn the whole scene"),
    (TOOL_SELECT, "Object selection", "A click selects what the ray hits; hand gestures move only the selection"),
)
TOOL_IDS = tuple(tool for tool, _name, _description in TOOLS)


def target(kind, tool, selected, on_selection):
    """VIEW, OBJECT or NOTHING for a manipulation of `kind` starting now with `tool`. `selected`:
    Blender has a selected object; `on_selection`: the user's ray is on it (a one-hand drag grabs
    only what it points at; a two-hand gesture has no single ray and takes the selection wherever
    the hands are)."""
    if tool != TOOL_SELECT:
        return VIEW
    if not selected:
        return NOTHING
    if kind == ONE_HAND and not on_selection:
        return NOTHING
    return OBJECT


def click_selects(tool, double_tap):
    """Whether a click picks what its ray hits: every click with Object selection, a double tap
    with any tool."""
    return double_tap or tool == TOOL_SELECT


def spread(translation):
    """The signed change of the distance between the hands (metres) of a two-hand scale delta."""
    length = math.sqrt(sum(c * c for c in translation))
    return length if sum(translation) >= 0.0 else -length


def scale_factor(translation):
    return math.exp(spread(translation) / SCALE_DISTANCE)


def quat_mul(a, b):
    """a * b for quaternions (x, y, z, w): b first, then a."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quat_conj(q):
    return (-q[0], -q[1], -q[2], q[3])


def normalized(q):
    length = math.sqrt(sum(c * c for c in q))
    return (0.0, 0.0, 0.0, 1.0) if length < 1.0e-9 else tuple(c / length for c in q)


@dataclass(frozen=True)
class AppTransform:
    """Where content sits in the volume: app pose (position, orientation x, y, z, w) and scale."""

    position: tuple = (0.0, 0.0, 0.0)
    orientation: tuple = (0.0, 0.0, 0.0, 1.0)
    scale: float = 1.0


def view_after(kind, start, translation, rotation):
    """The app transform a view gesture of `kind` leads to from `start`: a drag carries the content
    with the hand, a spread scales and a turn turns it about the volume's centre (app volume
    space's origin), so the scene does not slide away under the hands."""
    if kind == ONE_HAND:
        position = tuple(p + t for p, t in zip(start.position, translation))
        return AppTransform(position, start.orientation, start.scale)
    if kind == TWO_HAND_SCALE:
        factor = scale_factor(translation)
        scale = min(max(start.scale * factor, MIN_APP_SCALE), MAX_APP_SCALE)
        ratio = scale / start.scale
        return AppTransform(tuple(p * ratio for p in start.position), start.orientation, scale)
    if kind == TWO_HAND_ROTATE:
        turn = normalized(rotation)
        orientation = normalized(quat_mul(turn, start.orientation))
        return AppTransform(rotate(turn, start.position), orientation, start.scale)
    return start


def content_rotation_to_blender(q):
    """A rotation in app content space (Y up) as one in Blender's world (Z up): the vector part maps
    like a point, (x, y, z) -> (x, -z, y), a proper rotation of the axes."""
    x, y, z, w = q
    return (x, -z, y, w)


def object_rotation(rotation, app_orientation):
    """A two-hand turn (app volume space) as the rotation of a Blender object in Blender's world:
    undone through the app orientation into content space, then into Blender's axes."""
    content = quat_mul(quat_conj(app_orientation), quat_mul(normalized(rotation), app_orientation))
    return normalized(content_rotation_to_blender(content))


@dataclass
class ViewGesture:
    kind: int
    start: AppTransform


class ViewGestures:
    """The view gestures in progress, by (peer, gesture id). Pure: `read()` returns the app
    transform now and `write(transform)` sets it; a canceled gesture puts the view back."""

    def __init__(self, read, write):
        self.read = read
        self.write = write
        self.running = {}

    def handle(self, event):
        key = (event["peer"], event["gesture_id"])
        phase = event["phase"]
        if phase == STARTED:
            self.running[key] = ViewGesture(event["kind"], self.read())
            return
        gesture = self.running.get(key)
        if gesture is None:
            return
        if phase == UPDATED:
            self.write(view_after(gesture.kind, gesture.start, event["translation"], event["rotation"]))
        elif phase in (ENDED, CANCELED):
            del self.running[key]
            if phase == CANCELED:
                self.write(gesture.start)


def coalesce(events):
    """`events` in order without an update a later update of the same gesture supersedes: deltas are
    accumulated, so only the newest of a run matters."""
    newest = {}
    for index, event in enumerate(events):
        if event["phase"] == UPDATED:
            newest[(event["peer"], event["gesture_id"])] = index
    return [
        event
        for index, event in enumerate(events)
        if event["phase"] != UPDATED or newest[(event["peer"], event["gesture_id"])] == index
    ]


class ViewWorker:
    """Applies view gestures off QAR's callback thread, newest update first: the C API calls that
    move the view must not run inside a gesture callback, and Blender's UI thread ticks too slowly
    (20 a second) for a view that follows the hands. `log(text)` reports a failed gesture."""

    def __init__(self, read, write, log):
        import threading

        self.views = ViewGestures(read, write)
        self.log = log
        self.pending = []
        self.closed = False
        self.wake = threading.Condition()
        self.thread = threading.Thread(target=self.run, name="qaros-anari-view", daemon=True)
        self.thread.start()

    def put(self, event):
        with self.wake:
            self.pending.append(event)
            self.wake.notify()

    def run(self):
        while True:
            with self.wake:
                while not self.pending and not self.closed:
                    self.wake.wait()
                if self.closed:
                    return
                events, self.pending = coalesce(self.pending), []
            for event in events:
                try:
                    self.views.handle(event)
                except Exception as error:  # noqa: BLE001 - one failed gesture must not stop the next
                    self.log(f"view gesture failed: {error!r}")

    def close(self):
        with self.wake:
            self.closed = True
            self.wake.notify()
        self.thread.join(timeout=1.0)
