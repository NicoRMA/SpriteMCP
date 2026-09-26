"""Manual pixel editor for SpriteMCP design layers.

Launches a small local web UI so a human can edit the 10 design-layer PNGs
after the agent shading pass. Apply writes only ``design/layers/*.png`` —
never ``base/``.
"""

from .server import open_pixel_editor

__all__ = ["open_pixel_editor"]
