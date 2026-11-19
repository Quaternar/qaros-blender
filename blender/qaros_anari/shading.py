# SPDX-License-Identifier: MIT
"""How the stream shades the scene, after the 3D viewport's shading (Wireframe, Solid, Material
Preview, Rendered) or a fixed choice in the QAROS tab. Plain Python: no bpy, so the mapping is unit
tested; `from_view` reads a View3DShading duck-typed.

| Viewport | Stream |
|---|---|
| Wireframe | the mesh edges as thin cylinders, one light grey matte material, studio light |
| Solid | matte, in Blender's solid colour (material viewport colour, or the single colour), studio light |
| Material Preview | the exported materials; scene lights and World only when the viewport uses them, else the viewport's studio HDRI |
| Rendered | the exported materials, scene lights and World (the stream before this mode existed), or the studio HDRI when the viewport leaves the World out |"""
from dataclasses import dataclass, field

from .materials import COLOR_KEY_PREFIX, MaterialDescription

WIREFRAME = "WIREFRAME"
SOLID = "SOLID"
MATERIAL = "MATERIAL"
RENDERED = "RENDERED"
MODES = (WIREFRAME, SOLID, MATERIAL, RENDERED)
# The QAROS tab's "follow the 3D viewport" choice.
FOLLOW = "VIEWPORT"

# Blender's solid grey, and a wire light enough to read against the room.
SOLID_GREY = (0.8, 0.8, 0.8)
WIRE_COLOR = (0.85, 0.85, 0.85)
# Material key of the wires: one material, so every object's wires can merge into one chunk. No
# Blender material name starts with a control character.
WIRE_KEY = "\x1fwire"
WIRE_MATERIAL = MaterialDescription(base_color=WIRE_COLOR)
# Wire radius as a share of the scene's bounding box diagonal, and its floor in Blender units.
WIRE_RADIUS_SHARE = 0.001
WIRE_RADIUS_MIN = 1e-4


@dataclass(frozen=True)
class Shading:
    """One stream shading. Two equal values stream the same picture, so a change of any field is a
    full resync, and fields a mode ignores keep their defaults."""

    mode: str = RENDERED
    color_type: str = "MATERIAL"  # Solid only: Blender's View3DShading.color_type
    single_color: tuple = SOLID_GREY  # Solid only, color_type SINGLE
    scene_lights: bool = True
    scene_world: bool = True
    # Material Preview, and Rendered without the scene World: Blender lights the viewport with a
    # studio HDRI (its file, intensity and Z rotation), which the stream takes in place of the
    # World. Intensity and rotation change no geometry, so they stay out of the comparison:
    # Streamer.follow_shading sends only the lights again when just they change.
    studio_light: str = ""
    studio_strength: float = field(default=1.0, compare=False)
    studio_rotation: float = field(default=0.0, compare=False)

    @property
    def wire(self):
        return self.mode == WIREFRAME

    @property
    def studio(self):
        """Everything about the studio HDRI, for telling a changed intensity or rotation apart."""
        return (self.studio_light, self.studio_strength, self.studio_rotation)


def _studio(view):
    """The viewport's studio HDRI as Shading fields; none when the view names no file."""
    light = getattr(view, "selected_studio_light", None)
    path = str(getattr(light, "path", "") or "")
    if not path:
        return {}
    return dict(
        studio_light=path,
        studio_strength=round(float(getattr(view, "studiolight_intensity", 1.0)), 4),
        studio_rotation=round(float(getattr(view, "studiolight_rotate_z", 0.0)), 4),
    )


def from_view(view, mode=None):
    """The stream shading for a View3DShading (`view`, None without a 3D view), in its own mode
    or in `mode`. Without a view: Rendered, the stream as it was before following the viewport."""
    mode = mode or (view.type if view is not None else RENDERED)
    if mode == WIREFRAME:
        return Shading(WIREFRAME, scene_lights=False, scene_world=False)
    if mode == SOLID:
        if view is None:
            return Shading(SOLID, scene_lights=False, scene_world=False)
        color_type = view.color_type
        single = tuple(round(float(value), 4) for value in view.single_color) if color_type == "SINGLE" else SOLID_GREY
        return Shading(SOLID, color_type, single, scene_lights=False, scene_world=False)
    if mode == MATERIAL:
        if view is None:
            return Shading(MATERIAL, scene_lights=False, scene_world=False)
        world = bool(view.use_scene_world)
        return Shading(
            MATERIAL, scene_lights=bool(view.use_scene_lights), scene_world=world, **({} if world else _studio(view))
        )
    if view is None:
        return Shading(RENDERED)
    world = bool(view.use_scene_world_render)
    return Shading(
        RENDERED, scene_lights=bool(view.use_scene_lights_render), scene_world=world,
        **({} if world else _studio(view)),
    )


def shaded_description(key, description, shading):
    """The material `key` (`description`, the exported one) streams with under `shading`. Solid
    and wireframe are matte (not principled): every backend then takes its matte model, without
    the grain of a physically based one."""
    if shading.mode == WIREFRAME:
        return WIRE_MATERIAL
    if shading.mode != SOLID:
        return description
    if shading.color_type == "SINGLE":
        color = shading.single_color
    elif shading.color_type == "VERTEX" and key.startswith(COLOR_KEY_PREFIX):
        color = description.base_color
    elif shading.color_type == "MATERIAL" and key and not key.startswith(COLOR_KEY_PREFIX):
        color = description.viewport_color
    else:
        # Object, Random and Texture colours, and faces without a material: Blender's grey.
        color = SOLID_GREY
    return MaterialDescription(base_color=tuple(color), viewport_color=tuple(color))


def wire_radius(lower, upper):
    """The wires' radius in Blender units for a scene with these bounds (None: nothing visible)."""
    if lower is None or upper is None:
        return WIRE_RADIUS_MIN * 10
    diagonal = sum((high - low) ** 2 for low, high in zip(lower, upper)) ** 0.5
    return max(WIRE_RADIUS_MIN, diagonal * WIRE_RADIUS_SHARE)


LABELS = {
    WIREFRAME: "Wireframe",
    SOLID: "Solid",
    MATERIAL: "Material Preview",
    RENDERED: "Rendered",
}
ICONS = {
    FOLLOW: "LINKED",
    WIREFRAME: "SHADING_WIRE",
    SOLID: "SHADING_SOLID",
    MATERIAL: "SHADING_TEXTURE",
    RENDERED: "SHADING_RENDERED",
}
