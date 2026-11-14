# SPDX-License-Identifier: MIT
"""The demo scene: a turbofan jet engine on a display stand, built from `assets/turbofan.glb`.

The engine is "Turbine | Turbofan Engine | Jet Engine" by blenderbirb, CC BY 4.0 (see
assets/CREDITS.md). This script regroups its ~21,000 loose pieces into six engine sections, by
where each piece sits along the engine axis, and places the engine on a stand with a credit plaque.
Everything turns slowly on a turntable.

Built to show the add-on, not only to look good:
- each section (fan, compressor, combustion chamber, turbine, exhaust, frame and shaft) is one
  object, so pointing at it in the headset selects it and a one-hand drag pulls it out of the
  engine: an exploded view made by hand, which Blender follows;
- the turntable is animated (frames 1-480), so the controls panel's Frame tab and Play/Pause have
  something to show;
- the materials are the model's own constant colours, which every QAROS backend reads;
- 236,000 triangles of real modelling detail, which VisGL streams at full frame rate.

Use it from Blender:

    blender --python demo/build_demo_scene.py -- [--save demo/turbofan.blend]

or import it and call `build()` (demo/start_demo.py does)."""
import math
import os
import sys

import bmesh
import bpy
from mathutils import Matrix, Vector

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "assets", "turbofan.glb")
FRAMES = 480  # one turntable revolution at 24 fps: 20 s
AXIS_HEIGHT = 1.25  # engine axis above the base, Blender units
CREDIT = "Turbofan model by blenderbirb, CC BY 4.0"

# Sections along the engine axis (+X is the intake). A piece longer than SPAN_FOR_FRAME along the
# axis runs through several sections (the shaft, long struts): it goes to the frame.
SECTIONS = (
    ("Fan", 1.45, math.inf),
    ("Compressor", 0.40, 1.45),
    ("Combustion chamber", -0.15, 0.40),
    ("Turbine", -0.95, -0.15),
    ("Exhaust", -math.inf, -0.95),
)
FRAME_SECTION = "Frame and shaft"
SPAN_FOR_FRAME = 1.0


def section_of(min_x, max_x, centre_x):
    if max_x - min_x > SPAN_FOR_FRAME:
        return FRAME_SECTION
    for name, low, high in SECTIONS:
        if low < centre_x <= high:
            return name
    return FRAME_SECTION


# --- the engine ---------------------------------------------------------------------------------


def import_engine():
    """Imports the model and returns its mesh objects, transforms applied, and its materials."""
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=SOURCE)
    imported = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in imported if o.type == "MESH"]
    return imported, meshes


def loose_pieces(bm):
    """The faces of `bm` grouped into connected pieces."""
    bm.faces.ensure_lookup_table()
    seen = set()
    pieces = []
    for face in bm.faces:
        if face.index in seen:
            continue
        stack, piece = [face], []
        seen.add(face.index)
        while stack:
            current = stack.pop()
            piece.append(current)
            for edge in current.edges:
                for other in edge.link_faces:
                    if other.index not in seen:
                        seen.add(other.index)
                        stack.append(other)
        pieces.append(piece)
    return pieces


def split_into_sections(meshes):
    """Six bmeshes, one per section, in engine coordinates, with one material slot per material."""
    targets = {}
    materials = []
    for obj in meshes:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.transform(obj.matrix_world)
        slot_materials = list(obj.data.materials)
        for piece in loose_pieces(bm):
            xs = [v.co.x for face in piece for v in face.verts]
            name = section_of(min(xs), max(xs), sum(xs) / len(xs))
            target = targets.setdefault(name, {"bm": bmesh.new(), "verts": {}})
            for face in piece:
                material = slot_materials[face.material_index] if slot_materials else None
                if material not in materials:
                    materials.append(material)
                verts = []
                for v in face.verts:
                    key = (obj.name, v.index)  # never id(bm): a freed bmesh's id is reused
                    if key not in target["verts"]:
                        target["verts"][key] = target["bm"].verts.new(v.co)
                    verts.append(target["verts"][key])
                try:
                    new_face = target["bm"].faces.new(verts)
                except ValueError:  # a duplicate face in the source
                    continue
                new_face.material_index = materials.index(material)
                new_face.smooth = face.smooth
        bm.free()
    return targets, materials


