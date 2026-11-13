# SPDX-License-Identifier: MIT
"""Blender's own Python panels, replayed for the controls page.

A panel's `draw(self, context)` builds its UI by calling `layout.prop(data, "name")` and friends.
Replaying it against a layout that records those calls gives the panel's fields in their order,
with the RNA type, limits and current value of each, without Blender drawing anything. Only Python
panels replay; C templates (`template_ID`, `template_list`, node views) become a note on the page.

Runs on Blender's UI thread only."""
import inspect
import math
import types

import bpy

# The panels the page shows by default, in tab order: (panel class name, page title). The add-on's
# preferences choose others from `available_panels`.
PANELS = (
    ("OBJECT_PT_transform", "Transform"),
    ("OBJECT_PT_relations", "Relations"),
    ("OBJECT_PT_visibility", "Visibility"),
)
DEFAULT_PANELS = tuple(name for name, _ in PANELS)
# The Properties editor tabs whose panels the replay context can serve (it knows the active object,
# its data, its material and the scene), with the heading the preferences group them under.
PANEL_CONTEXTS = (("object", "Object"), ("data", "Object Data"), ("material", "Material"), ("physics", "Physics"))
# The ID collections a field's owner may live in, by the owner's RNA type.
ID_COLLECTIONS = {
    "Object": "objects",
    "Scene": "scenes",
    "Material": "materials",
    "Mesh": "meshes",
    "Light": "lights",
    "Camera": "cameras",
    "World": "worlds",
    "Collection": "collections",
}
# Component labels of a vector by its RNA subtype.
COMPONENT_LABELS = {
    "TRANSLATION": "XYZ",
    "XYZ": "XYZ",
    "EULER": "XYZ",
    "DIRECTION": "XYZ",
    "VELOCITY": "XYZ",
    "ACCELERATION": "XYZ",
    "COLOR": "RGBA",
    "COLOR_GAMMA": "RGBA",
    "QUATERNION": "WXYZ",
    "AXISANGLE": "WXYZ",
}
# A soft range wider than this is not a slider range: the page centres a window on the value.
SLIDER_SPAN_LIMIT = 1.0e5
# Text fields: the longest string an unlimited RNA string takes from the page, in UTF-8 bytes.
TEXT_LIMIT = 1024
# A data-block name, in UTF-8 bytes (Blender 4.5+ allows 255).
ID_NAME_LIMIT = 255
# Names offered under a link or search field; the whole list rides in every panels message.
CHOICES_LIMIT = 200
# Strings that name files on the PC stay read-only on the page.
READ_ONLY_STRING_SUBTYPES = {"FILE_PATH", "DIR_PATH", "FILE_NAME", "BYTE_STRING", "PASSWORD"}


class ContextProxy:
    """The context a panel's draw expects in the Properties editor, built from Blender's context in a
    timer (where `context.object` and `context.engine` are not set)."""

    def __init__(self, base, **overrides):
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)


# The Properties editor's context member for an object's data, by object type (None for the others).
DATA_MEMBERS = {
    "MESH": "mesh",
    "CURVE": "curve",
    "SURFACE": "curve",
    "FONT": "curve",
    "CAMERA": "camera",
    "LIGHT": "light",
    "ARMATURE": "armature",
    "LATTICE": "lattice",
    "META": "meta_ball",
    "LIGHT_PROBE": "lightprobe",
    "SPEAKER": "speaker",
    "VOLUME": "volume",
    "GREASEPENCIL": "grease_pencil",
    "CURVES": "curves",
    "POINTCLOUD": "pointcloud",
}
# The Properties editor's physics members: the object's modifier of that type, else None.
PHYSICS_MEMBERS = {
    "cloth": "CLOTH",
    "soft_body": "SOFT_BODY",
    "fluid": "FLUID",
    "dynamic_paint": "DYNAMIC_PAINT",
    "collision": "COLLISION",
}


def properties_context(obj):
    """What the Properties editor sets for `obj`: every data and physics member is present, None
    where it does not apply, so a panel's poll says no instead of raising."""
    base = bpy.context
    scene = base.scene
    members = {member: None for member in set(DATA_MEMBERS.values()) | set(PHYSICS_MEMBERS)}
    if obj is not None:
        if obj.type in DATA_MEMBERS:
            members[DATA_MEMBERS[obj.type]] = obj.data
        for member, kind in PHYSICS_MEMBERS.items():
            members[member] = next((m for m in obj.modifiers if m.type == kind), None)
    systems = getattr(obj, "particle_systems", None)
    return ContextProxy(
        base,
        object=obj,
        active_object=obj,
        engine=scene.render.engine,
        material=obj.active_material if obj is not None else None,
        particle_system=systems.active if systems else None,
        # A Properties editor with nothing pinned: panels read `space_data.pin_id`.
        space_data=types.SimpleNamespace(pin_id=None, use_pin_id=False, type="PROPERTIES"),
        **members,
    )


