"""Character outfit / design layers — paint clothes on export body layers.

Product model
-------------
- **Base** = shared armature / debug only (``base/layers/``).
- **Design** = visual content on the **same 10 export layer names** as
  ``EXPORT_LAYER_NAMES`` (one PNG per layer under ``design/layers/``).
- **Paint**: body underlay for that ONE export layer → paint
  ``design/layers/<layer>.png``.
- **Shading** (recommended): ``plan_shading`` → paint one-step-darker
  shadows on design layers only (see ``shading`` module).
- **Animate**: each design layer rigid-rotates 1:1 with that body part
  (single parent — no far+near mask split).

``lower_*`` export layers include hand/foot (same as base export). Outfit
**plan notes** may still mention jingasa / do / hakama conceptually, but
paint targets are always the 10 layer files.

Overhang rule (important)
-------------------------
Clothing **may** extend outside the body silhouette margins. Parent body
parts are the **alignment / proportion reference** under the design layer —
**not** a hard clip mask. ``prepare_outfit_slot_reference`` never clips
paint to body alpha; ``compose_character`` composites the full design layer.

Disk layout
-----------
::

    output/characters/<name>/design/
      plan.json                 # locked outfit plan (10 layers + visual notes)
      shading_plan.json         # optional locked shading plan
      layers_meta.json          # layer → parent_part / body_parts / draw_after
      layers/<layer>.png        # design-only RGBA (matches EXPORT_LAYER_NAMES)
      refs/<layer>_ref.png      # body underlay UNDER + design on TOP
      compose_preview.png       # dressed rest (base + design)

Pipeline (before animation)
---------------------------
1. ``generate_character``
2. ``plan_outfit`` — agent authors the 10 layer notes from a free brief;
   show ``user_facing_summary`` to the user
3. Per layer: ``prepare_outfit_slot_reference`` then paint via MCP tools
4. ``plan_shading`` (recommended) — show summary → paint shadows on design
5. ``compose_character`` — dressed rest preview
6. Then ``plan_animation`` / frame build as usual

No hardcoded outfit style tables — visual content comes from the agent brief
and painted layer PNGs only.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageChops

from .base_idle import (
    DRAW_ORDER,
    EXPORT_LAYER_NAMES,
    EXPORT_LAYER_PARTS,
    PART_NAMES,
    PART_VIZ_COLORS,
    PartMap,
)
from .character import (
    character_base_dir,
    character_dir,
    validate_character_name,
)
from .show_ref_grid import render_ref_grid

PLAN_SCHEMA = "spritemcp.outfit_plan.v2"
META_SCHEMA = "spritemcp.outfit_layers_meta.v2"
COMPOSE_SCHEMA = "spritemcp.compose_character.v1"
PLAN_FILENAME = "plan.json"
META_FILENAME = "layers_meta.json"

# Conceptual brief vocabulary → export layer (notes only; paint uses layer keys).
CONCEPTUAL_LAYER_HINTS: dict[str, str] = {
    "headwear": "head",
    "jingasa": "head",
    "helmet": "head",
    "do": "torso",
    "shirt": "torso",
    "chest_armor": "torso",
    "sleeves": "upper_arm_* / lower_arm_*",
    "hakama": "upper_leg_* / lower_leg_*",
    "pants": "upper_leg_* / lower_leg_*",
    "boots": "lower_leg_* (foot included in export layer)",
    "gloves": "lower_arm_* (hand included in export layer)",
}

_LAYER_DESCRIPTIONS: dict[str, str] = {
    "upper_arm_far": (
        "Far upper-arm design (sleeves / arm armor). Single parent: "
        "upper_arm_far. Skip if bare upper arm."
    ),
    "lower_arm_far": (
        "Far lower-arm + hand design (sleeve cuff / gloves). Export layer "
        "includes hand_far. Single parent: lower_arm_far."
    ),
    "upper_leg_far": (
        "Far upper-leg design (hakama / pants / thigh armor). "
        "Single parent: upper_leg_far."
    ),
    "lower_leg_far": (
        "Far lower-leg + foot design (shin armor / boots). Export layer "
        "includes foot_far. Single parent: lower_leg_far."
    ),
    "torso": (
        "Torso design (do / shirt / chest armor). Single parent: torso. "
        "May be bulkier than torso silhouette."
    ),
    "head": (
        "Head design (jingasa / helmet / hood). Single parent: head. "
        "May overhang head silhouette; keep scale sensible."
    ),
    "upper_arm_near": (
        "Near upper-arm design (sleeves / arm armor). Single parent: "
        "upper_arm_near. Skip if bare upper arm."
    ),
    "lower_arm_near": (
        "Near lower-arm + hand design (sleeve cuff / gloves). Export layer "
        "includes hand_near. Single parent: lower_arm_near."
    ),
    "upper_leg_near": (
        "Near upper-leg design (hakama / pants / thigh armor). "
        "Single parent: upper_leg_near."
    ),
    "lower_leg_near": (
        "Near lower-leg + foot design (shin armor / boots). Export layer "
        "includes foot_near. Single parent: lower_leg_near."
    ),
}


def _draw_after_for_layer(layer_name: str) -> str:
    """Insert design after the last DRAW_ORDER part in this export layer."""
    parts = EXPORT_LAYER_PARTS[layer_name]
    order_index = {name: i for i, name in enumerate(DRAW_ORDER)}
    return max(parts, key=lambda p: order_index.get(p, -1))


def _build_outfit_slot_specs() -> dict[str, dict[str, Any]]:
    """One design slot per EXPORT_LAYER_NAMES — matches ``base/layers/``."""
    specs: dict[str, dict[str, Any]] = {}
    for layer_name in EXPORT_LAYER_NAMES:
        body_parts = EXPORT_LAYER_PARTS[layer_name]
        specs[layer_name] = {
            # Pose / animate: single parent = export layer name (1:1 rigid rotate).
            "parent_parts": (layer_name,),
            # Paint / ref underlay: all body parts grouped into this export layer.
            "body_parts": body_parts,
            "draw_after": _draw_after_for_layer(layer_name),
            "allow_overhang": True,
            "optional": True,
            "description": _LAYER_DESCRIPTIONS[layer_name],
        }
    return specs


# Structural slots = export body layers (no clothing-slot merges, no style presets).
OUTFIT_SLOT_SPECS: dict[str, dict[str, Any]] = _build_outfit_slot_specs()

_OVERHANG_GUIDANCE = (
    "Clothing MAY extend outside body silhouette margins (helmet larger than "
    "head, hat brim sticking out, bulky armor). Parent body parts are alignment "
    "/ proportion reference only — NOT a hard clip mask. Keep scale sensible."
)

OUTFIT_SLOT_NAMES: tuple[str, ...] = tuple(OUTFIT_SLOT_SPECS.keys())

# Legacy clothing-slot PNGs (pre-v2) → export layer targets for migration.
_LEGACY_SLOT_SPLIT: dict[str, tuple[str, ...]] = {
    "headwear": ("head",),
    "body_shirt_armor": ("torso",),
    "arms_sleeves_armor": (
        "upper_arm_far",
        "lower_arm_far",
        "upper_arm_near",
        "lower_arm_near",
    ),
    "legs_pants_armor": (
        "upper_leg_far",
        "lower_leg_far",
        "upper_leg_near",
        "lower_leg_near",
    ),
    "feet_boots": ("lower_leg_far", "lower_leg_near"),
    "gloves": ("lower_arm_far", "lower_arm_near"),
}

# Which legacy body parts claim pixels into each legacy→export merge.
_LEGACY_CLAIM_PARTS: dict[str, dict[str, tuple[str, ...]]] = {
    "headwear": {"head": ("head",)},
    "body_shirt_armor": {"torso": ("torso",)},
    "arms_sleeves_armor": {
        "upper_arm_far": ("upper_arm_far",),
        "lower_arm_far": ("lower_arm_far",),
        "upper_arm_near": ("upper_arm_near",),
        "lower_arm_near": ("lower_arm_near",),
    },
    "legs_pants_armor": {
        "upper_leg_far": ("upper_leg_far",),
        "lower_leg_far": ("lower_leg_far",),
        "upper_leg_near": ("upper_leg_near",),
        "lower_leg_near": ("lower_leg_near",),
    },
    "feet_boots": {
        "lower_leg_far": ("foot_far",),
        "lower_leg_near": ("foot_near",),
    },
    "gloves": {
        "lower_arm_far": ("hand_far",),
        "lower_arm_near": ("hand_near",),
    },
}

_MIN_BRIEF = 3
_MIN_VISUAL_NOTES = 8
_MIN_OVERALL = 16

_TRANSPARENT = (0, 0, 0, 0)
# Body reference tint: slightly dimmed viz colors so paint on top reads clearly.
_REF_DIM = 0.72


def design_dir(name: str, output_dir: Path | str | None = None) -> Path:
    return character_dir(name, output_dir) / "design"


def design_layers_dir(name: str, output_dir: Path | str | None = None) -> Path:
    return design_dir(name, output_dir) / "layers"


def design_refs_dir(name: str, output_dir: Path | str | None = None) -> Path:
    return design_dir(name, output_dir) / "refs"


def plan_path(name: str, output_dir: Path | str | None = None) -> Path:
    return design_dir(name, output_dir) / PLAN_FILENAME


def meta_path(name: str, output_dir: Path | str | None = None) -> Path:
    return design_dir(name, output_dir) / META_FILENAME


def slot_layer_path(
    name: str,
    slot: str,
    output_dir: Path | str | None = None,
) -> Path:
    return design_layers_dir(name, output_dir) / f"{_validate_slot(slot)}.png"


def slot_ref_path(
    name: str,
    slot: str,
    output_dir: Path | str | None = None,
) -> Path:
    return design_refs_dir(name, output_dir) / f"{_validate_slot(slot)}_ref.png"


def list_outfit_slot_specs() -> list[dict[str, Any]]:
    """Return the 10 export-layer design specs (same names as ``base/layers/``).

    Every layer has ``allow_overhang=True``. ``parent_parts`` is always a
    single export-layer name (pose 1:1). ``body_parts`` lists the part-map
    silhouettes used for paint fill / reference underlay (``lower_*`` includes
    hand/foot).
    """
    out: list[dict[str, Any]] = []
    for slot, spec in OUTFIT_SLOT_SPECS.items():
        out.append(
            {
                "slot": slot,
                "parent_parts": list(spec["parent_parts"]),
                "body_parts": list(spec["body_parts"]),
                "draw_after": spec["draw_after"],
                "allow_overhang": bool(spec["allow_overhang"]),
                "optional": bool(spec["optional"]),
                "description": spec["description"],
                "overhang_note": _OVERHANG_GUIDANCE,
            }
        )
    return out


def _validate_slot(slot: str) -> str:
    if not isinstance(slot, str) or slot not in OUTFIT_SLOT_SPECS:
        raise ValueError(
            f"Unknown outfit slot {slot!r}. Valid: {', '.join(OUTFIT_SLOT_NAMES)}"
        )
    return slot


def _as_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = " ".join(value.split()).strip()
    if not text:
        raise ValueError(f"{field} must be non-empty")
    return text


def _write_json(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _ensure_character_base(name: str, output_dir: Path | str | None = None) -> Path:
    base = character_base_dir(name, output_dir)
    if not (base / "part_map.json").is_file():
        raise FileNotFoundError(
            f"Character '{name}' base assets missing under {base}. "
            "Call generate_character first."
        )
    return base


def _load_part_map(name: str, output_dir: Path | str | None = None) -> PartMap:
    from .character import _load_rest

    part_map, _pivots = _load_rest(name, output_dir)
    return part_map


def _canvas_size(part_map: PartMap) -> tuple[int, int]:
    return part_map.width, part_map.height


def _blank_rgba(width: int, height: int) -> Image.Image:
    return Image.new("RGBA", (width, height), _TRANSPARENT)


def _dim_color(rgba: tuple[int, int, int, int], factor: float = _REF_DIM) -> tuple[int, int, int, int]:
    r, g, b, a = rgba
    return (int(r * factor), int(g * factor), int(b * factor), a)


def _body_part_layer(part_map: PartMap, part_name: str) -> Image.Image:
    """Full-canvas RGBA for one body part using viz colors (reference underlay).

    Head always includes mandatory white eye detail from the authored base
    (or ``part_map.eyes_detail``). Design layers may cover eyes; base does not
    erase them.
    """
    w, h = _canvas_size(part_map)
    img = _blank_rgba(w, h)
    if part_name not in PART_NAMES or part_name == "empty":
        return img
    pid = PART_NAMES[part_name]
    color = _dim_color(PART_VIZ_COLORS[pid])
    pix = img.load()
    assert pix is not None
    for x, y in part_map.part_pixels(part_name):
        if 0 <= x < w and 0 <= y < h:
            pix[x, y] = color
    if part_name == "head":
        from .base_idle import _stamp_eyes_detail, load_authored_eyes_coords

        eyes = part_map.eyes_detail or load_authored_eyes_coords()
        img = _stamp_eyes_detail(img, list(eyes))
    return img


def _body_reference_stack(
    part_map: PartMap,
    parent_parts: Sequence[str],
) -> Image.Image:
    """Composite parent body parts in DRAW_ORDER as the paint-under reference."""
    w, h = _canvas_size(part_map)
    canvas = _blank_rgba(w, h)
    wanted = set(parent_parts)
    for part_name in DRAW_ORDER:
        if part_name not in wanted:
            continue
        canvas = Image.alpha_composite(canvas, _body_part_layer(part_map, part_name))
    return canvas


def _ensure_design_layer(
    path: Path,
    width: int,
    height: int,
) -> Image.Image:
    """Load design PNG or create a transparent canvas of the character size."""
    if path.is_file():
        img = Image.open(path).convert("RGBA")
        if img.size != (width, height):
            raise ValueError(
                f"Design layer {path} size {img.size} != character canvas "
                f"{(width, height)}. Keep the same {width}x{height} canvas."
            )
        return img
    path.parent.mkdir(parents=True, exist_ok=True)
    img = _blank_rgba(width, height)
    img.save(path)
    return img


def _default_layers_meta() -> dict[str, Any]:
    slots: dict[str, Any] = {}
    for slot, spec in OUTFIT_SLOT_SPECS.items():
        slots[slot] = {
            "parent_parts": list(spec["parent_parts"]),
            "body_parts": list(spec["body_parts"]),
            "draw_after": spec["draw_after"],
            "allow_overhang": bool(spec["allow_overhang"]),
            "optional": bool(spec["optional"]),
            "status": "empty",
            "layer_file": f"layers/{slot}.png",
        }
    return {
        "schema": META_SCHEMA,
        "export_layers": list(EXPORT_LAYER_NAMES),
        "draw_order_body": list(DRAW_ORDER),
        "compose_note": (
            "At rest compose: for each body part in draw_order_body, paint the "
            "body layer, then after draw_after insert the matching design export "
            "layer (overhang allowed — body is not a clip mask). Each design "
            "PNG rigid-rotates 1:1 with its single parent_parts entry (same name "
            "as base/layers/). "
            + _OVERHANG_GUIDANCE
        ),
        "slots": slots,
    }


def _sync_layers_meta(
    name: str,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    path = meta_path(name, output_dir)
    if path.is_file():
        meta = _read_json(path)
    else:
        meta = _default_layers_meta()
    # Drop legacy clothing-slot keys; keep only EXPORT_LAYER_NAMES.
    slots_in = meta.get("slots") if isinstance(meta.get("slots"), dict) else {}
    slots: dict[str, Any] = {}
    for slot, spec in OUTFIT_SLOT_SPECS.items():
        entry = dict(slots_in.get(slot) or {})
        entry["parent_parts"] = list(spec["parent_parts"])
        entry["body_parts"] = list(spec["body_parts"])
        entry["draw_after"] = spec["draw_after"]
        entry["allow_overhang"] = bool(spec["allow_overhang"])
        entry["optional"] = bool(spec["optional"])
        entry["layer_file"] = f"layers/{slot}.png"
        layer = slot_layer_path(name, slot, output_dir)
        if layer.is_file():
            img = Image.open(layer).convert("RGBA")
            has_paint = img.getchannel("A").getextrema()[1] > 0
            entry["status"] = "painted" if has_paint else "blank"
        else:
            entry["status"] = "empty"
        slots[slot] = entry
    meta["slots"] = slots
    meta["schema"] = META_SCHEMA
    meta["export_layers"] = list(EXPORT_LAYER_NAMES)
    meta["draw_order_body"] = list(DRAW_ORDER)
    meta["compose_note"] = _default_layers_meta()["compose_note"]
    _write_json(path, meta)
    return meta


def validate_outfit_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate agent-authored outfit plan; return normalized dict.

    Plan keys are the 10 ``EXPORT_LAYER_NAMES``. ``visual_notes`` /
    ``overall_look`` may describe conceptual clothing (jingasa, do, hakama)
    and overhang; paint targets remain the export layer PNGs. Body underlay
    is alignment only — paint is never clip-masked to body alpha.
    """
    if not isinstance(plan, Mapping):
        raise ValueError(
            "plan must be an object with overall_look + slots "
            f"({', '.join(OUTFIT_SLOT_NAMES)})"
        )

    overall = _as_nonempty_str(plan.get("overall_look", ""), "overall_look")
    if len(overall) < _MIN_OVERALL:
        raise ValueError(
            f"overall_look too short (min {_MIN_OVERALL} chars) — "
            "describe the dressed silhouette for the user"
        )

    raw_slots = plan.get("slots")
    if not isinstance(raw_slots, Mapping):
        raise ValueError(
            "plan.slots must be an object keyed by export layer name: "
            + ", ".join(OUTFIT_SLOT_NAMES)
        )

    unknown = [k for k in raw_slots if k not in OUTFIT_SLOT_SPECS]
    if unknown:
        raise ValueError(
            f"Unknown slots in plan: {unknown}. Valid export layers: "
            f"{', '.join(OUTFIT_SLOT_NAMES)}. "
            f"Conceptual hints (notes only): {CONCEPTUAL_LAYER_HINTS}"
        )

    missing = [s for s in OUTFIT_SLOT_NAMES if s not in raw_slots]
    if missing:
        raise ValueError(
            "plan.slots must include every export layer (use skip:true when "
            "unused): " + ", ".join(missing)
        )

    normalized_slots: dict[str, Any] = {}
    for slot in OUTFIT_SLOT_NAMES:
        item = raw_slots[slot]
        if not isinstance(item, Mapping):
            raise ValueError(f"plan.slots[{slot}] must be an object")
        skip = bool(item.get("skip", False))
        notes_raw = item.get("visual_notes", "")
        if skip:
            notes = (
                _as_nonempty_str(notes_raw, f"slots[{slot}].visual_notes")
                if isinstance(notes_raw, str) and notes_raw.strip()
                else "Skipped for this outfit."
            )
        else:
            notes = _as_nonempty_str(notes_raw, f"slots[{slot}].visual_notes")
            if len(notes) < _MIN_VISUAL_NOTES:
                raise ValueError(
                    f"slots[{slot}].visual_notes too short (min {_MIN_VISUAL_NOTES}) "
                    "— describe colors, shapes, materials to paint"
                )
        spec = OUTFIT_SLOT_SPECS[slot]
        normalized_slots[slot] = {
            "skip": skip,
            "visual_notes": notes,
            "parent_parts": list(spec["parent_parts"]),
            "body_parts": list(spec["body_parts"]),
            "draw_after": spec["draw_after"],
            "allow_overhang": bool(spec["allow_overhang"]),
        }

    return {"overall_look": overall, "slots": normalized_slots}


