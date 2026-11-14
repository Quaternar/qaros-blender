# Changelog

Changes to the QAROS ANARI Streaming add-on that a user sees or must act on.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow `bl_info["version"]`.

## [Unreleased]

### Added

- Streams the open scene into a QAROS app volume through the QAROS ANARI device: VisGL, OSPRay and
  Helide backends, constant Principled material inputs, lights in Cycles units, culling outside
  the app volume, batching of static objects.
- App volume naming per Blender ("Blender", "Blender 2", ...) and **Fit scene to volume**.
- Controls panel in the headset: Object, Material, Frame and Panels tabs, undo and redo; tabs and
  Blender panels chosen in the preferences.
- Pointing into the app volume selects an object and moves it with a one-hand drag.
- **File → Import → USD for QAROS** keeps shape materials, opacity and light radii of USD scenes.
- A Blender started from QAROS quits when QAROS stops it, saving `quit.blend` first.
- A Blender the QAROS Visualizer's Blender tile starts streams by itself: with
  `QAR_BLENDER_AUTO_START=1` (also `true`, `on`, `yes`) the add-on presses **Start streaming** once
  after Blender has loaded. Off words (`0`, `false`, `off`, `no`) or no value keep the button; other
  text is off with a warning. A failed start is not retried.
- With **ANARI folder** empty, the add-on loads the ANARI device of the QAROS that started Blender
  (`QAR_ANARI_FOLDER`), else the first folder on the system `PATH` holding `anari.dll` and
  `anari_library_qaros.dll`.