def build_engine(collection, parent):
    imported, meshes = import_engine()
    targets, materials = split_into_sections(meshes)
    sections = []
    for name in [s[0] for s in SECTIONS] + [FRAME_SECTION]:
        if name not in targets:
            continue
        bm = targets[name]["bm"]
        xs = [v.co.x for v in bm.verts]
        origin = Vector(((min(xs) + max(xs)) / 2, 0.0, 0.0))  # on the engine axis
        bm.transform(Matrix.Translation(-origin))
        mesh = bpy.data.meshes.new(name)
        bm.to_mesh(mesh)
        bm.free()
        for material in materials:
            mesh.materials.append(material)
        obj = bpy.data.objects.new(name, mesh)
        obj.location = origin + Vector((0.0, 0.0, AXIS_HEIGHT))
        obj.parent = parent
        collection.objects.link(obj)
        sections.append(obj)
    for obj in imported:
        bpy.data.objects.remove(obj)
    return sections


# --- the stand ----------------------------------------------------------------------------------


def material(name, color, metallic=0.0, roughness=0.5):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    node = mat.node_tree.nodes["Principled BSDF"]
    node.inputs["Base Color"].default_value = (*color, 1.0)
    node.inputs["Metallic"].default_value = metallic
    node.inputs["Roughness"].default_value = roughness
    mat.diffuse_color = (*color, 1.0)
    return mat


def stand_object(name, add_parts, mats, collection, parent):
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    keys = []
    for add, matrix, key in add_parts:
        before = set(bm.faces)
        add(bm, matrix)
        if key not in keys:
            keys.append(key)
        for face in set(bm.faces) - before:
            face.material_index = keys.index(key)
    bm.to_mesh(mesh)
    bm.free()
    for key in keys:
        mesh.materials.append(mats[key])
    obj = bpy.data.objects.new(name, mesh)
    obj.parent = parent
    collection.objects.link(obj)
    return obj


def box(bm, matrix):
    bmesh.ops.create_cube(bm, size=1.0, matrix=matrix)


def place(location, rotation=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0)):
    from mathutils import Euler

    return Matrix.Translation(location) @ Euler(rotation, "XYZ").to_matrix().to_4x4() @ Matrix.Diagonal((*scale, 1.0))


def underside(sections, x, half_width=0.12):
    """The lowest point of the engine near `x` along its axis, in the turntable's frame."""
    lowest = AXIS_HEIGHT
    for obj in sections:
        for v in obj.data.vertices:
            world = obj.location + v.co
            if abs(world.x - x) < half_width and abs(world.y) < 0.25:
                lowest = min(lowest, world.z)
    return lowest


def text_object(name, body, size, location, tilt, mat, collection, parent):
    curve = bpy.data.curves.new(name, "FONT")
    curve.body = body
    curve.align_x = "CENTER"
    curve.align_y = "CENTER"
    curve.size = size
    curve.extrude = 0.003
    curve.materials.append(mat)
    obj = bpy.data.objects.new(name, curve)
    obj.location = location
    obj.rotation_euler = (tilt, 0.0, 0.0)
    obj.parent = parent
    collection.objects.link(obj)
    return obj


def build_stand(collection, parent, sections):
    mats = {
        "base": material("Display base", (0.05, 0.055, 0.06), metallic=0.4, roughness=0.4),
        "steel": material("Stand steel", (0.20, 0.21, 0.22), metallic=1.0, roughness=0.3),
        "brass": material("Brass", (0.82, 0.62, 0.30), metallic=1.0, roughness=0.25),
        "engraving": material("Engraving", (0.04, 0.04, 0.04), roughness=0.6),
    }
    base_top = 0.14
    parts = [(box, place((0, 0, base_top / 2), scale=(4.8, 2.2, base_top)), "base")]
    for y in (-1.105, 1.105):  # brass edge strips
        parts.append((box, place((0, y, base_top - 0.01), scale=(4.82, 0.012, 0.02)), "brass"))
    for x in (-2.405, 2.405):
        parts.append((box, place((x, 0, base_top - 0.01), scale=(0.012, 2.22, 0.02)), "brass"))
    for x in (-1.0, 1.05):  # posts up to the casing's underside, with a saddle on top
        top = underside(sections, x) - 0.005
        height = top - base_top
        parts.append((box, place((x, 0, base_top + height / 2 - 0.03), scale=(0.16, 0.5, height - 0.06)), "steel"))
        parts.append((box, place((x, 0, top - 0.03), scale=(0.24, 0.7, 0.06)), "steel"))
        parts.append((box, place((x, 0, base_top + 0.03), scale=(0.5, 0.9, 0.06)), "steel"))
    tilt = math.radians(35)
    plate_centre = Vector((0.0, -0.78, base_top + 0.11))
    parts.append((box, place(plate_centre, rotation=(tilt, 0, 0), scale=(1.9, 0.36, 0.025)), "brass"))
    parts.append((box, place((0.0, -0.70, base_top + 0.05), scale=(0.5, 0.06, 0.1)), "steel"))
    stand = stand_object("Display stand", parts, mats, collection, parent)

    face = Matrix.Rotation(tilt, 4, "X")
    normal = face @ Vector((0.0, 0.0, 1.0))
    surface = plate_centre + normal * 0.014
    up = face @ Vector((0.0, 1.0, 0.0))
    text_object("Plaque title", "TURBOFAN ENGINE", 0.13, surface + up * 0.06, tilt, mats["engraving"], collection, parent)
    text_object("Plaque credit", CREDIT, 0.06, surface - up * 0.09, tilt, mats["engraving"], collection, parent)
    return stand


