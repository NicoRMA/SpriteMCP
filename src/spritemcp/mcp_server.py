"""MCP server for SpriteMCP — stdio transport for Cursor agents.

Wraps ``spritemcp.api`` as tools. Returns JSON-serializable dicts; image
outputs include absolute/relative path strings under ``paths`` /
``preview_paths``. Agents can inspect those PNGs with the Read tool.

This module's FastMCP ``instructions`` are the durable SOP for any agent —
do not rely on a prior chat. Keep them concise and self-contained.
"""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import api

# First-class outfit paint (agents must NOT fall back to Shell/PIL).
# Cursor catalogs that show ~28 tools are STALE (pre-paint); restart MCP.
_PAINT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "paint_pixels",
        "set_pixels",
        "fill_rect",
        "stroke_rect",
        "draw_line",
        "fill_ellipse",
        "clear_rect",
        "flood_fill",
        "get_layer_pixels",
        "paint_from_commands",
        "fill_parts_on_slot",
    }
)

# Must stay registered on the FastMCP instance Cursor loads. main() refuses
# to serve if any of these are missing (guards stale processes / wrong module).
_REQUIRED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "set_output_root",
        "get_output_root",
        "clear_output_root",
        "list_outfit_slot_specs",
        "plan_outfit",
        "get_outfit_plan",
        "plan_shading",
        "get_shading_plan",
        "suggest_shade_regions",
        "get_shade_palette",
        "prepare_outfit_slot_reference",
        "generate_outfit_slot",
        "list_outfit_layers",
        "clear_outfit_slot",
        "compose_character",
        "generate_character",
        "open_pixel_editor",
        "plan_animation",
        "build_frame_animation",
        "finish_frame_animation",
        "export_animation_preview",
        "list_registered_tools",
        *_PAINT_TOOL_NAMES,
    }
)

# Decorated @mcp.tool count in this module (outfit+anim+paint+shading). Cursor UI
# showing fewer (e.g. 28) usually means Cursor reused a stale tools/list
# lease and skipped ListToolsRequest after restart — bump
# SPRITEMCP_CATALOG_EPOCH in .cursor/mcp.json and restart SpriteMCP.
_EXPECTED_TOOL_COUNT = 45

