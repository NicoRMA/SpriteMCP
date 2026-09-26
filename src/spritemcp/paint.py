"""MCP-facing pixel paint ops for outfit design layers.

Agents MUST paint via these tools (exposed on the MCP server). Editing
``design/layers/<slot>.png`` with Shell/PIL scripts or external image
generators is forbidden for agents — Pillow here is server-side only.

All ops mutate only the design layer (never base body). After writes they
refresh the body-under reference preview so agents can Read the PNG paths.
"""

from __future__ import annotations

import math
from collections import deque
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw

from .base_idle import PART_ID_TO_NAME, PART_NAMES
from .character import validate_character_name
from .outfit import (
    OUTFIT_SLOT_SPECS,
    _OVERHANG_GUIDANCE,
    _blank_rgba,
    _canvas_size,
    _ensure_design_layer,
    _load_part_map,
    _sync_layers_meta,
    _validate_slot,
    design_dir,
    meta_path,
    prepare_outfit_slot_reference,
    require_outfit_plan,
    slot_layer_path,
)

PAINT_SCHEMA = "spritemcp.outfit_paint.v1"
_MAX_PIXELS_PER_CALL = 8_000
_MAX_GET_PIXELS = 4_000
_FLOOD_WARN_FRACTION = 0.35

_FLOOD_WARNING = (
    "WARNING: flood_fill covered a large fraction of the canvas. Do NOT use "
    "flood_fill of the entire body silhouette as the sole outfit design — "
    "use fill_parts_on_slot for body silhouettes, then fill_rect / "
    "paint_pixels / draw_line / fill_ellipse for details."
)


