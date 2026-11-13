# SPDX-License-Identifier: MIT
"""Corrects what Blender's own USD import leaves out of the streamed scene.

- **Primitive shapes lose their materials.** Blender (5.2.3 and main, usd_reader_shape.cc) turns a
  Sphere, Cube, Cylinder, Cone, Capsule or Plane prim into a mesh but never resolves its material
  binding; only Mesh prims get theirs. The shape then streams white.
- **Opacity becomes transmission.** Blender imports a constant UsdPreviewSurface opacity (with no
  opacityThreshold) as Principled Transmission Weight 1 - opacity, a glass reading. The add-on
  streams coverage from Alpha only, so the surface streamed opaque. ANARI reads that opacity as
  blended coverage (hdanari: `opacity`, alphaMode blend), so it moves to Alpha.
- **Sphere light radii stay in stage units.** Blender imports a SphereLight's radius as authored,
  but scales the light object by the unit conversion, which Cycles ignores for a radius: in a
  centimetre stage a 5 cm light became 5 m, and with USD's default (not normalized) its power
  grows with that size.

Two parts apply the fixes:

- An import hook, run after every USD import while the add-on is enabled (File > Import, drag and
  drop, scripts). It binds each shape mesh without a material to the material bound to its prim,
  when that import created it, and moves opacity to Alpha on the materials the import created.
- "USD for QAROS" in File > Import: Blender's import with Import All Materials, so the materials
  the hook needs exist; then it scales sphere light radii to the world (the hook runs before the
  import applies its unit scale, so it cannot) and removes the materials nothing uses.

Bindings resolve as Blender's default Material Purpose (Full, then Preview); the hook cannot read
the purpose an import was given."""
import bpy
from bpy_extras.io_utils import ImportHelper
from pxr import UsdGeom, UsdShade


def log(text):
    print(f"[qaros-anari] {text}", flush=True)


def bound_material_path(prim):
    """Path of the material bound to `prim` (directly or inherited), Full purpose then Preview,
    as Blender's importer resolves it by default; None when nothing is bound."""
    api = UsdShade.MaterialBindingAPI(prim)
    for purpose in (UsdShade.Tokens.full, UsdShade.Tokens.preview):
        material, _ = api.ComputeBoundMaterial(purpose)
        if material:
            return material.GetPath()
    return None


def first_of_type(ids, id_type):
    return next((item for item in ids if isinstance(item, id_type)), None)


def unbound_shapes(stage, prim_map):
    """(prim path, mesh, material path) for every imported shape prim whose mesh has no material
    and whose prim has one bound. Mesh prims are skipped: Blender binds those itself."""
    for path, ids in prim_map.items():
        prim = stage.GetPrimAtPath(path)
        if not prim or not prim.IsA(UsdGeom.Gprim) or prim.IsA(UsdGeom.Mesh):
            continue
        mesh = first_of_type(ids, bpy.types.Mesh)
        if mesh is None or len(mesh.materials) > 0:
            continue
        material_path = bound_material_path(prim)
        if material_path is not None:
            yield path, mesh, material_path


def assign_shape_materials(stage, prim_map):
    """Binds each unbound shape mesh to the material this import created for its prim's binding.
    Returns (shapes bound, sorted material paths the import did not create)."""
    imported = {}
    for path, ids in prim_map.items():
        material = first_of_type(ids, bpy.types.Material)
        if material is not None:
            imported[path] = material
    assigned = 0
    missing = set()
    for _, mesh, material_path in unbound_shapes(stage, prim_map):
        material = imported.get(material_path)
        if material is None:
            missing.add(str(material_path))
            continue
        mesh.materials.append(material)
        assigned += 1
    return assigned, sorted(missing)


def authored_constant(shader, name):
    """The value authored on `shader`'s input `name` when it is a constant (not connected), else
    None."""
    shader_input = shader.GetInput(name)
    if not shader_input or shader_input.HasConnectedSource() or not shader_input.GetAttr().HasAuthoredValue():
        return None
    return shader_input.Get()


def transparency_opacity(material_prim):
    """The opacity Blender imports as transmission: a constant UsdPreviewSurface opacity below 1
    with no opacityThreshold above 0 (a threshold makes Blender use Alpha itself). Else None."""
    surface, _, _ = UsdShade.Material(material_prim).ComputeSurfaceSource()
    if not surface or surface.GetIdAttr().Get() != "UsdPreviewSurface":
        return None
    opacity = authored_constant(surface, "opacity")
    if opacity is None or opacity >= 1.0:
        return None
    threshold_input = surface.GetInput("opacityThreshold")
    if threshold_input and threshold_input.HasConnectedSource():
        return None
    threshold = authored_constant(surface, "opacityThreshold")
    if threshold is not None and threshold > 0.0:
        return None
    return float(opacity)