# Durable cold-start SOP (English). Shown to every Cursor agent via MCP
# server instructions — not conversation history.
_MCP_INSTRUCTIONS = (
    "SpriteMCP (outfit+anim+shading). Use THESE MCP tools only — never "
    "Shell, never python -c, never import spritemcp.api as a substitute. "
    "Expected live tool_count=45 including 11 paint tools "
    "(fill_parts_on_slot, paint_pixels, set_pixels, fill_rect, stroke_rect, "
    "draw_line, fill_ellipse, clear_rect, flood_fill, get_layer_pixels, "
    "paint_from_commands) plus open_pixel_editor. If Cursor catalog shows "
    "~28 tools or "
    "plan_outfit / plan_shading / compose_character / paint_pixels / fill_rect / "
    "fill_parts_on_slot are missing "
    "from the tool list, stop and tell the user to restart SpriteMCP "
    "(catalog is stale).\n"
    "\n"
    "Pixel sprite toolkit (side view +X only). Any character name works; "
    "there are no hardcoded styles or character-specific code paths.\n"
    "\n"
    "OUTPUT PATH: default writes under <process cwd>/output/ (the agent/MCP "
    "working directory — usually the Cursor project), NOT the package install "
    "folder. Optional SPRITE_GEN_OUTPUT_ROOT env at MCP start seeds the "
    "session root. Call set_output_root(absolute_or_relative_path) once "
    "(session-persisted; creates dirs) OR pass output_dir on each write tool "
    "(generate_character, outfit, plan_animation, build/finish). If unsure "
    "the active root is the workspace project, call set_output_root to "
    "<workspace_absolute>/output before generate_character. "
    "get_default_paths / get_output_root show the active root. Existing "
    "characters under an old path stay valid if you keep that root.\n"
    "\n"
    "SHARED ARMATURE: every character copies the same naked base idle + "
    "pivots under characters/<name>/base/. Canvas is authored 90x128 "
    "(from spritemcp/base/ → output/base_idle_90x128/). Authored base/"
    "eyes.png is REQUIRED and always composited onto the head (white face "
    "detail, not a part id) — naked/base always shows eyes; design head "
    "layers (helmet/hair) may cover them at compose. Visual identity "
    "is ONLY painted "
    "design layers under design/ — never rewrite base geometry per style.\n"
    "\n"
    "DISK LAYOUT: <output_root>/characters/<name>/{base, design, anims/<anim>/}. "
    "design/ has plan.json, shading_plan.json (optional), layers/<layer>.png "
    "(same 10 names as base/layers/), refs/<layer>_ref.png, compose_preview.png. "
    "anims/<anim>/ has plan.json + frame_00..07.\n"
    "\n"
    "MANDATORY PIPELINE (do not skip gates):\n"
    "0) Ensure output root is the agent workspace project "
    "(cwd/output, or set_output_root to <workspace>/output). If unsure, "
    "set_output_root to the workspace absolute path + /output.\n"
    "1) generate_character(name)\n"
    "2) plan_outfit(name, brief, plan) — agent authors the 10 export-layer "
    "notes from a free brief (e.g. samurai); conceptual words like jingasa/do/"
    "hakama OK in notes, but slot keys ARE export layers; SHOW "
    "user_facing_summary; wait for OK\n"
    "3) list_outfit_slot_specs if needed. Design layers = EXPORT_LAYER_NAMES: "
    "upper_arm_far, lower_arm_far, upper_leg_far, lower_leg_far, torso, head, "
    "upper_leg_near, lower_leg_near, upper_arm_near, lower_arm_near "
    "(lower_* includes hand/foot). Per active layer: "
    "prepare_outfit_slot_reference → paint with MCP paint tools "
    "(fill_parts_on_slot for that layer's body_parts; paint_pixels / fill_rect / "
    "stroke_rect / draw_line / fill_ellipse / clear_rect / paint_from_commands "
    "for details; flood_fill optional with caution; get_layer_pixels for "
    "read-modify) onto design/layers/<layer>.png. "
    "generate_outfit_slot is OPTIONAL (import existing PNG / refresh refs "
    "only) — paint tools already write the layer and refresh previews. "
    "Flat local colors only here — do NOT bake shading in fill_parts_on_slot.\n"
    "4) RECOMMENDED shading pass (soft gate — compose does not refuse if "
    "skipped): optional suggest_shade_regions(name) for underside/far hints → "
    "plan_shading(name, brief, plan) — SHOW user_facing_summary; wait for OK → "
    "paint one-step-darker hard shadows on design layers only (get_shade_palette "
    "/ SHADE_STEP; no gradients; light = top-front / +X-up → undersides, under "
    "chin, armpits, folds, slightly darker far, under feet). Never rewrite "
    "base/; do not strip eyes.\n"
    "5) compose_character — dressed rest preview REQUIRED before animation "
    "when design exists (re-compose after shading)\n"
    "6) HUMAN EDIT GATE (required ask): after compose, ASK the user if they "
    "want to edit anything manually. If no → continue to plan_animation. "
    "If yes → open_pixel_editor(name) (opens local browser UI; pencil / "
    "eyedropper / eraser on the 10 design layers; Apply writes "
    "design/layers/*.png only — never base/) → wait for user Apply → "
    "re-run compose_character → ask again or continue.\n"
    "7) plan_animation — SHOW user_facing_summary; wait for OK\n"
    "8) (build_frame_animation → finish_frame_animation) × 8 frames (0..7). "
    "Finishing frame 7 also writes <anim>_contact_sheet.png + <anim>_preview.gif "
    "(re-run with export_animation_preview). Preview GIFs are ALWAYS native "
    "90x128 (ANIMATION_PREVIEW_SCALE=1) — never pass a custom GIF scale; every "
    "clip must match. "
    "Frames are DRESSED: each design layer rigid-rotates 1:1 with that body "
    "part (NO multi-parent far+near split). No design → base-only.\n"
    "\n"
    "OUTFIT PAINT (REQUIRED path):\n"
    "- FORBIDDEN: Shell python, ad-hoc PIL scripts, GenerateImage / OpenAI "
    "image / any external image generator for outfit layers.\n"
    "- REQUIRED: MCP paint tools + prepare_outfit_slot_reference + "
    "compose_character.\n"
    "- Exception: open_pixel_editor is the human-only manual edit path after "
    "shading+compose (not a substitute for agent paint tools).\n"
    "- Typical clothing fill: fill_parts_on_slot(name, layer, color) stamps "
    "that export layer's body_parts onto design/layers/<layer>.png; then "
    "fill_rect / draw_line / paint_pixels for belt/cross/plates/overhang.\n"
    "- Map conceptually: headwear→head, do→torso, sleeves→arm layers, "
    "hakama→leg layers, boots→lower_leg_*, gloves→lower_arm_* "
    "(hand/foot included in lower_* export layers).\n"
    "- Body underlay for that ONE layer sits UNDER the transparent design "
    "layer (alignment/proportions only — NOT a clip mask). Clothing MAY "
    "overhang body silhouette if scale stays sensible. Persist paint only in "
    "design/layers/.\n"
    "- After each paint op, Read preview_paths.reference (or "
    "reference_preview) from the tool response.\n"
    "- Do NOT use flood_fill of the entire body silhouette as the sole design.\n"
    "\n"
    "SHADING (recommended after flat outfit paint):\n"
    "- plan_shading → show summary → paint shadows with existing paint_* "
    "tools → compose_character.\n"
    "- Style lock: ONE darker step of local color (get_shade_palette); hard "
    "bands only — no soft gradients.\n"
    "- Shade design layers only; eyes stay on base head.\n"
    "\n"
    "ANIMATION: joint angles are absolute degrees from rest. Pose = rigid "
    "full-layer rotate around articulation pivots (hierarchical parenting), "
    "not freehand redraw. Design clothes follow the same transforms. Call "
    "get_joints / get_joint_docs / get_draw_order. For +X idle: negative "
    "hip≈forward, positive knee≈flexion. Never build frames without a locked "
    "anims/<anim>/plan.json. Never invent if-style code paths — brief + "
    "painted layers only.\n"
    "\n"
    "PREVIEW: tool results include PNG path strings — Read those images. "
    "Call list_registered_tools once if unsure the catalog includes outfit "
    "paint tools. After code changes to this MCP server, restart the "
    "SpriteMCP so tools/instructions refresh."
)