def build_outfit_user_facing_summary(
    *,
    name: str,
    brief: str,
    plan: Mapping[str, Any],
) -> str:
    lines: list[str] = [
        f"Outfit plan for `{name}`",
        f"Brief: {brief.strip()}",
        "",
        "Overall look:",
        plan["overall_look"],
        "",
        "Design layers (same 10 names as base/layers/):",
    ]
    for slot in OUTFIT_SLOT_NAMES:
        entry = plan["slots"][slot]
        if entry["skip"]:
            lines.append(f"  - {slot}: SKIP — {entry['visual_notes']}")
        else:
            body = ", ".join(entry["body_parts"])
            lines.append(
                f"  - {slot}: paint on [{body}] "
                f"(pose parent={entry['parent_parts'][0]}) — "
                f"{entry['visual_notes']}"
            )
    lines.append("")
    lines.append(
        "Painting rule: for each active export layer, "
        "prepare_outfit_slot_reference then paint ONLY via MCP tools onto "
        "design/layers/<layer>.png (fill_parts_on_slot for that layer's "
        "body_parts silhouette; fill_rect / draw_line / paint_pixels for "
        "details). FORBIDDEN: Shell/PIL scripts or external image generators. "
        "Each design file rigid-rotates 1:1 with its body part (no multi-parent "
        "split). Clothing MAY overhang the body silhouette — body underlay is "
        "alignment reference, not a clip mask; keep proportions sensible. "
        "If this matches, say OK and paint layers; otherwise revise the plan."
    )
    return "\n".join(lines)


