# SPDX-License-Identifier: MIT
"""Blender's World as one plain environment description, copied on Blender's UI thread: an
equirectangular image (Environment Texture) or a plain colour, its strength and its rotation. The
sync worker turns it into an ANARI `hdri` light on backends that have one (sync.py)."""
from dataclasses import dataclass, field
import math

import numpy as np

from . import blender_support

# Wider images are averaged down to this width before they are sent: a 4K HDRI is 100 MB of
# floats, and a light this blurred by the materials it lights loses nothing visible at 1K.
MAX_ENVIRONMENT_WIDTH = 1024
# A plain World colour is sent as a constant image of this size (columns, rows).
UNIFORM_SIZE = (4, 2)

# Blender world (Z up) -> app volume (Y up), as sync.Z_UP_TO_Y_UP: (x, y, z) -> (x, z, -y).
_Z_UP_TO_Y_UP = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=np.float64)


@dataclass(frozen=True)
class Environment:
    """`pixels`: linear RGB, float32, shape (rows, columns, 3), row 0 at the bottom (Blender's
    order, which is also the order ANARI's hdri light reads). `source` names what the pixels were
    read from; two environments with the same source share their pixels. `direction` is where the
    image's centre column points and `up` where its top row points, both in app-volume space."""
    source: tuple
    pixels: np.ndarray = field(compare=False, repr=False)
    strength: float = 1.0
    direction: tuple = (1.0, 0.0, 0.0)
    up: tuple = (0.0, 1.0, 0.0)
    warnings: tuple = field(default=(), compare=False)


def _follow(socket, warnings, what):
    """The node feeding `socket` through reroutes, or None when it is unlinked."""
    visited = set()
    while socket is not None and socket.is_linked:
        links = [link for link in socket.links if not link.is_muted]
        if len(links) != 1:
            return None
        node = links[0].from_node
        if node.as_pointer() in visited:
            warnings.append(f"{what} path contains a cycle.")
            return None
        visited.add(node.as_pointer())
        if node.mute:
            warnings.append(f"{what} path contains a muted node.")
            return None
        if node.type != "REROUTE":
            return node
        socket = node.inputs[0] if node.inputs else None
    return None


def _euler_matrix(euler):
    """Blender's XYZ Euler rotation: Rz @ Ry @ Rx."""
    x, y, z = (float(angle) for angle in euler)
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return rz @ ry @ rx


def _orientation(texture, warnings):
    """(direction, up) of the image in app-volume space, from a Mapping node on the Environment
    Texture's Vector input. The texture looks up M @ d for a view direction d, where M is the
    mapping's rotation (inverted for the Texture type), so its centre (+X) shows at M^T @ X and its
    top (+Z) at M^T @ Z. Location and scale are ignored: they do not turn a direction."""
    rotation = np.identity(3)
    vector = texture.inputs.get("Vector")
    node = _follow(vector, warnings, "Environment vector") if vector is not None else None
    if node is not None and node.type == "MAPPING":
        socket = node.inputs.get("Rotation")
        if socket is not None and socket.is_linked:
            warnings.append("Mapping rotation is linked; using no rotation.")
        elif socket is not None:
            rotation = _euler_matrix(socket.default_value)
            if node.vector_type == "TEXTURE":
                rotation = rotation.T
    elif node is not None and node.type != "TEX_COORD":
        warnings.append(f"Environment vector from {node.bl_label} is not supported; using no rotation.")
    direction = _Z_UP_TO_Y_UP @ (rotation.T @ np.array([1.0, 0.0, 0.0]))
    up = _Z_UP_TO_Y_UP @ (rotation.T @ np.array([0.0, 0.0, 1.0]))
    return tuple(float(v) for v in direction), tuple(float(v) for v in up)


def srgb_to_linear(values):
    return np.where(values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4)


