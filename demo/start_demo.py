# SPDX-License-Identifier: MIT
"""Starts the turbofan demo in one go: enable the add-on, choose the backend, build the scene from
assets/turbofan.glb (unless a .blend was opened), start streaming, fit the engine to its app volume,
play the turntable.

    blender [saved.blend] --python demo/start_demo.py -- [options]

Options (all optional):
    --backend NAME      visgl (default), visrtx, ospray, helide or automatic
    --addon-dir DIR     folder that holds qaros_anari/ (a checkout's `blender` folder); default: the
                        add-on installed in Blender
    --anari-folder DIR  folder with anari.dll and anari_library_qaros.dll; default: the add-on's
                        preference (empty: the QAROS setup's ANARI component)
    --no-play           leave the animation stopped
    --no-fit            keep the app volume's placement as it is

Each step is printed with a `[qaros-demo]` prefix, so a launcher log shows how far it got."""
import os
import sys
import time

import addon_utils
import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_demo_scene  # noqa: E402

T0 = time.monotonic()
STATUS_EVERY_S = 10.0
FIT_WAIT_S = 30.0


def log(text):
    print(f"[qaros-demo {time.monotonic() - T0:5.1f}s] {text}", flush=True)


def options(argv):
    args = argv[argv.index("--") + 1:] if "--" in argv else []

    def value(name, default=None):
        return args[args.index(name) + 1] if name in args else default

    return {
        "backend": value("--backend", "visgl"),
        "addon_dir": value("--addon-dir"),
        "anari_folder": value("--anari-folder"),
        "play": "--no-play" not in args,
        "fit": "--no-fit" not in args,
    }


OPTIONS = options(sys.argv)


def enable_addon():
    if OPTIONS["addon_dir"]:
        sys.path.insert(0, OPTIONS["addon_dir"])
    addon_utils.enable("qaros_anari", default_set=True, handle_error=lambda error: log(f"add-on failed: {error!r}"))
    if "qaros_anari" not in bpy.context.preferences.addons:
        log("the QAROS ANARI Streaming add-on is not installed: install qaros_anari.zip or pass --addon-dir")
        return None
    import qaros_anari

    prefs = bpy.context.preferences.addons["qaros_anari"].preferences
    prefs.backend = "" if OPTIONS["backend"] == "automatic" else OPTIONS["backend"]
    if OPTIONS["anari_folder"]:
        prefs.anari_folder = OPTIONS["anari_folder"]
    log(f"add-on enabled, backend {OPTIONS['backend']}")
    return qaros_anari


def view3d():
    window = bpy.context.window_manager.windows[0]
    for area in window.screen.areas:
        if area.type == "VIEW_3D":
            return window, area, area.regions[-1]
    return window, None, None


def run_in_view(operator):
    window, area, region = view3d()
    with bpy.context.temp_override(window=window, area=area, region=region):
        return operator()


ADDON = enable_addon()
if not bpy.data.filepath:  # Blender opened no .blend: build the demo
    build_demo_scene.build()
    log("scene built")
else:
    log(f"opened {bpy.data.filepath}")
build_demo_scene.frame_viewports()


def start():
    if ADDON is None:
        return None
    run_in_view(bpy.ops.qaros.start_streaming)
    log(f"start pressed: {ADDON.STREAMER.status()}")
    return None


FIT_STARTED = time.monotonic()


def fit_and_play():
    """Fits once the app volume is known, then plays; gives up on the fit after FIT_WAIT_S."""
    if ADDON is None or not ADDON.STREAMER.running:
        return 1.0 if time.monotonic() - FIT_STARTED < FIT_WAIT_S else None
    if OPTIONS["fit"]:
        if not ADDON.STREAMER.volume_known():
            if time.monotonic() - FIT_STARTED < FIT_WAIT_S:
                return 1.0
            log("app volume not known yet: not fitted (use Fit scene to volume in the QAROS tab)")
        else:
            run_in_view(bpy.ops.qaros.fit_volume)
            log("fitted to the app volume")
    screen = bpy.context.window_manager.windows[0].screen
    if OPTIONS["play"] and not screen.is_animation_playing:
        run_in_view(bpy.ops.screen.animation_play)
        log("playing")
    return None


def report():
    if ADDON is None:
        return None
    log(f"status: {ADDON.STREAMER.status()}")
    return STATUS_EVERY_S


bpy.app.timers.register(start, first_interval=2.0)
bpy.app.timers.register(fit_and_play, first_interval=4.0)
bpy.app.timers.register(report, first_interval=6.0)
