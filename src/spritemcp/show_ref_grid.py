"""Visualize character_style_ref.png with a visible per-pixel grid.

No part labels — grid, axes, and optional content bbox only.

Run:
  python -m spritemcp show-ref-grid
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import DEFAULT_STYLE_REF, subdir

DEFAULT_REF = DEFAULT_STYLE_REF
DEFAULT_OUT_DIR = subdir("reference_grid")

# Background of the style ref (neutral grey) — used only to find content bbox.
_BG_TOLERANCE = 18


def _is_background(pixel: tuple[int, ...], bg: tuple[int, int, int], tol: int) -> bool:
    r, g, b = pixel[:3]
    return (
        abs(r - bg[0]) <= tol
        and abs(g - bg[1]) <= tol
        and abs(b - bg[2]) <= tol
    )


def content_bbox(
    image: Image.Image,
    *,
    tol: int = _BG_TOLERANCE,
) -> tuple[int, int, int, int] | None:
    """Axis-aligned bbox of non-background pixels: (x0, y0, x1, y1) exclusive."""
    rgba = image.convert("RGBA")
    # Sample corners for background estimate.
    w, h = rgba.size
    samples = [
        rgba.getpixel((0, 0))[:3],
        rgba.getpixel((w - 1, 0))[:3],
        rgba.getpixel((0, h - 1))[:3],
        rgba.getpixel((w - 1, h - 1))[:3],
    ]
    bg = tuple(sum(c[i] for c in samples) // 4 for i in range(3))

    min_x, min_y = w, h
    max_x, max_y = -1, -1
    for y in range(h):
        for x in range(w):
            if not _is_background(rgba.getpixel((x, y)), bg, tol):
                if x < min_x:
                    min_x = x
                if y < min_y:
                    min_y = y
                if x > max_x:
                    max_x = x
                if y > max_y:
                    max_y = y
    if max_x < 0:
        return None
    return (min_x, min_y, max_x + 1, max_y + 1)


def _axis_margin(cell: int, label_every: int) -> int:
    """Left/top margin so axis numbers fit."""
    # Enough room for multi-digit labels at cell scale.
    return max(28, cell * 2 + 8 + label_every // 2)


def render_ref_grid(
    image: Image.Image,
    *,
    scale: int = 8,
    grid_rgba: tuple[int, int, int, int] = (40, 40, 40, 160),
    axis_bg: tuple[int, int, int, int] = (32, 32, 32, 255),
    axis_fg: tuple[int, int, int, int] = (220, 220, 220, 255),
    bbox_rgba: tuple[int, int, int, int] = (0, 200, 255, 220),
    label_every: int = 5,
    draw_bbox: bool = True,
    grid_line_width: int = 1,
) -> Image.Image:
    """Nearest-neighbor upscale + grid lines + numbered axes.

    Each source pixel becomes a ``scale``×``scale`` cell; grid lines sit on
    cell boundaries (``grid_line_width`` pixels thick).
    """
    if scale < 2:
        raise ValueError("scale must be >= 2 so grid lines are visible")
    if grid_line_width < 1:
        raise ValueError("grid_line_width must be >= 1")

    src = image.convert("RGBA")
    w, h = src.size
    cell = scale
    margin = _axis_margin(cell, label_every)
    bottom_margin = 22  # room for bbox / axis captions

    # Upscale sprite (no grid yet).
    up = src.resize((w * cell, h * cell), Image.Resampling.NEAREST)

    canvas_w = margin + w * cell + 1
    canvas_h = margin + h * cell + 1 + bottom_margin
    out = Image.new("RGBA", (canvas_w, canvas_h), axis_bg)
    out.paste(up, (margin, margin), up)

    draw = ImageDraw.Draw(out)
    try:
        font = ImageFont.load_default()
    except OSError:
        font = None

    ox, oy = margin, margin
    # Vertical grid lines (x = 0 .. w).
    for x in range(w + 1):
        px = ox + x * cell
        draw.line(
            [(px, oy), (px, oy + h * cell)],
            fill=grid_rgba,
            width=grid_line_width,
        )
    # Horizontal grid lines (y = 0 .. h).
    for y in range(h + 1):
        py = oy + y * cell
        draw.line(
            [(ox, py), (ox + w * cell, py)],
            fill=grid_rgba,
            width=grid_line_width,
        )

    # Numbered axes (every label_every pixels; always include 0 and last).
    def _ticks(n: int) -> list[int]:
        ticks = list(range(0, n, label_every))
        if ticks[-1] != n - 1 and n > 0:
            ticks.append(n - 1)
        return ticks

    for x in _ticks(w):
        cx = ox + x * cell + cell // 2
        label = str(x)
        # Anchor near top of left/top gutter.
        draw.text((cx - 4, 4), label, fill=axis_fg, font=font)
        # Tick into gutter.
        draw.line([(cx, oy - 3), (cx, oy)], fill=axis_fg, width=1)

    for y in _ticks(h):
        cy = oy + y * cell + cell // 2
        label = str(y)
        draw.text((4, cy - 5), label, fill=axis_fg, font=font)
        draw.line([(ox - 3, cy), (ox, cy)], fill=axis_fg, width=1)

    # Axis titles.
    footer_y = oy + h * cell + 4
    draw.text((ox + w * cell // 2 - 10, footer_y), "x ->", fill=axis_fg, font=font)
    draw.text((2, oy + h * cell // 2 - 6), "y", fill=axis_fg, font=font)

    if draw_bbox:
        box = content_bbox(src)
        if box is not None:
            x0, y0, x1, y1 = box
            # Inclusive outer edge of the content cells.
            left = ox + x0 * cell
            top = oy + y0 * cell
            right = ox + x1 * cell
            bottom = oy + y1 * cell
            for t in range(2):
                draw.rectangle(
                    [left - t, top - t, right + t, bottom + t],
                    outline=bbox_rgba,
                )
            caption = (
                f"bbox x={x0}..{x1 - 1} y={y0}..{y1 - 1}  ({x1 - x0}x{y1 - y0} px)"
            )
            draw.text((ox, footer_y), caption, fill=bbox_rgba, font=font)

    # Meta strip: size / scale (top gutter).
    meta = f"{w}x{h} px  |  cell={cell}px  |  scale={scale}x NN"
    draw.text((ox, 2), meta, fill=axis_fg, font=font)

    return out


def build_outputs(
    ref_path: Path,
    out_dir: Path,
    *,
    scale: int = 8,
    readable_scale: int = 12,
) -> dict[str, Path]:
    """Write main grid PNG and a thicker-line readable upscale."""
    image = Image.open(ref_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    main = render_ref_grid(image, scale=scale, grid_line_width=1, label_every=5)
    main_path = out_dir / "character_style_ref_grid.png"
    main.save(main_path)

    readable = render_ref_grid(
        image,
        scale=readable_scale,
        grid_line_width=2,
        label_every=5,
        grid_rgba=(30, 30, 30, 200),
    )
    readable_path = out_dir / "character_style_ref_grid_readable.png"
    readable.save(readable_path)

    return {
        "grid": main_path,
        "readable": readable_path,
        "ref": ref_path,
        "size": image.size,
        "scale": scale,
        "readable_scale": readable_scale,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Draw character_style_ref.png onto a visible pixel grid.",
    )
    parser.add_argument(
        "--ref",
        type=Path,
        default=DEFAULT_REF,
        help="Path to style reference PNG",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Output directory for grid PNGs",
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=8,
        help="Nearest-neighbor upscale factor (pixels per source pixel)",
    )
    parser.add_argument(
        "--readable-scale",
        type=int,
        default=12,
        help="Upscale for the thicker-grid readable PNG",
    )
    args = parser.parse_args(argv)

    if not args.ref.is_file():
        print(f"Reference not found: {args.ref}", file=sys.stderr)
        return 1

    result = build_outputs(
        args.ref,
        args.out_dir,
        scale=args.scale,
        readable_scale=args.readable_scale,
    )
    w, h = result["size"]
    print(f"Ref: {result['ref']} ({w}x{h})")
    print(f"Cell size: {args.scale}x{args.scale} px per source pixel (NN)")
    print(f"Wrote:")
    print(f"  {result['grid']}")
    print(f"  {result['readable']}  (scale={args.readable_scale}x, thicker lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
