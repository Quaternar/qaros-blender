# Changelog

Changes to the QAROS ANARI Streaming add-on that a user sees or must act on.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is the file `VERSION`, equal
to `bl_info["version"]`, and the release tag `vX.Y.Z`; how a version is released is in
[docs/DEVELOPING.md](docs/DEVELOPING.md#releasing).

## [Unreleased]

### Added

- Metal reflects its surroundings in the headset on VisGL, as in Blender's Material Preview and
  EEVEE, instead of showing only the lamps' highlights. The stream lights the scene with the World's
  environment image, and in Material Preview (and Rendered with the viewport's World option off)
  with the viewport's studio HDRI, its intensity and rotation. Needs a QAROS whose VisGL has the
  environment light; with an older one the stream looks as before.

## [0.5.2] - 2026-11-17

### Fixed

- A double tap in the headset selects the whole model, not the part it hit: the top of the part's
  parent chain becomes the selected and active object (a glTF that imports as an empty with child
  meshes is selected by the empty, its parts show selected in Blender), and a one-hand drag on any
  part moves the whole model, and the stream outlines the whole model. A click with Object
  selection still selects the single part.
- Blender no longer crashes when you select or move objects while the controls panel pushes an undo
  step. The step was pushed from a timer through a window that could be a file browser or in the middle
  of a viewport move or an import; it is now pushed only through a freshly found, ready window, and
  waits for the next tick when there is none.
- Switching the Renderer to VisRTX while streaming no longer freezes Blender. VisRTX compiles its GPU
  programs on its first start, which takes about two minutes while the NVIDIA OptiX cache is empty
  (a new PC or driver, a cleared cache) and under a second afterwards. The add-on now prepares VisRTX
  in the background, keeps streaming on the current renderer meanwhile ("Starting VisRTX... the first
  start can take a few minutes" in the QAROS tab), and restarts on it when it is ready. If VisRTX
  fails or does not answer within ten minutes, the stream stays on the renderer it had and the tab
  says why. Choosing VisRTX while not streaming prepares it in the background too, so Start is quick.

## [0.5.1] - 2026-11-17

### Fixed

- Blender 5.3 crashes with the add-on (an access violation in the GPU driver's OpenGL library), so the supported range is Blender 4.5 to 5.2. On Blender 5.3
  or newer the add-on refuses to start and says why, and `manifest.json` names the newest Blender
  (`max_blender_version`) so the QAROS Visualizer's Blender tile does not offer it.

## [0.5.0] - 2026-11-17

### Added

- **Render rate** preference (frames per second per user, 1 to 120, default 30) replaces
  **Frame rate**, and applies at once, also while streaming. A lower rate leaves the renderer time
  for more users; scene changes then show later, head motion stays smooth. A Frame rate you had
  set is not carried over: set Render rate again.
- **Tools** tab, first on the controls panel in the headset, and **Tool** in the QAROS tab and the
  preferences: **Navigation** (the default; hand gestures pan, zoom and turn the whole scene) or
  **Object selection** (a click selects what the hand's ray hits by Blender's ray cast; one-hand
  drags, two-hand spreads and turns move only the selected object, and with nothing selected do
  nothing). One tool for every user.
- A double tap (two clicks of one hand within half a second) selects what the ray hits, with
  either tool.
- A hand's ray turns solid green while it pinches for a click and for a moment after.
- Two hands scale or turn the selected object about its origin. A two-hand turn about the vertical
  now works at all (QAROS's default rules had none).
- The selected object is outlined in the headset, in Blender's orange, as in Blender's viewport:
  selected in the headset or in Blender. Needs a QAROS whose ANARI device draws the selection
  outline, and a backend that renders object ids (VisRTX, OSPRay, Helide; not VisGL).
- The stream follows the 3D viewport's shading: **Wireframe** streams mesh edges as thin lines,
  **Solid** matte surfaces in the viewport colour, **Material Preview** and **Rendered** the
  exported materials, with the scene's lamps and World as the viewport's options say. The QAROS
  tab's **View** panel can fix one shading instead.
- **Renderer** in the QAROS tab: choose VisGL, VisRTX, OSPRay, Helide or Automatic; while
  streaming, the stream restarts on the new one. No Blender restart.
- **Active Object** in the QAROS tab follows Blender's selection: what the stream does with the
  object, its **Stream to QAROS** setting (also in **Properties → Object → QAROS**), the material
  values the stream uses and its transform. **Select an object** when nothing is selected.
- **Stream to QAROS** per object: off, the object stays in Blender only. Saved with the `.blend`.

### Changed

- The QAROS tab is Blender-style: collapsible sub-panels (Active Object, View, Renderer, Streams
  To, Headset, Diagnostics), icons for the status and on buttons, help in tooltips. Material
  notes and objects left out moved to **Diagnostics**, closed by default.
- **Backend** preference is now **Renderer** and applies at the next Start instead of after a
  Blender restart.

- Blender decides what every hand gesture in the app volume does; QAROS only reports gestures and
  draws the rays. While streaming the add-on replaces the volume's gesture rules, and restores them
  when it stops.
- One selection for every user, Blender's: the controls panel shows the object selected in the
  headset as well as in Blender, and a click on empty space no longer clears it.

## [0.4.1] - 2026-11-15

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