def _resolve_part_name(value: Any, *, field: str) -> str:
    """Accept part name (str) or part id (int); return canonical part name."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a part name or part id")
    if isinstance(value, int):
        if value not in PART_ID_TO_NAME or value == 0:
            raise ValueError(
                f"{field}: unknown part id {value}. "
                f"Valid ids: {sorted(k for k in PART_ID_TO_NAME if k)}"
            )
        return PART_ID_TO_NAME[value]
    if isinstance(value, str):
        name = value.strip()
        if name not in PART_NAMES or name == "empty":
            valid = ", ".join(n for n in PART_NAMES if n != "empty")
            raise ValueError(f"{field}: unknown part {value!r}. Valid: {valid}")
        return name
    raise ValueError(f"{field} must be a part name (str) or part id (int)")


def _normalize_parts_list(
    parts: Sequence[Any] | None,
    *,
    field: str = "parts",
) -> list[str]:
    if parts is None:
        return []
    if not isinstance(parts, Sequence) or isinstance(parts, (str, bytes)):
        raise ValueError(f"{field} must be a list of part names or ids")
    out: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(parts):
        pname = _resolve_part_name(item, field=f"{field}[{i}]")
        if pname not in seen:
            seen.add(pname)
            out.append(pname)
    return out


def _parse_color(value: Any, *, field: str = "color") -> tuple[int, int, int, int]:
    """Parse RGBA from hex string, sequence, or mapping.

    Accepted:
    - ``\"#RGB\"``, ``\"#RRGGBB\"``, ``\"#RRGGBBAA\"`` (optional leading #)
    - ``[r,g,b]`` / ``[r,g,b,a]`` (0..255)
    - ``{\"r\":..,\"g\":..,\"b\":..,\"a\":..}`` (a optional, default 255)
    - ``\"transparent\"`` / ``null`` → (0,0,0,0) erase
    """
    if value is None:
        return (0, 0, 0, 0)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("transparent", "clear", "erase", "none"):
            return (0, 0, 0, 0)
        if text.startswith("#"):
            text = text[1:]
        if len(text) == 3:
            text = "".join(c * 2 for c in text)
        if len(text) == 6:
            text += "ff"
        if len(text) != 8 or any(c not in "0123456789abcdef" for c in text):
            raise ValueError(
                f"{field} hex must be #RGB, #RRGGBB, or #RRGGBBAA (got {value!r})"
            )
        return (
            int(text[0:2], 16),
            int(text[2:4], 16),
            int(text[4:6], 16),
            int(text[6:8], 16),
        )
    if isinstance(value, Mapping):
        try:
            r = int(value["r"])
            g = int(value["g"])
            b = int(value["b"])
            a = int(value.get("a", 255))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"{field} mapping needs r,g,b and optional a (0..255)"
            ) from exc
        return _clamp_rgba(r, g, b, a, field=field)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) == 3:
            r, g, b = value
            a = 255
        elif len(value) == 4:
            r, g, b, a = value
        else:
            raise ValueError(f"{field} sequence must be [r,g,b] or [r,g,b,a]")
        return _clamp_rgba(int(r), int(g), int(b), int(a), field=field)
    raise ValueError(
        f"{field} must be hex string, [r,g,b,a], mapping, or transparent"
    )


def _clamp_rgba(
    r: int, g: int, b: int, a: int, *, field: str
) -> tuple[int, int, int, int]:
    for name, v in (("r", r), ("g", g), ("b", b), ("a", a)):
        if not 0 <= v <= 255:
            raise ValueError(f"{field}.{name} must be 0..255 (got {v})")
    return (r, g, b, a)


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be an integer")
    return int(value)


def _open_design_for_paint(
    name: str,
    slot: str,
    *,
    output_dir: Path | str | None,
    plan_id: str | None,
) -> tuple[str, str, dict[str, Any], Image.Image, Path, int, int]:
    name = validate_character_name(name)
    slot = _validate_slot(slot)
    locked = require_outfit_plan(name, plan_id=plan_id, output_dir=output_dir)
    slot_plan = locked["plan"]["slots"][slot]
    if slot_plan.get("skip"):
        raise ValueError(
            f"Slot '{slot}' is marked skip in the outfit plan. "
            "Re-plan with skip:false to paint it."
        )
    part_map = _load_part_map(name, output_dir)
    w, h = _canvas_size(part_map)
    path = slot_layer_path(name, slot, output_dir)
    img = _ensure_design_layer(path, w, h)
    return name, slot, locked, img, path, w, h


def _finish_paint(
    name: str,
    slot: str,
    *,
    img: Image.Image,
    path: Path,
    locked: Mapping[str, Any],
    output_dir: Path | str | None,
    scale: int,
    op: str,
    pixels_touched: int,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    ref = prepare_outfit_slot_reference(
        name,
        slot,
        output_dir=output_dir,
        scale=scale,
        plan_id=locked["plan_id"],
    )
    meta = _sync_layers_meta(name, output_dir)
    painted = img.getchannel("A").getextrema()[1] > 0
    out: dict[str, Any] = {
        "schema": PAINT_SCHEMA,
        "op": op,
        "name": name,
        "slot": slot,
        "plan_id": locked["plan_id"],
        "pixels_touched": pixels_touched,
        "painted": painted,
        "canvas": {"width": img.width, "height": img.height},
        "allow_overhang": True,
        "overhang_note": _OVERHANG_GUIDANCE,
        "paint_instruction": ref["paint_instruction"],
        "preview_paths": ref["preview_paths"],
        "paths": {
            **ref["paths"],
            "layers_meta": str(meta_path(name, output_dir)),
        },
        "layers_meta": meta["slots"].get(slot),
        "next_step": (
            "Do not Cursor-Read after every paint. Call show_preview "
            "(kind=layer, layer=...) when you need visual QA, or continue "
            "painting then compose_character (preview embeds a scaled PNG). "
            "generate_outfit_slot is optional (import / refresh only) — "
            "paint tools already write the layer."
        ),
        "forbidden_note": (
            "FORBIDDEN for outfit painting: Shell python/PIL scripts, "
            "GenerateImage, OpenAI/other external image generators. "
            "REQUIRED: MCP paint tools + prepare_outfit_slot_reference + "
            "compose_character."
        ),
    }
    if extra:
        out.update(dict(extra))
    return out


def paint_pixels(
    name: str,
    slot: str,
    pixels: Sequence[Mapping[str, Any]],
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Set individual pixels on ``design/layers/<slot>.png``.

    Each pixel: ``{x, y, color}`` or ``{x, y, rgba}`` / ``{x, y, hex}``.
    Color accepts hex / [r,g,b,a] / transparent. Coordinates are character
    canvas space (same as body ref). Out-of-bounds pixels are skipped.
    """
    name, slot, locked, img, path, w, h = _open_design_for_paint(
        name, slot, output_dir=output_dir, plan_id=plan_id
    )
    if not isinstance(pixels, Sequence) or isinstance(pixels, (str, bytes)):
        raise ValueError("pixels must be a list of {x, y, color} objects")
    if len(pixels) > _MAX_PIXELS_PER_CALL:
        raise ValueError(
            f"pixels list too long (max {_MAX_PIXELS_PER_CALL}); "
            "use paint_from_commands or fill_rect for large areas"
        )
    pix = img.load()
    assert pix is not None
    touched = 0
    skipped = 0
    for i, item in enumerate(pixels):
        if not isinstance(item, Mapping):
            raise ValueError(f"pixels[{i}] must be an object with x, y, color")
        x = _as_int(item.get("x"), f"pixels[{i}].x")
        y = _as_int(item.get("y"), f"pixels[{i}].y")
        if "color" in item:
            color = _parse_color(item["color"], field=f"pixels[{i}].color")
        elif "rgba" in item:
            color = _parse_color(item["rgba"], field=f"pixels[{i}].rgba")
        elif "hex" in item:
            color = _parse_color(item["hex"], field=f"pixels[{i}].hex")
        else:
            raise ValueError(
                f"pixels[{i}] needs color, rgba, or hex"
            )
        if not (0 <= x < w and 0 <= y < h):
            skipped += 1
            continue
        pix[x, y] = color
        touched += 1
    return _finish_paint(
        name,
        slot,
        img=img,
        path=path,
        locked=locked,
        output_dir=output_dir,
        scale=scale,
        op="paint_pixels",
        pixels_touched=touched,
        extra={"skipped_oob": skipped},
    )


# Alias used by some agents / docs.
set_pixels = paint_pixels


def fill_parts_on_slot(
    name: str,
    slot: str,
    color: Any = None,
    *,
    parts: Sequence[Any] | None = None,
    part_ids: Sequence[Any] | None = None,
    part_colors: Mapping[str, Any] | None = None,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Fill body-part silhouettes onto ``design/layers/<slot>.png``.

    Direct replacement for ad-hoc scripts that looped ``part_map.part_pixels``
    and stamped a solid color. Writes ONLY the design layer (base untouched).

    ``parts`` / ``part_ids``: body part names (``torso``, ``head``, …) and/or
    integer ids from ``get_part_ids``. If both omitted, fills the slot's
    ``body_parts`` (export-layer silhouettes; ``lower_*`` includes hand/foot).

    ``color``: solid fill for all selected parts. Optional ``part_colors`` map
    ``{part_name: color}`` overrides per part (e.g. darker far legs).

    After fill, add details with ``fill_rect`` / ``draw_line`` / ``paint_pixels``
    (belt row, cross, nasal bar, etc.).
    """
    name, slot, locked, img, path, w, h = _open_design_for_paint(
        name, slot, output_dir=output_dir, plan_id=plan_id
    )
    slot_plan = locked["plan"]["slots"][slot]
    body_parts = list(
        slot_plan.get("body_parts") or OUTFIT_SLOT_SPECS[slot]["body_parts"]
    )
    parents = list(
        slot_plan.get("parent_parts") or OUTFIT_SLOT_SPECS[slot]["parent_parts"]
    )

    selected = _normalize_parts_list(parts, field="parts")
    if part_ids is not None:
        selected.extend(_normalize_parts_list(part_ids, field="part_ids"))
    # Dedupe preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for p in selected:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    selected = ordered

    if not selected:
        selected = list(body_parts)

    default_rgba: tuple[int, int, int, int] | None = None
    if color is not None:
        default_rgba = _parse_color(color, field="color")

    per_part: dict[str, tuple[int, int, int, int]] = {}
    if part_colors is not None:
        if not isinstance(part_colors, Mapping):
            raise ValueError("part_colors must be an object {part_name: color}")
        for key, val in part_colors.items():
            pname = _resolve_part_name(key, field="part_colors key")
            per_part[pname] = _parse_color(val, field=f"part_colors[{pname}]")
            if pname not in selected:
                selected.append(pname)

    if default_rgba is None and not per_part:
        raise ValueError(
            "Provide color and/or part_colors. Example: "
            'fill_parts_on_slot(name, "torso", color="#8B1A1A")'
        )

    part_map = _load_part_map(name, output_dir)
    pix = img.load()
    assert pix is not None
    touched = 0
    filled_parts: list[dict[str, Any]] = []
    for pname in selected:
        rgba = per_part.get(pname, default_rgba)
        if rgba is None:
            raise ValueError(
                f"No color for part {pname!r} — set color= or part_colors[{pname}]"
            )
        coords = part_map.part_pixels(pname)
        for x, y in coords:
            if 0 <= x < w and 0 <= y < h:
                pix[x, y] = rgba
                touched += 1
        filled_parts.append(
            {
                "part": pname,
                "part_id": PART_NAMES[pname],
                "pixels": len(coords),
                "color": list(rgba),
            }
        )

    return _finish_paint(
        name,
        slot,
        img=img,
        path=path,
        locked=locked,
        output_dir=output_dir,
        scale=scale,
        op="fill_parts_on_slot",
        pixels_touched=touched,
        extra={
            "parts_filled": filled_parts,
            "slot_parent_parts": parents,
            "slot_body_parts": body_parts,
            "note": (
                "Silhouette fill done on design layer. Add details with "
                "fill_rect / draw_line / paint_pixels / paint_from_commands "
                "(belt, cross, nasal, plates). Overhang: paint outside the "
                "silhouette with those primitives — body is not a clip mask."
            ),
        },
    )


def fill_rect(
    name: str,
    slot: str,
    x: int,
    y: int,
    width: int,
    height: int,
    color: Any,
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Fill an axis-aligned rectangle on the design layer (inclusive of edges)."""
    name, slot, locked, img, path, w, h = _open_design_for_paint(
        name, slot, output_dir=output_dir, plan_id=plan_id
    )
    rgba = _parse_color(color)
    x0 = _as_int(x, "x")
    y0 = _as_int(y, "y")
    rw = _as_int(width, "width")
    rh = _as_int(height, "height")
    if rw <= 0 or rh <= 0:
        raise ValueError("width and height must be positive")
    draw = ImageDraw.Draw(img)
    # Pillow rectangle: x1,y1 are inclusive for fill when using outline=None
    x1 = min(w - 1, x0 + rw - 1)
    y1 = min(h - 1, y0 + rh - 1)
    if x0 >= w or y0 >= h or x1 < 0 or y1 < 0:
        touched = 0
    else:
        cx0 = max(0, x0)
        cy0 = max(0, y0)
        draw.rectangle([cx0, cy0, x1, y1], fill=rgba)
        touched = (x1 - cx0 + 1) * (y1 - cy0 + 1)
    return _finish_paint(
        name,
        slot,
        img=img,
        path=path,
        locked=locked,
        output_dir=output_dir,
        scale=scale,
        op="fill_rect",
        pixels_touched=touched,
        extra={"rect": {"x": x0, "y": y0, "width": rw, "height": rh}, "color": list(rgba)},
    )


def stroke_rect(
    name: str,
    slot: str,
    x: int,
    y: int,
    width: int,
    height: int,
    color: Any,
    *,
    thickness: int = 1,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Stroke (outline) an axis-aligned rectangle on the design layer."""
    name, slot, locked, img, path, w, h = _open_design_for_paint(
        name, slot, output_dir=output_dir, plan_id=plan_id
    )
    rgba = _parse_color(color)
    x0 = _as_int(x, "x")
    y0 = _as_int(y, "y")
    rw = _as_int(width, "width")
    rh = _as_int(height, "height")
    t = _as_int(thickness, "thickness")
    if rw <= 0 or rh <= 0:
        raise ValueError("width and height must be positive")
    if t <= 0:
        raise ValueError("thickness must be positive")
    draw = ImageDraw.Draw(img)
    x1 = x0 + rw - 1
    y1 = y0 + rh - 1
    draw.rectangle([x0, y0, x1, y1], outline=rgba, width=t)
    # Approximate touched count (outline perimeter * thickness, clipped loosely)
    touched = max(0, 2 * (rw + rh) * t)
    return _finish_paint(
        name,
        slot,
        img=img,
        path=path,
        locked=locked,
        output_dir=output_dir,
        scale=scale,
        op="stroke_rect",
        pixels_touched=touched,
        extra={
            "rect": {"x": x0, "y": y0, "width": rw, "height": rh},
            "thickness": t,
            "color": list(rgba),
        },
    )


def draw_line(
    name: str,
    slot: str,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    color: Any,
    *,
    thickness: int = 1,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Draw a 1px (or thicker) line on the design layer."""
    name, slot, locked, img, path, _w, _h = _open_design_for_paint(
        name, slot, output_dir=output_dir, plan_id=plan_id
    )
    rgba = _parse_color(color)
    ax = _as_int(x0, "x0")
    ay = _as_int(y0, "y0")
    bx = _as_int(x1, "x1")
    by = _as_int(y1, "y1")
    t = _as_int(thickness, "thickness")
    if t <= 0:
        raise ValueError("thickness must be positive")
    draw = ImageDraw.Draw(img)
    draw.line([(ax, ay), (bx, by)], fill=rgba, width=t)
    touched = int(math.hypot(bx - ax, by - ay)) + 1
    return _finish_paint(
        name,
        slot,
        img=img,
        path=path,
        locked=locked,
        output_dir=output_dir,
        scale=scale,
        op="draw_line",
        pixels_touched=touched,
        extra={
            "line": {"x0": ax, "y0": ay, "x1": bx, "y1": by},
            "thickness": t,
            "color": list(rgba),
        },
    )


def fill_ellipse(
    name: str,
    slot: str,
    x: int,
    y: int,
    width: int,
    height: int,
    color: Any,
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Fill an ellipse bounded by the given rectangle on the design layer."""
    name, slot, locked, img, path, w, h = _open_design_for_paint(
        name, slot, output_dir=output_dir, plan_id=plan_id
    )
    rgba = _parse_color(color)
    x0 = _as_int(x, "x")
    y0 = _as_int(y, "y")
    rw = _as_int(width, "width")
    rh = _as_int(height, "height")
    if rw <= 0 or rh <= 0:
        raise ValueError("width and height must be positive")
    draw = ImageDraw.Draw(img)
    x1 = x0 + rw - 1
    y1 = y0 + rh - 1
    draw.ellipse([x0, y0, x1, y1], fill=rgba)
    # Rough upper bound for touched pixels
    touched = min(w * h, int(math.pi * (rw / 2.0) * (rh / 2.0)))
    return _finish_paint(
        name,
        slot,
        img=img,
        path=path,
        locked=locked,
        output_dir=output_dir,
        scale=scale,
        op="fill_ellipse",
        pixels_touched=touched,
        extra={
            "ellipse": {"x": x0, "y": y0, "width": rw, "height": rh},
            "color": list(rgba),
        },
    )


def clear_rect(
    name: str,
    slot: str,
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Erase (set transparent) a rectangle on the design layer only."""
    name, slot, locked, img, path, w, h = _open_design_for_paint(
        name, slot, output_dir=output_dir, plan_id=plan_id
    )
    x0 = _as_int(x, "x")
    y0 = _as_int(y, "y")
    rw = _as_int(width, "width")
    rh = _as_int(height, "height")
    if rw <= 0 or rh <= 0:
        raise ValueError("width and height must be positive")
    draw = ImageDraw.Draw(img)
    x1 = min(w - 1, x0 + rw - 1)
    y1 = min(h - 1, y0 + rh - 1)
    if x0 >= w or y0 >= h or x1 < 0 or y1 < 0:
        touched = 0
    else:
        cx0 = max(0, x0)
        cy0 = max(0, y0)
        draw.rectangle([cx0, cy0, x1, y1], fill=(0, 0, 0, 0))
        touched = (x1 - cx0 + 1) * (y1 - cy0 + 1)
    return _finish_paint(
        name,
        slot,
        img=img,
        path=path,
        locked=locked,
        output_dir=output_dir,
        scale=scale,
        op="clear_rect",
        pixels_touched=touched,
        extra={"rect": {"x": x0, "y": y0, "width": rw, "height": rh}},
    )


def flood_fill(
    name: str,
    slot: str,
    x: int,
    y: int,
    color: Any,
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Flood-fill connected same-color pixels on the design layer from (x,y).

    Operates on the design layer only (never body). Prefer shape primitives
    for clothing — do not flood the entire silhouette as the sole design.
    """
    name, slot, locked, img, path, w, h = _open_design_for_paint(
        name, slot, output_dir=output_dir, plan_id=plan_id
    )
    rgba = _parse_color(color)
    sx = _as_int(x, "x")
    sy = _as_int(y, "y")
    if not (0 <= sx < w and 0 <= sy < h):
        raise ValueError(f"flood seed ({sx},{sy}) outside canvas {w}x{h}")

    pix = img.load()
    assert pix is not None
    target = pix[sx, sy]
    if target == rgba:
        return _finish_paint(
            name,
            slot,
            img=img,
            path=path,
            locked=locked,
            output_dir=output_dir,
            scale=scale,
            op="flood_fill",
            pixels_touched=0,
            extra={
                "seed": {"x": sx, "y": sy},
                "color": list(rgba),
                "warning": None,
                "note": "Seed already matches fill color; nothing changed.",
            },
        )

    # BFS flood on RGBA exact match (including transparent).
    q: deque[tuple[int, int]] = deque([(sx, sy)])
    seen = {(sx, sy)}
    touched = 0
    while q:
        cx, cy = q.popleft()
        if pix[cx, cy] != target:
            continue
        pix[cx, cy] = rgba
        touched += 1
        for nx, ny in (
            (cx + 1, cy),
            (cx - 1, cy),
            (cx, cy + 1),
            (cx, cy - 1),
        ):
            if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in seen:
                seen.add((nx, ny))
                q.append((nx, ny))

    warning = None
    if touched >= int(w * h * _FLOOD_WARN_FRACTION):
        warning = _FLOOD_WARNING

    return _finish_paint(
        name,
        slot,
        img=img,
        path=path,
        locked=locked,
        output_dir=output_dir,
        scale=scale,
        op="flood_fill",
        pixels_touched=touched,
        extra={
            "seed": {"x": sx, "y": sy},
            "color": list(rgba),
            "warning": warning,
        },
    )


def get_layer_pixels(
    name: str,
    slot: str,
    *,
    x: int = 0,
    y: int = 0,
    width: int | None = None,
    height: int | None = None,
    opaque_only: bool = False,
    output_dir: Path | str | None = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Sample a region of the design layer for read-modify workflows.

    Returns ``pixels`` as ``[{x,y,rgba:[r,g,b,a]}, ...]``. Caps at
    ``_MAX_GET_PIXELS`` entries (set a smaller region or opaque_only=true).
    Does not modify the layer.
    """
    name, slot, locked, img, path, w, h = _open_design_for_paint(
        name, slot, output_dir=output_dir, plan_id=plan_id
    )
    x0 = _as_int(x, "x")
    y0 = _as_int(y, "y")
    rw = w - x0 if width is None else _as_int(width, "width")
    rh = h - y0 if height is None else _as_int(height, "height")
    if rw <= 0 or rh <= 0:
        raise ValueError("width and height must be positive")
    x1 = min(w, x0 + rw)
    y1 = min(h, y0 + rh)
    x0c = max(0, x0)
    y0c = max(0, y0)
    pix = img.load()
    assert pix is not None
    pixels: list[dict[str, Any]] = []
    truncated = False
    for py in range(y0c, y1):
        for px in range(x0c, x1):
            r, g, b, a = pix[px, py]
            if opaque_only and a == 0:
                continue
            pixels.append({"x": px, "y": py, "rgba": [r, g, b, a]})
            if len(pixels) >= _MAX_GET_PIXELS:
                truncated = True
                break
        if truncated:
            break

    return {
        "schema": PAINT_SCHEMA,
        "op": "get_layer_pixels",
        "name": name,
        "slot": slot,
        "plan_id": locked["plan_id"],
        "canvas": {"width": w, "height": h},
        "region": {
            "x": x0c,
            "y": y0c,
            "width": max(0, x1 - x0c),
            "height": max(0, y1 - y0c),
        },
        "opaque_only": opaque_only,
        "count": len(pixels),
        "truncated": truncated,
        "max_pixels": _MAX_GET_PIXELS,
        "pixels": pixels,
        "paths": {
            "design_layer": str(path),
            "design_dir": str(design_dir(name, output_dir)),
        },
        "next_step": (
            "Use paint_pixels / fill_rect / etc. to modify, then Read the "
            "ref preview from those paint tool responses."
        ),
    }


def _apply_command(
    img: Image.Image,
    cmd: Mapping[str, Any],
    *,
    index: int,
    part_map: Any | None = None,
) -> int:
    """Apply one paint command to ``img`` in-place; return approx pixels touched."""
    if not isinstance(cmd, Mapping):
        raise ValueError(f"commands[{index}] must be an object")
    op = cmd.get("op")
    if not isinstance(op, str):
        raise ValueError(f"commands[{index}].op must be a string")
    op = op.strip().lower()
    w, h = img.size
    pix = img.load()
    assert pix is not None

    if op in ("fill_parts_on_slot", "fill_parts", "fill_silhouette"):
        if part_map is None:
            raise ValueError(
                f"commands[{index}]: fill_parts requires character part_map "
                "(internal error — call paint_from_commands)"
            )
        parts = cmd.get("parts")
        part_ids = cmd.get("part_ids")
        selected = _normalize_parts_list(parts, field=f"commands[{index}].parts")
        if part_ids is not None:
            selected.extend(
                _normalize_parts_list(part_ids, field=f"commands[{index}].part_ids")
            )
        seen: set[str] = set()
        ordered: list[str] = []
        for p in selected:
            if p not in seen:
                seen.add(p)
                ordered.append(p)
        selected = ordered
        if not selected:
            parents = cmd.get("parent_parts")
            if isinstance(parents, Sequence) and not isinstance(parents, (str, bytes)):
                selected = _normalize_parts_list(
                    parents, field=f"commands[{index}].parent_parts"
                )
        if not selected:
            raise ValueError(
                f"commands[{index}] fill_parts needs parts/part_ids "
                "(or parent_parts when batching)"
            )
        default_rgba = None
        if "color" in cmd and cmd.get("color") is not None:
            default_rgba = _parse_color(
                cmd.get("color"), field=f"commands[{index}].color"
            )
        per_part: dict[str, tuple[int, int, int, int]] = {}
        raw_pc = cmd.get("part_colors")
        if raw_pc is not None:
            if not isinstance(raw_pc, Mapping):
                raise ValueError(f"commands[{index}].part_colors must be object")
            for key, val in raw_pc.items():
                pname = _resolve_part_name(
                    key, field=f"commands[{index}].part_colors key"
                )
                per_part[pname] = _parse_color(
                    val, field=f"commands[{index}].part_colors[{pname}]"
                )
                if pname not in selected:
                    selected.append(pname)
        if default_rgba is None and not per_part:
            raise ValueError(f"commands[{index}] needs color and/or part_colors")
        touched = 0
        for pname in selected:
            rgba = per_part.get(pname, default_rgba)
            if rgba is None:
                raise ValueError(
                    f"commands[{index}]: no color for part {pname!r}"
                )
            for x, y in part_map.part_pixels(pname):
                if 0 <= x < w and 0 <= y < h:
                    pix[x, y] = rgba
                    touched += 1
        return touched

    if op in ("paint_pixels", "set_pixels", "pixels"):
        pixels = cmd.get("pixels")
        if not isinstance(pixels, Sequence) or isinstance(pixels, (str, bytes)):
            raise ValueError(f"commands[{index}].pixels must be a list")
        touched = 0
        for j, item in enumerate(pixels):
            if not isinstance(item, Mapping):
                raise ValueError(f"commands[{index}].pixels[{j}] must be object")
            x = _as_int(item.get("x"), f"commands[{index}].pixels[{j}].x")
            y = _as_int(item.get("y"), f"commands[{index}].pixels[{j}].y")
            if "color" in item:
                color = _parse_color(
                    item["color"], field=f"commands[{index}].pixels[{j}].color"
                )
            elif "rgba" in item:
                color = _parse_color(
                    item["rgba"], field=f"commands[{index}].pixels[{j}].rgba"
                )
            elif "hex" in item:
                color = _parse_color(
                    item["hex"], field=f"commands[{index}].pixels[{j}].hex"
                )
            else:
                raise ValueError(
                    f"commands[{index}].pixels[{j}] needs color/rgba/hex"
                )
            if 0 <= x < w and 0 <= y < h:
                pix[x, y] = color
                touched += 1
        return touched

    if op == "fill_rect":
        rgba = _parse_color(cmd.get("color"), field=f"commands[{index}].color")
        x0 = _as_int(cmd.get("x"), f"commands[{index}].x")
        y0 = _as_int(cmd.get("y"), f"commands[{index}].y")
        rw = _as_int(cmd.get("width"), f"commands[{index}].width")
        rh = _as_int(cmd.get("height"), f"commands[{index}].height")
        if rw <= 0 or rh <= 0:
            raise ValueError(f"commands[{index}] width/height must be positive")
        draw = ImageDraw.Draw(img)
        x1 = min(w - 1, x0 + rw - 1)
        y1 = min(h - 1, y0 + rh - 1)
        if x0 >= w or y0 >= h or x1 < 0 or y1 < 0:
            return 0
        cx0, cy0 = max(0, x0), max(0, y0)
        draw.rectangle([cx0, cy0, x1, y1], fill=rgba)
        return (x1 - cx0 + 1) * (y1 - cy0 + 1)

    if op == "clear_rect":
        return _apply_command(
            img,
            {
                "op": "fill_rect",
                "x": cmd.get("x"),
                "y": cmd.get("y"),
                "width": cmd.get("width"),
                "height": cmd.get("height"),
                "color": "transparent",
            },
            index=index,
            part_map=part_map,
        )

    if op == "stroke_rect":
        rgba = _parse_color(cmd.get("color"), field=f"commands[{index}].color")
        x0 = _as_int(cmd.get("x"), f"commands[{index}].x")
        y0 = _as_int(cmd.get("y"), f"commands[{index}].y")
        rw = _as_int(cmd.get("width"), f"commands[{index}].width")
        rh = _as_int(cmd.get("height"), f"commands[{index}].height")
        t = _as_int(cmd.get("thickness", 1), f"commands[{index}].thickness")
        if rw <= 0 or rh <= 0 or t <= 0:
            raise ValueError(f"commands[{index}] invalid rect/thickness")
        draw = ImageDraw.Draw(img)
        draw.rectangle(
            [x0, y0, x0 + rw - 1, y0 + rh - 1], outline=rgba, width=t
        )
        return max(0, 2 * (rw + rh) * t)

    if op == "draw_line":
        rgba = _parse_color(cmd.get("color"), field=f"commands[{index}].color")
        ax = _as_int(cmd.get("x0"), f"commands[{index}].x0")
        ay = _as_int(cmd.get("y0"), f"commands[{index}].y0")
        bx = _as_int(cmd.get("x1"), f"commands[{index}].x1")
        by = _as_int(cmd.get("y1"), f"commands[{index}].y1")
        t = _as_int(cmd.get("thickness", 1), f"commands[{index}].thickness")
        if t <= 0:
            raise ValueError(f"commands[{index}].thickness must be positive")
        draw = ImageDraw.Draw(img)
        draw.line([(ax, ay), (bx, by)], fill=rgba, width=t)
        return int(math.hypot(bx - ax, by - ay)) + 1

    if op == "fill_ellipse":
        rgba = _parse_color(cmd.get("color"), field=f"commands[{index}].color")
        x0 = _as_int(cmd.get("x"), f"commands[{index}].x")
        y0 = _as_int(cmd.get("y"), f"commands[{index}].y")
        rw = _as_int(cmd.get("width"), f"commands[{index}].width")
        rh = _as_int(cmd.get("height"), f"commands[{index}].height")
        if rw <= 0 or rh <= 0:
            raise ValueError(f"commands[{index}] width/height must be positive")
        draw = ImageDraw.Draw(img)
        draw.ellipse([x0, y0, x0 + rw - 1, y0 + rh - 1], fill=rgba)
        return min(w * h, int(math.pi * (rw / 2.0) * (rh / 2.0)))

    if op == "flood_fill":
        # Apply inline (no finish) for batch; warning handled by caller aggregate.
        rgba = _parse_color(cmd.get("color"), field=f"commands[{index}].color")
        sx = _as_int(cmd.get("x"), f"commands[{index}].x")
        sy = _as_int(cmd.get("y"), f"commands[{index}].y")
        if not (0 <= sx < w and 0 <= sy < h):
            raise ValueError(f"commands[{index}] flood seed out of bounds")
        target = pix[sx, sy]
        if target == rgba:
            return 0
        q: deque[tuple[int, int]] = deque([(sx, sy)])
        seen_pts = {(sx, sy)}
        touched = 0
        while q:
            cx, cy = q.popleft()
            if pix[cx, cy] != target:
                continue
            pix[cx, cy] = rgba
            touched += 1
            for nx, ny in (
                (cx + 1, cy),
                (cx - 1, cy),
                (cx, cy + 1),
                (cx, cy - 1),
            ):
                if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in seen_pts:
                    seen_pts.add((nx, ny))
                    q.append((nx, ny))
        return touched

    raise ValueError(
        f"commands[{index}].op unknown: {op!r}. Valid: fill_parts_on_slot, "
        "paint_pixels, fill_rect, clear_rect, stroke_rect, draw_line, "
        "fill_ellipse, flood_fill"
    )


def paint_from_commands(
    name: str,
    slot: str,
    commands: Sequence[Mapping[str, Any]],
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Batch paint commands on one design layer; single save + ref refresh.

    Each command is ``{op, ...}`` matching the individual paint tools
    (``fill_parts_on_slot``, ``fill_rect``, ``paint_pixels``, ``draw_line``,
    ``clear_rect``, ``stroke_rect``, ``fill_ellipse``, ``flood_fill``). Prefer
    this for multi-step pixel-art construction (silhouette + details).
    """
    name, slot, locked, img, path, w, h = _open_design_for_paint(
        name, slot, output_dir=output_dir, plan_id=plan_id
    )
    if not isinstance(commands, Sequence) or isinstance(commands, (str, bytes)):
        raise ValueError("commands must be a list of paint command objects")
    if len(commands) == 0:
        raise ValueError("commands must be non-empty")
    if len(commands) > 500:
        raise ValueError("commands list too long (max 500)")

    part_map = _load_part_map(name, output_dir)
    slot_plan = locked["plan"]["slots"][slot]
    body_parts = list(
        slot_plan.get("body_parts") or OUTFIT_SLOT_SPECS[slot]["body_parts"]
    )

    total = 0
    flood_touched = 0
    ops_run: list[str] = []
    for i, cmd in enumerate(commands):
        op_name = (
            str(cmd.get("op", "")).strip().lower()
            if isinstance(cmd, Mapping)
            else ""
        )
        # Inject slot body_parts for fill_parts when parts omitted.
        cmd_use: Mapping[str, Any] = cmd
        if (
            isinstance(cmd, Mapping)
            and op_name in ("fill_parts_on_slot", "fill_parts", "fill_silhouette")
            and not cmd.get("parts")
            and not cmd.get("part_ids")
            and not cmd.get("parent_parts")
            and not cmd.get("body_parts")
        ):
            cmd_use = {**dict(cmd), "parent_parts": body_parts}
        n = _apply_command(img, cmd_use, index=i, part_map=part_map)
        total += n
        ops_run.append(op_name or "?")
        if op_name == "flood_fill":
            flood_touched += n

    warning = None
    if flood_touched >= int(w * h * _FLOOD_WARN_FRACTION):
        warning = _FLOOD_WARNING

    return _finish_paint(
        name,
        slot,
        img=img,
        path=path,
        locked=locked,
        output_dir=output_dir,
        scale=scale,
        op="paint_from_commands",
        pixels_touched=total,
        extra={
            "commands_run": len(commands),
            "ops": ops_run,
            "warning": warning,
        },
    )


def reset_design_layer_blank(
    name: str,
    slot: str,
    *,
    output_dir: Path | str | None = None,
) -> Image.Image:
    """Internal helper: ensure blank design canvas (used by tests/smoke)."""
    part_map = _load_part_map(name, output_dir)
    w, h = _canvas_size(part_map)
    path = slot_layer_path(name, slot, output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    img = _blank_rgba(w, h)
    img.save(path)
    return img
