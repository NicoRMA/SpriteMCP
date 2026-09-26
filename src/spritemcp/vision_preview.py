"""Deterministic nearest-neighbor scaled previews for agent vision QA.

Native 90×128 (and other authored PNGs) remain the source of truth on disk.
Vision consumers receive a nearest-neighbor upscale (default ×8), matching
``compose_character``'s ``compose_preview_scaled.png`` approach.

This module has no MCP dependency — the MCP server embeds PNG bytes as
``ImageContent`` via FastMCP's ``Image`` helper.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, Literal

from PIL import Image

from .character import (
    anim_dir,
    frame_stem,
    validate_animation_name,
    validate_character_name,
    validate_frame_index,
)
from .outfit import design_dir, slot_layer_path

# Match compose_character / build_frame_animation default scale.
VISION_PREVIEW_SCALE = 8

ShowPreviewKind = Literal["compose", "layer", "frame", "contact_sheet"]
SHOW_PREVIEW_KINDS: tuple[str, ...] = ("compose", "layer", "frame", "contact_sheet")
SHOW_PREVIEW_SCHEMA = "spritemcp.show_preview.v1"


def scale_rgba_nearest(
    image: Image.Image,
    scale: int = VISION_PREVIEW_SCALE,
) -> Image.Image:
    """Nearest-neighbor upscale (or identity when ``scale == 1``)."""
    if not isinstance(scale, int) or isinstance(scale, bool) or scale < 1:
        raise ValueError(f"scale must be an int >= 1, got {scale!r}")
    rgba = image.convert("RGBA")
    if scale == 1:
        return rgba
    w, h = rgba.size
    return rgba.resize((w * scale, h * scale), Image.Resampling.NEAREST)


def png_bytes_nearest_scaled(
    path: Path | str,
    *,
    scale: int = VISION_PREVIEW_SCALE,
) -> bytes:
    """Load a PNG from disk and return nearest-neighbor scaled PNG bytes."""
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"Preview image not found: {src}")
    scaled = scale_rgba_nearest(Image.open(src), scale)
    buf = BytesIO()
    scaled.save(buf, format="PNG")
    return buf.getvalue()


def _native_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        return int(img.width), int(img.height)


def resolve_preview_source(
    name: str,
    *,
    kind: str = "compose",
    layer: str | None = None,
    animation_name: str | None = None,
    frame_index: int | None = None,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Resolve an authored on-disk PNG path for the given preview kind.

    Does not invent pixels — only paths that already exist after the normal
    pipeline (compose / paint / finish / export).
    """
    name = validate_character_name(name)
    if kind not in SHOW_PREVIEW_KINDS:
        raise ValueError(
            f"Unknown preview kind {kind!r}. Valid: {', '.join(SHOW_PREVIEW_KINDS)}"
        )

    source: Path
    detail: dict[str, Any] = {"kind": kind, "name": name}

    if kind == "compose":
        source = design_dir(name, output_dir) / "compose_preview.png"
        if not source.is_file():
            raise FileNotFoundError(
                f"compose_preview.png missing for {name!r} under "
                f"{design_dir(name, output_dir)}. Call compose_character first."
            )
    elif kind == "layer":
        if layer is None:
            raise ValueError("kind='layer' requires layer=<EXPORT_LAYER_NAMES key>")
        source = slot_layer_path(name, layer, output_dir)
        detail["layer"] = layer
        if not source.is_file():
            raise FileNotFoundError(
                f"Design layer PNG missing: {source}. Paint the layer first."
            )
    elif kind == "frame":
        if animation_name is None:
            raise ValueError("kind='frame' requires animation_name")
        if frame_index is None:
            raise ValueError("kind='frame' requires frame_index (0..7)")
        animation_name = validate_animation_name(animation_name)
        frame_index = validate_frame_index(frame_index)
        anim = anim_dir(name, animation_name, output_dir)
        stem = frame_stem(frame_index)
        final_png = anim / f"{stem}.png"
        draft_png = anim / "draft" / f"{stem}.png"
        if final_png.is_file():
            source = final_png
            detail["frame_status"] = "final"
        elif draft_png.is_file():
            source = draft_png
            detail["frame_status"] = "draft"
        else:
            raise FileNotFoundError(
                f"No frame PNG for {name}/{animation_name} frame {frame_index} "
                f"(checked {final_png} and {draft_png}). "
                "Call build_frame_animation / finish_frame_animation first."
            )
        detail["animation_name"] = animation_name
        detail["frame_index"] = frame_index
    else:  # contact_sheet
        if animation_name is None:
            raise ValueError("kind='contact_sheet' requires animation_name")
        animation_name = validate_animation_name(animation_name)
        source = (
            anim_dir(name, animation_name, output_dir)
            / f"{animation_name}_contact_sheet.png"
        )
        detail["animation_name"] = animation_name
        if not source.is_file():
            raise FileNotFoundError(
                f"Contact sheet missing: {source}. Finish all frames 0..7 "
                "(or call export_animation_preview)."
            )

    native_w, native_h = _native_size(source)
    detail.update(
        {
            "source_path": str(source),
            "native_width": native_w,
            "native_height": native_h,
        }
    )
    return detail


def show_preview(
    name: str,
    *,
    kind: str = "compose",
    layer: str | None = None,
    animation_name: str | None = None,
    frame_index: int | None = None,
    output_dir: Path | str | None = None,
    scale: int = VISION_PREVIEW_SCALE,
) -> dict[str, Any]:
    """Resolve an authored preview and return JSON metadata for MCP embedding.

    Does not embed image bytes (MCP layer does). ``source_path`` is the native
    on-disk PNG; callers scale with ``png_bytes_nearest_scaled`` for vision.
    """
    if not isinstance(scale, int) or isinstance(scale, bool) or scale < 1:
        raise ValueError(f"scale must be an int >= 1, got {scale!r}")

    resolved = resolve_preview_source(
        name,
        kind=kind,
        layer=layer,
        animation_name=animation_name,
        frame_index=frame_index,
        output_dir=output_dir,
    )
    native_w = int(resolved["native_width"])
    native_h = int(resolved["native_height"])
    source_path = str(resolved["source_path"])

    result: dict[str, Any] = {
        "schema": SHOW_PREVIEW_SCHEMA,
        "name": resolved["name"],
        "kind": resolved["kind"],
        "source_path": source_path,
        "native_width": native_w,
        "native_height": native_h,
        "vision_scale": scale,
        "vision_width": native_w * scale,
        "vision_height": native_h * scale,
        "resampling": "nearest",
        "paths": {"source": source_path},
        "preview_paths": {"source": source_path},
        "next_step": (
            "Scaled PNG is embedded by the MCP tool as ImageContent. "
            "Use that image for visual QA — do not rely on Cursor Read."
        ),
    }
    for key in ("layer", "animation_name", "frame_index", "frame_status"):
        if key in resolved:
            result[key] = resolved[key]
    return result