def downsample(pixels, max_width=MAX_ENVIRONMENT_WIDTH):
    """`pixels` averaged over whole blocks of factor x factor, so it is at most `max_width` wide."""
    rows, columns = pixels.shape[:2]
    factor = math.ceil(columns / max_width)
    if factor <= 1:
        return pixels
    rows, columns = rows // factor * factor, columns // factor * factor
    blocks = pixels[:rows, :columns].reshape(rows // factor, factor, columns // factor, factor, -1)
    return blocks.mean(axis=(1, 3), dtype=np.float64).astype(np.float32)


def image_pixels(image):
    """The image's pixels as linear RGB float32 (rows, columns, 3), row 0 at the bottom, at most
    MAX_ENVIRONMENT_WIDTH wide, or None when it has none (missing file)."""
    columns, rows = image.size
    channels = image.channels
    if not columns or not rows or not channels:
        return None
    values = np.empty(columns * rows * channels, dtype=np.float32)
    image.pixels.foreach_get(values)
    values = values.reshape(rows, columns, channels)
    rgb = values[:, :, :3] if channels >= 3 else np.repeat(values[:, :, :1], 3, axis=2)
    rgb = np.nan_to_num(rgb, nan=0.0, posinf=0.0, neginf=0.0)
    # Float images (.hdr, .exr) hold scene-linear values; 8-bit sRGB ones hold display values.
    if not image.is_float and image.colorspace_settings.name == "sRGB":
        rgb = srgb_to_linear(rgb)
    return np.ascontiguousarray(downsample(np.maximum(rgb, 0.0)), dtype=np.float32)


def image_source(image):
    return (
        "image", image.name, image.filepath_raw, tuple(image.size), image.channels,
        image.colorspace_settings.name, bool(image.is_float),
    )


def uniform(color, strength, warnings=()):
    color = tuple(max(0.0, float(c)) if math.isfinite(c) else 0.0 for c in tuple(color)[:3])
    if strength <= 0.0 or not any(color):
        return None
    columns, rows = UNIFORM_SIZE
    pixels = np.empty((rows, columns, 3), dtype=np.float32)
    pixels[:, :] = color
    return Environment(("colour",) + color, pixels, strength, warnings=tuple(warnings))


def studio_environment(source, pixels, strength, rotation):
    """Blender's viewport studio HDRI (`pixels` as image_pixels reads them, from `source`) as an
    environment, or None without pixels. Blender maps it like a World's Environment Texture
    behind a Vector Mapping rotated by `rotation` about Z (Cycles' viewport builds exactly that),
    so its centre faces Rz(rotation)^T @ X."""
    if strength <= 0.0 or pixels is None:
        return None
    turned = _euler_matrix((0.0, 0.0, rotation)).T
    direction = _Z_UP_TO_Y_UP @ (turned @ np.array([1.0, 0.0, 0.0]))
    up = _Z_UP_TO_Y_UP @ np.array([0.0, 0.0, 1.0])
    return Environment(
        ("studio",) + tuple(source), pixels, float(strength),
        tuple(float(v) for v in direction), tuple(float(v) for v in up),
    )


def world_environment(world, cache=None):
    """The environment `world` lights the scene with, or None (no World, a black one, or strength
    0). `cache` maps a source to its pixels, so an image is read again only when it changed; it is
    trimmed to the source in use."""
    if world is None:
        return None
    warnings = []
    if not blender_support.uses_nodes(world) or world.node_tree is None:
        return uniform(world.color, 1.0)
    outputs = [
        node for node in world.node_tree.nodes
        if node.type == "OUTPUT_WORLD" and node.is_active_output and getattr(node, "target", "ALL") == "ALL"
    ]
    if len(outputs) != 1:
        return None
    surface = outputs[0].inputs.get("Surface")
    background = _follow(surface, warnings, "World surface") if surface is not None else None
    if background is None:
        return None
    if background.type != "BACKGROUND":
        warnings.append(f"World shader {background.bl_label} is not supported; using the World's viewport colour.")
        return uniform(world.color, 1.0, warnings)
    strength_socket = background.inputs.get("Strength")
    strength = 1.0
    if strength_socket is not None:
        if strength_socket.is_linked:
            warnings.append("World strength is linked; using strength 1.")
        else:
            strength = float(strength_socket.default_value)
            strength = strength if math.isfinite(strength) else 1.0
    if strength <= 0.0:
        return None
    color_socket = background.inputs.get("Color")
    if color_socket is None:
        return uniform(world.color, strength, warnings)
    if not color_socket.is_linked:
        return uniform(color_socket.default_value, strength, warnings)
    texture = _follow(color_socket, warnings, "World colour")
    if texture is None or texture.type != "TEX_ENVIRONMENT":
        label = texture.bl_label if texture is not None else "an unresolved link"
        warnings.append(f"World colour from {label} is not supported; using the World's viewport colour.")
        return uniform(world.color, strength, warnings)
    if texture.projection != "EQUIRECTANGULAR":
        warnings.append("Environment texture is not equirectangular; using the World's viewport colour.")
        return uniform(world.color, strength, warnings)
    image = texture.image
    if image is None:
        warnings.append("Environment texture has no image; using the World's viewport colour.")
        return uniform(world.color, strength, warnings)
    source = image_source(image)
    cache = {} if cache is None else cache
    pixels = cache.get(source)
    if pixels is None:
        pixels = image_pixels(image)
        if pixels is None:
            warnings.append(f"Environment image '{image.name}' has no pixels; using the World's viewport colour.")
            return uniform(world.color, strength, warnings)
    cache.clear()
    cache[source] = pixels
    direction, up = _orientation(texture, warnings)
    return Environment(source, pixels, strength, direction, up, tuple(warnings))