def principled_node(material):
    if material.node_tree is None:
        return None
    return next((node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"), None)


def opacity_to_alpha(stage, prim_map):
    """Moves each imported UsdPreviewSurface material's transparency from Transmission Weight to
    Alpha, where the add-on streams it as coverage. Returns how many materials changed."""
    changed = 0
    for path, ids in prim_map.items():
        material = first_of_type(ids, bpy.types.Material)
        if material is None:
            continue
        opacity = transparency_opacity(stage.GetPrimAtPath(path))
        node = principled_node(material)
        if opacity is None or node is None:
            continue
        transmission, alpha = node.inputs["Transmission Weight"], node.inputs["Alpha"]
        if transmission.is_linked or alpha.is_linked:
            continue
        transmission.default_value = 0.0
        alpha.default_value = opacity
        changed += 1
    return changed


class QAROS_USDHook_shape_materials(bpy.types.USDHook):
    bl_idname = "qaros_shape_materials"
    bl_label = "QAROS: USD materials for streaming"
    bl_description = (
        "Gives imported USD primitive shapes the materials bound to them, and reads "
        "UsdPreviewSurface opacity as alpha"
    )

    @staticmethod
    def on_import(import_context):
        stage, prim_map = import_context.get_stage(), import_context.get_prim_map()
        transparent = opacity_to_alpha(stage, prim_map)
        if transparent:
            log(f"USD import: opacity of {transparent} material(s) moved from Transmission to Alpha")
        assigned, missing = assign_shape_materials(stage, prim_map)
        if assigned:
            log(f"USD import: bound materials to {assigned} primitive shape(s)")
        if missing:
            log(
                f"USD import: {len(missing)} material(s) bound to primitive shapes were not imported "
                f"({', '.join(missing)}); use File > Import > USD for QAROS, or enable Import All Materials"
            )
        return True


def scale_light_radii(objects):
    """Gives each point and spot light among `objects` the radius its USD sphere light has in the
    world. USD scales a light's radius with its transforms, the unit conversion included; Blender
    imports the radius as authored and Cycles ignores the object's scale for it, so a 5 cm sphere
    light in a centimetre stage became a 5 m sphere. Area lights need nothing: their size follows
    the object's scale. Returns how many radii changed."""
    changed = 0
    done = set()
    for obj in objects:
        light = obj.data if obj.type == "LIGHT" else None
        if light is None or light.type not in ("POINT", "SPOT") or light.as_pointer() in done:
            continue
        done.add(light.as_pointer())
        scale = sum(abs(component) for component in obj.matrix_world.to_scale()) / 3.0
        if light.shadow_soft_size > 0.0 and abs(scale - 1.0) > 1e-6:
            light.shadow_soft_size *= scale
            changed += 1
    return changed


class QAROS_OT_import_usd(bpy.types.Operator, ImportHelper):
    bl_idname = "qaros.import_usd"
    bl_label = "Import USD for QAROS"
    bl_description = (
        "Blender's USD import, with the materials of primitive shapes (spheres, cubes, cylinders, "
        "...) kept so they stream with their colours"
    )
    bl_options = {"REGISTER", "UNDO"}

    filter_glob: bpy.props.StringProperty(default="*.usd;*.usda;*.usdc;*.usdz", options={"HIDDEN"})

    def execute(self, context):
        before = {material.as_pointer() for material in bpy.data.materials}
        objects_before = {obj.as_pointer() for obj in bpy.data.objects}
        result = bpy.ops.wm.usd_import(filepath=self.filepath, import_all_materials=True)
        if "FINISHED" not in result:
            self.report({"ERROR"}, f"USD import failed: {self.filepath}")
            return {"CANCELLED"}
        context.view_layer.update()
        resized = scale_light_radii([obj for obj in bpy.data.objects if obj.as_pointer() not in objects_before])
        if resized:
            log(f"USD import: {resized} sphere light radius(es) scaled to world units")
        # Import All Materials keeps the materials no mesh used at import time with a fake user. The
        # hook has bound the shapes' since: drop that fake user, then the materials still unused.
        created = [material for material in bpy.data.materials if material.as_pointer() not in before]
        for material in created:
            material.use_fake_user = False
        for material in created:
            if material.users == 0:
                bpy.data.materials.remove(material)
        return {"FINISHED"}


def import_menu(menu, _context):
    menu.layout.operator(QAROS_OT_import_usd.bl_idname, text="USD for QAROS (.usd*)")


CLASSES = (QAROS_USDHook_shape_materials, QAROS_OT_import_usd)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(import_menu)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(import_menu)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
