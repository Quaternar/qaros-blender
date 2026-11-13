# SPDX-License-Identifier: MIT
"""Which QAROS app volume a Blender streams into. Plain Python, no bpy, so it is tested outside
Blender (tests/test_blender_volume_name.py).

- No name typed (the usual case, also a Blender started from QAROS with no file): the volume
  "Blender". When another Blender on this PC already shows it, the qaros device streams this one
  as "Blender 2", "Blender 3", ... instead of leaving it invisible.
- A name typed in the QAROS panel: exactly that volume. When another Blender shows it, streaming
  does not start and the panel says so; nothing is renamed behind the user's back.
"""
import hashlib

AUTOMATIC_NAME = "blender"
AUTOMATIC_DISPLAY_NAME = "Blender"


class VolumeChoice:
    def __init__(self, name, display_name, numbering):
        self.name = name  # common name: the volume's identity, ASCII
        self.display_name = display_name  # shown to people: any text
        self.numbering = numbering  # True: the device may stream as "<display> 2" instead

    @property
    def automatic(self):
        return self.numbering


def choose_volume(typed):
    """The volume for the name typed in the panel ('' or None: automatic)."""
    display = (typed or "").strip()
    if not display:
        return VolumeChoice(AUTOMATIC_NAME, AUTOMATIC_DISPLAY_NAME, True)
    # A common name must be ASCII; a display name may be anything, so the name is a hash of it.
    # The same typed name always gives the same volume, in the place it was left.
    digest = hashlib.sha256(display.encode("utf-8")).hexdigest()[:12]
    return VolumeChoice(f"{AUTOMATIC_NAME}-{digest}", display, False)


def streaming_note(choice, used_display_name):
    """The panel line when streaming runs under another name than asked for, else None."""
    if not used_display_name or used_display_name == choice.display_name:
        return None
    return f"{choice.display_name} is already shown by another Blender: streaming as {used_display_name}"


def conflict_message(choice, stream_error):
    """The panel line when streaming did not start because the name is taken, else None."""
    if not stream_error or "already shown" not in stream_error:
        return None
    if choice.automatic:
        return "Every Blender volume name is in use by other Blenders on this PC"
    return f"'{choice.display_name}' is already shown by another Blender. Choose another name."
