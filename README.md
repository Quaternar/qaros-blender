# QAROS for Blender

The **QAROS ANARI Streaming** add-on streams the scene open in Blender into a QAROS app volume, live,
to every headset and visualizer in the session. Keep working in Blender: moved objects, edited meshes
and changed materials show up in the room within a moment. A controls panel in the headset edits the
active object, and pointing into the volume selects and moves objects.

```mermaid
flowchart LR
    B[Blender scene] -->|qaros_anari add-on| D[QAROS ANARI device]
    D -->|renders with| R[VisGL, OSPRay or Helide]
    D -->|stream| H[QAROS Hub]
    H --> T[headsets and visualizers]
```

## Requirements

- Windows, Blender 4.5 LTS or newer.
- [QAROS](https://docs.quaternar.com/docs/operator-guide/installation) installed with the **ANARI
  device** component (`/ANARI`); optionally the **OSPRay backend (CPU)** component (`/OSPRay`).

## Install

Either way installs the latest [release](https://github.com/Quaternar/qaros-blender/releases/latest):

- **From QAROS:** the QAROS Visualizer's **Blender** tile (Sources) downloads the latest release of
  the add-on and starts Blender with it; the add-on then starts streaming by itself.
- **By hand:** download `qaros_blender_anari-<version>.zip` from the
  [latest release](https://github.com/Quaternar/qaros-blender/releases/latest). Blender: **Edit →
  Preferences → Add-ons → Install from Disk**, pick the ZIP, enable **QAROS ANARI Streaming**. Then
  3D View sidebar (`N`) → **QAROS** tab → **Start streaming**.

Allow Blender in QAROS the first time it joins.

## Documentation

| Page | For |
|---|---|
| [User guide](docs/user-guide.md) | setup, app volume, backends, preferences, materials, troubleshooting, limits |
| [Demo](demo/README.md) | a turbofan engine demo on VisGL to pull apart in the headset, with a start guide for presenting it |
| [Developing](docs/DEVELOPING.md) | how the add-on works, repository layout, tests, building the ZIP, releasing |
| [Changelog](CHANGELOG.md) | changes per version |

QAROS itself is documented at [docs.quaternar.com](https://docs.quaternar.com).

## Licence

MIT, see [LICENSE](LICENSE).