class _ReplayedPanel:
    """`self` for a panel's draw: the recording layout, plus the panel class's own attributes and
    helper methods (`_context_path`, `draw_action_and_slot_selector`), bound to this object."""

    def __init__(self, cls, layout):
        self.layout = layout
        self._cls = cls

    def __getattr__(self, name):
        value = getattr(self._cls, name)
        return value.__get__(self) if inspect.isfunction(value) else value


def _finite(value):
    return value is not None and math.isfinite(value)


def field_id(data, prop):
    """`<collection>:<ID name>:<RNA path>`, or None when the owner is not an ID Blender lists."""
    owner = getattr(data, "id_data", None)
    if owner is None:
        return None
    collection = ID_COLLECTIONS.get(owner.bl_rna.identifier)
    if collection is None:
        return None
    try:
        path = data.path_from_id(prop)
    except (ValueError, TypeError):
        return None
    return f"{collection}:{owner.name}:{path}"


def describe(data, prop, text, index, invert=False):
    """The page's field for `layout.prop(data, prop)`, or a read-only line for a type the page
    cannot edit."""
    rna = data.bl_rna.properties.get(prop)
    if rna is None:
        return {"kind": "note", "text": f"{prop}: not found"}
    label = text if text not in (None, "") else rna.name
    value = getattr(data, prop)
    if rna.type == "POINTER":
        return _pointer_field(data, prop, rna, label, value)
    if rna.type == "STRING":
        return _string_field(data, prop, rna, label, value)
    if rna.type == "COLLECTION":
        return {"kind": "note", "text": f"{label}: a list, edited in Blender"}
    identity = field_id(data, prop)
    field = {
        "kind": "field",
        "id": identity,
        "label": label,
        "type": rna.type.lower(),
        "length": getattr(rna, "array_length", 0) or 0,
        "index": index if index is not None and index >= 0 else -1,
        "readonly": identity is None or rna.is_readonly,
    }
    if rna.type in ("FLOAT", "INT"):
        degrees = rna.type == "FLOAT" and rna.unit == "ROTATION"
        scale = 180.0 / math.pi if degrees else 1.0
        field["unit"] = "deg" if degrees else ""
        field["min"] = rna.hard_min * scale if _finite(rna.hard_min) else None
        field["max"] = rna.hard_max * scale if _finite(rna.hard_max) else None
        low, high = rna.soft_min * scale, rna.soft_max * scale
        if _finite(low) and _finite(high) and high - low <= SLIDER_SPAN_LIMIT:
            field["soft"] = [low, high]
        step = getattr(rna, "step", 1)
        # RNA float steps are Blender's drag step per pixel, in hundredths (location: 1 = 0.01). A
        # step button moves ten of those (0.1), coarse enough for a finger; rotations step 5 degrees.
        field["step"] = 5.0 if degrees else (max(step / 10.0, 0.001) if rna.type == "FLOAT" else max(1, step))
        field["value"] = _numbers(value, scale)
        field["components"] = COMPONENT_LABELS.get(rna.subtype, "")
    elif rna.type == "BOOLEAN":
        field["value"] = list(value) if field["length"] else bool(value)
        # Drawn inverted in Blender (`invert_checkbox`): "Selectable" is `hide_select` off. The page
        # shows and sends the drawn meaning; the add-on writes the property as it is.
        field["invert"] = bool(invert)
        field["components"] = COMPONENT_LABELS.get(rna.subtype, "XYZ" if field["length"] == 3 else "")
    elif rna.type == "ENUM":
        if rna.is_enum_flag:
            return {"kind": "text", "label": label, "value": ", ".join(sorted(value))}
        field["items"] = [[item.identifier, item.name] for item in rna.enum_items]
        field["value"] = value
    else:
        return {"kind": "text", "label": label, "value": str(value)}
    return field


def _text_base(data, prop, rna, label, kind):
    identity = field_id(data, prop)
    return {
        "kind": "field",
        "id": identity,
        "label": label,
        "type": kind,
        "length": 0,
        "index": -1,
        "readonly": identity is None or rna.is_readonly,
    }


