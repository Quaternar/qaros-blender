# SPDX-License-Identifier: MIT
"""Mirrors Blender's evaluated scene into one ANARI world, independent of any viewport or render
engine. Meshes become one triangle geometry per material slot, shared by every instance of the same
object; lights map to ANARI lights, the World to an `hdri` light where the backend has one;
transforms change from Blender's Z-up to ANARI's Y-up.

Two halves, so Blender's UI thread never waits for the qaros scene lock (a target holds it for its
whole render and readback): SceneCapture reads the depsgraph into a Snapshot of plain arrays on the
UI thread, and SceneMirror applies snapshots to the device on a SyncWorker thread. A snapshot the
worker has not started yet absorbs the next one, so a drag sends only the latest transforms."""
import math
import threading
import time

import numpy as np

from . import anari
from .materials import (
    COLOR_KEY_PREFIX,
    FALLBACK_MATERIAL,
    MaterialDescription,
    MaterialDiagnostics,
    material_description,
    material_export,
    material_key,
)
from .world import world_environment

# Blender world (Z up, -Y forward) -> app volume (Y up, -Z forward): (x, y, z) -> (x, z, -y).
Z_UP_TO_Y_UP = np.array(
    [[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], dtype=np.float32
)
GEOMETRY_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT"}


def column_major(matrix):
    """A Blender 4x4 Matrix in app-volume space, as ANARI's column-major FLOAT32_MAT4."""
    converted = Z_UP_TO_Y_UP @ np.array(matrix, dtype=np.float32)
    return converted.T.flatten().tolist()


def direction_in_volume(matrix, local):
    """`local` direction of a Blender object, in app-volume space, normalised."""
    world = np.array(matrix.to_3x3(), dtype=np.float32) @ np.array(local, dtype=np.float32)
    converted = Z_UP_TO_Y_UP[:3, :3] @ world
    length = float(np.linalg.norm(converted))
    return (converted / length).tolist() if length > 0 else [0.0, -1.0, 0.0]


def mesh_arrays(mesh):
    """Per-corner positions and normals plus triangle corner indices, each triangle's material
    slot, each corner's vertex and whether each triangle is flat-shaded; mesh_parts welds."""
    mesh.calc_loop_triangles()
    corners = len(mesh.loops)
    triangles = len(mesh.loop_triangles)
    vertex_positions = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", vertex_positions)
    corner_vertices = np.empty(corners, dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", corner_vertices)
    positions = vertex_positions.reshape(-1, 3)[corner_vertices]
    normals = np.empty(corners * 3, dtype=np.float32)
    mesh.corner_normals.foreach_get("vector", normals)
    indices = np.empty(triangles * 3, dtype=np.uint32)
    mesh.loop_triangles.foreach_get("loops", indices)
    slots = np.empty(triangles, dtype=np.int32)
    mesh.loop_triangles.foreach_get("material_index", slots)
    polygons = np.empty(triangles, dtype=np.int32)
    mesh.loop_triangles.foreach_get("polygon_index", polygons)
    smooth = np.empty(len(mesh.polygons), dtype=bool)
    mesh.polygons.foreach_get("use_smooth", smooth)
    flat = ~smooth[polygons]
    return positions, normals.reshape(-1, 3), indices.reshape(-1, 3), slots, corner_vertices, flat


def weld_corners(corners, corner_vertices, normals=None):
    """One output vertex per (Blender vertex, normal) among `corners`, the triangle corner indices
    of one part, or per Blender vertex alone when `normals` is None (flat faces, sent without
    normals). A rasterizing backend shades every vertex it is given; per-corner vertices made
    VisGL's frames about 2.8 times slower for the same triangles. Returns the corners kept and the
    triangles re-indexed into them."""
    corners = corners.ravel()
    if normals is None:
        keys = corner_vertices[corners]
    else:
        keys = np.empty(len(corners), dtype=[("vertex", np.int32), ("normal", np.float32, 3)])
        keys["vertex"] = corner_vertices[corners]
        keys["normal"] = normals[corners]
    _, first, remapped = np.unique(keys, return_index=True, return_inverse=True)
    return corners[first], remapped.reshape(-1, 3).astype(np.uint32)


# Faces with no material take their colour from the mesh's active colour attribute (a USD file's
# displayColor, vertex paint), as Blender's Solid view and Hydra show them. Each distinct
# face colour becomes a material of its own (key: COLOR_KEY_PREFIX and the colour), so objects of
# one colour still merge into one chunk. Colours are compared at 12 bits per channel.
COLOR_STEPS = 4095
# More distinct face colours than this on one object (a gradient, a colour map) would split it
# into that many draws; it is then streamed in its mean colour instead.
MAX_ATTRIBUTE_COLORS = 16
MEAN_COLOR_WARNING = (
    f"Colour attribute has more than {MAX_ATTRIBUTE_COLORS} face colours; streaming its mean colour."
)


def corner_colors(mesh, corner_vertices):
    """The active colour attribute of `mesh` per corner, linear RGB, or None without one."""
    attribute = mesh.color_attributes.active_color
    if attribute is None or attribute.domain not in ("POINT", "CORNER"):
        return None
    values = np.empty(len(attribute.data) * 4, dtype=np.float32)
    attribute.data.foreach_get("color", values)
    values = values.reshape(-1, 4)[:, :3]
    return values[corner_vertices] if attribute.domain == "POINT" else values


def color_key(steps):
    return COLOR_KEY_PREFIX + "".join(f"{int(step):03x}" for step in steps)


def color_groups(triangle_colors):
    """Splits triangles by colour: [(material key, description, triangle mask)], one per distinct
    colour, or one in the mean colour (with a warning) above MAX_ATTRIBUTE_COLORS."""
    steps = np.clip(np.round(triangle_colors * COLOR_STEPS), 0, COLOR_STEPS).astype(np.int32)
    distinct, inverse = np.unique(steps, axis=0, return_inverse=True)
    inverse = inverse.ravel()
    if len(distinct) > MAX_ATTRIBUTE_COLORS:
        mean = np.round(steps.mean(axis=0)).astype(np.int32)
        description = MaterialDescription(
            base_color=tuple(float(v) / COLOR_STEPS for v in mean), warnings=(MEAN_COLOR_WARNING,)
        )
        return [(color_key(mean), description, np.ones(len(steps), dtype=bool))]
    return [
        (color_key(color), MaterialDescription(base_color=tuple(float(v) / COLOR_STEPS for v in color)),
         inverse == index)
        for index, color in enumerate(distinct)
    ]


def slot_groups(material, in_slot, indices, colors):
    """(material key, description or None, triangle mask) for the triangles of one material slot:
    the slot's material, or its faces' attribute colours when it has none. A None description
    means: describe `material` when the key is new."""
    if material is not None or colors is None:
        return [(material_key(material), None, in_slot)]
    triangle_colors = colors[indices[in_slot]].mean(axis=1)
    groups = []
    for key, description, mask in color_groups(triangle_colors):
        selected = np.zeros(len(in_slot), dtype=bool)
        selected[np.flatnonzero(in_slot)[mask]] = True
        groups.append((key, description, selected))
    return groups


def mesh_parts(evaluated, materials):
    """One part per used material slot (or attribute colour of a slot without one) and shading of
    `evaluated`: (positions, normals, triangles, material key) as arrays the worker can send without
    bpy. Flat-shaded faces form their own part with normals None: every backend then shades with the
    geometric normal, and their corners can share vertices (3.5 times faster on VisGL than one
    vertex per corner). None when it has no faces. Adds a plain description of every material it
    uses to `materials`."""
    mesh = evaluated.to_mesh()
    try:
        if mesh is None or not mesh.polygons:
            return None
        positions, normals, indices, slots, corner_vertices, flat = mesh_arrays(mesh)
        material_slots = evaluated.material_slots
        colors = corner_colors(mesh, corner_vertices)
        parts = []
        for slot in np.unique(slots):
            material = material_slots[int(slot)].material if slot < len(material_slots) else None
            for key, description, selected_triangles in slot_groups(material, slots == slot, indices, colors):
                if key not in materials:
                    materials[key] = description if description is not None else material_description(material)
                for flat_faces in (False, True):
                    selected = indices[selected_triangles & (flat == flat_faces)]
                    if not len(selected):
                        continue
                    used, remapped = weld_corners(selected, corner_vertices, None if flat_faces else normals)
                    parts.append((
                        np.ascontiguousarray(positions[used]),
                        None if flat_faces else np.ascontiguousarray(normals[used]),
                        np.ascontiguousarray(remapped),
                        key,
                    ))
        return parts
    finally:
        evaluated.to_mesh_clear()


def same_mesh_parts(previous, current):
    """Material edits may tag the evaluated geometry dirty. Rebuild only when exported geometry
    or slot assignments changed, even if an actual mesh edit arrived in the same update."""
    if previous is None or current is None:
        return previous is current
    def same(left, right):
        if left is None or right is None:
            return left is right
        return np.array_equal(left, right)

    return len(previous) == len(current) and all(
        old[3] == new[3] and all(same(left, right) for left, right in zip(old[:3], new[:3]))
        for old, new in zip(previous, current)
    )


def area_light_area(light, matrix):
    """The emitting area of a Blender area light in world units: its size follows the object's
    scale (Cycles does the same)."""
    scale_x, scale_y = matrix.col[0].to_3d().length, matrix.col[1].to_3d().length
    width = light.size * scale_x
    height = (light.size_y if light.shape in ("RECTANGLE", "ELLIPSE") else light.size) * scale_y
    return width * height * (math.pi / 4.0 if light.shape in ("DISK", "ELLIPSE") else 1.0)


def light_strength_scale(light, matrix):
    """What Cycles multiplies a light's Power or Strength by (measured against Cycles):
    2^Exposure, and with Normalize off its emitting size: a sun's disk solid angle, a point or
    spot's sphere surface (its radius ignores the object's scale), an area light's area."""
    scale = 2.0 ** getattr(light, "exposure", 0.0)
    if getattr(light, "normalize", True):
        return scale
    if light.type == "SUN":
        return scale * math.pi * (light.angle / 2.0) ** 2
    if light.type in ("POINT", "SPOT"):
        return scale * 4.0 * math.pi * light.shadow_soft_size ** 2
    if light.type == "AREA":
        return scale * area_light_area(light, matrix)
    return scale


def light_parameters(light, matrix):
    """(ANARI light subtype, [(parameter, type, value)]) for a Blender light, or None. Intensities
    are those Cycles renders with: a sun's Strength is irradiance, a point or spot of P watts has
    intensity P/(4 pi) W/sr, an area light of P watts P/pi on its axis (a Lambertian emitter)."""
    color = ("color", anari.FLOAT32_VEC3, tuple(light.color))
    strength = light.energy * light_strength_scale(light, matrix)
    if light.type == "SUN":
        return "directional", [
            ("direction", anari.FLOAT32_VEC3, direction_in_volume(matrix, (0, 0, -1))),
            ("irradiance", anari.FLOAT32, strength),
            color,
        ]
    if light.type not in ("POINT", "SPOT", "AREA"):
        return None
    position = ("position", anari.FLOAT32_VEC3,
                (Z_UP_TO_Y_UP[:3, :3] @ np.array(matrix.translation, dtype=np.float32)).tolist())
    direction = ("direction", anari.FLOAT32_VEC3, direction_in_volume(matrix, (0, 0, -1)))
    if light.type == "SPOT":
        cone = light.spot_size
        return "spot", [
            position, direction,
            ("intensity", anari.FLOAT32, strength / (4.0 * math.pi)),
            ("openingAngle", anari.FLOAT32, cone),
            ("falloffAngle", anari.FLOAT32, cone / 2.0 * light.spot_blend),
            color,
        ]
    if light.type == "AREA":
        # A spot over the front hemisphere, fading to its rim like a cosine, rather than ANARI's
        # quad light: VisRTX and OSPRay sample a quad at one random point per pixel per frame, which
        # a headset's ever-moving view never averages out (grain); VisGL has no quad light.
        return "spot", [
            position, direction,
            ("intensity", anari.FLOAT32, strength / math.pi),
            ("openingAngle", anari.FLOAT32, math.pi),
            ("falloffAngle", anari.FLOAT32, math.pi / 2.0),
            color,
        ]
    return "point", [
        position,
        ("intensity", anari.FLOAT32, strength / (4.0 * math.pi)),
        # A true point, as Helide and Blender's viewport treat it. VisRTX defaults to a
        # one-unit sphere sampled at one random point per pixel per frame, which a headset's
        # ever-moving view never averages out: lit faces showed as black-and-white static.
        ("radius", anari.FLOAT32, 0.0),
        color,
    ]


# A scene without lights would stream black; light it from above, like a studio.
STUDIO_LIGHT = ("directional", [
    ("direction", anari.FLOAT32_VEC3, (-0.3, -1.0, -0.5)),
    ("irradiance", anari.FLOAT32, 2.0),
])


class Snapshot:
    """What one sync changes, in plain data: `meshes` object name -> parts (None: no faces) for
    every object rebuilt, `instances` (object name, column-major transform), `lights` and
    `environment` (world.Environment or None) for the whole scene, `materials` material key ->
    plain material description for new or changed materials. `full` rebuilds every object."""

    def __init__(self, full):
        self.full = full
        self.meshes = {}
        self.materials = {}
        self.instances = []
        self.lights = []
        self.environment = None

    def absorb(self, newer):
        """Keep the latest description per material. A newer full capture replaces older scene
        data; incremental captures retain changes still waiting to be applied."""
        self.full = self.full or newer.full
        if newer.full:
            self.meshes = newer.meshes.copy()
            self.materials = newer.materials.copy()
        else:
            self.meshes.update(newer.meshes)
            self.materials.update(newer.materials)
        self.instances = newer.instances
        self.lights = newer.lights
        self.environment = newer.environment
        return self


POINT_TYPES = {"POINTCLOUD", "GAUSSIAN_SPLAT"}
SPLAT_ATTRIBUTES = ("f_dc_0", "opacity", "scale_0", "rot_0")


def unstreamed_reason(evaluated):
    """Why the stream leaves `evaluated` out although it is visible, or None when it is drawn: a
    point cloud or Gaussian splat object, or a mesh with vertices but no faces (a 3DGS PLY imported
    as loose vertices). Text for the status list and the log."""
    name = evaluated.original.name
    if evaluated.type in POINT_TYPES:
        return f"'{name}': Gaussian splats and point clouds are not streamed"
    if evaluated.type == "MESH":
        mesh = evaluated.data
        if len(mesh.vertices) and not len(mesh.polygons):
            if all(attribute in mesh.attributes for attribute in SPLAT_ATTRIBUTES):
                return f"'{name}': Gaussian splats and point clouds are not streamed"
            return f"'{name}': has no faces: not streamed"
    return None


def scene_bounds(depsgraph):
    """(lower, upper) of every visible object the stream draws, in the app volume's space (Y up)
    the device renders, or None without one: what "Fit scene to volume" sends as qar.fitBox. Each
    object counts by its bounding box, so a rotated object adds a little more than its surface."""
    corners = []
    for instance in depsgraph.object_instances:
        evaluated = instance.object
        if evaluated.type not in GEOMETRY_TYPES:
            continue
        if not instance.is_instance and not evaluated.original.visible_get():
            continue
        if unstreamed_reason(evaluated) is not None:
            continue
        local = np.array([tuple(corner) + (1.0,) for corner in evaluated.bound_box], dtype=np.float32)
        world = Z_UP_TO_Y_UP @ np.array(instance.matrix_world, dtype=np.float32)
        corners.append((local @ world.T)[:, :3])
    if not corners:
        return None
    points = np.concatenate(corners)
    return points.min(axis=0).tolist(), points.max(axis=0).tolist()


class SceneCapture:
    """UI thread: reads the depsgraph into snapshots. Remembers which objects it has sent, so an
    object is extracted again only when its geometry changed or it is new."""

    def __init__(self, on_warning=None, material_support=None):
        self.meshes = {}
        self.materials = {}
        self.diagnostics = MaterialDiagnostics(on_warning)
        self.on_warning = on_warning
        self.unstreamed = {}  # object name -> reason, for the objects now in the scene
        self.reported_unstreamed = set()
        self.material_support = material_support
        self.on_warning = on_warning
        self.environment_pixels = {}  # the World image's source -> its pixels, read once
        self.world_reported = set()  # World warnings already logged

    def capture(self, depsgraph, dirty_objects, full, materials=None):
        """`dirty_objects`: names whose evaluated geometry may have changed. `materials`: plain
        descriptions already captured on the UI thread for changed materials."""
        snapshot = Snapshot(full)
        snapshot.materials.update(materials or {})
        if full:
            self.meshes.clear()
            self.materials.clear()
        extracted = set()
        seen = set()
        unstreamed = {}
        for instance in depsgraph.object_instances:
            evaluated = instance.object
            original = evaluated.original
            if not instance.is_instance and not original.visible_get():
                continue
            reason = unstreamed_reason(evaluated)
            if reason is not None:
                unstreamed[original.name] = reason
                if original.name not in self.reported_unstreamed:
                    self.reported_unstreamed.add(original.name)
                    if self.on_warning is not None:
                        self.on_warning(reason)
                continue
            if evaluated.type in GEOMETRY_TYPES:
                name = original.name
                seen.add(name)
                if name not in extracted and (name in dirty_objects or name not in self.meshes):
                    parts = mesh_parts(evaluated, snapshot.materials)
                    if name not in self.meshes or not same_mesh_parts(self.meshes[name], parts):
                        snapshot.meshes[name] = parts
                        self.meshes[name] = parts
                    extracted.add(name)
                snapshot.instances.append((name, column_major(instance.matrix_world)))
            elif evaluated.type == "LIGHT":
                light = light_parameters(evaluated.data, instance.matrix_world)
                if light is not None:
                    snapshot.lights.append(light)
        self.unstreamed = unstreamed
        self.meshes = {name: parts for name, parts in self.meshes.items() if name in seen}
        snapshot.environment = self.capture_environment(depsgraph.scene)
        descriptions = snapshot.materials
        snapshot.materials = {
            key: description for key, description in descriptions.items()
            if self.materials.get(key) != description
        }
        self.materials.update(descriptions)
        used_keys = {part[3] for parts in self.meshes.values() if parts for part in parts}
        self.diagnostics.update(self.materials, used_keys, self.material_support)
        return snapshot

    def capture_environment(self, scene):
        """The scene's World as a plain environment; each reason it is approximated is logged once."""
        environment = world_environment(getattr(scene, "world", None), self.environment_pixels)
        for reason in environment.warnings if environment is not None else ():
            if reason not in self.world_reported:
                self.world_reported.add(reason)
                if self.on_warning is not None:
                    self.on_warning(f"World: {reason}")
        return environment


# A rasterizing backend (VisGL) pays a fixed cost per surface, per eye, per user: about 6 us on an
# RTX 3070 Ti, so a few thousand separate objects cannot hold 30 fps. Once a material has this many
# static single-material objects, they are merged into chunks of at most BATCH_MAX_OBJECTS, so an
# object leaving a chunk rebuilds only its chunk. An object that left one (it moved or changed
# shape) rejoins once it has been still for SETTLE_SECONDS, so a drag costs one chunk rebuild when
# it starts and one when it ends instead of per-object draws for the rest of the session.
BATCH_MIN_OBJECTS = 16
BATCH_MAX_OBJECTS = 64
SETTLE_SECONDS = 2.0
VOLUME_POLL_SECONDS = 0.25  # how often the sync worker checks where the app volume is


def part_key(part):
    """The chunk a part can join: its material and whether it carries normals. Smooth and flat
    parts never share a chunk: one geometry has normals or none."""
    return part[3], part[1] is not None


class Chunk:
    """Parts of static objects with one material and shading, merged into one surface and drawn by
    one identity instance. An object with several parts sits in one chunk per part."""

    def __init__(self, key):
        self.key = key  # (material key, whether its parts carry normals)
        self.members = []  # (object name, index into its parts)
        self.entry = None  # (group, owned, triangles) as build_group returns it
        self.instance = None
        self.dirty = True


SCIVIS_MATTE_ONLY = "OSPRay streams with its SciVis renderer, which shades matte materials only"
VISRTX_MATTE_ONLY = (
    "VisRTX materials preference is Matte: physically based materials sample the ambient light at "
    "random, which a moving view shows as grain"
)


def streaming_renderer(backend, visrtx_matte=False):
    """The renderer subtype, its integer parameters and why the stream uses matte materials only
    (None: the backend's preferred model) for `backend`.

    - OSPRay's default is its path tracer: at one sample per pixel and a view that moves every
      frame it never converges, so every frame is grain. Its SciVis renderer without ambient
      occlusion is noise-free and several times faster, but shades only matte materials.
    - VisRTX shades a physically based material with one random sample of the ambient light per
      pixel, ambient occlusion off or not: faces lit only by it show grain in a moving view. A
      matte material takes the ambient light as a constant. The add-on's VisRTX materials
      preference chooses: physically based (metal and highlights, with that grain; the default)
      or matte (`visrtx_matte`, smooth)."""
    if backend == "ospray":
        return "scivis", {"aoSamples": 0}, SCIVIS_MATTE_ONLY
    if backend == "visrtx" and visrtx_matte:
        return "default", {}, VISRTX_MATTE_ONLY
    return "default", {}, None


def renderer_material_support(matte_only, support):
    """`support` narrowed to the matte model when `matte_only` gives a reason and the backend has
    one; otherwise unchanged."""
    if matte_only is None or support.subtype == "matte" or support.matte is None:
        return support
    matte = support.matte
    matte.fallback_reason = matte_only
    return matte


class SceneMirror:
    """Owns every ANARI object it creates on `device` and the world the stream renders. Never
    touches bpy: it runs on the SyncWorker thread. `backend` is the one qaros forwards to."""

    def __init__(self, device, backend=None, volume_cube=None, visrtx_matte=False):
        self.device = device
        # Returns qar.volumeCube (16 floats, column-major) or None. None as the callable: no culling.
        self.volume_cube = volume_cube
        self.cube = None  # 4x4 row-major numpy matrix: scene point -> the app volume's unit cube
        self.bounds = {}  # object name -> (low, high) corners of its parts, object space
        self.culled = 0  # instances outside the volume in the last placement
        subtype, self.renderer_parameters, matte_only = streaming_renderer(backend, visrtx_matte)
        # Before publishing the first frame: no target can hold the backend gate yet. Later
        # material updates and panel redraws use this copied metadata, never query the backend.
        self.material_support = renderer_material_support(matte_only, device.material_support())
        # The World becomes an hdri light only where one can light the scene: VisGL has no hdri
        # light, and OSPRay's SciVis renderer ignores one.
        self.environment_support = subtype != "scivis" and device.environment_support()
        self.world = device.new("World")
        self.renderer_subtype = subtype
        self.renderer = device.new("Renderer", subtype)
        self.groups = {}  # object name -> (group, [surfaces, geometries], triangles), None: no faces
        self.materials = {}  # material key -> material
        self.material_models = {}  # material key -> selected cached support
        self.surface_materials = {}  # surface -> material key, for rebinding shared materials
        self.material_descriptions = {}  # material key -> latest plain description
        self.instances = []
        self.instance_slots = {}  # (object name, occurrence) -> (instance, group, transform)
        self.lights = []  # every light in the world: the lamps, then the environment
        self.lamps = []  # lights built from `light_parameters`
        self.light_parameters = None  # the captured lights `lamps` were built from
        self.environment = None  # the hdri light, or None
        self.environment_description = None  # the world.Environment it was built from
        self.parts = {}  # object name -> latest captured parts (None: no faces)
        self.chunks = []  # Chunk, each one surface of merged static objects
        self.batched = {}  # object name -> (chunk, transform baked into it)
        self.dynamic = {}  # object that left a chunk -> when it last moved or changed (monotonic)
        self.captured = []  # the latest snapshot's instances, which settle() places again
        # The selection outline: every instance of an object carries the object's ANARI `id`, the
        # same for the whole session, and qar.selection names the selected object's.
        self.object_ids = {}  # object name -> id of its instances
        self.selected = None  # object name the stream outlines, or None

    @property
    def objects(self):
        return sum(1 for entry in self.groups.values() if entry is not None) + len(self.batched)

    @property
    def triangles(self):
        standalone = sum(entry[2] for entry in self.groups.values() if entry is not None)
        return standalone + sum(chunk.entry[2] for chunk in self.chunks if chunk.entry)

    def configure_renderer(self, ambient, background):
        device = self.device
        device.set(self.renderer, "ambientRadiance", anari.FLOAT32, ambient)
        # Constant ambient light, no ambient occlusion. VisRTX traces one random occlusion ray per
        # pixel per frame by default, and a headset's view moves every frame, so its image never
        # converges: faces lit only by the ambient term showed as static noise fixed to the view.
        # Backends without the parameter ignore it.
        device.set(self.renderer, "ambientSamples", anari.INT32, 0)
        for name, value in self.renderer_parameters.items():
            device.set(self.renderer, name, anari.INT32, value)
        device.set(self.renderer, "background", anari.FLOAT32_VEC4, background)
        device.commit(self.renderer)

    # Materials ---------------------------------------------------------------------------------

    def material(self, key):
        if key not in self.materials:
            self.update_material(key, self.material_descriptions.get(key, FALLBACK_MATERIAL))
        return self.materials[key]

    def write_material(self, handle, parameters):
        for name, data_type, value in parameters:
            self.device.set(handle, name, data_type, value)
        self.device.commit(handle)

    def update_material(self, key, description):
        support, parameters, _warnings = material_export(description, self.material_support)
        previous = self.materials.get(key)
        if previous is not None and self.material_models[key].subtype == support.subtype:
            self.write_material(previous, parameters)
            return
        replacement = self.device.new("Material", support.subtype)
        try:
            self.write_material(replacement, parameters)
            # Surfaces retain their material. Transfer every reference before releasing our old
            # owner, including when several objects or material slots share the same material.
            for surface, material_key in self.surface_materials.items():
                if material_key == key:
                    self.device.set(surface, "material", anari.MATERIAL, replacement)
                    self.device.commit(surface)
        except Exception:
            self.device.release(replacement)
            raise
        self.materials[key] = replacement
        self.material_models[key] = support
        if previous is not None:
            self.device.release(previous)

    def update_materials(self, descriptions):
        self.material_descriptions.update(descriptions)
        for key, description in descriptions.items():
            if key in self.materials:
                self.update_material(key, description)

    # Geometry ----------------------------------------------------------------------------------

    def build_group(self, parts):
        """One group per object: a triangle geometry and surface per part."""
        device = self.device
        owned = []
        surfaces = []
        triangles = 0
        for positions, normals, indices, key in parts:
            geometry = device.new("Geometry", "triangle")
            owned.append(geometry)
            arrays = [
                ("vertex.position", anari.FLOAT32_VEC3, positions, 12),
                ("primitive.index", anari.UINT32_VEC3, indices, 12),
            ]
            if normals is not None:
                arrays.append(("vertex.normal", anari.FLOAT32_VEC3, normals, 12))
            for name, data_type, data, item_bytes in arrays:
                array = device.array(data_type, len(data), data, item_bytes)
                device.set(geometry, name, anari.ARRAY1D, array)
                device.release(array)
            device.commit(geometry)
            surface = device.new("Surface")
            owned.append(surface)
            device.set(surface, "geometry", anari.GEOMETRY, geometry)
            device.set(surface, "material", anari.MATERIAL, self.material(key))
            device.commit(surface)
            self.surface_materials[surface] = key
            surfaces.append(surface)
            triangles += len(indices)
        group = device.new("Group")
        surface_array = device.array(anari.SURFACE, len(surfaces), list(surfaces))
        device.set(group, "surface", anari.ARRAY1D, surface_array)
        device.release(surface_array)
        device.commit(group)
        return group, owned, triangles

    def drop_group(self, name):
        self.release_entry(self.groups.pop(name, None))

    def release_entry(self, entry):
        if entry:
            group, owned, _triangles = entry
            for handle in owned:
                self.surface_materials.pop(handle, None)
                self.device.release(handle)
            self.device.release(group)

    # Batches -----------------------------------------------------------------------------------

    def leave_batch(self, name):
        chunks, _transform = self.batched.pop(name)
        for chunk in chunks:
            chunk.members = [member for member in chunk.members if member[0] != name]
            chunk.dirty = True

    def join_batch(self, name, transform):
        """Every part of `name` into a chunk of its material and shading."""
        chunks = []
        for index, part in enumerate(self.parts[name]):
            key = part_key(part)
            chunk = next((c for c in self.chunks if c.key == key and len(c.members) < BATCH_MAX_OBJECTS), None)
            if chunk is None:
                chunk = Chunk(key)
                self.chunks.append(chunk)
            chunk.members.append((name, index))
            chunk.dirty = True
            if chunk not in chunks:
                chunks.append(chunk)
        self.batched[name] = (chunks, transform)

    def mergeable(self, name, count):
        parts = self.parts.get(name)
        return (count == 1 and bool(parts) and name not in self.batched and name not in self.dynamic
                and name != self.selected)

    def update_batches(self, counts, transforms, now):
        """Batched objects that moved, changed shape or gained instances leave their chunk and stay
        standalone until they have been still for SETTLE_SECONDS; enough static objects of one
        material join one."""
        for name, (_chunk, transform) in list(self.batched.items()):
            moved = counts.get(name) != 1 or transforms[name] != transform
            # The selected object needs an instance of its own: a chunk's instance has no one id.
            if moved or name == self.selected:
                self.leave_batch(name)
                if moved and name in counts:
                    self.dynamic[name] = now
        for name in [name for name, changed in self.dynamic.items() if now - changed >= SETTLE_SECONDS]:
            del self.dynamic[name]
        # An object joins when every one of its parts has enough company: then none of its parts
        # stays behind as a draw of its own.
        candidates = [name for name, count in counts.items() if self.mergeable(name, count)]
        company = {}
        for chunk in self.chunks:
            company[chunk.key] = company.get(chunk.key, 0) + len(chunk.members)
        for name in candidates:
            for key in {part_key(part) for part in self.parts[name]}:
                company[key] = company.get(key, 0) + 1
        for name in candidates:
            if all(company[part_key(part)] >= BATCH_MIN_OBJECTS for part in self.parts[name]):
                self.drop_group(name)
                self.instance_slots.pop((name, 0), None)
                self.join_batch(name, transforms[name])
        for chunk in [c for c in self.chunks if c.dirty]:
            self.rebuild_chunk(chunk)

    def rebuild_chunk(self, chunk):
        """One surface for the whole chunk: its objects' parts with their transforms baked in. The
        chunk's previous instance leaves with the next instance list."""
        self.release_entry(chunk.entry)
        chunk.entry, chunk.instance, chunk.dirty = None, None, False
        if not chunk.members:
            self.chunks.remove(chunk)
            return
        material, smooth = chunk.key
        positions, normals, indices = [], [], []
        offset = 0
        for name, index in chunk.members:
            part_positions, part_normals, part_indices, _key = self.parts[name][index]
            matrix = np.array(self.batched[name][1], dtype=np.float32).reshape(4, 4).T
            rotation = matrix[:3, :3]
            positions.append(part_positions @ rotation.T + matrix[:3, 3])
            if smooth:
                baked = part_normals @ np.linalg.inv(rotation)
                lengths = np.linalg.norm(baked, axis=1, keepdims=True)
                normals.append(baked / np.where(lengths > 0, lengths, 1))
            indices.append(part_indices + np.uint32(offset))
            offset += len(part_positions)
        merged = (
            np.ascontiguousarray(np.concatenate(positions), dtype=np.float32),
            np.ascontiguousarray(np.concatenate(normals), dtype=np.float32) if smooth else None,
            np.ascontiguousarray(np.concatenate(indices), dtype=np.uint32),
            material,
        )
        chunk.entry = self.build_group([merged])
        chunk.instance = self.device.new("Instance", "transform")
        self.device.set(chunk.instance, "group", anari.GROUP, chunk.entry[0])
        self.device.commit(chunk.instance)

    def drop_batches(self):
        for chunk in self.chunks:
            self.release_entry(chunk.entry)
        self.chunks, self.batched = [], {}
        self.dynamic.clear()

    # Lights ------------------------------------------------------------------------------------

    def build_light(self, light):
        subtype, parameters = light
        handle = self.device.new("Light", subtype)
        for name, data_type, value in parameters:
            self.device.set(handle, name, data_type, value)
        self.device.commit(handle)
        return handle

    # Apply -------------------------------------------------------------------------------------

    def apply(self, snapshot):
        """Rebuild the snapshot's objects (all when `full`), then the world's instance and light
        lists. Objects no instance uses any more are released."""
        device = self.device
        if snapshot.full:
            for name in list(self.groups):
                self.drop_group(name)
            self.drop_batches()
            self.parts.clear()
            self.bounds.clear()
        self.update_materials(snapshot.materials)
        now = time.monotonic()
        for name, parts in snapshot.meshes.items():
            self.parts[name] = parts
            self.bounds.pop(name, None)
            self.drop_group(name)
            if name in self.batched:
                self.leave_batch(name)
            if not snapshot.full:
                self.dynamic[name] = now
        self.captured = snapshot.instances
        changed = bool(snapshot.full or snapshot.meshes or snapshot.materials)
        changed |= self.place(snapshot.instances, now)
        environment = snapshot.environment if self.environment_support else None
        # The studio light stands in for lamps only when nothing else lights the scene.
        lamps = snapshot.lights or ([] if environment is not None else [STUDIO_LIGHT])
        changed |= self.update_lights(lamps, environment)
        if changed:
            device.commit(self.world)

    def settle_deadline(self):
        """When the earliest standalone object becomes still enough to rejoin a chunk (monotonic
        seconds), or None."""
        return min(self.dynamic.values()) + SETTLE_SECONDS if self.dynamic else None

    def settle(self, now=None):
        """Merges objects that have been still for SETTLE_SECONDS back into chunks. Runs on the
        worker when no snapshot arrives: once a drag ends, nothing else would."""
        now = time.monotonic() if now is None else now
        if self.settle_deadline() is None or self.settle_deadline() > now:
            return False
        changed = self.place(self.captured, now)
        if changed:
            self.device.commit(self.world)
        return changed

    # Culling -----------------------------------------------------------------------------------

    def object_bounds(self, name):
        if name not in self.bounds:
            parts = self.parts.get(name) or []
            if not parts:
                return None
            corners = np.concatenate([positions for positions, _normals, _indices, _key in parts])
            self.bounds[name] = (corners.min(axis=0), corners.max(axis=0))
        return self.bounds[name]

    def inside_volume(self, name, transform):
        """Whether any of the object's bounding box can be inside the app volume. Conservative: the
        box's eight corners in the volume's unit cube, tested as one axis-aligned box. Content
        outside the volume is not rendered at all: the mixer would cut it away, but not what it
        hid inside the volume."""
        bounds = self.object_bounds(name)
        if self.cube is None or bounds is None:
            return True
        low, high = bounds
        corners = np.array([[x, y, z, 1.0] for x in (low[0], high[0]) for y in (low[1], high[1])
                            for z in (low[2], high[2])], dtype=np.float64)
        to_scene = np.array(transform, dtype=np.float64).reshape(4, 4).T
        cube = (corners @ (self.cube @ to_scene).T)[:, :3]
        return bool(np.all(cube.min(axis=0) <= 1.0) and np.all(cube.max(axis=0) >= -1.0))

    def refresh_volume(self, now=None):
        """Reads where the app volume is; when it moved, scaled or appeared, places the captured
        scene again. True when the world changed."""
        if self.volume_cube is None:
            return False
        values = self.volume_cube()
        cube = None if values is None else np.array(values, dtype=np.float64).reshape(4, 4).T
        unchanged = (cube is None and self.cube is None) or (
            cube is not None and self.cube is not None and np.allclose(cube, self.cube, atol=1e-6)
        )
        if unchanged:
            return False
        self.cube = cube
        changed = self.place(self.captured, time.monotonic() if now is None else now)
        if changed:
            self.device.commit(self.world)
        return changed

    def place(self, captured, now):
        """Chunks, standalone groups and the world's instance list for `captured`, without the
        instances outside the app volume. True when the instance list or an instance changed."""
        names = {name for name, _transform in captured}
        # A culled object keeps its mesh: it comes back when the volume reaches it.
        self.parts = {name: parts for name, parts in self.parts.items() if name in names}
        self.bounds = {name: bounds for name, bounds in self.bounds.items() if name in names}
        visible = [(name, transform) for name, transform in captured if self.inside_volume(name, transform)]
        self.culled = len(captured) - len(visible)
        captured = visible
        counts, transforms = {}, {}
        for name, transform in captured:
            counts[name] = counts.get(name, 0) + 1
            transforms[name] = transform
        self.dynamic = {name: changed for name, changed in self.dynamic.items() if name in counts}
        for name in self.dynamic:
            slot = self.instance_slots.get((name, 0))
            if slot is not None and slot[2] != transforms[name]:
                self.dynamic[name] = now
        batches_before = [chunk.instance for chunk in self.chunks]
        self.update_batches(counts, transforms, now)
        for name in counts:
            if name not in self.batched and name not in self.groups and name in self.parts:
                parts = self.parts[name]
                self.groups[name] = self.build_group(parts) if parts else None
        changed = [chunk.instance for chunk in self.chunks] != batches_before
        return self.update_instances(captured) or changed

    def update_instances(self, captured):
        """Keep every instance whose object and transform are unchanged, and move the others in
        place. Recreating them all on every edit made each drag or animation frame cost a new
        object per instance, and made rasterizing backends treat the whole scene as new. Batched
        objects are drawn by their chunk's instance."""
        device = self.device
        slots = {}
        occurrences = {}
        moved = False
        used = {name for name, _transform in captured}
        for name, transform in captured:
            if name in self.batched:
                continue
            entry = self.groups.get(name)
            if entry is None:
                continue
            key = (name, occurrences.get(name, 0))
            occurrences[name] = key[1] + 1
            handle, group, previous = self.instance_slots.get(key, (None, None, None))
            if handle is None or group != entry[0]:
                handle = device.new("Instance", "transform")
                device.set(handle, "group", anari.GROUP, entry[0])
                device.set(handle, "id", anari.UINT32, self.object_id(name))
            elif previous == transform:
                slots[key] = (handle, group, previous)
                continue
            device.set(handle, "transform", anari.FLOAT32_MAT4, transform)
            device.commit(handle)
            slots[key] = (handle, entry[0], transform)
            moved = True
        for name in [name for name in self.groups if name not in used]:
            self.drop_group(name)
        instances = [slot[0] for slot in slots.values()] + [chunk.instance for chunk in self.chunks]
        self.instance_slots = slots
        if instances == self.instances:
            return moved
        self.replace_list("instance", anari.INSTANCE, instances, "instances", keep=set(instances))
        return True

    # Selection outline ---------------------------------------------------------------------------

    def object_id(self, name):
        """The ANARI `id` of `name`'s instances, given once and kept for the session: the device
        matches qar.selection against the instanceId channel."""
        if name not in self.object_ids:
            self.object_ids[name] = len(self.object_ids) + 1
        return self.object_ids[name]

    def selection_parameter(self):
        """qar.selection for the selected object: its id, or empty for none."""
        return str(self.object_id(self.selected)) if self.selected is not None else ""

    def select(self, name, now=None):
        """Outlines `name` (None: nothing) in the stream. A batched object first leaves its chunk,
        so it is drawn by an instance with its own id; the world is committed before the device
        names that id. True when the selection changed."""
        if name == self.selected:
            return False
        self.selected = name
        if self.place(self.captured, time.monotonic() if now is None else now):
            self.device.commit(self.world)
        self.device.set(self.device.handle, "qar.selection", anari.STRING, self.selection_parameter())
        self.device.commit()
        return True

    def update_lights(self, captured, environment=None):
        """The world's lamps from `captured` and its hdri light from `environment`. An environment
        whose image is unchanged (a new strength or rotation) is updated in place, without sending
        the image again."""
        lamps_changed = captured != self.light_parameters
        environment_changed = environment != self.environment_description
        if not lamps_changed and not environment_changed:
            return False
        lamps = [self.build_light(light) for light in captured] if lamps_changed else self.lamps
        if environment_changed:
            self.update_environment(environment)
        lights = lamps + ([self.environment] if self.environment is not None else [])
        self.replace_list("light", anari.LIGHT, lights, "lights", keep=set(lights))
        self.lamps = lamps
        self.light_parameters = captured
        return True

    def update_environment(self, environment):
        """Points self.environment at an hdri light for `environment` (None: no light). The
        previous light is left for update_lights to release once the world no longer lists it."""
        previous = self.environment_description
        if environment is None:
            self.environment = None
        elif self.environment is None or previous is None or previous.source != environment.source:
            self.environment = self.device.new("Light", "hdri")
            rows, columns = environment.pixels.shape[:2]
            radiance = self.device.array2d(anari.FLOAT32_VEC3, columns, rows, environment.pixels, 12)
            self.device.set(self.environment, "radiance", anari.ARRAY2D, radiance)
            self.device.release(radiance)
            # Lights the scene without being drawn behind it: the stream's background stays clear.
            self.device.set(self.environment, "visible", anari.BOOL, 0)
        if environment is not None:
            self.device.set(self.environment, "scale", anari.FLOAT32, environment.strength)
            self.device.set(self.environment, "direction", anari.FLOAT32_VEC3, environment.direction)
            self.device.set(self.environment, "up", anari.FLOAT32_VEC3, environment.up)
            self.device.commit(self.environment)
        self.environment_description = environment

    def replace_list(self, parameter, data_type, handles, attribute, keep=frozenset()):
        """Point the world at `handles`, releasing the previous ones except those in `keep`."""
        device = self.device
        if handles:
            array = device.array(data_type, len(handles), list(handles))
            device.set(self.world, parameter, anari.ARRAY1D, array)
            device.release(array)
        else:
            device.unset(self.world, parameter)
        for handle in getattr(self, attribute):
            if handle not in keep:
                device.release(handle)
        setattr(self, attribute, handles)

    def close(self):
        device = self.device
        for name in list(self.groups):
            self.drop_group(name)
        self.drop_batches()
        self.parts.clear()
        for handle in self.instances + self.lights + list(self.materials.values()):
            device.release(handle)
        self.instances, self.lights, self.materials = [], [], {}
        self.lamps, self.environment, self.environment_description = [], None, None
        self.instance_slots, self.light_parameters = {}, None
        self.material_models.clear()
        self.surface_materials.clear()
        self.material_descriptions.clear()
        device.release(self.world)
        device.release(self.renderer)


# SyncWorker.selection when no new selection waits.
UNCHANGED = object()


class SyncWorker:
    """The one thread that applies snapshots to the mirror. `submit` never blocks: a snapshot the
    worker has not started yet absorbs the newer one. After a failed apply, `failed` stays set until
    the UI reads it, so it can send a full snapshot."""

    def __init__(self, mirror, on_error):
        self.mirror = mirror
        self.on_error = on_error
        self.condition = threading.Condition()
        self.pending = None
        self.selection = UNCHANGED  # object name to outline next, None for none
        self.closing = False
        self.applying = False
        self.failed = False
        self.thread = threading.Thread(target=self.run, name="qaros-anari-sync", daemon=True)
        self.thread.start()

    def submit(self, snapshot):
        with self.condition:
            self.pending = snapshot if self.pending is None else self.pending.absorb(snapshot)
            self.condition.notify_all()

    def select(self, name):
        """Outlines `name` (None: nothing) once the snapshots submitted before are applied."""
        with self.condition:
            self.selection = name
            self.condition.notify_all()

    def take_failure(self):
        with self.condition:
            failed, self.failed = self.failed, False
            return failed

    def run(self):
        while True:
            with self.condition:
                if (self.pending is None and self.selection is UNCHANGED and not self.closing
                        and not self.settle_due()):
                    self.condition.wait(self.poll_wait())
                if self.closing:
                    return
                snapshot, self.pending = self.pending, None
                selection, self.selection = self.selection, UNCHANGED
                self.applying = True
            failed = False
            try:
                # The app volume moves and scales under gestures without any Blender edit.
                self.mirror.refresh_volume()
                if snapshot is not None:
                    self.mirror.apply(snapshot)
                elif self.settle_due():
                    self.mirror.settle()
                if selection is not UNCHANGED:
                    self.mirror.select(selection)
            except Exception as error:  # noqa: BLE001 - reported, the next sync is a full one
                failed = True
                if snapshot is None:
                    # Not again until the full sync that follows a failure; it would fail again.
                    self.mirror.dynamic.clear()
                self.on_error(error)
            with self.condition:
                self.applying = False
                self.failed = self.failed or failed
                self.condition.notify_all()

    # The mirror is only touched by this thread: these read it between applies, under the lock.
    def poll_wait(self):
        """Until the next settle or volume check, whichever comes first."""
        settle = self.settle_wait()
        return VOLUME_POLL_SECONDS if settle is None else min(settle, VOLUME_POLL_SECONDS)

    def settle_wait(self):
        deadline = self.mirror.settle_deadline()
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    def settle_due(self):
        deadline = self.mirror.settle_deadline()
        return deadline is not None and deadline <= time.monotonic()

    def wait_idle(self, timeout=None):
        """Until every submitted snapshot is applied (tests, and before a full stop)."""
        with self.condition:
            return self.condition.wait_for(
                lambda: self.pending is None and self.selection is UNCHANGED and not self.applying, timeout
            )

    def close(self):
        """Stops after the snapshot being applied; one still pending is dropped."""
        with self.condition:
            self.closing = True
            self.pending = None
            self.selection = UNCHANGED
            self.condition.notify_all()
        self.thread.join()
