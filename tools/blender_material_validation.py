# SPDX-License-Identifier: MIT
"""Live material reference scene, launched in a fresh Blender process by QAROS's Visualizer.

Arguments after --: --anari-folder PATH --backend-folder PATH --control-dir PATH
--backend visgl|helide|visrtx. Writes status.json; command.json accepts a sequence and a named case,
or action "stop" / "start" to exercise the add-on's live stream lifecycle.
The script replaces the fresh process's scene. It never saves Blender preferences or the file.
"""
import argparse
from functools import partial
import json
import math
import os
from pathlib import Path
import sys
import time

import bpy


def principled(name, colour):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    node = material.node_tree.nodes.get("Principled BSDF")
    node.inputs["Base Color"].default_value = (*colour, 1.0)
    node.inputs["Metallic"].default_value = 0.0
    node.inputs["Roughness"].default_value = 0.15
    material.diffuse_color = (*colour, 1.0)
    return material, node


def geometry_handles(mirror):
    groups = {name: entry[0] for name, entry in mirror.groups.items() if entry}
    chunks = tuple(chunk.entry[0] for chunk in mirror.chunks if chunk.entry)
    return groups, chunks


class Validation:
    def __init__(self, control, addon, material, node):
        self.control = control
        self.addon = addon
        self.material = material
        self.node = node
        self.output = next(n for n in material.node_tree.nodes if n.type == "OUTPUT_MATERIAL")
        self.sequence = 0
        self.case = "baseline"
        self.error = None
        self.initial_geometry = None
        self.started = time.monotonic()

    def apply(self, case):
        if case not in {"baseline", "red", "metallic", "rough", "emission", "half", "invisible", "matte", "linked"}:
            raise ValueError(f"Unknown reference case: {case}")
        tree, node = self.material.node_tree, self.node
        for link in list(node.inputs["Base Color"].links):
            tree.links.remove(link)
        tree.links.new(node.outputs["BSDF"], self.output.inputs["Surface"])
        node.inputs["Base Color"].default_value = (0.25, 0.125, 0.0625, 1.0)
        node.inputs["Alpha"].default_value = 1.0
        node.inputs["Metallic"].default_value = 0.0
        node.inputs["Roughness"].default_value = 0.15
        node.inputs["Emission Color"].default_value = (0.0, 0.0, 0.0, 1.0)
        node.inputs["Emission Strength"].default_value = 0.0
        self.material.diffuse_color = (0.0, 0.5, 0.0, 1.0)
        if case == "red":
            node.inputs["Base Color"].default_value = (0.75, 0.0, 0.0, 1.0)
        elif case in ("metallic", "rough"):
            node.inputs["Metallic"].default_value = 1.0
            if case == "rough":
                node.inputs["Roughness"].default_value = 0.8
        elif case == "emission":
            node.inputs["Emission Color"].default_value = (2.0, 0.0, 0.0, 1.0)
            node.inputs["Emission Strength"].default_value = 2.0
        elif case in ("half", "invisible"):
            node.inputs["Alpha"].default_value = 0.5 if case == "half" else 0.0
        elif case == "matte":
            diffuse = tree.nodes.get("Reference diffuse") or tree.nodes.new("ShaderNodeBsdfDiffuse")
            diffuse.name = "Reference diffuse"
            tree.links.new(diffuse.outputs[0], self.output.inputs["Surface"])
        elif case == "linked":
            colour = tree.nodes.get("Reference linked colour") or tree.nodes.new("ShaderNodeRGB")
            colour.name = "Reference linked colour"
            colour.outputs[0].default_value = (0.0, 0.0, 0.75, 1.0)
            tree.links.new(colour.outputs[0], node.inputs["Base Color"])
        self.material.update_tag()
        bpy.context.view_layer.update()
        self.case = case

    def tick(self):
        try:
            command = self.control / "command.json"
            if command.exists():
                request = json.loads(command.read_text(encoding="utf-8-sig"))
                sequence = int(request["sequence"])
                if sequence > self.sequence:
                    action = request.get("action", "material")
                    if action == "stop":
                        self.addon.STREAMER.stop()
                    elif action == "start":
                        if self.addon.STREAMER.running:
                            raise ValueError("Stop the stream before starting it again")
                        self.initial_geometry = None
                        self.addon.STREAMER.start(bpy.context)
                    elif action == "material":
                        self.apply(request["case"])
                    else:
                        raise ValueError(f"Unknown validation action: {action}")
                    self.sequence, self.error = sequence, None
            streamer = self.addon.STREAMER
            idle = streamer.worker is not None and streamer.worker.wait_idle(0)
            geometry = geometry_handles(streamer.mirror) if idle else None
            if geometry is not None and self.initial_geometry is None:
                self.initial_geometry = geometry
            status = {
                "pid": os.getpid(), "instance": os.environ.get("QAR_INSTANCE"),
                "seconds": round(time.monotonic() - self.started, 2),
                "case": self.case, "sequence": self.sequence,
                "applied_sequence": self.sequence if idle or not streamer.running else None,
                "stream": streamer.status(), "backend": os.environ.get("QAR_ANARI_BACKEND"),
                "materials": streamer.mirror.material_support.status if streamer.mirror is not None else None,
                "geometry_preserved": geometry == self.initial_geometry if idle else None,
                "approximations": streamer.capture.diagnostics.active if streamer.capture is not None else {},
                "error": self.error,
            }
            temporary = self.control / "status.tmp"
            try:
                temporary.write_text(json.dumps(status, indent=2), encoding="utf-8")
                temporary.replace(self.control / "status.json")
            except PermissionError:
                # Windows readers can briefly prevent replacement, retry on the next UI tick.
                print("Material validation: status file busy, retrying next tick", flush=True)
        except (KeyError, ValueError, OSError) as error:
            self.error = str(error)
            print(f"Material validation: {error}", flush=True)
        return 0.2


