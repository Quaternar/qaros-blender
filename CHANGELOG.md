# Changelog

Changes to the QAROS ANARI Streaming add-on that a user sees or must act on.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is the file `VERSION`, equal
to `bl_info["version"]`, and the release tag `vX.Y.Z`; how a version is released is in
[docs/DEVELOPING.md](docs/DEVELOPING.md#releasing).

## [Unreleased]

### Added

- **Streams to** in the QAROS tab chooses the devices the scene streams to: **All devices** (the
  default, as before) or a checkbox per connected headset and Visualizer receiver, following devices
  as they join and leave, applied at once and saved with the `.blend`. A Blender started from QAROS
  without a file streams to every device. Needs a QAROS whose ANARI device takes `qar.targets`; an
  older one keeps streaming to every device and the tab says so.
- **VisRTX materials** preference: **Physically based (metal, grain)**, the default, or **Matte
  (smooth)**. Before, VisRTX always streamed matte materials.
- The scene's World (an equirectangular Environment Texture with its strength and Mapping
  rotation, or a plain colour) becomes an environment light on a backend that can light with an
  environment it does not draw. None of today's backends can: VisGL has no environment light,
  OSPRay's streaming renderer ignores it and VisRTX draws it behind the scene, so the World is
  left out there without an error (console: `world: not streamed`).

### Changed

- Gaussian splats, point cloud objects and meshes without faces are not streamed. The tab and the
  console now say so once per object, and **Fit scene to volume** frames only what is streamed.
- **Automatic** backend tries VisGL first, then VisRTX, OSPRay and Helide. Needs a QAROS whose
  ANARI device reads `QAR_ANARI_BACKEND_ORDER`; an older one keeps starting with VisRTX.
- VisRTX streams physically based materials (metal, highlights, emission) by default, with grain
  on faces lit by the ambient light in a moving view.

### Fixed

- Moving, turning or scaling the app volume's content only changes the view, never Blender's
  scene: after a click selected an object, a one-hand drag moves that object only when it grabs
  it. A drag anywhere else (the controls panel's title bar, empty space, another object) moves the
  view, or nothing, and leaves Blender's objects where they are.

## [0.4.0] - 2026-11-15

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
- A Blender started with `--addons qaros_anari` alone (QAROS starts Blender so) streams with the
  preferences' defaults when Blender's preferences do not list the add-on, as in a portable or
  never-started Blender, instead of failing with `key "qaros_anari" not found`.
- Releases on GitHub: `qaros_blender_anari-<version>.zip` for **Install from Disk** and a `manifest.json`
  from which the QAROS Visualizer's Blender tile installs the latest add-on. The first release is
  0.4.0, above the 0.3.1 that QAROS builds carried, so those installs are offered the update.
- With **ANARI folder** empty, the add-on loads the ANARI device of the QAROS that started Blender
  (`QAR_ANARI_FOLDER`), else the first folder on the system `PATH` holding `anari.dll` and
  `anari_library_qaros.dll`.
