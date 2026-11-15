# SPDX-License-Identifier: MIT
"""Which QAROS devices a Blender streams to. Plain Python, no bpy, so it is tested outside Blender
(tests/test_blender_targets.py).

The qaros device streams to every target application of the session (headsets, Visualizer
receivers) unless the application sets ``qar.targets``: a list of peer ids, an empty list for none.
It reports the session's targets as JSON in ``qar.sessionTargets`` (docs/user-guide.md).

The choice follows the QAROS Visualizer's **Streams to**:

- **All devices** (the default, also for a Blender QAROS started): every target, also those that
  join later. Nothing is set on the device.
- **A selection**: only the ticked devices. A ticked device that leaves stays ticked and gets the
  stream again when it comes back.
- Unticking All with nothing ticked yet ticks every connected device, so nothing changes until a
  device is unticked. Unticking one device while All is on does the same, without that device.

The choice is saved with the .blend, like the app volume name: a scene's audience goes with the
scene. Ids of devices another QAROS knows are kept and shown as not connected.
"""
import json

# The Scene property's text when nothing is selected yet.
EMPTY = ""


class SessionTarget:
    """A target application the device reports."""

    def __init__(self, peer_id, name="", online=False, selected=True, streaming=False):
        self.peer_id = peer_id
        self.name = name
        self.online = online
        self.selected = selected
        self.streaming = streaming


def parse_session_targets(text):
    """The device's ``qar.sessionTargets`` JSON as SessionTargets, or None when the device does not
    answer it (not streaming yet, or a QAROS without target choice)."""
    if text is None:
        return None
    try:
        items = json.loads(text)
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    targets = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        targets.append(
            SessionTarget(
                normalize_id(item["id"]),
                str(item.get("name") or ""),
                bool(item.get("online")),
                bool(item.get("selected", True)),
                bool(item.get("streaming")),
            )
        )
    return targets


def normalize_id(peer_id):
    return peer_id.replace("-", "").strip().lower()


def short_id(peer_id):
    return peer_id[:8]


def display_name(name, peer_id):
    return name or f"Device {short_id(peer_id)}"


class TargetChoice:
    """All devices, or the selected peer ids with the names they were last seen under."""

    def __init__(self, stream_to_all=True, selected=None):
        self.stream_to_all = stream_to_all
        self.selected = dict(selected or {})  # peer id -> name

    @classmethod
    def from_scene(cls, stream_to_all, text):
        """From the Scene properties: a bool and a JSON object of id -> name. A damaged text keeps
        the selection empty rather than failing the panel."""
        selected = {}
        if text:
            try:
                loaded = json.loads(text)
            except ValueError:
                loaded = {}
            if isinstance(loaded, dict):
                selected = {normalize_id(k): str(v or "") for k, v in loaded.items() if isinstance(k, str)}
        return cls(bool(stream_to_all), selected)

    def selected_text(self):
        """The Scene property's text."""
        if not self.selected:
            return EMPTY
        return json.dumps(dict(sorted(self.selected.items())), ensure_ascii=False, separators=(",", ":"))

    def parameter(self):
        """The ``qar.targets`` value: None to unset it (every target), else comma-separated ids; an
        empty text streams to nobody."""
        if self.stream_to_all:
            return None
        return ",".join(sorted(self.selected))

    def is_ticked(self, peer_id):
        return self.stream_to_all or peer_id in self.selected


def connected(targets):
    return [t for t in targets or () if t.online]


def set_all(choice, on, targets):
    """The All checkbox changed."""
    selected = dict(choice.selected)
    if not on and not selected:
        selected = {t.peer_id: t.name for t in connected(targets)}
    return TargetChoice(on, selected)


def set_target(choice, peer_id, on, targets):
    """One device's checkbox changed. Unticking one while All is on keeps every other connected
    device."""
    names = {t.peer_id: t.name for t in targets or ()}
    selected = dict(choice.selected)
    if choice.stream_to_all:
        selected = {t.peer_id: t.name for t in connected(targets)}
    if on:
        selected[peer_id] = names.get(peer_id, selected.get(peer_id, ""))
    else:
        selected.pop(peer_id, None)
    return TargetChoice(False, selected)


def remember_names(choice, targets):
    """The choice with the selected devices' current names, so a device that leaves keeps its name
    in the list. The same object when nothing changed."""
    changed = False
    selected = dict(choice.selected)
    for target in targets or ():
        if target.peer_id in selected and target.name and selected[target.peer_id] != target.name:
            selected[target.peer_id] = target.name
            changed = True
    return TargetChoice(choice.stream_to_all, selected) if changed else choice


class Row:
    """One line of the panel's device list."""

    def __init__(self, peer_id, label, ticked, online, streaming):
        self.peer_id = peer_id
        self.label = label
        self.ticked = ticked
        self.online = online
        self.streaming = streaming


def rows(choice, targets):
    """Every connected device, then every selected device that is not connected, by name."""
    shown = {}
    for target in connected(targets):
        shown[target.peer_id] = Row(
            target.peer_id,
            display_name(target.name, target.peer_id),
            choice.is_ticked(target.peer_id),
            True,
            target.streaming,
        )
    if not choice.stream_to_all:
        known = {t.peer_id: t for t in targets or ()}
        for peer_id, name in choice.selected.items():
            if peer_id in shown:
                continue
            seen = known.get(peer_id)
            label = display_name((seen.name if seen else "") or name, peer_id)
            shown[peer_id] = Row(peer_id, f"{label} (not connected)", True, False, False)
    return sorted(shown.values(), key=lambda row: (not row.online, row.label.lower(), row.peer_id))


def summary(choice, targets):
    """The line under "Streams to"."""
    if choice.stream_to_all:
        return "All devices"
    count = len(choice.selected)
    if count == 0:
        return "No device: nobody receives the stream"
    online = sum(1 for t in connected(targets) if t.peer_id in choice.selected)
    text = f"{count} selected device{'s' if count != 1 else ''}"
    if targets is not None:
        text += f", {online} connected"
    return text


def over_limit(choice, targets, max_users):
    """A warning when more devices are chosen than the device serves (the Users preference), else
    None."""
    chosen = len(connected(targets)) if choice.stream_to_all else sum(
        1 for t in connected(targets) if t.peer_id in choice.selected
    )
    if chosen <= max_users:
        return None
    return f"Only {max_users} of {chosen} devices get the stream (Preferences: Users)"