def build_scene(args):
    bpy.ops.preferences.addon_enable(module="qaros_anari")
    import qaros_anari
    prefs = qaros_anari.preferences()
    prefs.anari_folder = str(args.anari_folder.resolve())
    prefs.backend = args.backend
    prefs.max_users, prefs.render_rate, prefs.app_scale, prefs.ambient = 4, 30, 0.35, 0.05
    material, node = principled("Live shared material", (0.25, 0.125, 0.0625))
    for x in (-0.7, 0.7):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=20, radius=0.6, location=(x, -0.2, 0.0))
        sphere = bpy.context.view_layer.objects.active
        sphere.name = "Shared left" if x < 0 else "Shared right"
        sphere.data.materials.append(material)
        for polygon in sphere.data.polygons:
            polygon.use_smooth = True
    rear, _rear_node = principled("Blue rear surface", (0.0625, 0.25, 0.5))
    bpy.ops.mesh.primitive_plane_add(size=3.0, location=(0.0, 0.65, 0.0), rotation=(math.pi / 2, 0.0, 0.0))
    plane = bpy.context.view_layer.objects.active
    plane.name = "Rear surface"
    plane.scale.y = 0.65
    plane.data.materials.append(rear)
    light = bpy.data.lights.new("Reference sun", "SUN")
    light.energy = 2.0
    sun = bpy.data.objects.new("Reference sun", light)
    sun.rotation_euler.x = math.pi / 2
    bpy.context.scene.collection.objects.link(sun)
    bpy.context.view_layer.update()
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.shading.type = "SOLID"
    validation = Validation(args.control_dir.resolve(), qaros_anari, material, node)
    validation.apply("baseline")
    qaros_anari.STREAMER.start(bpy.context)
    bpy.app.timers.register(validation.tick, first_interval=0.2, persistent=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anari-folder", type=Path, required=True)
    parser.add_argument("--backend-folder", type=Path, required=True)
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--backend", choices=("visgl", "helide", "visrtx"), default="visgl")
    parser.add_argument("--sdk", type=Path, help="QAR SDK DLL; a development ZIP's sibling bin is detected automatically")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if not os.environ.get("QAR_INSTANCE"):
        raise RuntimeError("Launch this fresh Blender process from the development hub's Visualizer")
    args.control_dir.mkdir(parents=True, exist_ok=True)
    os.environ["QAR_ANARI_BACKEND_DIR"] = str(args.backend_folder.resolve())
    packaged_sdk = args.anari_folder.resolve().parent / "bin" / "qar-streaming-c.dll"
    if args.sdk is not None:
        os.environ["QAR_ANARI_SDK"] = str(args.sdk.resolve())
    elif packaged_sdk.is_file():
        os.environ.setdefault("QAR_ANARI_SDK", str(packaged_sdk))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # The GUI context is replaced by factory reset, so wait until Blender has installed it.
    bpy.app.timers.register(partial(build_scene, args), first_interval=0.5)


if __name__ == "__main__":
    main()
