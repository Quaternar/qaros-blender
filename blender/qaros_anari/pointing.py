# SPDX-License-Identifier: MIT
"""Pointing into the app volume, without Blender: where a gesture's points are in Blender's world,
the ray a click casts, and what each user's one-hand drag does to the object they selected.

Moving, turning or scaling the app volume's content (a one-hand drag in the default mode, every
two-hand gesture, Fit scene to volume) is a view change: the device renders each user's eyes in app
content space, so it moves the camera over the scene and never touches a Blender object. Only a
one-hand drag that grabs the user's selected object edits the scene. A user is in the object mode
only while their ray is on their selection: a drag anywhere else, a GUI panel's title bar included,
stays a view change.

A gesture's points are in app content space: the ANARI world the add-on builds, which is Blender's
world with Y up (sync.py: Blender (x, y, z) -> (x, z, -y)), unscaled. A one-hand drag's
translation is the grabbed point's accumulated motion (it rides the hand's ray) in app volume
space; turned into content space by the app transform, it is how far the object moves."""
import math
from dataclasses import dataclass, field

# QarGestureKind and QarGesturePhase values (qar_streaming_c types.h).
CLICK, HOVER, ONE_HAND = 0, 1, 2
STARTED, UPDATED, ENDED, INSTANT, CANCELED = 0, 1, 2, 3, 4


def content_to_blender(point):
    """App content space (Y up) -> Blender world (Z up): the inverse of sync.py's Z_UP_TO_Y_UP."""
    x, y, z = point
    return (x, -z, y)


# How old a hover hit may be to stand in for a click that carries none (seconds). Hovers stop while
# a hand rests (one goes out per fresh hand sample; the Visualizer's simulated hand at rest sends
# the same one, measured 1.25 s from the last hover to the click), so the window covers a pause.
HOVER_FALLBACK = 2.0


# Recent hovers kept per user and hand: a peer can have two hands of one side (the Visualizer's
# pointing hand beside its simulated pinching hand), told apart by where each starts.
HOVERS_KEPT = 16


def closest_hover(hovers, start, now, window=None):
    """Of `hovers`, [(time, (start, action))] newest last, the newest within `window` seconds whose
    start is nearest the click's `start`: the hover of the hand that clicked. None when none is."""
    window = HOVER_FALLBACK if window is None else window
    best = None
    for seen, (hover_start, action) in reversed(hovers):
        if now - seen > window:
            break
        distance = sum((a - b) ** 2 for a, b in zip(hover_start, start))
        if best is None or distance < best[0] - 1.0e-9:
            best = (distance, (hover_start, action))
    return None if best is None else best[1]


def has_hit(event):
    """Whether a gesture carries a hit: without one its action point is its start point."""
    start, action = event.get("start"), event.get("action")
    return action is not None and start is not None and tuple(action) != tuple(start)


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


def volume_to_content_delta(delta, app_orientation, app_scale):
    """A displacement in app volume space as one in content space: the app transform places content
    in the volume rotated by `app_orientation` and scaled by `app_scale`; its inverse undoes both."""
    x, y, z, w = app_orientation
    unrotated = rotate((-x, -y, -z, w), delta)
    scale = app_scale if app_scale > 1.0e-9 else 1.0
    return tuple(c / scale for c in unrotated)


def click_ray(event, hover=None):
    """(origin, unit direction) in Blender's world for a click: from the hand when it pinched
    towards the content it pointed at. A click without a hit takes `hover`, the same hand's latest
    hover as (start, action), and casts that hover's own ray: a pinch moves the hand (a simulated
    pinch also its depth), so the click can carry no hit while the hover just before it pointed at
    the object, and the click's hand with the hover's hit is a ray nobody aimed. None when there is
    nothing to aim at."""
    start, action = event.get("start"), event.get("action")
    if not has_hit(event):
        start, action = hover if hover is not None else (None, None)
    if start is None or action is None:
        return None
    origin = content_to_blender(start)
    target = content_to_blender(action)
    direction = tuple(t - o for t, o in zip(target, origin))
    length = math.sqrt(sum(d * d for d in direction))
    if length < 1.0e-6:
        return None
    return origin, tuple(d / length for d in direction)


@dataclass
class Drag:
    """One user's drag of their selected object."""

    object_name: str
    object_start: tuple  # the object's world translation when the drag began
    moved: bool = False


@dataclass
class Pointing:
    """Per-user state: which object each user selected, and their drag in progress. Pure: the
    caller reads and writes Blender and the gesture modes."""

    selected: dict = field(default_factory=dict)  # peer -> object name
    drags: dict = field(default_factory=dict)  # (peer, gesture id) -> Drag

    def clicked(self, peer, object_name):
        """A click by `peer` hit `object_name` (None: nothing). Returns the mode it needs:
        'object' when it now has a selection, 'default' when not."""
        if object_name is None:
            self.selected.pop(peer, None)
            return "default"
        self.selected[peer] = object_name
        return "object"

    def wanted_mode(self, peer, aimed_name):
        """The mode `peer` needs while their ray is on `aimed_name` (None: on nothing Blender
        picks): 'object' only on their own selection, so a one-hand drag grabs it; 'default'
        everywhere else, so a drag moves the view."""
        name = self.selected.get(peer)
        return "object" if name is not None and aimed_name == name else "default"

    def dragging(self, peer):
        """Whether `peer` is moving an object now: their mode is left alone until it ends."""
        return any(key[0] == peer for key in self.drags)

    def drag_started(self, peer, gesture_id, object_world, grabbed_name):
        """A one-hand drag began on `grabbed_name` (what its ray hit, None: nothing); returns the
        object it moves, or None. Only a drag that grabbed the user's selection moves it: a pinch
        on a GUI panel or on empty space reaches the volume too and must move nothing."""
        name = self.selected.get(peer)
        if name is None or grabbed_name != name:
            return None
        self.drags[(peer, gesture_id)] = Drag(name, tuple(object_world))
        return name

    def drag_moved(self, peer, gesture_id, content_delta):
        """(object name, new world translation) for a drag update whose accumulated motion is
        `content_delta` (content space), or None."""
        drag = self.drags.get((peer, gesture_id))
        if drag is None or content_delta is None:
            return None
        delta = content_to_blender(content_delta)
        drag.moved = True
        return drag.object_name, tuple(o + d for o, d in zip(drag.object_start, delta))

    def drag_finished(self, peer, gesture_id, canceled):
        """Ends a drag. Returns (object name, translation to restore or None, moved): a canceled drag
        puts the object back where it began."""
        drag = self.drags.pop((peer, gesture_id), None)
        if drag is None:
            return None
        return drag.object_name, drag.object_start if canceled else None, drag.moved

    def forget_object(self, object_name):
        """An object that is gone (deleted, renamed) is nobody's selection any more. Returns the peers
        that lose their selection, to switch back to the default mode."""
        peers = [peer for peer, name in self.selected.items() if name == object_name]
        for peer in peers:
            del self.selected[peer]
        for key in [key for key, drag in self.drags.items() if drag.object_name == object_name]:
            del self.drags[key]
        return peers
