"""PNG / GIF export helpers for sprite canvases, layers, and animations."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PIL import Image

from .canvas import Layer, PixelGrid, SpriteCanvas
from .palette import Color

DEFAULT_PREVIEW_DURATION_MS = 100
# Locked: every animation preview GIF uses native frame resolution (90×128).
# Agents must not upscale GIFs per-clip — that makes demos/comparisons messy.
ANIMATION_PREVIEW_SCALE = 1


def load_rgba_images(paths: Sequence[Path | str]) -> list[Image.Image]:
    """Load each path as an RGBA image. Raises ``FileNotFoundError`` if missing."""
    images: list[Image.Image] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            raise FileNotFoundError(f"Frame image not found: {path}")
        images.append(Image.open(path).convert("RGBA"))
    if not images:
        raise ValueError("At least one frame image is required")
    return images


def make_contact_sheet(frames: Sequence[Image.Image]) -> Image.Image:
    """Horizontal contact sheet: frames left → right, top-aligned on a transparent canvas."""
    if not frames:
        raise ValueError("At least one frame is required for a contact sheet")
    rgba_frames = [f.convert("RGBA") for f in frames]
    width = sum(f.width for f in rgba_frames)
    height = max(f.height for f in rgba_frames)
    sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    x = 0
    for frame in rgba_frames:
        sheet.paste(frame, (x, 0), frame)
        x += frame.width
    return sheet


def save_contact_sheet(
    frames: Sequence[Image.Image],
    path: Path | str,
) -> Path:
    """Write a horizontal RGBA contact sheet PNG."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    make_contact_sheet(frames).save(out)
    return out


def _scale_nearest(frame: Image.Image, scale: int) -> Image.Image:
    if scale < 1:
        raise ValueError(f"scale must be >= 1, got {scale}")
    rgba = frame.convert("RGBA")
    if scale == 1:
        return rgba
    w, h = rgba.size
    return rgba.resize((w * scale, h * scale), Image.Resampling.NEAREST)


def _rgba_to_gif_frame(frame: Image.Image) -> Image.Image:
    """Quantize RGBA to a palette GIF frame with a reserved transparency index."""
    rgba = frame.convert("RGBA")
    alpha = rgba.getchannel("A")
    opaque = alpha.point(lambda a: 255 if a > 128 else 0)
    rgb = Image.new("RGB", rgba.size, (0, 0, 0))
    rgb.paste(rgba.convert("RGB"), mask=opaque)
    # Reserve index 255 for transparency (quantize uses at most 255 colors).
    pal = rgb.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    palette = list(pal.getpalette() or [])
    if len(palette) < 768:
        palette.extend([0] * (768 - len(palette)))
    palette[765:768] = [0, 0, 0]
    pal.putpalette(palette)

    transparent = 255
    clear = alpha.point(lambda a: 255 if a <= 128 else 0)
    px = bytearray(pal.tobytes())
    for i, flag in enumerate(clear.tobytes()):
        if flag:
            px[i] = transparent
    out = Image.frombytes("P", pal.size, bytes(px))
    out.putpalette(palette)
    out.info["transparency"] = transparent
    return out


def save_preview_gif(
    frames: Sequence[Image.Image],
    path: Path | str,
    *,
    duration_ms: int = DEFAULT_PREVIEW_DURATION_MS,
    scale: int = ANIMATION_PREVIEW_SCALE,
    loop: int = 0,
) -> Path:
    """Write an animated GIF (loops forever when ``loop=0``) from RGBA frames.

    ``scale`` must be ``ANIMATION_PREVIEW_SCALE`` (native size). Upscaled
    preview GIFs are forbidden so every clip shares the same resolution.
    """
    if not frames:
        raise ValueError("At least one frame is required for a preview GIF")
    if duration_ms < 1:
        raise ValueError(f"duration_ms must be >= 1, got {duration_ms}")
    if scale != ANIMATION_PREVIEW_SCALE:
        raise ValueError(
            f"Animation preview GIF scale is locked to "
            f"{ANIMATION_PREVIEW_SCALE} (native frame size); got {scale}. "
            "Do not pass a custom scale — all clips must match."
        )

    prepared = [_rgba_to_gif_frame(_scale_nearest(f, scale)) for f in frames]
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    prepared[0].save(
        out,
        save_all=True,
        append_images=prepared[1:],
        duration=duration_ms,
        loop=loop,
        disposal=2,
        transparency=prepared[0].info.get("transparency", 255),
        optimize=False,
    )
    return out


def export_animation_sheet_and_gif(
    frame_paths: Sequence[Path | str],
    *,
    sheet_path: Path | str,
    gif_path: Path | str,
    duration_ms: int = DEFAULT_PREVIEW_DURATION_MS,
    scale: int = ANIMATION_PREVIEW_SCALE,
) -> dict[str, Path]:
    """Load frame PNGs and write a contact sheet + looping preview GIF.

    GIF scale is locked to ``ANIMATION_PREVIEW_SCALE`` (native 90×128).
    """
    frames = load_rgba_images(frame_paths)
    sheet = save_contact_sheet(frames, sheet_path)
    gif = save_preview_gif(
        frames,
        gif_path,
        duration_ms=duration_ms,
        scale=scale,
    )
    return {"contact_sheet": sheet, "preview_gif": gif}


def grid_to_image(grid: PixelGrid) -> Image.Image:
    height = len(grid)
    width = len(grid[0]) if height else 0
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = img.load()
    assert pixels is not None
    for y in range(height):
        for x in range(width):
            pixels[x, y] = grid[y][x]
    return img


def save_grid(grid: PixelGrid, path: Path | str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    grid_to_image(grid).save(out)
    return out


def save_layer(layer: Layer, path: Path | str) -> Path:
    return save_grid(layer.pixels, path)


def save_composite(canvas: SpriteCanvas, path: Path | str) -> Path:
    return save_grid(canvas.composite(), path)


def export_layers(
    canvas: SpriteCanvas,
    directory: Path | str,
    *,
    prefix: str = "",
    only_visible: bool = True,
) -> list[Path]:
    """Write each layer as ``{prefix}{layer_name}.png`` under ``directory``."""
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for layer in canvas.layers():
        if only_visible and not layer.visible:
            continue
        path = out_dir / f"{prefix}{layer.name}.png"
        save_layer(layer, path)
        written.append(path)
    return written


def export_all(
    canvas: SpriteCanvas,
    directory: Path | str,
    *,
    composite_name: str = "preview.png",
    prefix: str = "",
) -> dict[str, Path]:
    """Export every visible layer PNG plus a composited preview."""
    out_dir = Path(directory)
    layer_paths = export_layers(canvas, out_dir, prefix=prefix)
    preview = save_composite(canvas, out_dir / composite_name)
    return {
        "preview": preview,
        **{p.stem: p for p in layer_paths},
    }


def swatch_image(
    colors: Sequence[tuple[str, Color]],
    *,
    cell: int = 8,
) -> Image.Image:
    """Tiny horizontal palette strip (role order left → right)."""
    img = Image.new("RGBA", (cell * len(colors), cell), (0, 0, 0, 0))
    pixels = img.load()
    assert pixels is not None
    for i, (_name, color) in enumerate(colors):
        for y in range(cell):
            for x in range(cell):
                pixels[i * cell + x, y] = color
    return img
