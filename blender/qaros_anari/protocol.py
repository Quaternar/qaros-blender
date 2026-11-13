# SPDX-License-Identifier: MIT
"""What the controls page and the add-on say to each other, without Blender: message validation,
the edit queue between QAR's library thread and Blender's UI thread, and the state message.

Page -> add-on (one JSON value per message):
  {"t": "hello"}                                         the page loaded; answer with the state
  {"t": "set", "key": K, "index": i, "value": v, "final": b}
                                                         one control moved; `final` on release
  {"t": "call", "id": n, "name": "undo" | "redo" | "play"}
  {"t": "prop", "id": F, "index": i, "value": v, "final": b}
                                                         one field of a replayed Blender panel;
                                                         checked against that field (coerce_prop)
Add-on -> page:
  {"t": "state", "key": "scene", "value": {...}}         see `state_message`
  {"t": "state", "key": "panels", "value": [...]}        the replayed panels (blender_panels.py)
  {"t": "result", "id": n, "ok": b, "error": "..."}      the answer to a call
"""
import json
import math
import threading
from dataclasses import dataclass, replace

# Keys whose value is a vector, edited one component (`index`) at a time.
VECTOR_KEYS = {"location": 3, "rotation": 3, "scale": 3, "color": 3}
# Keys with one value.
SCALAR_KEYS = {"roughness", "metallic", "frame"}
CALLS = {"undo", "redo", "play"}
# What a value is held to, so a malformed or hostile message cannot put Blender far off.
LIMITS = {
    "location": (-1.0e4, 1.0e4),
    "rotation": (-36000.0, 36000.0),  # degrees
    "scale": (1.0e-3, 1.0e3),
    "color": (0.0, 1.0),
    "roughness": (0.0, 1.0),
    "metallic": (0.0, 1.0),
    "frame": (-1.0e6, 1.0e6),
}
MAX_MESSAGE_BYTES = 64 * 1024


@dataclass(frozen=True)
class Set:
    key: str
    index: int
    value: float
    final: bool
    user: str


@dataclass(frozen=True)
class PropSet:
    id: str
    index: int
    value: object
    final: bool
    user: str


@dataclass(frozen=True)
class Call:
    id: int
    name: str
    user: str


@dataclass(frozen=True)
class Hello:
    user: str


class Refused(ValueError):
    pass


