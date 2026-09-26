"""Post-design shading plan — agent-authored shadows on outfit layers.

Recommended flow (soft gate — compose does not refuse without this)
-------------------------------------------------------------------
1. Flat outfit paint on ``design/layers/`` (local colors only).
2. ``plan_shading(name, brief, plan)`` — agent authors where shadows go;
   show ``user_facing_summary``; wait for user OK.
3. Apply shadows with existing MCP paint tools (``paint_pixels``,
   ``fill_rect``, ``draw_line``, …) using **one darker step** of the
   local palette color — no soft gradients.
4. ``compose_character`` (or re-compose) for dressed rest preview.

Shade **design layers only** — never rewrite naked ``base/`` geometry.
Do **not** bake shading inside ``fill_parts_on_slot``.

Light assumption (side view +X): top-front / +X-up → undersides, under
chin, armpits, folds, slightly darker far limbs, under feet.

Optional ``suggest_shade_regions`` proposes candidate bands from part
geometry / design alpha; the agent filters and paints.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image

from .character import (
    character_base_dir,
    character_dir,
    validate_character_name,
)
from .outfit import (
    OUTFIT_SLOT_NAMES,
    OUTFIT_SLOT_SPECS,
    design_dir,
    require_outfit_plan,
    slot_layer_path,
    _ensure_character_base,
    _load_part_map,
    _read_json,
    _write_json,
)
from .palette import (
    CHARACTER_PALETTE,
    SHADE_STEP,
    as_hex,
    get_color,
    get_shade_role,
    list_shade_palette,
)

PLAN_SCHEMA = "spritemcp.shading_plan.v1"
PLAN_FILENAME = "shading_plan.json"

DEFAULT_LIGHT_DIRECTION = "top-front / +X-up"
REQUIRED_RULES = ("one_step_darker", "no_gradients")

_MIN_BRIEF = 3
_MIN_SHADE_NOTES = 8
_MIN_LIGHT = 8
_MAX_SAMPLE_PIXELS = 24
_DEFAULT_BAND_HEIGHT = 2

_EYES_NOTE = (
    "Eyes live on the naked base head layer — shade design/head clothing "
    "only; do not clear or flood the head in a way that strips base eyes "
    "from compose underlays."
)


def shading_plan_path(
    name: str,
    output_dir: Path | str | None = None,
) -> Path:
    return design_dir(name, output_dir) / PLAN_FILENAME


def _as_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = " ".join(value.split()).strip()
    if not text:
        raise ValueError(f"{field} must be non-empty")
    return text


def _normalize_rules(raw: Any) -> list[str]:
    if isinstance(raw, str):
        items = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        items = [_as_nonempty_str(str(x), "rules item") for x in raw]
    else:
        raise ValueError(
            "plan.rules must be a list of strings (or a comma-separated string)"
        )
    if not items:
        raise ValueError("plan.rules must be non-empty")
    lowered = {r.lower().replace(" ", "_") for r in items}
    missing = [r for r in REQUIRED_RULES if r not in lowered]
    if missing:
        raise ValueError(
            "plan.rules must include style-lock rules: "
            + ", ".join(REQUIRED_RULES)
            + f" (missing: {', '.join(missing)})"
        )
    # Preserve agent order; ensure required appear.
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.lower().replace(" ", "_")
        if key in seen:
            continue
        seen.add(key)
        out.append(item if " " not in item else key)
    for req in REQUIRED_RULES:
        if req not in seen:
            out.append(req)
            seen.add(req)
    return out


def _normalize_stroke(raw: Any, index: int, layer: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"layers[{layer}].strokes[{index}] must be an object")
    intent = _as_nonempty_str(
        raw.get("intent") or raw.get("note") or raw.get("ops") or "",
        f"layers[{layer}].strokes[{index}].intent",
    )
    stroke: dict[str, Any] = {"intent": intent}
    region = raw.get("region")
    if region is not None:
        stroke["region"] = _as_nonempty_str(
            str(region), f"layers[{layer}].strokes[{index}].region"
        )
    tool = raw.get("suggested_tool") or raw.get("tool") or raw.get("ops")
    if isinstance(tool, str) and tool.strip() and tool.strip() != intent:
        stroke["suggested_tool"] = " ".join(tool.split()).strip()
    color_role = raw.get("color_role") or raw.get("suggested_color_role")
    if color_role is not None:
        role = _as_nonempty_str(
            str(color_role), f"layers[{layer}].strokes[{index}].color_role"
        )
        if role not in CHARACTER_PALETTE:
            known = ", ".join(CHARACTER_PALETTE)
            raise ValueError(
                f"layers[{layer}].strokes[{index}].color_role {role!r} "
                f"unknown. Known: {known}"
            )
        stroke["color_role"] = role
        stroke["color_hex"] = as_hex(get_color(role))
    return stroke


def validate_shading_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate agent-authored shading plan; return normalized dict."""
    if not isinstance(plan, Mapping):
        raise ValueError("plan must be an object")

    light = _as_nonempty_str(
        plan.get("light_direction") or plan.get("light") or DEFAULT_LIGHT_DIRECTION,
        "light_direction",
    )
    if len(light) < _MIN_LIGHT:
        raise ValueError("light_direction too short")

    rules = _normalize_rules(plan.get("rules", list(REQUIRED_RULES)))

    raw_layers = plan.get("layers")
    if raw_layers is None and plan.get("strokes") is not None:
        # Convenience: flat strokes list → per-layer buckets.
        raw_layers = _layers_from_flat_strokes(plan.get("strokes"))
    if not isinstance(raw_layers, Mapping):
        raise ValueError(
            "plan.layers must be an object keyed by EXPORT_LAYER_NAMES "
            "(or provide plan.strokes as a flat list with layer keys)"
        )

    unknown = [k for k in raw_layers if k not in OUTFIT_SLOT_SPECS]
    if unknown:
        raise ValueError(
            f"Unknown layers in shading plan: {unknown}. Valid: "
            f"{', '.join(OUTFIT_SLOT_NAMES)}"
        )

    missing = [s for s in OUTFIT_SLOT_NAMES if s not in raw_layers]
    if missing:
        raise ValueError(
            "plan.layers must include every export layer (use skip:true when "
            "unused): " + ", ".join(missing)
        )

    normalized_layers: dict[str, Any] = {}
    for layer in OUTFIT_SLOT_NAMES:
        item = raw_layers[layer]
        if not isinstance(item, Mapping):
            raise ValueError(f"plan.layers[{layer}] must be an object")
        skip = bool(item.get("skip", False))
        notes_raw = item.get("shade_notes") or item.get("visual_notes") or ""
        if skip:
            notes = (
                _as_nonempty_str(notes_raw, f"layers[{layer}].shade_notes")
                if isinstance(notes_raw, str) and notes_raw.strip()
                else "No shading on this layer."
            )
        else:
            notes = _as_nonempty_str(notes_raw, f"layers[{layer}].shade_notes")
            if len(notes) < _MIN_SHADE_NOTES:
                raise ValueError(
                    f"layers[{layer}].shade_notes too short "
                    f"(min {_MIN_SHADE_NOTES}) — say where underside / fold "
                    "shadows go"
                )
        strokes_raw = item.get("strokes") or item.get("intents") or []
        strokes: list[dict[str, Any]] = []
        if isinstance(strokes_raw, Sequence) and not isinstance(
            strokes_raw, (str, bytes)
        ):
            for i, stroke in enumerate(strokes_raw):
                strokes.append(_normalize_stroke(stroke, i, layer))
        elif strokes_raw:
            raise ValueError(f"layers[{layer}].strokes must be a list")

        if not skip and not strokes and len(notes) < 24:
            # Soft: notes alone are OK if reasonably descriptive.
            pass

        normalized_layers[layer] = {
            "skip": skip,
            "shade_notes": notes,
            "strokes": strokes,
            "body_parts": list(OUTFIT_SLOT_SPECS[layer]["body_parts"]),
        }

    overall = plan.get("overall_notes") or plan.get("overall_look") or ""
    overall_text = (
        _as_nonempty_str(overall, "overall_notes")
        if isinstance(overall, str) and overall.strip()
        else (
            "One-step-darker hard shadows on design layers only; light from "
            f"{light}."
        )
    )

    return {
        "light_direction": light,
        "rules": rules,
        "overall_notes": overall_text,
        "layers": normalized_layers,
    }


