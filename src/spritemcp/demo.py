"""Minimal demo: paint a tiny test pattern with the locked character palette.

Outputs land under ``<cwd>/output/demo/`` by default.
"""

from __future__ import annotations

from .canvas import SpriteCanvas
from .config import subdir
from .export import export_all, swatch_image
from .frames import Frame, FrameSequence
from .palette import (
    CHARACTER_PALETTE,
    PRIMARY_ROLES,
    get_color,
)


def build_demo_canvas() -> SpriteCanvas:
    """16x16 canvas: dark field, beige block, red accent line."""
    canvas = SpriteCanvas(16, 16, name="demo_test_pattern")

    canvas.add_layer("background")
    canvas.add_layer("shape")
    canvas.add_layer("accent")

    # Near-black field (not the ref grey).
    canvas.fill_rect("background", 0, 0, 16, 16, get_color("near_black"))

    # Beige block with one shadow step (style-lock shading rule).
    canvas.fill_rect("shape", 3, 3, 8, 8, get_color("off_white"))
    canvas.fill_rect("shape", 3, 9, 8, 2, get_color("shadow_beige"))

    # Thin deep-red accent (1 px class).
    canvas.paint_points(
        "accent",
        [(12, y) for y in range(2, 14)],
        get_color("deep_red"),
    )
    # Corner markers so coordinate paint is obvious in the preview.
    for x, y in ((0, 0), (15, 0), (0, 15), (15, 15)):
        canvas.paint("accent", x, y, get_color("deep_red"))

    return canvas


def main() -> int:
    output_dir = subdir("demo")
    output_dir.mkdir(parents=True, exist_ok=True)

    canvas = build_demo_canvas()
    paths = export_all(canvas, output_dir, composite_name="preview.png")

    # Palette swatch for quick visual check against the style lock.
    swatch = swatch_image([(role, CHARACTER_PALETTE[role]) for role in PRIMARY_ROLES])
    swatch_path = output_dir / "palette_swatch.png"
    swatch.save(swatch_path)

    # Frame sequence stub — proves the API exists; not a full animation.
    seq = FrameSequence(name="demo_stub")
    seq.add(Frame(name="frame_0", canvas=canvas, duration_ms=100))

    print(f"Wrote demo outputs to {output_dir}")
    for key, path in sorted(paths.items()):
        print(f"  {key}: {path.name}")
    print(f"  palette_swatch: {swatch_path.name}")
    print(f"  frame_stub: {seq.name} ({len(seq)} frame(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