def parse(text, user):
    """One page message as a Set, Call or Hello. Raises Refused for anything else: every
    message is user input, and only these shapes are acted on."""
    if len(text) > MAX_MESSAGE_BYTES:
        raise Refused("message too large")
    try:
        message = json.loads(text)
    except ValueError as error:
        raise Refused(f"not JSON: {error}") from None
    if not isinstance(message, dict):
        raise Refused("not an object")
    kind = message.get("t")
    if kind == "hello":
        return Hello(user)
    if kind == "call":
        call_id, name = message.get("id"), message.get("name")
        if not isinstance(call_id, int) or isinstance(call_id, bool) or name not in CALLS:
            raise Refused("bad call")
        return Call(call_id, name, user)
    if kind == "prop":
        return _parse_prop(message, user)
    if kind != "set":
        raise Refused(f"unknown message type {kind!r}")
    key, value, final = message.get("key"), message.get("value"), message.get("final", False)
    index = message.get("index", 0)
    if key in VECTOR_KEYS:
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < VECTOR_KEYS[key]:
            raise Refused("bad index")
    elif key in SCALAR_KEYS:
        index = 0
    else:
        raise Refused(f"unknown key {key!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Refused("bad value")
    if not isinstance(final, bool):
        raise Refused("bad final")
    low, high = LIMITS[key]
    return Set(key, index, min(max(float(value), low), high), final, user)


def _parse_prop(message, user):
    identity, index = message.get("id"), message.get("index", -1)
    value, final = message.get("value"), message.get("final", False)
    if not isinstance(identity, str) or not 0 < len(identity) <= 512:
        raise Refused("bad field id")
    if not isinstance(index, int) or isinstance(index, bool) or not -1 <= index < 64:
        raise Refused("bad index")
    if not isinstance(value, (bool, int, float, str)):
        raise Refused("bad value")
    if not isinstance(final, bool):
        raise Refused("bad final")
    return PropSet(identity, index, value, final, user)


def coerce_prop(field, index, value):
    """The value to write for an edit of `field` (an editable field the add-on exposes right now),
    or Refused. Numbers must be finite and are held to the field's hard limits; enums must name one
    of its items; a vector field needs a component index unless the field is one component."""
    if field.get("readonly"):
        raise Refused("read-only field")
    if field["length"] and field["index"] < 0 and not 0 <= index < field["length"]:
        raise Refused("bad component")
    kind = field["type"]
    if kind == "boolean":
        if not isinstance(value, bool):
            raise Refused("not a boolean")
        return value
    if kind == "enum":
        if not isinstance(value, str) or value not in {item[0] for item in field["items"]}:
            raise Refused("not one of the items")
        return value
    if kind in ("string", "pointer"):
        return _coerce_text(field, value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Refused("not a number")
    low, high = field.get("min"), field.get("max")
    value = float(value)
    if low is not None:
        value = max(value, low)
    if high is not None:
        value = min(value, high)
    if kind == "int":
        return int(round(value))
    if kind == "float":
        return value
    raise Refused(f"unsupported field type {kind!r}")


def _coerce_text(field, value):
    """Typed text: no control characters, at most the field's `maxlen` UTF-8 bytes. A field with a
    complete list of `choices` (a link, a search field) takes one of them or "" (none); with the
    list cut short, any name, which the UI thread looks up."""
    if not isinstance(value, str):
        raise Refused("not text")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise Refused("control character")
    if len(value.encode("utf-8")) > field.get("maxlen", 0):
        raise Refused("too long")
    choices = field.get("choices")
    if choices is not None and value and not field.get("more") and value not in choices:
        raise Refused("not one of the choices")
    return value


class EditQueue:
    """Filled on QAR's library thread, drained on Blender's UI thread once per tick. Sets of one
    control coalesce to the latest value, so a drag applies at most one value per tick; a set that
    ended a drag stays final. Calls and hellos keep their order."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sets = {}
        self._others = []

    def put(self, item):
        with self._lock:
            if isinstance(item, (Set, PropSet)):
                slot = (type(item).__name__, item.key if isinstance(item, Set) else item.id, item.index)
                previous = self._sets.pop(slot, None)
                final = item.final or (previous is not None and previous.final)
                self._sets[slot] = replace(item, final=final)
            else:
                self._others.append(item)

    def drain(self):
        """(sets in first-moved order, calls and hellos in arrival order)."""
        with self._lock:
            sets, self._sets = list(self._sets.values()), {}
            others, self._others = self._others, []
        return sets, others


# The page's tabs, in page order; the add-on's preferences switch each on or off.
TABS = ("object", "material", "frame", "panels")
# The tab whose controls send a `set` of a key, or a `call` of a name. Undo and redo are in the
# header, on every tab.
KEY_TABS = {"location": "object", "rotation": "object", "scale": "object",
            "color": "material", "roughness": "material", "metallic": "material", "frame": "frame"}
CALL_TABS = {"play": "frame"}


def shown(item, tabs):
    """Whether a page message acts on a tab that is switched on. A hidden tab's controls are not
    on any page, so a message for them is refused rather than trusted."""
    if isinstance(item, Set):
        return KEY_TABS[item.key] in tabs
    if isinstance(item, PropSet):
        return "panels" in tabs
    if isinstance(item, Call):
        return item.name not in CALL_TABS or CALL_TABS[item.name] in tabs
    return True


def panel_names(text):
    """The chosen panel class names a preference string holds ("A,B"), in order, without repeats."""
    names = []
    for name in (part.strip() for part in text.split(",")):
        if name and name not in names:
            names.append(name)
    return tuple(names)


def toggled_panel(text, name):
    """The preference string with `name` removed when chosen, else appended (shown last)."""
    names = list(panel_names(text))
    if name in names:
        names.remove(name)
    else:
        names.append(name)
    return ",".join(names)


def panels_message(panels, limit=MAX_MESSAGE_BYTES):
    """(the panels state message, the panels it carries). Panels that do not fit in one message are
    left out from the end, and a last entry says how many."""
    kept = list(panels)
    text = state_message(kept, key="panels")
    while len(text.encode("utf-8")) > limit and kept:
        kept.pop()
        dropped = len(panels) - len(kept)
        note = {"name": "QAROS_too_many", "title": "More…", "items": [
            {"kind": "note", "text": f"{dropped} panel(s) left out: choose fewer in the add-on's preferences"}]}
        text = state_message(kept + [note], key="panels")
    return text, kept


def state_message(snapshot, key="scene"):
    """The state the page shows, as compact JSON. Floats are rounded so that a value that did not
    change does not post again."""

    def rounded(value):
        if isinstance(value, float):
            return round(value, 4)
        if isinstance(value, (list, tuple)):
            return [rounded(v) for v in value]
        if isinstance(value, dict):
            return {k: rounded(v) for k, v in value.items()}
        return value

    return json.dumps({"t": "state", "key": key, "value": rounded(snapshot)}, separators=(",", ":"))


def result_message(call, ok, error=None):
    message = {"t": "result", "id": call.id, "ok": bool(ok)}
    if error:
        message["error"] = str(error)
    return json.dumps(message, separators=(",", ":"))