def _new_plan_id(name: str, normalized: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"name": name, "plan": normalized},
        sort_keys=True,
        ensure_ascii=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"{name}-outfit-{digest}-{uuid.uuid4().hex[:8]}"


def require_outfit_plan(
    name: str,
    *,
    plan_id: str | None = None,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    name = validate_character_name(name)
    path = plan_path(name, output_dir)
    if not path.is_file():
        raise FileNotFoundError(
            f"No outfit plan for '{name}' (missing {path}). "
            "Call plan_outfit first, show user_facing_summary, then paint slots."
        )
    data = _read_json(path)
    if not isinstance(data, dict) or "plan" not in data or "plan_id" not in data:
        raise FileNotFoundError(
            f"Outfit plan at {path} is incomplete. Re-submit via plan_outfit."
        )
    if plan_id is not None and data.get("plan_id") != plan_id:
        raise ValueError(
            f"plan_id mismatch for outfit '{name}': "
            f"got {plan_id!r}, stored {data.get('plan_id')!r}."
        )
    return data


def plan_outfit(
    name: str,
    brief: str,
    plan: Mapping[str, Any],
    *,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Validate + lock an agent-authored outfit plan under ``design/plan.json``.

    The calling agent must author ``plan`` from the free ``brief`` (e.g.
    \"samurai\") — this tool does **not** invent clothing styles.

    Overhang: ``visual_notes`` / ``overall_look`` may describe clothing that
    extends past body margins; body parents are alignment refs, not clip masks.
    """
    name = validate_character_name(name)
    brief_text = _as_nonempty_str(brief, "brief")
    if len(brief_text) < _MIN_BRIEF:
        raise ValueError("brief must describe the intended look (e.g. 'samurai')")

    _ensure_character_base(name, output_dir)
    normalized = validate_outfit_plan(plan)
    plan_id = _new_plan_id(name, normalized)
    summary = build_outfit_user_facing_summary(
        name=name, brief=brief_text, plan=normalized
    )

    ddir = design_dir(name, output_dir)
    ddir.mkdir(parents=True, exist_ok=True)
    design_layers_dir(name, output_dir).mkdir(parents=True, exist_ok=True)
    design_refs_dir(name, output_dir).mkdir(parents=True, exist_ok=True)

    stored = {
        "schema": PLAN_SCHEMA,
        "plan_id": plan_id,
        "name": name,
        "brief": brief_text,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plan": normalized,
        "user_facing_summary": summary,
        "slot_specs": list_outfit_slot_specs(),
    }
    path = plan_path(name, output_dir)
    _write_json(path, stored)
    meta = _sync_layers_meta(name, output_dir)

    return {
        "schema": PLAN_SCHEMA,
        "plan_id": plan_id,
        "name": name,
        "brief": brief_text,
        "plan": normalized,
        "user_facing_summary": summary,
        "slot_specs": list_outfit_slot_specs(),
        "paths": {
            "plan_json": str(path),
            "design_dir": str(ddir),
            "layers_meta": str(meta_path(name, output_dir)),
        },
        "layers_meta": meta,
        "next_step": (
            "Show user_facing_summary to the user. After OK, for each "
            "non-skipped slot: prepare_outfit_slot_reference → "
            "fill_parts_on_slot (silhouette) + fill_rect/draw_line/"
            "paint_pixels (details) via MCP only → recommended plan_shading "
            "+ shadow paint → compose_character when ready. "
            "generate_outfit_slot is optional import/refresh. "
            "FORBIDDEN: Shell/PIL scripts or external image generators."
        ),
    }


def get_outfit_plan(
    name: str,
    *,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    data = require_outfit_plan(name, output_dir=output_dir)
    return {
        "schema": data.get("schema", PLAN_SCHEMA),
        "plan_id": data["plan_id"],
        "name": data.get("name", name),
        "brief": data.get("brief", ""),
        "plan": data["plan"],
        "user_facing_summary": data.get("user_facing_summary", ""),
        "paths": {
            "plan_json": str(plan_path(name, output_dir)),
            "design_dir": str(design_dir(name, output_dir)),
        },
        "next_step": (
            "Show user_facing_summary if needed, then prepare_outfit_slot_reference "
            "+ fill_parts_on_slot / MCP paint tools per active slot, then "
            "recommended plan_shading → shadow paint → compose_character. "
            "Never Shell/PIL paint scripts."
        ),
    }


def prepare_outfit_slot_reference(
    name: str,
    slot: str,
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Write the mandatory body-under + design-on-top reference PNG for ``slot``.

    ``slot`` is one of the 10 ``EXPORT_LAYER_NAMES``. The reference stack is:
    that export layer's ``body_parts`` (dimmed viz) → current design layer on
    top. Persist only the design layer PNG. Single-parent pose later uses the
    export layer name itself.
    """
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
    body_parts = list(
        slot_plan.get("body_parts") or OUTFIT_SLOT_SPECS[slot]["body_parts"]
    )
    parents = list(slot_plan.get("parent_parts") or OUTFIT_SLOT_SPECS[slot]["parent_parts"])

    design_path = slot_layer_path(name, slot, output_dir)
    design = _ensure_design_layer(design_path, w, h)

    body = _body_reference_stack(part_map, body_parts)
    # Checker-ish dark panel so transparency is visible when inspecting.
    # Design is composited full (no clip-to-body) so overhang paints show.
    ref = Image.new("RGBA", (w, h), (36, 36, 42, 255))
    ref = Image.alpha_composite(ref, body)
    ref = Image.alpha_composite(ref, design)

    refs_dir = design_refs_dir(name, output_dir)
    refs_dir.mkdir(parents=True, exist_ok=True)
    ref_path = slot_ref_path(name, slot, output_dir)
    ref.save(ref_path)

    preview = ref.resize((w * scale, h * scale), Image.Resampling.NEAREST)
    preview_path = refs_dir / f"{slot}_ref_preview.png"
    preview.save(preview_path)

    grid_path = refs_dir / f"{slot}_ref_grid.png"
    render_ref_grid(ref, scale=scale).save(grid_path)

    meta = _sync_layers_meta(name, output_dir)

    return {
        "schema": "spritemcp.outfit_slot_reference.v1",
        "name": name,
        "slot": slot,
        "plan_id": locked["plan_id"],
        "parent_parts": parents,
        "body_parts": body_parts,
        "draw_after": slot_plan["draw_after"],
        "allow_overhang": True,
        "visual_notes": slot_plan["visual_notes"],
        "overhang_note": _OVERHANG_GUIDANCE,
        "paint_instruction": (
            f"Body reference for {body_parts} is UNDERNEATH (alignment only — not "
            f"a clip mask). Paint clothing/detail ONLY on the design layer "
            f"({design_path.name}); this file rigid-rotates 1:1 with "
            f"{parents[0]}. Overhang past the body silhouette is allowed if "
            f"proportions stay sensible. Do not bake body pixels into the "
            f"design PNG. Inspect {ref_path.name}, then edit {design_path.name}."
        ),
        "preview_paths": {
            "reference": str(ref_path),
            "reference_preview": str(preview_path),
            "reference_grid": str(grid_path),
        },
        "paths": {
            "design_layer": str(design_path),
            "reference": str(ref_path),
            "reference_preview": str(preview_path),
            "reference_grid": str(grid_path),
            "design_dir": str(design_dir(name, output_dir)),
        },
        "layers_meta": meta["slots"].get(slot),
        "next_step": (
            "Paint via MCP: fill_parts_on_slot for this layer's body_parts, then "
            "fill_rect / draw_line / paint_pixels / paint_from_commands for "
            "details (overhang OK). When layers are ready, call compose_character. "
            "FORBIDDEN: Shell/PIL scripts or GenerateImage."
        ),
    }


def generate_outfit_slot(
    name: str,
    slot: str,
    *,
    design_image_path: str | Path | None = None,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Ensure design layer for ``slot``, optionally import a painted PNG, refresh ref.

    Always rebuilds the body-under reference stack so painting stays non-destructive
    (design layer only is persisted under ``design/layers/<slot>.png``). Body
    underlay is not a clip mask — imported / painted pixels may overhang the
    parent silhouette.

    Agent paint workflow example
    ----------------------------
    1. ``prepare_outfit_slot_reference(\"vagabond\", \"torso\")``
    2. Read ``.../design/refs/torso_ref.png`` (torso under empty design)
    3. Paint shirt pixels into ``.../design/layers/torso.png``
       (or save elsewhere and pass ``design_image_path``); overhang OK
    4. Call this tool again to validate size + refresh the reference composite
    """
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
    dest = slot_layer_path(name, slot, output_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if design_image_path is not None:
        src = Path(design_image_path)
        if not src.is_file():
            raise FileNotFoundError(f"design_image_path not found: {src}")
        img = Image.open(src).convert("RGBA")
        if img.size != (w, h):
            raise ValueError(
                f"Imported design size {img.size} != character canvas {(w, h)}. "
                f"Paint on a {w}x{h} transparent PNG matching the body reference."
            )
        img.save(dest)
        source = "imported"
    else:
        _ensure_design_layer(dest, w, h)
        source = "existing_or_blank"

    ref_result = prepare_outfit_slot_reference(
        name,
        slot,
        output_dir=output_dir,
        scale=scale,
        plan_id=locked["plan_id"],
    )
    meta = _sync_layers_meta(name, output_dir)
    design = Image.open(dest).convert("RGBA")
    painted = design.getchannel("A").getextrema()[1] > 0

    return {
        "schema": "spritemcp.generate_outfit_slot.v1",
        "name": name,
        "slot": slot,
        "plan_id": locked["plan_id"],
        "source": source,
        "painted": painted,
        "parent_parts": list(slot_plan["parent_parts"]),
        "body_parts": list(
            slot_plan.get("body_parts") or OUTFIT_SLOT_SPECS[slot]["body_parts"]
        ),
        "allow_overhang": True,
        "overhang_note": _OVERHANG_GUIDANCE,
        "visual_notes": slot_plan["visual_notes"],
        "paint_instruction": ref_result["paint_instruction"],
        "preview_paths": ref_result["preview_paths"],
        "paths": {
            **ref_result["paths"],
            "layers_meta": str(meta_path(name, output_dir)),
        },
        "layers_meta": meta["slots"].get(slot),
        "next_step": (
            "If the design layer is still blank, paint on it while viewing the "
            "reference (body underneath; overhang allowed). Re-call "
            "generate_outfit_slot after edits. When layers are ready, call "
            "compose_character."
        ),
    }


def list_outfit_layers(
    name: str,
    *,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """List outfit slots, parent parts, paint status, and file paths."""
    name = validate_character_name(name)
    _ensure_character_base(name, output_dir)
    meta = _sync_layers_meta(name, output_dir)
    plan_data = None
    plan_file = plan_path(name, output_dir)
    if plan_file.is_file():
        plan_data = _read_json(plan_file)

    layers: list[dict[str, Any]] = []
    for slot in OUTFIT_SLOT_NAMES:
        entry = meta["slots"][slot]
        layer_p = slot_layer_path(name, slot, output_dir)
        ref_p = slot_ref_path(name, slot, output_dir)
        skip = False
        notes = ""
        if plan_data and isinstance(plan_data.get("plan"), dict):
            slot_plan = plan_data["plan"].get("slots", {}).get(slot, {})
            skip = bool(slot_plan.get("skip", False))
            notes = str(slot_plan.get("visual_notes", ""))
        layers.append(
            {
                "slot": slot,
                "skip": skip,
                "status": entry.get("status", "empty"),
                "parent_parts": entry.get("parent_parts", []),
                "body_parts": entry.get("body_parts", []),
                "draw_after": entry.get("draw_after"),
                "allow_overhang": entry.get("allow_overhang", True),
                "visual_notes": notes,
                "paths": {
                    "design_layer": str(layer_p) if layer_p.is_file() else None,
                    "reference": str(ref_p) if ref_p.is_file() else None,
                },
            }
        )

    return {
        "schema": "spritemcp.list_outfit_layers.v1",
        "name": name,
        "plan_id": plan_data.get("plan_id") if plan_data else None,
        "design_dir": str(design_dir(name, output_dir)),
        "layers": layers,
        "layers_meta_path": str(meta_path(name, output_dir)),
        "slot_specs": list_outfit_slot_specs(),
    }


def clear_outfit_slot(
    name: str,
    slot: str,
    *,
    output_dir: Path | str | None = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Replace the design layer with a blank transparent canvas; refresh reference."""
    name = validate_character_name(name)
    slot = _validate_slot(slot)
    locked = require_outfit_plan(name, plan_id=plan_id, output_dir=output_dir)
    part_map = _load_part_map(name, output_dir)
    w, h = _canvas_size(part_map)
    dest = slot_layer_path(name, slot, output_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _blank_rgba(w, h).save(dest)

    if locked["plan"]["slots"][slot].get("skip"):
        meta = _sync_layers_meta(name, output_dir)
        return {
            "schema": "spritemcp.clear_outfit_slot.v1",
            "name": name,
            "slot": slot,
            "cleared": True,
            "skipped_in_plan": True,
            "paths": {"design_layer": str(dest)},
            "layers_meta": meta["slots"].get(slot),
        }

    ref_result = prepare_outfit_slot_reference(
        name,
        slot,
        output_dir=output_dir,
        plan_id=locked["plan_id"],
    )
    return {
        "schema": "spritemcp.clear_outfit_slot.v1",
        "name": name,
        "slot": slot,
        "plan_id": locked["plan_id"],
        "cleared": True,
        "skipped_in_plan": False,
        "preview_paths": ref_result["preview_paths"],
        "paths": ref_result["paths"],
        "layers_meta": ref_result.get("layers_meta"),
    }


def compose_character(
    name: str,
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
    include_skipped: bool = False,
) -> dict[str, Any]:
    """Compose dressed rest = base body parts + active design layers.

    Design layers parent to body parts (see ``layers_meta.json``) so later
    animation can rotate them with those parts. Rest compose uses DRAW_ORDER
    for body, then inserts each full design after ``draw_after`` — never
    clip-masks to body alpha (clothing may overhang; keep proportions sensible).
    """
    name = validate_character_name(name)
    _ensure_character_base(name, output_dir)
    part_map = _load_part_map(name, output_dir)
    w, h = _canvas_size(part_map)
    meta = _sync_layers_meta(name, output_dir)

    plan_data = None
    plan_file = plan_path(name, output_dir)
    if plan_file.is_file():
        plan_data = _read_json(plan_file)

    # Active design layers keyed by draw_after / parents.
    active: list[dict[str, Any]] = []
    for slot in OUTFIT_SLOT_NAMES:
        entry = meta["slots"][slot]
        skip = False
        if plan_data and isinstance(plan_data.get("plan"), dict):
            skip = bool(
                plan_data["plan"].get("slots", {}).get(slot, {}).get("skip", False)
            )
        if skip and not include_skipped:
            continue
        layer_p = slot_layer_path(name, slot, output_dir)
        if not layer_p.is_file():
            continue
        design = Image.open(layer_p).convert("RGBA")
        if design.size != (w, h):
            continue
        if design.getchannel("A").getextrema()[1] == 0:
            continue
        active.append(
            {
                "slot": slot,
                "parent_parts": list(entry["parent_parts"]),
                "body_parts": list(entry.get("body_parts") or []),
                "draw_after": entry["draw_after"],
                "allow_overhang": True,
                "image": design,
            }
        )

    canvas = _blank_rgba(w, h)
    done_slots: set[str] = set()

    for part_name in DRAW_ORDER:
        canvas = Image.alpha_composite(canvas, _body_part_layer(part_map, part_name))
        for item in active:
            slot = item["slot"]
            if slot in done_slots:
                continue
            # Full design layer after draw_after — no clip-to-body masking.
            if part_name == item["draw_after"]:
                canvas = Image.alpha_composite(canvas, item["image"])
                done_slots.add(slot)

    ddir = design_dir(name, output_dir)
    ddir.mkdir(parents=True, exist_ok=True)
    compose_path = ddir / "compose_preview.png"
    canvas.save(compose_path)

    preview = canvas.resize((w * scale, h * scale), Image.Resampling.NEAREST)
    preview_path = ddir / "compose_preview_scaled.png"
    preview.save(preview_path)

    grid_path = ddir / "compose_preview_grid.png"
    # save_grid expects pixel grid; use render_ref_grid on the native compose.
    render_ref_grid(canvas, scale=scale).save(grid_path)

    used_slots = [a["slot"] for a in active]
    return {
        "schema": COMPOSE_SCHEMA,
        "name": name,
        "width": w,
        "height": h,
        "plan_id": plan_data.get("plan_id") if plan_data else None,
        "slots_composited": used_slots,
        "draw_order_body": list(DRAW_ORDER),
        "overhang_note": _OVERHANG_GUIDANCE,
        "ready_for_animation": True,
        "preview_paths": {
            "compose": str(compose_path),
            "compose_scaled": str(preview_path),
            "compose_grid": str(grid_path),
        },
        "paths": {
            "compose_preview": str(compose_path),
            "compose_preview_scaled": str(preview_path),
            "compose_preview_grid": str(grid_path),
            "design_dir": str(ddir),
            "layers_meta": str(meta_path(name, output_dir)),
            "plan_json": str(plan_file) if plan_file.is_file() else None,
        },
        "next_step": (
            "Dressed rest is ready. HUMAN EDIT GATE: ask the user if they want "
            "to edit pixels manually. If yes → open_pixel_editor(name) → user "
            "Apply → re-run compose_character. If no → plan_animation → "
            "build/finish frames. Animation frames include these design layers "
            "(each rigid-rotated 1:1 with its single parent export layer; "
            "DRAW_ORDER + draw_after respected)."
        ),
    }


def has_painted_design(
    name: str,
    *,
    output_dir: Path | str | None = None,
) -> bool:
    """True if any outfit slot PNG under ``design/layers/`` has opaque paint."""
    name = validate_character_name(name)
    for slot in OUTFIT_SLOT_NAMES:
        layer_p = slot_layer_path(name, slot, output_dir)
        if not layer_p.is_file():
            continue
        img = Image.open(layer_p).convert("RGBA")
        if img.getchannel("A").getextrema()[1] > 0:
            return True
    return False


def require_compose_for_animation(
    name: str,
    *,
    output_dir: Path | str | None = None,
) -> None:
    """If painted design (or an outfit plan) exists, require compose_character.

    Naked characters (no design paint / no plan) skip this gate.
    """
    name = validate_character_name(name)
    ddir = design_dir(name, output_dir)
    plan_file = plan_path(name, output_dir)
    painted = has_painted_design(name, output_dir=output_dir)
    has_plan = plan_file.is_file()
    if not painted and not has_plan:
        return
    compose_preview = ddir / "compose_preview.png"
    if not compose_preview.is_file():
        raise FileNotFoundError(
            f"Character '{name}' has outfit design under {ddir} but "
            f"compose_preview.png is missing. Call compose_character(name) "
            "after painting slots (and preferably after plan_outfit) before "
            "build_frame_animation — frames are dressed from design layers."
        )


def load_active_design_layers(
    name: str,
    *,
    output_dir: Path | str | None = None,
    include_skipped: bool = False,
) -> list:
    """Load painted design layers as ``pose.DesignLayerInput`` for posed compose.

    Returns an empty list when there is no design (base-only animation).
    """
    from .pose import DesignLayerInput

    name = validate_character_name(name)
    if not has_painted_design(name, output_dir=output_dir):
        return []

    part_map = _load_part_map(name, output_dir)
    w, h = _canvas_size(part_map)
    meta = _sync_layers_meta(name, output_dir)

    plan_data = None
    plan_file = plan_path(name, output_dir)
    if plan_file.is_file():
        plan_data = _read_json(plan_file)

    active: list[DesignLayerInput] = []
    for slot in OUTFIT_SLOT_NAMES:
        entry = meta["slots"][slot]
        skip = False
        if plan_data and isinstance(plan_data.get("plan"), dict):
            skip = bool(
                plan_data["plan"].get("slots", {}).get(slot, {}).get("skip", False)
            )
        if skip and not include_skipped:
            continue
        layer_p = slot_layer_path(name, slot, output_dir)
        if not layer_p.is_file():
            continue
        design = Image.open(layer_p).convert("RGBA")
        if design.size != (w, h):
            continue
        if design.getchannel("A").getextrema()[1] == 0:
            continue
        active.append(
            DesignLayerInput(
                slot=slot,
                parent_parts=tuple(entry["parent_parts"]),
                draw_after=str(entry["draw_after"]),
                image=design,
            )
        )
    return active


def _rest_part_alpha_mask(part_map: PartMap, part_name: str) -> Image.Image:
    w, h = part_map.width, part_map.height
    mask = Image.new("L", (w, h), 0)
    pix = mask.load()
    assert pix is not None
    if part_name not in PART_NAMES or part_name == "empty":
        return mask
    for x, y in part_map.part_pixels(part_name):
        if 0 <= x < w and 0 <= y < h:
            pix[x, y] = 255
    return mask


def _union_part_mask(part_map: PartMap, parts: Sequence[str]) -> Image.Image:
    w, h = part_map.width, part_map.height
    union = Image.new("L", (w, h), 0)
    for part in parts:
        union = ImageChops.lighter(union, _rest_part_alpha_mask(part_map, part))
    return union


def _claim_design_by_masks(
    design: Image.Image,
    part_map: PartMap,
    claims: Mapping[str, Sequence[str]],
) -> dict[str, Image.Image]:
    """Split a design PNG into per-export-layer pieces by rest body masks.

    Overhang leftover is assigned to the last claim key in DRAW_ORDER among
    claimed layers (stable, deterministic).
    """
    remaining_a = design.getchannel("A")
    pieces: dict[str, Image.Image] = {
        layer: _blank_rgba(design.size[0], design.size[1]) for layer in claims
    }
    order_index = {name: i for i, name in enumerate(DRAW_ORDER)}
    ordered_layers = sorted(
        claims.keys(),
        key=lambda layer: max(
            (order_index.get(p, -1) for p in claims[layer]),
            default=-1,
        ),
    )

    for layer in ordered_layers:
        body_mask = _union_part_mask(part_map, claims[layer])
        claim = ImageChops.multiply(remaining_a, body_mask)
        if claim.getextrema()[1] == 0:
            continue
        piece = design.copy()
        piece.putalpha(claim)
        pieces[layer] = piece
        remaining_a = ImageChops.subtract(remaining_a, claim)

    if remaining_a.getextrema()[1] > 0 and ordered_layers:
        leftover_parent = ordered_layers[-1]
        leftover = design.copy()
        leftover.putalpha(remaining_a)
        pieces[leftover_parent] = Image.alpha_composite(
            pieces[leftover_parent], leftover
        )
    return pieces


def _merge_notes(*notes: str) -> str:
    parts = [n.strip() for n in notes if isinstance(n, str) and n.strip()]
    if not parts:
        return "Migrated design layer."
    # Dedupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for n in parts:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return " | ".join(out)


def migrate_design_to_export_layers(
    name: str,
    *,
    output_dir: Path | str | None = None,
    delete_legacy: bool = True,
) -> dict[str, Any]:
    """Convert legacy clothing-slot design PNGs/plans to the 10 export layers.

    Splits multi-parent legacy slots (arms/legs) by rest body masks so far and
    near each get their own design file. Merges gloves→``lower_arm_*`` and
    boots→``lower_leg_*``. Rewrites ``plan.json`` to schema v2.
    """
    name = validate_character_name(name)
    _ensure_character_base(name, output_dir)
    part_map = _load_part_map(name, output_dir)
    w, h = _canvas_size(part_map)
    layers_dir = design_layers_dir(name, output_dir)
    layers_dir.mkdir(parents=True, exist_ok=True)
    refs_dir = design_refs_dir(name, output_dir)

    # Accumulator canvas per export layer.
    accum: dict[str, Image.Image] = {
        layer: _blank_rgba(w, h) for layer in OUTFIT_SLOT_NAMES
    }
    notes_by_layer: dict[str, list[str]] = {layer: [] for layer in OUTFIT_SLOT_NAMES}
    skip_by_layer: dict[str, bool] = {layer: True for layer in OUTFIT_SLOT_NAMES}
    overall_look = (
        f"Migrated outfit for {name}: design layers match base export layers."
    )
    brief = "migrated to export-layer design schema"
    legacy_found: list[str] = []
    already_export = any(
        (layers_dir / f"{layer}.png").is_file() for layer in OUTFIT_SLOT_NAMES
    )

    plan_file = plan_path(name, output_dir)
    legacy_plan_slots: dict[str, Any] = {}
    if plan_file.is_file():
        old = _read_json(plan_file)
        if isinstance(old.get("plan"), dict):
            overall_look = str(old["plan"].get("overall_look") or overall_look)
            legacy_plan_slots = dict(old["plan"].get("slots") or {})
        if isinstance(old.get("brief"), str) and old["brief"].strip():
            brief = old["brief"]

    for legacy_slot, targets in _LEGACY_SLOT_SPLIT.items():
        legacy_path = layers_dir / f"{legacy_slot}.png"
        if not legacy_path.is_file():
            continue
        legacy_found.append(legacy_slot)
        design = Image.open(legacy_path).convert("RGBA")
        if design.size != (w, h):
            raise ValueError(
                f"Legacy layer {legacy_path} size {design.size} != {(w, h)}"
            )
        if design.getchannel("A").getextrema()[1] == 0:
            continue

        claims = _LEGACY_CLAIM_PARTS[legacy_slot]
        # Single-target rename: keep full paint (including overhang).
        if len(targets) == 1 and set(claims.keys()) == set(targets):
            pieces = {targets[0]: design}
        else:
            pieces = _claim_design_by_masks(design, part_map, claims)

        slot_entry = legacy_plan_slots.get(legacy_slot) or {}
        notes = str(slot_entry.get("visual_notes") or f"From legacy {legacy_slot}")
        skipped = bool(slot_entry.get("skip", False))

        for layer, piece in pieces.items():
            if piece.getchannel("A").getextrema()[1] == 0:
                continue
            accum[layer] = Image.alpha_composite(accum[layer], piece)
            notes_by_layer[layer].append(notes)
            if not skipped:
                skip_by_layer[layer] = False

    # If export layers already exist and no legacy slots, keep existing paint.
    if not legacy_found and already_export:
        for layer in OUTFIT_SLOT_NAMES:
            path = layers_dir / f"{layer}.png"
            if path.is_file():
                img = Image.open(path).convert("RGBA")
                if img.getchannel("A").getextrema()[1] > 0:
                    accum[layer] = img
                    skip_by_layer[layer] = False
                    notes_by_layer[layer].append(
                        str(
                            (legacy_plan_slots.get(layer) or {}).get(
                                "visual_notes", f"Existing {layer} design"
                            )
                        )
                    )

    written: list[str] = []
    for layer in OUTFIT_SLOT_NAMES:
        dest = layers_dir / f"{layer}.png"
        img = accum[layer]
        img.save(dest)
        if img.getchannel("A").getextrema()[1] > 0:
            written.append(layer)
            skip_by_layer[layer] = False

    # Build v2 plan from migrated notes.
    plan_slots: dict[str, Any] = {}
    for layer in OUTFIT_SLOT_NAMES:
        notes = _merge_notes(*notes_by_layer[layer])
        if skip_by_layer[layer] and layer not in written:
            plan_slots[layer] = {
                "skip": True,
                "visual_notes": notes if notes != "Migrated design layer." else (
                    f"No paint migrated for {layer}; skipped."
                ),
            }
        else:
            # Ensure min visual_notes length for active layers.
            if len(notes) < _MIN_VISUAL_NOTES:
                notes = f"{notes} Paint on export layer {layer}."
            plan_slots[layer] = {"skip": False, "visual_notes": notes}

    if len(overall_look) < _MIN_OVERALL:
        overall_look = (
            f"{overall_look} Design uses the same 10 export body layers as "
            f"base/layers/ (far and near limbs each have their own paint)."
        )

    result = plan_outfit(
        name,
        brief,
        {"overall_look": overall_look, "slots": plan_slots},
        output_dir=output_dir,
    )

    deleted: list[str] = []
    if delete_legacy:
        for legacy_slot in _LEGACY_SLOT_SPLIT:
            for folder, suffix in (
                (layers_dir, f"{legacy_slot}.png"),
                (refs_dir, f"{legacy_slot}_ref.png"),
                (refs_dir, f"{legacy_slot}_ref_preview.png"),
                (refs_dir, f"{legacy_slot}_ref_grid.png"),
            ):
                path = folder / suffix
                if path.is_file():
                    path.unlink()
                    deleted.append(str(path))

    # Refresh refs for painted layers.
    refreshed: list[str] = []
    for layer in written:
        if result["plan"]["slots"][layer].get("skip"):
            continue
        prepare_outfit_slot_reference(
            name, layer, output_dir=output_dir, plan_id=result["plan_id"]
        )
        refreshed.append(layer)

    meta = _sync_layers_meta(name, output_dir)
    return {
        "schema": "spritemcp.migrate_design_export_layers.v1",
        "name": name,
        "legacy_slots_found": legacy_found,
        "layers_written": written,
        "refs_refreshed": refreshed,
        "legacy_files_deleted": deleted,
        "plan_id": result["plan_id"],
        "paths": result["paths"],
        "layers_meta": meta,
        "next_step": (
            "Call compose_character, then rebuild animation frames so far/near "
            "limbs each carry their own design layer."
        ),
    }
