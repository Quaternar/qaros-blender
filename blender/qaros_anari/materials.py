# SPDX-License-Identifier: MIT
"""Plain material values copied on Blender's UI thread, with no Blender references in snapshots."""
from dataclasses import dataclass
import math

from . import anari, blender_support


@dataclass(frozen=True, slots=True)
class MaterialDescription:
    base_color: tuple[float, float, float] = (0.8, 0.8, 0.8)
    opacity: float = 1.0
    metallic: float = 0.0
    roughness: float = 0.5
    emission_color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    emission_strength: float = 1.0
    is_principled: bool = False
    warnings: tuple[str, ...] = ()
    # Material > Viewport Display > Color: what Blender's Solid shading draws (shading.py).
    viewport_color: tuple[float, float, float] = (0.8, 0.8, 0.8)

    @property
    def emissive(self):
        return tuple(component * self.emission_strength for component in self.emission_color)


FALLBACK_MATERIAL = MaterialDescription()


def material_key(material):
    return material.name if material is not None else ""


# Key of a material made from a colour attribute (sync.py): this prefix and the colour.
# No Blender material name starts with a control character, so it never meets a real one.
COLOR_KEY_PREFIX = "\x1fcolour "


def material_label(key):
    """How a material key is named to the user."""
    if key.startswith(COLOR_KEY_PREFIX):
        return f"colour attribute #{key[len(COLOR_KEY_PREFIX):]}"
    return key


def constant_input(node, name, fallback, warnings, fallback_label):
    socket = node.inputs.get(name)
    if socket is None:
        return fallback
    if socket.is_linked:
        warnings.append(f"{name} is linked; using {fallback_label}.")
        return fallback
    return socket.default_value


def scalar_input(node, name, fallback, warnings, fallback_label, maximum=None):
    value = float(constant_input(node, name, fallback, warnings, fallback_label))
    if not math.isfinite(value):
        warnings.append(f"{name} is not finite; using {fallback_label}.")
        return fallback
    return max(0.0, value) if maximum is None else max(0.0, min(maximum, value))


def principled_shader(tree):
    """A directly connected Principled shader, allowing reroutes but no graph evaluation."""
    outputs = [
        node for node in tree.nodes
        if node.type == "OUTPUT_MATERIAL" and node.is_active_output and node.target == "ALL"
    ]
    if len(outputs) != 1:
        return None, "No unique active Material Output for All; using viewport colour."
    socket = outputs[0].inputs.get("Surface")
    visited = set()
    while socket is not None:
        links = [link for link in socket.links if not link.is_muted]
        if len(links) != 1:
            return None, "Surface shader path is unconnected or ambiguous; using viewport colour."
        node = links[0].from_node
        identity = node.as_pointer()
        if identity in visited:
            return None, "Surface shader path contains a cycle; using viewport colour."
        visited.add(identity)
        if node.mute:
            return None, "Surface shader path contains a muted node; using viewport colour."
        if node.type == "BSDF_PRINCIPLED":
            return node, None
        if node.type != "REROUTE":
            return None, f"Unsupported surface shader {node.bl_label}; using viewport colour."
        socket = node.inputs[0] if node.inputs else None
    return None, "Surface shader path has no input; using viewport colour."