mcp = FastMCP(
    "SpriteMCP",
    instructions=_MCP_INSTRUCTIONS,
)

_PREVIEW_NOTE = (
    "Preview/final PNGs are on disk. Use the Cursor Read tool on path strings "
    "under paths / preview_paths to inspect images."
)


def _with_preview_hint(result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    out.setdefault("_agent_note", _PREVIEW_NOTE)
    return out


@mcp.tool()
def list_registered_tools() -> dict[str, Any]:
    """Return every tool name registered on this live MCP process.

    Cold-start check: expected_tool_count=45 and must include plan_outfit,
    plan_shading, compose_character, open_pixel_editor, fill_parts_on_slot,
    paint_pixels, fill_rect, set_output_root, etc. If Cursor shows ~28 tools
    or paint_* are missing from the agent catalog, restart SpriteMCP — do
    not fall back to Shell/API/GenerateImage.
    """
    names = sorted(_tool_manager_names())
    name_set = set(names)
    missing = sorted(_REQUIRED_TOOL_NAMES - name_set)
    missing_paint = sorted(_PAINT_TOOL_NAMES - name_set)
    paint_ok = not missing_paint
    count_ok = len(names) == _EXPECTED_TOOL_COUNT
    return {
        "module_file": __file__,
        "tool_count": len(names),
        "expected_tool_count": _EXPECTED_TOOL_COUNT,
        "tools": names,
        "paint_tools": sorted(_PAINT_TOOL_NAMES),
        "paint_tools_present": paint_ok,
        "outfit_tools_present": not missing,
        "missing_required": missing,
        "missing_paint": missing_paint,
        "catalog_ok": paint_ok and count_ok and not missing,
        "_agent_note": (
            "Live process has tool_count=45 with paint+shading+open_pixel_editor. "
            "If Cursor "
            "Settings / agent catalog still shows ~28 tools, Cursor skipped "
            "tools/list after reconnect (stale lease). Fix: Settings → MCP → "
            "restart SpriteMCP AFTER bumping SPRITEMCP_CATALOG_EPOCH in "
            ".cursor/mcp.json; if still stale, delete the project mcps cache "
            "folder then reload the window."
            if paint_ok and count_ok
            else "Paint/required tools missing in this process — wrong "
            "entrypoint or failed registration; fix mcp.json / restart."
        ),
    }


def _tool_manager_names() -> list[str]:
    return [t.name for t in mcp._tool_manager.list_tools()]  # noqa: SLF001


@mcp.tool()
def get_part_ids() -> dict[str, int]:
    """Stable part name → integer id map for the base idle template."""
    return api.get_part_ids()


@mcp.tool()
def get_joints() -> list[dict[str, str]]:
    """Hierarchical joint specs: name, parent part, child part."""
    return api.get_joints()


@mcp.tool()
def get_joint_docs() -> list[dict[str, str]]:
    """Joint name, parent, child, and which parts each joint rotates."""
    return api.get_joint_docs()


@mcp.tool()
def get_draw_order() -> list[str]:
    """Side-view compose order: far limbs, torso/head, near legs, near arms."""
    return api.get_draw_order()


@mcp.tool()
def get_view_lock() -> dict[str, Any]:
    """Permanent view + layering rules (side profile; far behind, near in front)."""
    return api.get_view_lock()


@mcp.tool()
def get_default_paths() -> dict[str, str]:
    """Active output root, style ref, and common subdirs.

    Default is <cwd>/output/ (agent/MCP working directory). Optional
    SPRITE_GEN_OUTPUT_ROOT env seeds the session at start. Call
    set_output_root(path) or pass output_dir on write tools to redirect.
    Characters live under <output_root>/characters/<name>/{base,design,anims}.
    """
    return api.get_default_paths()


@mcp.tool()
def set_output_root(path: str) -> dict[str, str]:
    """Set where character/outfit/anim output is written for this MCP session.

    Resolves to an absolute path and creates the directory. Persists until the
    MCP process exits or clear_output_root(). Does NOT move existing files —
    only redirects new writes. Per-call output_dir still overrides.
    Example: set_output_root("C:/Users/me/game/assets/art/sprites").
    When unset, tools use <cwd>/output/.
    """
    return api.set_output_root(path)


@mcp.tool()
def get_output_root() -> dict[str, Any]:
    """Return the absolute output root currently in effect (session or default)."""
    return api.get_output_root()


@mcp.tool()
def clear_output_root() -> dict[str, str]:
    """Clear session output override; restore <cwd>/output default."""
    return api.clear_output_root()


@mcp.tool()
def get_frame_count() -> int:
    """Number of frames per character animation (always 8; indices 0..7)."""
    return api.get_frame_count()


@mcp.tool()
def list_outfit_slot_specs() -> list[dict[str, Any]]:
    """Structural design layers = the 10 EXPORT_LAYER_NAMES (no style presets).

    Same names as base/layers/: upper_arm_far, lower_arm_far, upper_leg_far,
    lower_leg_far, torso, head, upper_leg_near, lower_leg_near, upper_arm_near,
    lower_arm_near (lower_* includes hand/foot). Each layer has a single pose
    parent and body_parts for paint/ref underlay. Conceptual notes
    (jingasa/do/hakama) map onto these keys — paint targets are always the
    10 layer files. Every layer allows overhang.
    """
    return api.list_outfit_slot_specs()


@mcp.tool()
def show_reference_grid(
    style_ref: str | None = None,
    output_dir: str | None = None,
    scale: int = 8,
    readable_scale: int = 12,
) -> dict[str, Any]:
    """Upscale the style-ref PNG onto a visible per-pixel grid.

    Writes under output_dir/reference_grid/. Returns path strings for grid PNGs.
    """
    return _with_preview_hint(
        api.show_reference_grid(
            style_ref=style_ref,
            output_dir=output_dir,
            scale=scale,
            readable_scale=readable_scale,
        )
    )


@mcp.tool()
def generate_base_idle(
    output_dir: str | None = None,
    scale: int = 8,
) -> dict[str, Any]:
    """Author shared naked side-view idle as full separate layers + composed previews.

    Imports user-authored 90x128 masks from spritemcp/base/ into
    output_dir/base_idle_90x128/. Prefer generate_character for named chars.
    """
    return _with_preview_hint(
        api.generate_base_idle(output_dir=output_dir, scale=scale)
    )


@mcp.tool()
def generate_pivots(
    output_dir: str | None = None,
    scale: int = 8,
) -> dict[str, Any]:
    """Compute articulation pivots on the shared base idle map.

    Prefer generate_character for named chars.
    """
    return _with_preview_hint(
        api.generate_pivots(output_dir=output_dir, scale=scale)
    )


@mcp.tool()
def run_demo(output_dir: str | None = None) -> dict[str, Any]:
    """Paint the tiny palette test pattern under output_dir/demo/."""
    return _with_preview_hint(api.run_demo(output_dir=output_dir))


@mcp.tool()
def generate_character(
    name: str,
    output_dir: str | None = None,
    scale: int = 8,
) -> dict[str, Any]:
    """Copy the SHARED naked side-view armature into a named character folder.

    All characters share the same authored 90x128 base idle + pivots; do not
    invent a per-style skeleton. Writes
    <output_root>/characters/<name>/base/ (part map, layers, silhouette,
    pivots, previews). Optional output_dir overrides the session root from
    set_output_root. Call once before plan_outfit / animation.
    """
    return _with_preview_hint(
        api.generate_character(name, output_dir=output_dir, scale=scale)
    )


@mcp.tool()
def plan_outfit(
    name: str,
    brief: str,
    plan: dict[str, Any],
    output_dir: str | None = None,
) -> dict[str, Any]:
    """REQUIRED before painting clothes: lock an outfit plan from a free brief.

    YOU (the agent) author the plan from ``brief`` (e.g. "samurai") — no
    hardcoded style tables. Do NOT invent if-samurai code paths.

    Required plan keys:
    - overall_look: short paragraph of the dressed silhouette (may note
      overhang such as helmet bulk or hat brim past the head)
    - slots: object with ALL 10 EXPORT_LAYER_NAMES —
      upper_arm_far, lower_arm_far, upper_leg_far, lower_leg_far, torso, head,
      upper_leg_near, lower_leg_near, upper_arm_near, lower_arm_near —
      each {skip: bool, visual_notes: str}. Conceptual vocabulary (jingasa,
      do, hakama, boots, gloves) belongs in notes; paint keys are the layers.
      lower_* includes hand/foot (gloves/boots paint there).

    Overhang rule: clothing MAY extend outside body silhouette margins;
    parent body parts are alignment/proportion reference, NOT a hard clip.
    Keep proportions sensible (not absurd scale).

    Saves characters/<name>/design/plan.json. Returns plan_id +
    user_facing_summary — SHOW the summary to the user before painting.
    Next: prepare_outfit_slot_reference → MCP paint tools (flat local colors;
    do NOT bake shading in fill_parts_on_slot) → recommended plan_shading →
    shadow paint → compose_character.
    FORBIDDEN: Shell/PIL scripts or external image generators.
    """
    return api.plan_outfit(name, brief, plan, output_dir=output_dir)


@mcp.tool()
def get_outfit_plan(
    name: str,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Read the locked design/plan.json for a character outfit."""
    return api.get_outfit_plan(name, output_dir=output_dir)


@mcp.tool()
def plan_shading(
    name: str,
    brief: str,
    plan: dict[str, Any],
    output_dir: str | None = None,
) -> dict[str, Any]:
    """RECOMMENDED after flat outfit paint: lock where hard shadows go.

    YOU (the agent) author the plan — this tool validates and stores it.
    Soft gate: compose_character does NOT refuse without shading, but MCP
    SOP expects plan_shading → paint shadows → compose (or re-compose).

    Required plan keys:
    - light_direction: e.g. "top-front / +X-up"
    - rules: must include "one_step_darker" and "no_gradients"
    - layers: object with ALL 10 EXPORT_LAYER_NAMES, each
      {skip: bool, shade_notes: str, strokes?: [{region?, intent, color_role?}]}
    Optional overall_notes. Alternate: flat strokes list with layer + intent.

    Style: one darker step of local color (see get_shade_palette); hard 1–2px
    bands; shade design layers only; never rewrite base; do not strip eyes.

    Saves design/shading_plan.json. Returns plan_id + user_facing_summary —
    SHOW the summary and wait for OK before painting shadows with paint_*.
    """
    return api.plan_shading(name, brief, plan, output_dir=output_dir)


@mcp.tool()
def get_shading_plan(
    name: str,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Read the locked design/shading_plan.json for a character."""
    return api.get_shading_plan(name, output_dir=output_dir)


@mcp.tool()
def suggest_shade_regions(
    name: str,
    output_dir: str | None = None,
    band_height: int = 2,
) -> dict[str, Any]:
    """Propose underside / far-limb shade band hints (does NOT write pixels).

    Uses design-layer alpha when painted, else body-part geometry. Returns
    structured regions (layer, bbox, sample_pixels, reason). Agent filters
    into plan_shading, then paints with MCP tools. Optional helper before
    plan_shading.
    """
    return api.suggest_shade_regions(
        name, output_dir=output_dir, band_height=band_height
    )


@mcp.tool()
def get_shade_palette() -> dict[str, Any]:
    """Local flat color → one-step-darker shadow roles (style-lock shading).

    Example: off_white cloth → shadow_beige underside. Use these roles /
    hex values with paint_* after plan_shading. No soft gradients.
    """
    return api.get_shade_palette()


@mcp.tool()
def prepare_outfit_slot_reference(
    name: str,
    slot: str,
    output_dir: str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """MANDATORY before/during paint: body part layer(s) UNDER + design on TOP.

    ``slot`` is one of the 10 export layer names (same as base/layers/).
    Writes design/refs/<layer>_ref.png. That layer's body_parts are the underlay
    (alignment / proportions only — NOT a clip mask); design/layers/<layer>.png
    is the transparent paint target. Clothing MAY overhang the body silhouette
    if scale stays sensible. Example:
    prepare_outfit_slot_reference("<name>", "torso") → refs/torso_ref.png.
    Then paint with MCP fill_parts_on_slot (silhouette) + fill_rect /
    draw_line / paint_pixels (details) / paint_from_commands
    (never Shell/PIL or GenerateImage).
    """
    return _with_preview_hint(
        api.prepare_outfit_slot_reference(
            name,
            slot,
            output_dir=output_dir,
            scale=scale,
            plan_id=plan_id,
        )
    )


@mcp.tool()
def fill_parts_on_slot(
    name: str,
    slot: str,
    color: str | list[int] | None = None,
    parts: list[str] | None = None,
    part_ids: list[int] | None = None,
    part_colors: dict[str, str | list[int]] | None = None,
    output_dir: str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Fill body-part silhouettes onto design/layers/<layer>.png (MCP-native).

    REPLACES ad-hoc Shell/PIL scripts that looped part_pixels and stamped
    colors. Server uses the character part map; base body is never modified.

    - Omit parts/part_ids → fill this layer's body_parts (e.g. torso for torso,
      lower_arm_far+hand_far for lower_arm_far).
    - parts: names like \"torso\", \"head\" (see get_part_ids).
    - part_ids: integer ids from get_part_ids.
    - color: hex / [r,g,b,a] for all selected parts.
    - part_colors: optional {part_name: color} overrides (e.g. darker far legs).

    Flat local fill only — do NOT bake underside shading here; use
    plan_shading + paint_* after outfit paint. Then add details with
    fill_rect / draw_line / paint_pixels (belt, cross, nasal bar, plates).
    Overhang: paint outside silhouette with those tools.
    FORBIDDEN: Shell python/PIL scripts or external image generators.
    """
    return _with_preview_hint(
        api.fill_parts_on_slot(
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
    )


@mcp.tool()
def paint_pixels(
    name: str,
    slot: str,
    pixels: list[dict[str, Any]],
    output_dir: str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Paint individual pixels onto design/layers/<slot>.png (REQUIRED paint path).

    Each item: {x, y, color} where color is "#RRGGBB" / "#RRGGBBAA" /
    [r,g,b,a] / "transparent". Canvas coords match the character (body ref).
    Writes the design layer only; refreshes refs/<slot>_ref.png. Read
    preview_paths after. FORBIDDEN alternatives: Shell/PIL, GenerateImage.
    """
    return _with_preview_hint(
        api.paint_pixels(
            name,
            slot,
            pixels,
            output_dir=output_dir,
            scale=scale,
            plan_id=plan_id,
        )
    )


@mcp.tool()
def set_pixels(
    name: str,
    slot: str,
    pixels: list[dict[str, Any]],
    output_dir: str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Alias for paint_pixels — set {x,y,color} on the design layer."""
    return _with_preview_hint(
        api.set_pixels(
            name,
            slot,
            pixels,
            output_dir=output_dir,
            scale=scale,
            plan_id=plan_id,
        )
    )


@mcp.tool()
def fill_rect(
    name: str,
    slot: str,
    x: int,
    y: int,
    width: int,
    height: int,
    color: str | list[int],
    output_dir: str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Fill a rectangle on design/layers/<slot>.png; refresh body-under ref.

    color: hex / [r,g,b,a] / transparent. Overhang past body OK. Prefer this
    or paint_from_commands for blocky pixel-art clothing shapes.
    """
    return _with_preview_hint(
        api.fill_rect(
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
    )


@mcp.tool()
def stroke_rect(
    name: str,
    slot: str,
    x: int,
    y: int,
    width: int,
    height: int,
    color: str | list[int],
    thickness: int = 1,
    output_dir: str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Stroke (outline) a rectangle on the outfit design layer."""
    return _with_preview_hint(
        api.stroke_rect(
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
    )


@mcp.tool()
def draw_line(
    name: str,
    slot: str,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    color: str | list[int],
    thickness: int = 1,
    output_dir: str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Draw a line on design/layers/<slot>.png; refresh ref preview."""
    return _with_preview_hint(
        api.draw_line(
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
    )


@mcp.tool()
def fill_ellipse(
    name: str,
    slot: str,
    x: int,
    y: int,
    width: int,
    height: int,
    color: str | list[int],
    output_dir: str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Fill an ellipse bounded by (x,y,width,height) on the design layer."""
    return _with_preview_hint(
        api.fill_ellipse(
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
    )


@mcp.tool()
def clear_rect(
    name: str,
    slot: str,
    x: int,
    y: int,
    width: int,
    height: int,
    output_dir: str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Erase (transparent) a rectangle on the design layer only — never body."""
    return _with_preview_hint(
        api.clear_rect(
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
    )


@mcp.tool()
def flood_fill(
    name: str,
    slot: str,
    x: int,
    y: int,
    color: str | list[int],
    output_dir: str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Flood-fill connected pixels on the design layer from (x,y).

    Optional. Do NOT flood the entire body silhouette as the sole outfit
    design — prefer fill_rect / paint_pixels / paint_from_commands for shapes.
    Large fills return a warning field.
    """
    return _with_preview_hint(
        api.flood_fill(
            name,
            slot,
            x,
            y,
            color,
            output_dir=output_dir,
            scale=scale,
            plan_id=plan_id,
        )
    )


@mcp.tool()
def get_layer_pixels(
    name: str,
    slot: str,
    x: int = 0,
    y: int = 0,
    width: int | None = None,
    height: int | None = None,
    opaque_only: bool = False,
    output_dir: str | None = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Sample a region of design/layers/<slot>.png for read-modify workflows.

    Returns pixels as [{x,y,rgba:[r,g,b,a]}, ...]. Caps response size; use a
    smaller region or opaque_only=true. Does not modify the layer.
    """
    return api.get_layer_pixels(
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


@mcp.tool()
def paint_from_commands(
    name: str,
    slot: str,
    commands: list[dict[str, Any]],
    output_dir: str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Batch paint commands on one slot (single save + ref refresh).

    Each command: {op, ...} with op in fill_parts_on_slot|paint_pixels|
    fill_rect|clear_rect|stroke_rect|draw_line|fill_ellipse|flood_fill.
    Preferred for multi-step pixel-art (silhouette + belt/cross details).
    FORBIDDEN: Shell/PIL or external generators.
    """
    return _with_preview_hint(
        api.paint_from_commands(
            name,
            slot,
            commands,
            output_dir=output_dir,
            scale=scale,
            plan_id=plan_id,
        )
    )


@mcp.tool()
def generate_outfit_slot(
    name: str,
    slot: str,
    design_image_path: str | None = None,
    output_dir: str | None = None,
    scale: int = 8,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """OPTIONAL import/finalize: ensure design layer + refresh body-under ref.

    Preferred flow paints with MCP tools (paint_pixels, fill_rect,
    paint_from_commands, …) which already write design/layers/<slot>.png and
    refresh refs. Use this to import an existing RGBA PNG via
    design_image_path, or re-sync refs. Does not clip to body alpha.

    GATE: requires plan_outfit. Never use Shell/PIL or GenerateImage to create
    the layer — use MCP paint tools instead.
    """
    return _with_preview_hint(
        api.generate_outfit_slot(
            name,
            slot,
            design_image_path=design_image_path,
            output_dir=output_dir,
            scale=scale,
            plan_id=plan_id,
        )
    )


@mcp.tool()
def list_outfit_layers(
    name: str,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """List design layers (10 export names), parent/body parts, paint status, paths.

    ``allow_overhang`` is always true (body is not a clip).
    """
    return api.list_outfit_layers(name, output_dir=output_dir)


@mcp.tool()
def clear_outfit_slot(
    name: str,
    slot: str,
    output_dir: str | None = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Blank one slot's design layer (transparent PNG) and refresh its reference."""
    return _with_preview_hint(
        api.clear_outfit_slot(
            name, slot, output_dir=output_dir, plan_id=plan_id
        )
    )


@mcp.tool()
def compose_character(
    name: str,
    output_dir: str | None = None,
    scale: int = 8,
    include_skipped: bool = False,
) -> dict[str, Any]:
    """Compose dressed rest = base body + design layers (required before anim).

    Writes design/compose_preview.png. Uses DRAW_ORDER for body; each design
    export layer is inserted full after draw_after (no clip-to-body — overhang
    OK). GATE: call after painting layers (and preferably after plan_shading +
    shadow paint), BEFORE plan_animation. Soft gate: does not refuse when no
    shading plan exists. When design exists, build_frame_animation refuses
    without this compose preview — animation frames then rigid-rotate each
    design PNG 1:1 with its body part.

    After this returns: HUMAN EDIT GATE — ask the user if they want manual
    pixel edits. If yes → open_pixel_editor → Apply → re-compose. If no →
    plan_animation.
    """
    return _with_preview_hint(
        api.compose_character(
            name,
            output_dir=output_dir,
            scale=scale,
            include_skipped=include_skipped,
        )
    )


@mcp.tool()
def open_pixel_editor(
    name: str,
    output_dir: str | None = None,
    port: int = 0,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Open the local manual pixel editor for design layers (human edit gate).

    Call AFTER compose_character when the user wants to edit pixels by hand.
    Opens a small localhost web UI (pencil / eyedropper / eraser) for all 10
    EXPORT_LAYER_NAMES. Apply writes design/layers/<layer>.png only — never
    base/. After the user clicks Apply, re-run compose_character before
    plan_animation. Resolves paths via session output_root / output_dir.
    """
    return api.open_pixel_editor(
        name,
        output_dir=output_dir,
        port=port,
        open_browser=open_browser,
    )


@mcp.tool()
def plan_animation(
    name: str,
    animation_name: str,
    intent: str,
    plan: dict[str, Any],
    contrast_with: list[str] | None = None,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """REQUIRED before any frames: lock a reasoned animation plan.

    YOU (the agent) must author the plan — this tool validates and stores it.
    Do NOT jump to joint rotations. Do NOT reuse an unrelated clip's plan
    (e.g. a sword cut must not copy a bare-knuckle fight).

    Required plan keys:
    - beats: list of exactly 8 objects
      {frame_index: 0..7, pose_description: str, primary_joints: [joint names]}
    - silhouette_story: short paragraph describing how the clip reads to a viewer
    - differs_from: map {other_anim_name: explanation} (or list of
      {name, difference}) — must cover existing sibling clips / contrast_with
    - weapon_or_style_notes: e.g. overhead sword cut vs jab/punch fight

    Returns plan_id + user_facing_summary. You MUST show user_facing_summary
    to the user and wait for confirmation before build_frame_animation.
    next_step always says: show summary, then build frames.
    """
    return api.plan_animation(
        name,
        animation_name,
        intent,
        plan,
        contrast_with=contrast_with,
        output_dir=output_dir,
    )


@mcp.tool()
def get_animation_plan(
    name: str,
    animation_name: str,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Read the locked plan.json for a character animation.

    Use before build_frame_animation if you need the plan_id or to re-show
    user_facing_summary. Errors if plan_animation was never called.
    """
    return api.get_animation_plan(name, animation_name, output_dir=output_dir)


@mcp.tool()
def build_frame_animation(
    name: str,
    animation_name: str,
    frame_index: int,
    rotations: dict[str, float] | None = None,
    plan_id: str | None = None,
    output_dir: str | None = None,
    scale: int = 8,
) -> dict[str, Any]:
    """Draft one animation frame (0..7) with hierarchical joint rotations.

    GATE: refuses if no valid plan.json exists for this character+animation.
    If painted design/outfit plan exists, also requires compose_character.
    Optional plan_id must match the locked plan from plan_animation.

    NEVER call this until plan_animation succeeded AND you showed
    user_facing_summary to the user (and they OK'd). Jumping straight to
    rotations is forbidden (e.g. a sword clip must not reuse a fist-fight plan).

    DRESSED FRAMES: when design/ layers exist, the draft includes outfit
    layers rigid-rotated with the same pivot transforms as their parent body
    parts (DRAW_ORDER, draw_after, overhang preserved). No design → base only.
    Result includes dressed=true and design_slots when clothes were composited.

    Pose model: each body part is a RIGID full-canvas layer rotated about
    articulation pivots (parent joints rotate children) — not freehand redraw.
    Use get_joint_docs for which parts each joint rotates.

    rotations example: {"hip_near": -20, "knee_near": 6, "hip_far": 18, "knee_far": 14}.
    Joints: neck, shoulder_far, shoulder_near, elbow_far, elbow_near,
    hip_far, hip_near, knee_far, knee_near.
    Angles are absolute degrees from rest. Does not finalize — call
    finish_frame_animation next. For +X-facing idle: negative hip ≈ forward,
    positive knee ≈ flexion. Optional output_dir overrides session output root.
    """
    return _with_preview_hint(
        api.build_frame_animation(
            name,
            animation_name,
            frame_index,
            rotations,
            plan_id=plan_id,
            output_dir=output_dir,
            scale=scale,
        )
    )


@mcp.tool()
def finish_frame_animation(
    name: str,
    animation_name: str,
    frame_index: int,
    plan_id: str | None = None,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Lock the current draft frame as final under anims/<animation_name>/.

    GATE: refuses without a valid plan.json (same as build_frame_animation).
    Never finalize frames until plan_animation was called and the user saw
    user_facing_summary. Optional plan_id must match the stored plan.

    Writes frame_XX.png + poses/frame_XX.json. When frame 7 finishes (all
    frames present), also writes <animation_name>_contact_sheet.png and
    <animation_name>_preview.gif. Repeat build→finish for frames 0..7.
    """
    return _with_preview_hint(
        api.finish_frame_animation(
            name,
            animation_name,
            frame_index,
            plan_id=plan_id,
            output_dir=output_dir,
        )
    )


@mcp.tool()
def export_animation_preview(
    name: str,
    animation_name: str,
    duration_ms: int = 100,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Rebuild horizontal contact sheet + looping preview GIF for an animation.

    Requires finished frame_00.png … frame_07.png. Writes
    <animation_name>_contact_sheet.png and <animation_name>_preview.gif under
    anims/<animation_name>/. Preview GIF scale is LOCKED to native 90×128 —
    do not ask for or invent a per-clip scale. Also runs automatically when
    finish_frame_animation locks frame 7.
    """
    return _with_preview_hint(
        api.export_animation_preview(
            name,
            animation_name,
            duration_ms=duration_ms,
            output_dir=output_dir,
        )
    )


def _assert_required_tools_registered() -> None:
    names = set(_tool_manager_names())
    missing = sorted(_REQUIRED_TOOL_NAMES - names)
    missing_paint = sorted(_PAINT_TOOL_NAMES - names)
    count = len(names)
    paint_ok = not missing_paint
    count_ok = count == _EXPECTED_TOOL_COUNT
    banner = (
        f"SpriteMCP: module={__file__} tools={count}/"
        f"{_EXPECTED_TOOL_COUNT} outfit_ok={not missing} "
        f"paint_ok={paint_ok} count_ok={count_ok}"
    )
    print(banner, file=sys.stderr, flush=True)
    errors: list[str] = []
    if missing:
        errors.append("required tools not registered: " + ", ".join(missing))
    if missing_paint:
        errors.append("paint tools missing: " + ", ".join(missing_paint))
    if not count_ok:
        errors.append(
            f"tool_count={count} != expected {_EXPECTED_TOOL_COUNT} "
            "(stale/wrong module or incomplete @mcp.tool registration)"
        )
    if errors:
        raise RuntimeError(
            "SpriteMCP refused to start: "
            + "; ".join(errors)
            + f". Loaded module: {__file__}. Fix registration or mcp.json "
            "entrypoint, then restart SpriteMCP in Cursor Settings → MCP."
        )


def _install_list_tools_logging() -> None:
    """Log every tools/list response; Cursor sometimes skips re-list on restart.

    Re-registers the low-level handler so stderr shows the exact tool set the
    client receives (must be 45 including paint_* + shading + open_pixel_editor).
    """
    from mcp import types as mcp_types

    async def _logged_list_tools() -> list[mcp_types.Tool]:
        tools = await FastMCP.list_tools(mcp)
        names = sorted(t.name for t in tools)
        paint = sorted(_PAINT_TOOL_NAMES & set(names))
        print(
            f"SpriteMCP ListToolsResponse: count={len(names)}/"
            f"{_EXPECTED_TOOL_COUNT} paint={len(paint)}/{len(_PAINT_TOOL_NAMES)} "
            f"names={','.join(names)}",
            file=sys.stderr,
            flush=True,
        )
        if len(names) != _EXPECTED_TOOL_COUNT or len(paint) != len(_PAINT_TOOL_NAMES):
            print(
                "SpriteMCP WARNING: ListTools count mismatch — "
                "Cursor catalog will be wrong if it caches this response.",
                file=sys.stderr,
                flush=True,
            )
        return tools

    mcp._mcp_server.list_tools()(_logged_list_tools)  # noqa: SLF001


async def _run_stdio_with_list_changed() -> None:
    """stdio run advertising tools.listChanged so clients re-fetch catalogs."""
    from mcp.server.lowlevel.server import NotificationOptions
    from mcp.server.stdio import stdio_server

    init = mcp._mcp_server.create_initialization_options(  # noqa: SLF001
        notification_options=NotificationOptions(tools_changed=True),
    )
    print(
        "SpriteMCP: capabilities.tools.listChanged=True "
        f"catalog_epoch={__import__('os').environ.get('SPRITEMCP_CATALOG_EPOCH', '')!r}",
        file=sys.stderr,
        flush=True,
    )
    async with stdio_server() as (read_stream, write_stream):
        await mcp._mcp_server.run(read_stream, write_stream, init)  # noqa: SLF001


def main() -> None:
    """Run the MCP server on stdio (Cursor / Claude Desktop entrypoint)."""
    import asyncio

    _assert_required_tools_registered()
    _install_list_tools_logging()
    # Prefer explicit stdio + listChanged over mcp.run() so Cursor is told the
    # tool catalog can change (forces refresh after paint tools were added).
    asyncio.run(_run_stdio_with_list_changed())


if __name__ == "__main__":
    main()