def _string_field(data, prop, rna, label, value):
    """A text field typed on the mixer's keyboard. Paths stay read-only: a headset user must not
    point Blender at files on the PC."""
    field = _text_base(data, prop, rna, label, "string")
    field["value"] = str(value)
    # length_max counts the terminating NUL; 0 means unlimited.
    field["maxlen"] = rna.length_max - 1 if rna.length_max > 0 else TEXT_LIMIT
    if rna.subtype in READ_ONLY_STRING_SUBTYPES:
        field["readonly"] = True
    return field


def _pointer_field(data, prop, rna, label, value):
    """A link to a data-block, set by typing or tapping one of its collection's names. A pointer
    to anything that is not a listed data-block stays a read-only line."""
    collection = ID_COLLECTIONS.get(rna.fixed_type.identifier) if rna.fixed_type else None
    if collection is None:
        return {"kind": "text", "label": label, "value": getattr(value, "name", "") if value else ""}
    field = _text_base(data, prop, rna, label, "pointer")
    field["value"] = value.name if value else ""
    field["collection"] = collection
    field["maxlen"] = ID_NAME_LIMIT
    own = getattr(data, "id_data", None)
    # An object cannot be its own parent: leave the owner out of its own choices.
    field["choices"], field["more"] = _names(getattr(bpy.data, collection), exclude=own)
    return field


def _names(collection, exclude=None):
    """(sorted names, whether more were left out) of a collection, at most CHOICES_LIMIT."""
    names = sorted(item.name for item in collection if item != exclude)
    return names[:CHOICES_LIMIT], len(names) > CHOICES_LIMIT


def _numbers(value, scale):
    if hasattr(value, "__len__"):
        return [float(v) * scale for v in value]
    return float(value) * scale


class Recorder:
    """A UILayout that records instead of drawing: `prop` becomes a field, `label` and a column's
    `heading` a heading, every other call (operators, menus, templates) a note at most."""

    def __init__(self, items):
        self._items = items
        self.use_property_split = False
        self.use_property_decorate = False
        self.enabled = True
        self.active = True
        self.alert = False
        self.alignment = "EXPAND"
        self.scale_x = 1.0
        self.scale_y = 1.0
        self.emboss = "NORMAL"
        self.operator_context = "INVOKE_DEFAULT"

    def _sub(self, heading=""):
        if heading:
            self._items.append({"kind": "heading", "text": heading})
        return Recorder(self._items)

    def row(self, heading="", **_):
        return self._sub(heading)

    def column(self, heading="", **_):
        return self._sub(heading)

    def box(self, **_):
        return self._sub()

    def split(self, **_):
        return self._sub()

    def grid_flow(self, **_):
        return self._sub()

    def column_flow(self, **_):
        return self._sub()

    def panel(self, *_args, **_kwargs):
        # Blender 4.1+ layout panels: (header, body). Both record into the same list.
        return self._sub(), self._sub()

    def prop(self, data, prop, text=None, index=-1, invert_checkbox=False, **_):
        if data is None:
            return
        self._items.append(describe(data, prop, text, index, invert_checkbox))

    def prop_search(self, data, prop, search_data, search_prop, text=None, **_):
        """A text or link field whose valid values are the names in `search_data.search_prop`
        (Parent Bone: the armature's bones)."""
        if data is None:
            return
        field = describe(data, prop, text, -1)
        if field.get("type") in ("string", "pointer") and search_data is not None:
            field["choices"], field["more"] = _names(getattr(search_data, search_prop, ()))
        self._items.append(field)

    def label(self, text="", **_):
        if text:
            self._items.append({"kind": "heading", "text": text})

    def separator(self, **_):
        pass

    def operator(self, operator, text=None, **_):
        return _Ignored()

    def __getattr__(self, name):
        # Templates, menus, popovers: not replayed. Named once so the page says what is missing.
        def unsupported(*_args, **_kwargs):
            self._items.append({"kind": "note", "text": f"{name.replace('_', ' ')}: in Blender"})
            return Recorder(self._items)

        return unsupported


class _Ignored:
    """What `operator` returns: the draw code may set its properties; they are dropped."""

    def __setattr__(self, name, value):
        pass

    def __getattr__(self, name):
        return None


def replay(panel_name, obj):
    """The items of one panel for `obj`, or a single note when it does not apply or fails."""
    cls = getattr(bpy.types, panel_name, None)
    if cls is None:
        return [{"kind": "note", "text": "Not in this Blender"}]
    context = properties_context(obj)
    poll = getattr(cls, "poll", None)
    try:
        if poll is not None and not poll(context):
            return [{"kind": "note", "text": "Not available for this object"}]
    except Exception:  # noqa: BLE001 - a poll that needs more context than the proxy has
        return [{"kind": "note", "text": "Not available here: edit it in Blender"}]
    items = []
    fake = _ReplayedPanel(cls, Recorder(items))
    try:
        cls.draw(fake, context)
    except Exception as error:  # noqa: BLE001 - keep what was recorded, say why it stopped
        items.append({"kind": "note", "text": f"Stopped: {type(error).__name__}"})
    return items


