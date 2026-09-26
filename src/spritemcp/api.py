"""Public API for the sprite toolkit.

These functions are the intended surface for the MCP server
(``spritemcp.mcp_server`` — each function ≈ one tool).

Agent character / outfit / animation pipeline
---------------------------------------------
1. Optionally ``set_output_root(path)`` when assets should leave
   ``<cwd>/output`` (session-wide). Per-call ``output_dir`` still overrides.
2. ``generate_character(name)`` — base idle + pivots under
   ``<output_root>/characters/<name>/base/``
3. ``plan_outfit(name, brief, plan)`` — agent authors the 10 export-layer
   notes from a free brief (e.g. \"samurai\"); stores ``design/plan.json`` +
   ``user_facing_summary``
4. Per layer: ``prepare_outfit_slot_reference`` → paint via MCP paint tools
   (``fill_parts_on_slot`` for that layer's body_parts; ``paint_pixels`` /
   ``fill_rect`` / ``paint_from_commands`` / … for details) onto
   ``design/layers/<layer>.png`` (same 10 names as ``base/layers/``; clothing
   may overhang; body is alignment only, not a clip mask). Optional
   ``generate_outfit_slot`` for import / refresh only.
   FORBIDDEN for agents: Shell/PIL scripts, GenerateImage / external generators.
5. **Recommended shading pass:** ``plan_shading(name, brief, plan)`` → show
   ``user_facing_summary`` → paint one-step-darker shadows on design layers
   (optional ``suggest_shade_regions``; see ``get_shade_palette``). Soft gate —
   ``compose_character`` does not refuse without a shading plan.
6. ``compose_character`` for dressed rest (re-compose after shading).
7. **Human edit gate:** ask if the user wants manual pixel edits. If yes →
   ``open_pixel_editor(name)`` (browser UI; Apply writes ``design/layers/``
   only) → re-run ``compose_character``. If no → continue.
8. ``plan_animation(name, animation_name, intent, plan)`` — agent authors a
   structured plan; tool validates + stores ``plan.json`` and returns
   ``user_facing_summary`` (MUST be shown to the user before frames)
9. ``build_frame_animation`` / ``finish_frame_animation`` — draft/lock frames
   0..7 (refuse without a valid animation plan). Frames include design layers
   when outfit exists (each rigid-rotated 1:1 with its export body part).
   Finishing frame 7 also writes contact sheet + preview GIF
   (``export_animation_preview`` to rebuild).

Joint names for ``rotations``: neck, shoulder_far, shoulder_near,
elbow_far, elbow_near, hip_far, hip_near, knee_far, knee_near.
Angles are absolute degrees from rest; unspecified joints inherit the
frame start pose (rest / previous frame). See ``pose`` module limitations.

View lock: side profile only; ``*_far`` behind torso, ``*_near`` in front
(``DRAW_ORDER`` / ``get_view_lock()``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from . import animation_plan as _animation_plan
from . import base_idle as _base_idle
from . import character as _character
from . import demo as _demo
from . import outfit as _outfit
from . import paint as _paint
from . import pivots as _pivots
from . import pixel_editor as _pixel_editor
from . import shading as _shading
from . import show_ref_grid as _show_ref_grid
from .config import (
    DEFAULT_STYLE_REF,
    clear_output_root as _clear_output_root,
    get_output_root as _get_output_root,
    has_session_output_root,
    resolve_output_dir,
    resolve_style_ref,
    set_output_root as _set_output_root,
    subdir,
)
from .pose import JOINT_ORDER, joint_docs
from .base_idle import (
    ARMATURE_NAME,
    CANVAS_H,
    CANVAS_W,
    DRAW_ORDER,
    EXPORT_LAYER_NAMES,
    LAYERING_RULE,
    VIEW_MODE,
    default_base_idle_out_dir,
)


def set_output_root(path: str | Path) -> dict[str, str]:
    """Set session output root for all tools that omit ``output_dir``.

    Resolves to an absolute path, creates the directory if needed, and keeps
    the override for this process / MCP session only (not written to disk).
    Default when unset: ``<cwd>/output`` (agent workspace). Per-call
    ``output_dir`` still wins. Use when the user wants character/anim assets
    outside the process working directory.
    """
    root = _set_output_root(path)
    return {
        "output_root": str(root),
        "default_output_dir": str(_get_output_root()),
        "session_override": True,
        "note": (
            "Session output root set. Subsequent generate_character / outfit / "
            "animation calls without output_dir write under this path. "
            "Call clear_output_root() to restore the cwd/output default."
        ),
    }


def clear_output_root() -> dict[str, str]:
    """Clear session output override; restore ``<cwd>/output`` default."""
    root = _clear_output_root()
    return {
        "output_root": str(root),
        "default_output_dir": str(_get_output_root()),
        "session_override": False,
    }


def get_output_root() -> dict[str, Any]:
    """Absolute output root currently in effect (session or ``<cwd>/output``)."""
    root = _get_output_root()
    return {
        "output_root": str(root),
        "default_output_dir": str(_get_output_root()),
        "session_override": has_session_output_root(),
        "characters_dir": str(root / "characters"),
    }


def get_part_ids() -> dict[str, int]:
    """Return stable part name → integer id map for the base idle template."""
    return dict(_base_idle.PART_NAMES)


def get_draw_order() -> list[str]:
    """Side-view compose order: far limbs, torso/head, near legs, near arms."""
    return list(DRAW_ORDER)


def get_view_lock() -> dict[str, object]:
    """Permanent view + layering rules for every character."""
    return {
        "view": VIEW_MODE,
        "facing": _base_idle.FACING_DEFAULT,
        "layering": LAYERING_RULE,
        "draw_order": list(DRAW_ORDER),
        "armature": ARMATURE_NAME,
        "canvas": {"w": CANVAS_W, "h": CANVAS_H},
        "lateral_attachments": True,
        "note": (
            "Authored 90×128 armature keeps per-limb lateral shoulder/hip pivots; "
            "depth is z-order via DRAW_ORDER."
        ),
    }


def get_joints() -> list[dict[str, str]]:
    """Return hierarchical joint specs: name, parent part, child part."""
    return [
        {"name": name, "parent": parent, "child": child}
        for name, parent, child in _pivots.JOINT_SPECS
    ]


def get_default_paths() -> dict[str, str]:
    """Return output paths in effect (session root or ``<cwd>/output``).

    Characters live under ``<output_root>/characters/<name>/{base,design,anims}``.
    When the user wants assets outside the process working directory, call
    ``set_output_root`` or pass ``output_dir`` on write tools.
    """
    root = _get_output_root()
    return {
        "output_dir": str(root),
        "output_root": str(root),
        "default_output_dir": str(_get_output_root()),
        "session_override": "true" if has_session_output_root() else "false",
        "style_ref": str(DEFAULT_STYLE_REF),
        "armature": ARMATURE_NAME,
        "canvas": f"{CANVAS_W}x{CANVAS_H}",
        "base_idle_90x128_dir": str(default_base_idle_out_dir()),
        "default_base_idle_dir": str(default_base_idle_out_dir()),
        "reference_grid_dir": str(subdir("reference_grid")),
        "demo_dir": str(subdir("demo")),
        "characters_dir": str(subdir("characters")),
    }


def get_joint_docs() -> list[dict[str, str]]:
    """Return joint name, parent, child, and which parts each joint rotates."""
    return joint_docs()


def get_frame_count() -> int:
    """All character animations use this many frames (indices 0..n-1)."""
    return _character.FRAME_COUNT


def show_reference_grid(
    *,
    style_ref: Path | str | None = None,
    output_dir: Path | str | None = None,
    scale: int = 8,
    readable_scale: int = 12,
) -> dict[str, Any]:
    """Upscale the style-ref PNG onto a visible per-pixel grid.

    Writes under ``output_dir/reference_grid/`` (default: ``<cwd>/output``).
    """
    ref = resolve_style_ref(style_ref)
    out = subdir("reference_grid", output_dir)
    result = _show_ref_grid.build_outputs(
        ref,
        out,
        scale=scale,
        readable_scale=readable_scale,
    )
    return {
        "ref": str(result["ref"]),
        "size": result["size"],
        "scale": result["scale"],
        "readable_scale": result["readable_scale"],
        "paths": {
            "grid": str(result["grid"]),
            "readable": str(result["readable"]),
        },
    }


def generate_base_idle(
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
) -> dict[str, Any]:
    """Author the naked/base side-view idle as full layers + composed previews.

    Imports the user-authored 90×128 masks from ``spritemcp/base/`` into
    ``output_dir/base_idle_90x128/``. Requires ``base/eyes.png`` and always
    composites those white eye pixels onto the head layer and face previews.
    """
    out = default_base_idle_out_dir(output_dir)
    part_map, paths = _base_idle.build_outputs(out, scale=scale)
    box = part_map.opaque_bbox()
    return {
        "armature": ARMATURE_NAME,
        "width": part_map.width,
        "height": part_map.height,
        "pose": part_map.pose,
        "facing": part_map.facing,
        "view": VIEW_MODE,
        "storage": "layered",
        "layering": LAYERING_RULE,
        "draw_order": list(DRAW_ORDER),
        "export_layers": list(EXPORT_LAYER_NAMES),
        "opaque_height_px": part_map.height_px(),
        "opaque_bbox": (
            None
            if box is None
            else {"x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]}
        ),
        "parts_used": part_map.used_parts(),
        "preview_paths": {
            "layers_grid": str(paths["layers_grid"]),
            "layers_strip": str(paths["layers_strip"]),
            "compose_preview": str(paths["compose_preview"]),
            "part_map_grid": str(paths["part_map_grid"]),
            "silhouette": str(paths["silhouette"]),
        },
        "paths": {key: str(path) for key, path in paths.items()},
    }


def generate_pivots(
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
) -> dict[str, Any]:
    """Compute articulation pivots on the base idle map; write JSON + overlays."""
    out = default_base_idle_out_dir(output_dir)
    _pm, pivots, paths = _pivots.build_outputs(out, scale=scale)
    return {
        "armature": ARMATURE_NAME,
        "joint_count": len(pivots),
        "joints": [
            {
                "name": p.name,
                "parent": p.parent,
                "child": p.child,
                "x": p.x,
                "y": p.y,
                "method": p.method,
                "contact_pairs": p.contact_pairs,
            }
            for p in pivots
        ],
        "paths": {key: str(path) for key, path in paths.items()},
    }


def run_demo(
    *,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Paint the tiny palette test pattern; write under ``output_dir/demo/``."""
    out = subdir("demo", output_dir)
    # demo.main hardcodes OUTPUT_DIR; call build helpers directly.
    out.mkdir(parents=True, exist_ok=True)
    canvas = _demo.build_demo_canvas()
    from .export import export_all, swatch_image
    from .palette import CHARACTER_PALETTE, PRIMARY_ROLES

    paths = export_all(canvas, out, composite_name="preview.png")
    swatch_path = out / "palette_swatch.png"
    swatch_image([(role, CHARACTER_PALETTE[role]) for role in PRIMARY_ROLES]).save(
        swatch_path
    )
    return {
        "paths": {
            **{key: str(path) for key, path in paths.items()},
            "palette_swatch": str(swatch_path),
        },
        "output_dir": str(resolve_output_dir(output_dir)),
    }