def material_description(material):
    """Copy the connected shader's constants, without retaining Blender data or evaluating links."""
    if material is None:
        return FALLBACK_MATERIAL
    viewport = tuple(material.diffuse_color)
    if not blender_support.uses_nodes(material):
        return MaterialDescription(
            base_color=viewport[:3], opacity=max(0.0, min(1.0, viewport[3])), viewport_color=viewport[:3]
        )
    if material.node_tree is None:
        node, reason = None, "Node material has no node tree; using viewport colour."
    else:
        node, reason = principled_shader(material.node_tree)
    if node is None:
        return MaterialDescription(
            base_color=viewport[:3], opacity=max(0.0, min(1.0, viewport[3])), warnings=(reason,),
            viewport_color=viewport[:3],
        )
    warnings = []
    base_color = tuple(constant_input(node, "Base Color", viewport, warnings, "viewport colour"))[:3]
    opacity = scalar_input(node, "Alpha", 1.0, warnings, "full opacity", 1.0)
    metallic = scalar_input(node, "Metallic", 0.0, warnings, "non-metallic", 1.0)
    roughness = scalar_input(node, "Roughness", 0.5, warnings, "roughness 0.5", 1.0)
    emission_color = tuple(constant_input(
        node, "Emission Color", (0.0, 0.0, 0.0), warnings, "no emission"
    ))[:3]
    emission_strength = scalar_input(node, "Emission Strength", 1.0, warnings, "no emission")
    if any(
        socket is not None and socket.is_linked
        for socket in (node.inputs.get("Emission Color"), node.inputs.get("Emission Strength"))
    ):
        emission_color, emission_strength = (0.0, 0.0, 0.0), 0.0
    return MaterialDescription(
        base_color, opacity, metallic, roughness, emission_color, emission_strength,
        is_principled=True, warnings=tuple(warnings), viewport_color=viewport[:3],
    )


def material_descriptions(materials):
    descriptions = {material_key(material): material_description(material) for material in materials}
    descriptions[""] = FALLBACK_MATERIAL
    return descriptions


def material_export(description, preferred):
    """Choose a cached model and its typed parameters without making any ANARI calls."""
    support = preferred.matte if not description.is_principled and preferred.matte is not None else preferred
    parameters = [(support.color_parameter, anari.FLOAT32_VEC3, description.base_color)]
    warnings = []
    opacity = description.opacity
    if opacity < 1.0 and not support.supports_blend:
        opacity = 1.0
        warnings.append("Backend cannot represent alpha blending; using an opaque approximation.")
    if support.supports("opacity", anari.FLOAT32):
        parameters.append(("opacity", anari.FLOAT32, opacity))
    if support.supports("alphaMode", anari.STRING):
        if opacity == 1.0 and "opaque" in support.alpha_modes:
            parameters.append(("alphaMode", anari.STRING, "opaque"))
        elif support.supports_blend:
            parameters.append(("alphaMode", anari.STRING, "blend"))
    if support.subtype == "physicallyBased":
        for name, data_type, value in (
            ("metallic", anari.FLOAT32, description.metallic),
            ("roughness", anari.FLOAT32, description.roughness),
            ("emissive", anari.FLOAT32_VEC3, description.emissive),
        ):
            if support.supports(name, data_type):
                parameters.append((name, data_type, value))
            else:
                warnings.append(f"Backend cannot set {name}; using its renderer default.")
    elif description.is_principled:
        for requested, name in (
            (description.metallic != 0.0, "metallic"),
            (description.roughness != 0.5, "roughness"),
            (any(description.emissive), "emission"),
        ):
            if requested:
                warnings.append(f"Backend uses matte materials; {name} is approximated by diffuse colour.")
    return support, tuple(parameters), tuple(warnings)


class MaterialDiagnostics:
    """UI-thread status for used materials; log a reason once per material per stream session."""

    def __init__(self, on_warning=None):
        self.on_warning = on_warning
        self.active = {}
        self.reported = set()

    def update(self, descriptions, used_keys, preferred=None):
        self.active = {}
        for key in used_keys:
            if key not in descriptions:
                continue
            description = descriptions[key]
            warnings = description.warnings
            if preferred is not None and key:
                warnings += material_export(description, preferred)[2]
            if warnings:
                self.active[key] = warnings
        for key, warnings in self.active.items():
            for reason in warnings:
                identity = (key, reason)
                if identity not in self.reported:
                    self.reported.add(identity)
                    if self.on_warning is not None:
                        self.on_warning(f"Material '{material_label(key)}': {reason}")

    @property
    def summary(self):
        count = len(self.active)
        return f"{count} material{'s use approximations' if count != 1 else ' uses an approximation'}"