def panels_state(chosen=DEFAULT_PANELS):
    """Every page panel for the active object: [{name, title, items}], the object's name first,
    then the `chosen` panel classes in their order."""
    obj = bpy.context.view_layer.objects.active
    if obj is None:
        return []
    # The object's name is a C template in Blender's header, not in a Python panel: its own entry.
    named = {"name": "OBJECT_name", "title": "Object", "items": [describe(obj, "name", "Name", -1)]}
    return [named] + [{"name": name, "title": panel_title(name), "items": replay(name, obj)} for name in chosen]


def _registered_panel(name):
    cls = getattr(bpy.types, name, None)
    return cls if isinstance(cls, type) and issubclass(cls, bpy.types.Panel) else None


def panel_title(name, with_parent=False):
    """A panel's label; with its parent's in front for a sub-panel when asked ("Shading › Wireframe")."""
    cls = _registered_panel(name)
    if cls is None:
        return name
    label = getattr(cls, "bl_label", "") or name
    parent = _registered_panel(getattr(cls, "bl_parent_id", "")) if with_parent else None
    return f"{parent.bl_label} › {label}" if parent is not None else label


def _panel_subclasses(base):
    for cls in base.__subclasses__():
        yield cls
        yield from _panel_subclasses(cls)


def available_panels():
    """[(heading, [(class name, title)])] of every registered Python panel of the Properties editor
    tabs in PANEL_CONTEXTS, in Blender's registration order: what the preferences offer. Whether one
    shows anything for a given object is up to its own poll and draw."""
    found = {context: [] for context, _ in PANEL_CONTEXTS}
    seen = set()
    for cls in _panel_subclasses(bpy.types.Panel):
        name = getattr(cls, "bl_idname", "") or cls.__name__
        context = getattr(cls, "bl_context", "")
        if name in seen or context not in found or getattr(cls, "bl_space_type", "") != "PROPERTIES":
            continue
        # Only what `replay` can run: registered under its name, with a Python `draw`.
        if _registered_panel(name) is not cls or not inspect.isfunction(getattr(cls, "draw", None)):
            continue
        # Header-less panels are the tabs' context bars (the data-block selector): a C template.
        if "HIDE_HEADER" in getattr(cls, "bl_options", set()):
            continue
        seen.add(name)
        found[context].append((name, panel_title(name, with_parent=True)))
    return [(heading, found[context]) for context, heading in PANEL_CONTEXTS]


def exposed_fields(state):
    """id -> field of every editable field in `state` (what an edit is checked against)."""
    fields = {}
    for panel in state:
        for item in panel["items"]:
            if item.get("kind") == "field" and item.get("id") and not item.get("readonly"):
                fields[item["id"]] = item
    return fields


def resolve(identity):
    """(owner struct, property name) of a field id, or None when it is gone."""
    collection, name, path = identity.split(":", 2)
    owner = getattr(bpy.data, collection).get(name)
    if owner is None:
        return None
    owner_path, _, prop = path.rpartition(".")
    if owner_path:
        try:
            owner = owner.path_resolve(owner_path)
        except ValueError:
            return None
    return owner, prop


def apply_prop(field, index, value):
    """Writes a value checked by protocol.coerce_prop. False when the field is gone."""
    target = resolve(field["id"])
    if target is None:
        return False
    owner, prop = target
    if field["type"] == "float" and field.get("unit") == "deg":
        value = math.radians(value)
    if field["type"] == "pointer":
        return _apply_pointer(owner, prop, field["collection"], value)
    if field["length"]:
        component = index if field["index"] < 0 else field["index"]
        getattr(owner, prop)[component] = value
    else:
        setattr(owner, prop, value)
    return True


def _apply_pointer(owner, prop, collection, name):
    """Links `owner.prop` to the data-block called `name` ("" unlinks), as Blender's own field
    does (setting Parent keeps no parent inverse, so the child moves, as in Blender's UI). False
    when no such data-block exists or Blender refuses it (a parent loop, a wrong type)."""
    target = None
    if name:
        target = getattr(bpy.data, collection).get(name)
        if target is None:
            return False
    try:
        setattr(owner, prop, target)
    except (TypeError, ValueError, AttributeError):
        return False
    return getattr(owner, prop) == target