def _layers_from_flat_strokes(raw: Any) -> dict[str, Any]:
    """Build a layers map from a flat strokes list with ``layer`` keys."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("plan.strokes must be a list of stroke objects")
    buckets: dict[str, list[Any]] = {s: [] for s in OUTFIT_SLOT_NAMES}
    for i, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"strokes[{i}] must be an object with layer + intent")
        layer = item.get("layer") or item.get("slot")
        if layer is None:
            raise ValueError(f"strokes[{i}] must include layer")
        layer = str(layer)
        if layer not in OUTFIT_SLOT_SPECS:
            raise ValueError(
                f"strokes[{i}].layer {layer!r} unknown. Valid: "
                f"{', '.join(OUTFIT_SLOT_NAMES)}"
            )
        buckets[layer].append(item)

    layers: dict[str, Any] = {}
    for layer in OUTFIT_SLOT_NAMES:
        strokes = buckets[layer]
        if not strokes:
            layers[layer] = {
                "skip": True,
                "shade_notes": "No shading strokes listed.",
                "strokes": [],
            }
        else:
            notes_bits = []
            for s in strokes:
                bit = s.get("note") or s.get("intent") or s.get("region") or ""
                if bit:
                    notes_bits.append(str(bit))
            layers[layer] = {
                "skip": False,
                "shade_notes": "; ".join(notes_bits)
                if notes_bits
                else f"Shade accents on {layer}.",
                "strokes": strokes,
            }
    return layers


def build_shading_user_facing_summary(
    *,
    name: str,
    brief: str,
    plan: Mapping[str, Any],
) -> str:
    lines: list[str] = [
        f"Shading plan for `{name}`",
        f"Brief: {brief.strip()}",
        "",
        f"Light: {plan['light_direction']}",
        f"Rules: {', '.join(plan['rules'])}",
        "",
        "Overall:",
        plan["overall_notes"],
        "",
        "Per design layer (paint shadows on design/layers/ only):",
    ]
    for layer in OUTFIT_SLOT_NAMES:
        entry = plan["layers"][layer]
        if entry["skip"]:
            lines.append(f"  - {layer}: SKIP — {entry['shade_notes']}")
        else:
            stroke_n = len(entry.get("strokes") or [])
            extra = f" ({stroke_n} stroke intent(s))" if stroke_n else ""
            lines.append(f"  - {layer}{extra}: {entry['shade_notes']}")
            for stroke in entry.get("strokes") or []:
                region = stroke.get("region")
                prefix = f"[{region}] " if region else ""
                color = ""
                if stroke.get("color_role"):
                    color = f" → {stroke['color_role']} ({stroke.get('color_hex')})"
                lines.append(f"      · {prefix}{stroke['intent']}{color}")
    lines.append("")
    lines.append("Shade palette (one darker step from local flat color):")
    for row in list_shade_palette():
        lines.append(
            f"  - {row['local_role']} ({row['local_hex']}) → "
            f"{row['shade_role']} ({row['shade_hex']})"
        )
    lines.append("")
    lines.append(
        "Apply with MCP paint tools only (fill_rect / draw_line / paint_pixels "
        "/ paint_from_commands) on design layers — hard 1px–2px bands, no "
        "gradients, no Shell/PIL. Do not rewrite base geometry. "
        f"{_EYES_NOTE} "
        "Then compose_character (or re-compose). If this matches, say OK; "
        "otherwise revise the plan."
    )
    return "\n".join(lines)


def _new_plan_id(name: str, normalized: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"name": name, "plan": normalized},
        sort_keys=True,
        ensure_ascii=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"{name}-shade-{digest}-{uuid.uuid4().hex[:8]}"


def require_shading_plan(
    name: str,
    *,
    plan_id: str | None = None,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    name = validate_character_name(name)
    path = shading_plan_path(name, output_dir)
    if not path.is_file():
        raise FileNotFoundError(
            f"No shading plan for '{name}' (missing {path}). "
            "Call plan_shading first, show user_facing_summary, then paint "
            "shadows (optional — compose_character does not require this)."
        )
    data = _read_json(path)
    if not isinstance(data, dict) or "plan" not in data or "plan_id" not in data:
        raise FileNotFoundError(
            f"Shading plan at {path} is incomplete. Re-submit via plan_shading."
        )
    if plan_id is not None and data.get("plan_id") != plan_id:
        raise ValueError(
            f"plan_id mismatch for shading '{name}': "
            f"got {plan_id!r}, stored {data.get('plan_id')!r}."
        )
    return data


def plan_shading(
    name: str,
    brief: str,
    plan: Mapping[str, Any],
    *,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Validate + lock an agent-authored shading plan under ``design/shading_plan.json``.

    The calling agent authors ``plan`` from ``brief`` after flat outfit paint.
    This tool does not paint pixels — apply shadows with MCP paint tools, then
    ``compose_character``.
    """
    name = validate_character_name(name)
    brief_text = _as_nonempty_str(brief, "brief")
    if len(brief_text) < _MIN_BRIEF:
        raise ValueError(
            "brief must describe the shading intent (e.g. 'underside bands "
            "on do and hakama')"
        )

    _ensure_character_base(name, output_dir)
    # Soft preference: outfit plan exists when shading a dressed character.
    outfit_plan_id = None
    try:
        outfit = require_outfit_plan(name, output_dir=output_dir)
        outfit_plan_id = outfit.get("plan_id")
    except FileNotFoundError:
        outfit_plan_id = None

    normalized = validate_shading_plan(plan)
    plan_id = _new_plan_id(name, normalized)
    summary = build_shading_user_facing_summary(
        name=name, brief=brief_text, plan=normalized
    )

    ddir = design_dir(name, output_dir)
    ddir.mkdir(parents=True, exist_ok=True)

    shade_palette = list_shade_palette()
    stored = {
        "schema": PLAN_SCHEMA,
        "plan_id": plan_id,
        "name": name,
        "brief": brief_text,
        "outfit_plan_id": outfit_plan_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plan": normalized,
        "user_facing_summary": summary,
        "shade_palette": shade_palette,
        "style_notes": {
            "design_layers_only": True,
            "no_base_rewrite": True,
            "no_fill_parts_bake": True,
            "eyes": _EYES_NOTE,
            "canvas": "90x128",
        },
    }
    path = shading_plan_path(name, output_dir)
    _write_json(path, stored)

    return {
        "schema": PLAN_SCHEMA,
        "plan_id": plan_id,
        "name": name,
        "brief": brief_text,
        "outfit_plan_id": outfit_plan_id,
        "plan": normalized,
        "user_facing_summary": summary,
        "shade_palette": shade_palette,
        "paths": {
            "shading_plan_json": str(path),
            "design_dir": str(ddir),
        },
        "next_step": (
            "Show user_facing_summary to the user. After OK, paint one-step-"
            "darker shadows on design/layers/<layer>.png with MCP paint tools "
            "(fill_rect / draw_line / paint_pixels). Optional: "
            "suggest_shade_regions for geometry hints. Then compose_character "
            "(or re-compose). FORBIDDEN: Shell/PIL, gradients, rewriting base/."
        ),
    }