# --- scene --------------------------------------------------------------------------------------


def turntable(collection):
    pivot = bpy.data.objects.new("Turntable", None)
    pivot.empty_display_size = 0.5
    collection.objects.link(pivot)
    pivot.rotation_euler = (0.0, 0.0, 0.0)
    pivot.keyframe_insert("rotation_euler", index=2, frame=1)
    pivot.rotation_euler = (0.0, 0.0, 2.0 * math.pi)
    pivot.keyframe_insert("rotation_euler", index=2, frame=FRAMES + 1)
    for curve in fcurves(pivot):
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"
    return pivot


def fcurves(obj):
    """The object's F-curves, from Blender 4.4's layered actions or the older flat ones."""
    action = obj.animation_data.action
    curves = getattr(action, "fcurves", None)
    if curves is not None and len(curves):
        return list(curves)
    result = []
    for layer in getattr(action, "layers", ()):
        for strip in layer.strips:
            for bag in strip.channelbags:
                result.extend(bag.fcurves)
    return result


def build_lights(scene):
    sun = bpy.data.lights.new("Sun", "SUN")
    sun.energy = 3.0
    sun.angle = math.radians(3.0)
    sun_object = bpy.data.objects.new("Sun", sun)
    sun_object.rotation_euler = (math.radians(45), 0.0, math.radians(-35))
    scene.collection.objects.link(sun_object)


def clear(scene):
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    for data in (bpy.data.meshes, bpy.data.materials, bpy.data.lights, bpy.data.cameras, bpy.data.curves):
        for block in list(data):
            if block.users == 0:
                data.remove(block)
    for collection in list(scene.collection.children):
        bpy.data.collections.remove(collection)


def build(scene=None):
    """Replaces everything in `scene` (default: the current one) with the demo."""
    scene = scene or bpy.context.scene
    clear(scene)
    # The glTF importer scales by the scene's unit scale: a startup file in millimetres made the
    # engine 1000 times too large. The demo is built in metres, one unit per metre.
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene.unit_settings.length_unit = "METERS"
    col = bpy.data.collections.new("Turbofan display")
    scene.collection.children.link(col)
    pivot = turntable(col)
    sections = build_engine(col, pivot)
    build_stand(col, pivot, sections)
    build_lights(scene)
    scene.frame_start = 1
    scene.frame_end = FRAMES
    scene.render.fps = 24
    scene.frame_set(1)
    bpy.context.view_layer.objects.active = sections[0]
    return scene


def frame_viewports():
    """Points every 3D view at the engine from the front quarter, in Material Preview."""
    if bpy.app.background:
        return
    from mathutils import Euler

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            space.shading.type = "MATERIAL"
            space.overlay.show_floor = False
            region = space.region_3d
            region.view_location = (0.0, 0.0, AXIS_HEIGHT * 0.8)
            region.view_distance = 7.5
            region.view_rotation = Euler((math.radians(70), 0.0, math.radians(55)), "XYZ").to_quaternion()


def main(argv):
    args = argv[argv.index("--") + 1:] if "--" in argv else []
    build()
    frame_viewports()
    if "--save" in args:
        path = args[args.index("--save") + 1]
        bpy.ops.wm.save_as_mainfile(filepath=bpy.path.abspath(path))
        print(f"[qaros-demo] saved {path}", flush=True)


if __name__ == "__main__":
    main(sys.argv)