def generate_character(
    name: str,
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
) -> dict[str, Any]:
    """Generate fixed base idle + pivots for a named character.

    Writes ``output/characters/<name>/base/`` (part map, silhouette, pivots,
    previews). Returns structured parts/joints JSON **and** preview image paths
    (schema ``spritemcp.generate_character.v1``).

    Always uses the authored 90×128 armature from ``spritemcp/base/``
    (including required ``eyes.png`` composited onto the head).
    """
    return _character.generate_character(
        name, output_dir=output_dir, scale=scale
    )


def plan_animation(
    name: str,
    animation_name: str,
    intent: str,
    plan: Mapping[str, Any],
    *,
    contrast_with: Sequence[str] | None = None,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Validate + lock an agent-authored animation plan before any frames.

    The **calling agent** must fill ``plan`` with:
    - ``beats``: 8 entries ``{frame_index, pose_description, primary_joints}``
    - ``silhouette_story``: short paragraph for the user
    - ``differs_from``: map (or list) explaining difference vs other clips
    - ``weapon_or_style_notes``: e.g. sword cut vs punch

    Rejects empty/generic plans (including fight clones for sword-style names).
    Saves ``characters/<name>/anims/<animation_name>/plan.json``.

    Returns ``plan_id``, ``user_facing_summary`` (MUST show the user), and
    ``next_step``. Use ``get_animation_plan`` to re-read a locked plan.
    """
    return _animation_plan.plan_animation(
        name,
        animation_name,
        intent,
        plan,
        contrast_with=contrast_with,
        output_dir=output_dir,
    )


def get_animation_plan(
    name: str,
    animation_name: str,
    *,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Return the locked ``plan.json`` for a character animation."""
    return _animation_plan.get_animation_plan(
        name, animation_name, output_dir=output_dir
    )


def build_frame_animation(
    name: str,
    animation_name: str,
    frame_index: int,
    rotations: Mapping[str, float] | None = None,
    *,
    plan_id: str | None = None,
    output_dir: Path | str | None = None,
    scale: int = 8,
) -> dict[str, Any]:
    """Draft one 8-frame animation cell with hierarchical joint rotations.

    **Gate:** refuses unless ``plan_animation`` already locked a valid plan for
    this character+animation. Optional ``plan_id`` must match the stored plan.
    If painted design / outfit plan exists, also requires ``compose_character``.

    Never build frames until the plan's ``user_facing_summary`` was shown to
    the user.

    When design layers exist, the draft PNG is **dressed**: base parts + outfit
    layers rigid-rotated with the same pivot transforms as their parent body
    parts (DRAW_ORDER / draw_after / overhang). No design → base-only as before.

    ``rotations`` example: ``{"hip_near": 15, "knee_near": -20}``.
    Joint names: ``neck``, ``shoulder_far``, ``shoulder_near``, ``elbow_far``,
    ``elbow_near``, ``hip_far``, ``hip_near``, ``knee_far``, ``knee_near``
    (also ``get_joint_docs()`` / ``JOINT_ORDER``).

    Angles are absolute degrees from rest. Unspecified joints inherit the
    start pose (rest for frame 0; previous finished or draft for frame > 0).
    Does **not** finalize — call ``finish_frame_animation`` to lock.
    """
    return _character.build_frame_animation(
        name,
        animation_name,
        frame_index,
        rotations,
        plan_id=plan_id,
        output_dir=output_dir,
        scale=scale,
    )


def finish_frame_animation(
    name: str,
    animation_name: str,
    frame_index: int,
    *,
    plan_id: str | None = None,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Lock the current draft frame as final under ``anims/<animation_name>/``.

    **Gate:** refuses without a valid ``plan.json`` (same as build). Optional
    ``plan_id`` must match when provided.

    Writes ``frame_XX.png`` + ``poses/frame_XX.json``. When frame 7 is finished
    and all eight finals exist, also writes ``<animation_name>_contact_sheet.png``
    and ``<animation_name>_preview.gif``. Advances readiness for the next frame
    (schema ``spritemcp.finish_frame_animation.v1``).
    """
    return _character.finish_frame_animation(
        name,
        animation_name,
        frame_index,
        plan_id=plan_id,
        output_dir=output_dir,
    )


def export_animation_preview(
    name: str,
    animation_name: str,
    *,
    duration_ms: int = 100,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Rebuild contact sheet + looping preview GIF from finished frames 0..7.

    Requires all eight final ``frame_XX.png`` files. Writes
    ``<animation_name>_contact_sheet.png`` and ``<animation_name>_preview.gif``
    under ``anims/<animation_name>/``. GIF scale is locked to native 90×128
    (``ANIMATION_PREVIEW_SCALE``). Also runs automatically from
    ``finish_frame_animation`` when frame 7 completes.
    """
    return _character.export_animation_preview(
        name,
        animation_name,
        duration_ms=duration_ms,
        output_dir=output_dir,
    )


def list_outfit_slot_specs() -> list[dict[str, Any]]:
    """Structural design layers: the 10 EXPORT_LAYER_NAMES (no style presets).

    Same names as ``base/layers/``. Each has a single pose parent and
    ``body_parts`` for paint/ref. All layers allow overhang.
    """
    return _outfit.list_outfit_slot_specs()


def plan_outfit(
    name: str,
    brief: str,
    plan: Mapping[str, Any],
    *,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Validate + lock an agent-authored outfit plan before painting layers.

    The **calling agent** fills ``plan`` from the free ``brief`` (e.g. samurai):
    - ``overall_look``: user-facing dressed silhouette paragraph (overhang OK)
    - ``slots``: every EXPORT_LAYER_NAMES key with ``skip`` + ``visual_notes``
      (upper_arm_far … lower_leg_near). Conceptual notes (jingasa/do/hakama)
      OK; paint targets are the 10 layer files.

    Clothing may extend outside body silhouette margins; body parents are
    alignment refs, not clip masks. No hardcoded outfit styles — notes drive
    painting. Saves ``characters/<name>/design/plan.json``. Returns
    ``user_facing_summary`` (MUST show the user before painting).
    """
    return _outfit.plan_outfit(name, brief, plan, output_dir=output_dir)


def get_outfit_plan(
    name: str,
    *,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Return the locked outfit ``plan.json`` for a character."""
    return _outfit.get_outfit_plan(name, output_dir=output_dir)


def plan_shading(
    name: str,
    brief: str,
    plan: Mapping[str, Any],
    *,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Validate + lock an agent-authored shading plan after flat outfit paint.

    The **calling agent** fills ``plan`` from ``brief``:
    - ``light_direction``: e.g. \"top-front / +X-up\"
    - ``rules``: must include ``one_step_darker`` and ``no_gradients``
    - ``overall_notes`` (optional): short paragraph
    - ``layers``: every EXPORT_LAYER_NAMES key with ``skip`` + ``shade_notes``
      and optional ``strokes`` ``[{region?, intent, color_role?}]``

    Alternate: ``strokes`` as a flat list with ``layer`` + ``intent`` (tool
    expands into per-layer entries).

    Saves ``characters/<name>/design/shading_plan.json``. Returns
    ``user_facing_summary`` (MUST show the user before painting shadows).
    Soft gate — ``compose_character`` does not require this plan.
    """
    return _shading.plan_shading(name, brief, plan, output_dir=output_dir)


def get_shading_plan(
    name: str,
    *,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Return the locked ``design/shading_plan.json`` for a character."""
    return _shading.get_shading_plan(name, output_dir=output_dir)


def suggest_shade_regions(
    name: str,
    *,
    output_dir: Path | str | None = None,
    band_height: int = 2,
) -> dict[str, Any]:
    """Propose underside / far-limb shade band hints (no pixels written).

    Uses design-layer alpha when painted, else body-part geometry. Agent
    filters regions into ``plan_shading``, then paints with MCP tools.
    """
    return _shading.suggest_shade_regions(
        name, output_dir=output_dir, band_height=band_height
    )


def get_shade_palette() -> dict[str, Any]:
    """Local flat color → one-step-darker shadow roles (style-lock shading)."""
    return _shading.get_shade_palette()


def prepare_outfit_slot_reference(
    name: str,
    slot: str,
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Mandatory body-under reference stack before/during painting one layer.

    ``slot`` is an EXPORT_LAYER_NAMES key. Writes ``design/refs/<layer>_ref.png``
    = that layer's ``body_parts`` underneath + current design on top. Body
    underlay is alignment only — not a clip mask; paint may overhang. Persist
    paint only on ``design/layers/<layer>.png``.
    """
    return _outfit.prepare_outfit_slot_reference(
        name,
        slot,
        output_dir=output_dir,
        scale=scale,
        plan_id=plan_id,
    )


def generate_outfit_slot(
    name: str,
    slot: str,
    *,
    design_image_path: str | Path | None = None,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Optional import/finalize for one outfit design layer + refresh body-under ref.

    Preferred agent flow paints with MCP tools (``paint_pixels``, ``fill_rect``,
    ``paint_from_commands``, …) which already write ``design/layers/<slot>.png``
    and refresh refs. Use this tool to import an existing RGBA PNG via
    ``design_image_path``, or to re-sync refs after external edits. Does not
    clip to body alpha.
    """
    return _outfit.generate_outfit_slot(
        name,
        slot,
        design_image_path=design_image_path,
        output_dir=output_dir,
        scale=scale,
        plan_id=plan_id,
    )


def paint_pixels(
    name: str,
    slot: str,
    pixels: Sequence[Mapping[str, Any]],
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Set pixels on ``design/layers/<slot>.png``; refresh body-under ref preview."""
    return _paint.paint_pixels(
        name,
        slot,
        pixels,
        output_dir=output_dir,
        scale=scale,
        plan_id=plan_id,
    )


def set_pixels(
    name: str,
    slot: str,
    pixels: Sequence[Mapping[str, Any]],
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Alias for ``paint_pixels``."""
    return _paint.set_pixels(
        name,
        slot,
        pixels,
        output_dir=output_dir,
        scale=scale,
        plan_id=plan_id,
    )


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
    """Fill body-part silhouettes onto the design layer (replaces Shell/PIL scripts).

    Uses ``part_map.part_pixels`` server-side. Omit ``parts`` to fill the slot's
    parent body parts. Then add details with ``fill_rect`` / ``draw_line``.
    """
    return _paint.fill_parts_on_slot(
        name,
        slot,
        color,
        parts=parts,
        part_ids=part_ids,
        part_colors=part_colors,
        output_dir=output_dir,
        scale=scale,
        plan_id=plan_id,
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
    """Fill a rectangle on the outfit design layer; refresh ref preview."""
    return _paint.fill_rect(
        name,
        slot,
        x,
        y,
        width,
        height,
        color,
        output_dir=output_dir,
        scale=scale,
        plan_id=plan_id,
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
    """Stroke a rectangle outline on the outfit design layer."""
    return _paint.stroke_rect(
        name,
        slot,
        x,
        y,
        width,
        height,
        color,
        thickness=thickness,
        output_dir=output_dir,
        scale=scale,
        plan_id=plan_id,
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
    """Draw a line on the outfit design layer."""
    return _paint.draw_line(
        name,
        slot,
        x0,
        y0,
        x1,
        y1,
        color,
        thickness=thickness,
        output_dir=output_dir,
        scale=scale,
        plan_id=plan_id,
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
    """Fill an ellipse on the outfit design layer."""
    return _paint.fill_ellipse(
        name,
        slot,
        x,
        y,
        width,
        height,
        color,
        output_dir=output_dir,
        scale=scale,
        plan_id=plan_id,
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
    """Erase a rectangle on the design layer only (transparent)."""
    return _paint.clear_rect(
        name,
        slot,
        x,
        y,
        width,
        height,
        output_dir=output_dir,
        scale=scale,
        plan_id=plan_id,
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
    """Flood-fill on the design layer; warns if a huge area is filled."""
    return _paint.flood_fill(
        name,
        slot,
        x,
        y,
        color,
        output_dir=output_dir,
        scale=scale,
        plan_id=plan_id,
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
    """Sample a region of the design layer (read-modify)."""
    return _paint.get_layer_pixels(
        name,
        slot,
        x=x,
        y=y,
        width=width,
        height=height,
        opaque_only=opaque_only,
        output_dir=output_dir,
        plan_id=plan_id,
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
    """Batch paint commands on one slot; single save + ref refresh."""
    return _paint.paint_from_commands(
        name,
        slot,
        commands,
        output_dir=output_dir,
        scale=scale,
        plan_id=plan_id,
    )


def list_outfit_layers(
    name: str,
    *,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """List outfit slots, parent parts, paint status, and layer/ref paths."""
    return _outfit.list_outfit_layers(name, output_dir=output_dir)


def clear_outfit_slot(
    name: str,
    slot: str,
    *,
    output_dir: Path | str | None = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Blank one slot's design layer (transparent) and refresh its reference."""
    return _outfit.clear_outfit_slot(
        name, slot, output_dir=output_dir, plan_id=plan_id
    )


def compose_character(
    name: str,
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
    include_skipped: bool = False,
) -> dict[str, Any]:
    """Compose dressed rest = base body + design layers under ``design/``.

    Required before animation when outfit design exists. Full design layers (no
    clip-to-body); each file matches an EXPORT_LAYER_NAMES key and rigid-rotates
    1:1 with that body part in animation. Recommended: run ``plan_shading`` +
    shadow paint on design layers before this (or re-compose after shading).
    Soft gate — does not refuse when no shading plan exists.

    After compose, ask the user if they want manual pixel edits; if yes call
    ``open_pixel_editor`` then re-compose before ``plan_animation``.
    """
    return _outfit.compose_character(
        name,
        output_dir=output_dir,
        scale=scale,
        include_skipped=include_skipped,
    )


def open_pixel_editor(
    name: str,
    *,
    output_dir: Path | str | None = None,
    port: int = 0,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Open the local manual pixel editor for a character's design layers.

    Human exception path after shading + ``compose_character``. Apply writes
    ``design/layers/<layer>.png`` only (never ``base/``). After Apply, call
    ``compose_character`` again before ``plan_animation``.
    """
    return _pixel_editor.open_pixel_editor(
        name,
        output_dir=output_dir,
        port=port,
        open_browser=open_browser,
    )


def migrate_design_to_export_layers(
    name: str,
    *,
    output_dir: Path | str | None = None,
    delete_legacy: bool = True,
) -> dict[str, Any]:
    """Migrate legacy clothing-slot design PNGs to the 10 export layers."""
    return _outfit.migrate_design_to_export_layers(
        name, output_dir=output_dir, delete_legacy=delete_legacy
    )


__all__ = [
    "JOINT_ORDER",
    "build_frame_animation",
    "clear_outfit_slot",
    "clear_output_root",
    "clear_rect",
    "compose_character",
    "draw_line",
    "fill_ellipse",
    "fill_parts_on_slot",
    "fill_rect",
    "finish_frame_animation",
    "export_animation_preview",
    "flood_fill",
    "generate_base_idle",
    "generate_character",
    "generate_outfit_slot",
    "generate_pivots",
    "get_animation_plan",
    "get_default_paths",
    "get_draw_order",
    "get_frame_count",
    "get_joint_docs",
    "get_joints",
    "get_layer_pixels",
    "get_outfit_plan",
    "get_output_root",
    "get_part_ids",
    "get_shade_palette",
    "get_shading_plan",
    "get_view_lock",
    "list_outfit_layers",
    "list_outfit_slot_specs",
    "migrate_design_to_export_layers",
    "open_pixel_editor",
    "paint_from_commands",
    "paint_pixels",
    "plan_animation",
    "plan_outfit",
    "plan_shading",
    "prepare_outfit_slot_reference",
    "run_demo",
    "set_output_root",
    "set_pixels",
    "show_reference_grid",
    "stroke_rect",
    "suggest_shade_regions",
]