def get_shading_plan(
    name: str,
    *,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Return the locked ``design/shading_plan.json`` for a character."""
    data = require_shading_plan(name, output_dir=output_dir)
    return {
        "schema": data.get("schema", PLAN_SCHEMA),
        "plan_id": data["plan_id"],
        "name": data.get("name", name),
        "brief": data.get("brief", ""),
        "outfit_plan_id": data.get("outfit_plan_id"),
        "plan": data["plan"],
        "user_facing_summary": data.get("user_facing_summary", ""),
        "shade_palette": data.get("shade_palette") or list_shade_palette(),
        "paths": {
            "shading_plan_json": str(shading_plan_path(name, output_dir)),
            "design_dir": str(design_dir(name, output_dir)),
        },
        "next_step": (
            "Show user_facing_summary if needed, then paint shadows on design "
            "layers with MCP tools, then compose_character."
        ),
    }


def get_shade_palette() -> dict[str, Any]:
    """Document local → one-step-darker shadow roles for agents."""
    rows = list_shade_palette()
    return {
        "schema": "spritemcp.shade_palette.v1",
        "light_assumption": DEFAULT_LIGHT_DIRECTION,
        "rules": list(REQUIRED_RULES),
        "shade_step": dict(SHADE_STEP),
        "palette": rows,
        "usage": (
            "After flat local fill, paint shadow bands with the shade_role "
            "color (get_color / hex). Example: off_white cloth → shadow_beige "
            "underside. Never soft-gradient; never rewrite base geometry."
        ),
        "eyes_note": _EYES_NOTE,
    }


def _opaque_mask_from_image(img: Image.Image) -> list[list[bool]]:
    rgba = img.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()
    return [[px[x, y][3] > 0 for x in range(w)] for y in range(h)]


def _opaque_mask_from_parts(
    part_map: Any,
    body_parts: Sequence[str],
) -> list[list[bool]]:
    w, h = part_map.width, part_map.height
    mask = [[False] * w for _ in range(h)]
    for part in body_parts:
        for x, y in part_map.part_pixels(part):
            if 0 <= x < w and 0 <= y < h:
                mask[y][x] = True
    return mask


def _bbox_of_mask(mask: list[list[bool]]) -> dict[str, int] | None:
    h = len(mask)
    w = len(mask[0]) if h else 0
    min_x, min_y = w, h
    max_x, max_y = -1, -1
    for y in range(h):
        for x in range(w):
            if mask[y][x]:
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)
    if max_x < 0:
        return None
    return {
        "x": min_x,
        "y": min_y,
        "w": max_x - min_x + 1,
        "h": max_y - min_y + 1,
    }


def _bottom_edge_pixels(
    mask: list[list[bool]],
    *,
    band_height: int,
) -> list[tuple[int, int]]:
    """Opaque pixels that sit on the underside (no opaque neighbor below)."""
    h = len(mask)
    w = len(mask[0]) if h else 0
    edge: list[tuple[int, int]] = []
    for y in range(h):
        for x in range(w):
            if not mask[y][x]:
                continue
            below_clear = True
            for dy in range(1, band_height + 1):
                ny = y + dy
                if ny < h and mask[ny][x]:
                    below_clear = False
                    break
            if below_clear:
                edge.append((x, y))
    return edge


def _cluster_bbox(pixels: Sequence[tuple[int, int]]) -> dict[str, int] | None:
    if not pixels:
        return None
    xs = [p[0] for p in pixels]
    ys = [p[1] for p in pixels]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    return {
        "x": min_x,
        "y": min_y,
        "w": max_x - min_x + 1,
        "h": max_y - min_y + 1,
    }


def _sample_pixels(
    pixels: Sequence[tuple[int, int]],
    limit: int = _MAX_SAMPLE_PIXELS,
) -> list[list[int]]:
    if not pixels:
        return []
    if len(pixels) <= limit:
        return [[x, y] for x, y in pixels]
    step = max(1, len(pixels) // limit)
    sampled = [pixels[i] for i in range(0, len(pixels), step)][:limit]
    return [[x, y] for x, y in sampled]


def _reason_for_layer(layer: str, body_parts: Sequence[str]) -> list[str]:
    reasons: list[str] = ["underside_bottom_edge"]
    if layer.endswith("_far") or any(p.endswith("_far") for p in body_parts):
        reasons.append("far_limb_slightly_darker")
    if layer == "head" or "head" in body_parts:
        reasons.append("under_chin")
    if any("foot" in p for p in body_parts):
        reasons.append("under_feet")
    if "upper_arm" in layer:
        reasons.append("armpit_fold")
    if "torso" in layer:
        reasons.append("under_chest_waist_fold")
    if "knee" in layer or "leg" in layer:
        reasons.append("joint_fold")
    return reasons


def suggest_shade_regions(
    name: str,
    *,
    output_dir: Path | str | None = None,
    band_height: int = _DEFAULT_BAND_HEIGHT,
) -> dict[str, Any]:
    """Propose candidate shade bands from design alpha / part geometry.

    Does **not** write pixels. Agent should filter hints and paint via MCP
    tools after ``plan_shading`` + user OK.
    """
    name = validate_character_name(name)
    _ensure_character_base(name, output_dir)
    if band_height < 1 or band_height > 4:
        raise ValueError("band_height must be 1..4")

    part_map = _load_part_map(name, output_dir)
    regions: list[dict[str, Any]] = []

    for layer in OUTFIT_SLOT_NAMES:
        body_parts = list(OUTFIT_SLOT_SPECS[layer]["body_parts"])
        layer_path = slot_layer_path(name, layer, output_dir)
        source = "body_parts"
        mask: list[list[bool]]
        if layer_path.is_file():
            img = Image.open(layer_path).convert("RGBA")
            if img.getchannel("A").getextrema()[1] > 0:
                mask = _opaque_mask_from_image(img)
                source = "design_alpha"
            else:
                mask = _opaque_mask_from_parts(part_map, body_parts)
        else:
            mask = _opaque_mask_from_parts(part_map, body_parts)

        silhouette_bbox = _bbox_of_mask(mask)
        if silhouette_bbox is None:
            continue

        edge = _bottom_edge_pixels(mask, band_height=band_height)
        if not edge:
            continue

        edge_bbox = _cluster_bbox(edge)
        reasons = _reason_for_layer(layer, body_parts)
        regions.append(
            {
                "layer": layer,
                "body_parts": body_parts,
                "source": source,
                "reason": reasons[0],
                "reasons": reasons,
                "bbox": edge_bbox,
                "silhouette_bbox": silhouette_bbox,
                "band_height": band_height,
                "sample_pixels": _sample_pixels(edge),
                "pixel_count": len(edge),
                "hint": (
                    f"Paint a hard {band_height}px underside band on "
                    f"design/layers/{layer}.png with one-step-darker palette "
                    f"color ({', '.join(reasons)})."
                ),
            }
        )

        # Extra under-foot hint when foot parts exist.
        foot_parts = [p for p in body_parts if p.startswith("foot_")]
        if foot_parts:
            foot_mask = _opaque_mask_from_parts(part_map, foot_parts)
            foot_edge = _bottom_edge_pixels(foot_mask, band_height=1)
            foot_bbox = _cluster_bbox(foot_edge)
            if foot_bbox:
                regions.append(
                    {
                        "layer": layer,
                        "body_parts": foot_parts,
                        "source": "body_parts",
                        "reason": "under_feet",
                        "reasons": ["under_feet"],
                        "bbox": foot_bbox,
                        "silhouette_bbox": _bbox_of_mask(foot_mask),
                        "band_height": 1,
                        "sample_pixels": _sample_pixels(foot_edge),
                        "pixel_count": len(foot_edge),
                        "hint": (
                            f"Darken sole / under {', '.join(foot_parts)} on "
                            f"design/layers/{layer}.png (1px hard shadow)."
                        ),
                    }
                )

    return {
        "schema": "spritemcp.suggest_shade_regions.v1",
        "name": name,
        "light_assumption": DEFAULT_LIGHT_DIRECTION,
        "rules": list(REQUIRED_RULES),
        "band_height": band_height,
        "region_count": len(regions),
        "regions": regions,
        "shade_palette": list_shade_palette(),
        "character_dir": str(character_dir(name, output_dir)),
        "base_dir": str(character_base_dir(name, output_dir)),
        "eyes_note": _EYES_NOTE,
        "next_step": (
            "Filter regions that match the outfit, call plan_shading with "
            "layer shade_notes / strokes, show user_facing_summary, then "
            "paint with MCP tools on design layers only, then compose_character."
        ),
    }


def has_shading_plan(
    name: str,
    *,
    output_dir: Path | str | None = None,
) -> bool:
    try:
        name = validate_character_name(name)
    except ValueError:
        return False
    return shading_plan_path(name, output_dir).is_file()
