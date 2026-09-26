# Changelog

## 2026-09-26

### Output root required (no implicit cwd)

- Write tools refuse unless ``set_output_root(<agent_project>/output)`` was
  called, ``SPRITE_GEN_OUTPUT_ROOT`` seeded the session, or the call passes
  ``output_dir``. No silent ``<cwd>/output`` fallback (avoids writing into the
  wrong tree when the MCP process cwd is not the agent project).
- ``get_output_root`` / ``get_default_paths`` report ``ready_for_writes``.
- CLI still works: omitting ``--out-dir`` sets the session root to
  ``<cwd>/output`` explicitly for that process.

### Deterministic MCP image previews (ImageContent)

- Compose / finish / export gates and new **`show_preview`** embed a
  nearest-neighbor scaled PNG (default ×8) as MCP ``ImageContent`` via
  FastMCP ``Image`` — agents see pixels without Cursor Read.
- ``compose_character``, ``finish_frame_animation``, and
  ``export_animation_preview`` default ``preview=True``; pass ``preview=False``
  to skip embedding (disk writes unchanged). Paint tools still return paths
  only (no per-stroke images).
- ``show_preview(name, kind=compose|layer|frame|contact_sheet, ...)`` loads
  existing authored outputs only. Native 90×128 remains source of truth on disk.
- MCP catalog expected tool count **46** (was 45). Restart SpriteMCP after
  pull/update so the client refreshes ``tools/list``. Published installs keep
  using ``uvx --from git+… spritemcp`` (see ``docs/mcp.example.json``).

### Default output root = process cwd (superseded)

- Earlier: default write root was ``Path.cwd() / "output"``. **Superseded** by
  "Output root required" above — agents must set the root explicitly.
- Optional ``SPRITE_GEN_OUTPUT_ROOT`` env at process start still seeds the
  session root (same effect as ``set_output_root``).
- ``set_output_root`` / ``get_output_root`` / ``clear_output_root`` / per-call
  ``output_dir`` unchanged in spirit; writes now refuse when unset.

## 2026-09-25

### Manual pixel editor (human edit gate)

- After flat outfit + shading + `compose_character`, agents must **ask** whether
  the user wants manual pixel edits before `plan_animation`.
- New MCP/API tool **`open_pixel_editor(name)`** launches a local browser UI
  (stdlib HTTP + HTML/JS; no new deps) for the 10 design layers: pencil,
  eyedropper, eraser. **Apply** writes `design/layers/<layer>.png` only —
  never `base/`. Then re-run `compose_character`.
- CLI: `python -m spritemcp pixel-editor <name> [--out-dir PATH]`.
- MCP catalog expected tool count **45** (was 44). Restart SpriteMCP.

### Animation preview GIF scale lock

- Preview GIFs are locked to native **90×128** (`ANIMATION_PREVIEW_SCALE = 1`).
- `export_animation_preview` no longer accepts a custom `scale`; mismatched
  scales raise `ValueError`.
- See `AGENTS.md` / `.cursor/rules/animation-preview-gif-scale.mdc`.

### Public / open-source packaging

- Project public name: **SpriteMCP** (package and import: `spritemcp`).
- `uvx` support: core dep `mcp>=1.9.0,<2`, packaged `authored_base/` PNGs, entrypoint `spritemcp`.
- Renamed Python package from `sprite_gen` → `spritemcp`.
- Root README rewritten for standalone use (install, MCP, pipeline, demo gallery).
- Added MIT `LICENSE`. Public repo is read-only (clone/fork/use under MIT; no external write access).
- `demo/` is the tracked showcase gallery; `output/` stays gitignored.
- Example Cursor MCP config: `docs/mcp.example.json` (`uvx --from git+…`).

### Post-design shading pass

- Recommended pipeline step after flat outfit paint: `plan_shading` → show
  `user_facing_summary` → paint one-step-darker hard shadows on **design
  layers only** → `compose_character` (soft gate — compose does not refuse
  without a shading plan).
- New module `shading.py`: `plan_shading` / `get_shading_plan` store
  `design/shading_plan.json`; `suggest_shade_regions` proposes underside /
  far-limb bands from design alpha or body geometry (no pixels written);
  `get_shade_palette` documents `SHADE_STEP` local→shadow roles.
- Do **not** bake shading inside `fill_parts_on_slot`. Eyes stay on base head.
- MCP catalog expected tool count **44** (was 40). Restart spritemcp MCP.

### Eyes always on head

- Authored `base/eyes.png` is **required** (import fails if missing/empty).
- Eyes are white face detail on the head (not a part id): always stamped onto
  `layers/head.png`, compose/silhouette previews, outfit body underlay, and
  posed animation frames (transformed with the head). Design head layers may
  cover them; naked/base never omits them.

### Authored 90×128 only (classic 40×56 retired)

- Single canvas: authored **90×128** from `spritemcp/base/` →
  `output/base_idle_90x128/`. `CANVAS_W`/`CANVAS_H` are 90×128 only.
- Removed `classic_40x56`, `trial_90x128`, dual-preset switching, and
  procedural 40×56 geometry. API / MCP / CLI no longer take `preset=`.
- Shoulder/hip pivots are always lateral (per-limb). Restart spritemcp MCP
  after pull so tool signatures refresh.

### Animation contact sheet + preview GIF

- Finishing frame 7 via `finish_frame_animation` writes
  `<animation_name>_contact_sheet.png` (horizontal frames 0–7) and
  `<animation_name>_preview.gif` (looping; default 100ms/frame).
- Standalone `export_animation_preview` (API + MCP) rebuilds both from finished
  frames; optional nearest-neighbor `scale` for readable previews.
- Helpers live in `export.py` (`make_contact_sheet` / `save_preview_gif`).
- MCP catalog expected tool count **40** (was 39).

### Design layers = 10 export body layers

- Outfit design PNGs now use the same names as `base/layers/` /
  `EXPORT_LAYER_NAMES` (not clothing-slot merges like `legs_pants_armor`).
- Each design file has a **single** pose parent (1:1 rigid rotate). Removed
  multi-parent far+near mask split from `pose.apply_pose`.
- `lower_*` layers include hand/foot (gloves/boots paint there). Plan notes may
  still mention jingasa/do/hakama conceptually.
- Schema: `spritemcp.outfit_plan.v2` / `outfit_layers_meta.v2`.
- API: `migrate_design_to_export_layers` splits legacy clothing-slot PNGs.
- MCP instructions / docs updated; restart spritemcp MCP after pull.

## 2026-09-24

### Arms outfit slot (`arms_sleeves_armor`)

- New structural slot for sleeves / arm armor: parents
  `upper_arm_far`, `lower_arm_far`, `upper_arm_near`, `lower_arm_near`
  (`draw_after=lower_arm_near`, `allow_overhang=True`, optional — skip for
  short sleeves / bare arms). Hands stay on `gloves`.
- Wired into slot specs, `plan_outfit` validation, refs, fill defaults,
  compose / animation design layers, MCP tool docs, and overview.

### MCP outfit paint tools (no Shell/PIL scripts)

- First-class paint tools on the MCP server (Pillow server-side only):
  `fill_parts_on_slot`, `paint_pixels` / `set_pixels`, `fill_rect`, `stroke_rect`,
  `draw_line`, `fill_ellipse`, `clear_rect`, `flood_fill`, `get_layer_pixels`,
  `paint_from_commands` (**11** paint tools; live catalog should list **39** total).
- Startup guard requires all paint tools + `tools==39` (`paint_ok` / `count_ok`
  on stderr); Cursor catalogs stuck at ~28 are stale — restart spritemcp MCP.
- `fill_parts_on_slot` replaces ad-hoc scripts that looped `part_pixels` and
  stamped colors onto `design/layers/<slot>.png`.
- Pipeline: `prepare_outfit_slot_reference` → MCP paint → `compose_character`.
  `generate_outfit_slot` remains optional import/refresh.
- MCP instructions: FORBIDDEN Shell python/PIL scripts and external image
  generators (GenerateImage, etc.) for outfit painting.

### Configurable output root

- API/MCP: `set_output_root(path)`, `get_output_root()`, `clear_output_root()` — session-persisted absolute root (mkdir as needed); default remains `spritemcp/output/`.
- Per-call `output_dir` still overrides. `get_default_paths` reports active root + `session_override`.
- MCP SOP documents when to redirect writes outside the package folder.

### Dressed animation frames

- `build_frame_animation` composites base body + `design/layers` (rigid Pillow rotate with the same pivot transforms as `parent_parts`; DRAW_ORDER / `draw_after` / overhang).
- Gate: if painted design or outfit plan exists, require `compose_character` first; no design → base-only as before.
- Result fields: `dressed`, `design_slots`. Pipeline: design → compose → plan_animation → build/finish (frames are dressed).

### Outfit overhang (clothing past body margins)

- Rule: clothing **may** extend outside body silhouette margins; parent body parts are alignment / proportion reference, **not** a hard clip mask (keep proportions sensible).
- Specs: all slots `allow_overhang=True` (was false for `legs_pants_armor` / `gloves`).
- Code: removed compose clip-to-body path (`_mask_design_to_parts`); refs never clipped design to body alpha.
- Docs/MCP: tool descriptions, `user_facing_summary` painting rule, and `overhang_note` on slot specs / prepare / generate / compose.

### Outfit / design layers (before animation)

- New step between base and animation: `plan_outfit` → body-under slot refs → paint design layers → `compose_character`.
- Disk: `characters/<name>/design/` (`plan.json`, `layers_meta.json`, `layers/<slot>.png`, `refs/<slot>_ref.png`, `compose_preview.png`).
- Slots (structural only, no style presets): `headwear`, `body_shirt_armor`, `arms_sleeves_armor`, `legs_pants_armor`, `feet_boots`, `gloves` — each parents to body parts for later posed compose.
- API/MCP: `list_outfit_slot_specs`, `plan_outfit`, `get_outfit_plan`, `prepare_outfit_slot_reference`, `generate_outfit_slot`, `list_outfit_layers`, `clear_outfit_slot`, `compose_character`.
- Painting rule: reference stack always has parent body part(s) under the transparent design layer; only the design PNG is persisted.

### Base idle: full separate layers

- Geometry is stored as full layers under `output/base_idle/layers/` (far limbs keep under-torso pixels). Occlusion only at compose via `DRAW_ORDER`.
- Export groups: `upper_arm_*`, `lower_arm_*` (+hand), `upper_leg_*`, `lower_leg_*` (+foot), `torso`, `head`.
- New previews: `compose_preview.png`, `layers_grid.png`, `layers_strip.png`; `part_map.json` adds `storage`, `parts_full`, `export_layers`.
- Pose uses `parts_full` so articulation keeps far geometry under the torso; pivots still from composed topology + stacked/leg-column locks.
- Docs: `side_view.md` / `overview.md`. Regenerated `output/base_idle/` (+ vagabond base idle/pivots only; anims not regenerated).

### Base idle: knees share anatomic Y

- `knee_far` Y (39) and `knee_near` Y (40) differed by 1px from separate upper/lower boundaries.
- After leg-column-center X, both knees now share one knee-line Y (mean of the two boundary heights → `39.5`).
- New coords: `knee_far` `(18.5, 39.5)`, `knee_near` `(19.5, 39.5)`. Stacked shoulder/hip unchanged. Regenerated `output/base_idle/` pivots + vagabond base (anims not regenerated).

### Base idle: knee_far on leg-column centerline

- `knee_far` was pulled to the 1px far-leg peek edge `(17.5, 39)` after occlusion (only one upper/lower contact pair left).
- Knees now use `leg_column_center` X from designed far/near leg columns (`FAR_LEG_COLUMN` / `NEAR_LEG_COLUMN`); Y stays from the upper/lower boundary.
- New coords: `knee_far` `(18.5, 39)`, `knee_near` `(19.5, 40)`. Stacked shoulder/hip unchanged. Regenerated `output/base_idle/` pivots + vagabond base (anims not regenerated).

### Base idle: part-map occlusion (torso owns overlap)

- Removed post-torso far “peek” re-stamps on the shoulder/hip stack that left `upper_arm_far` / `upper_leg_far` labels on the chest/hip column.
- Part-map paint order now matches `DRAW_ORDER` occlusion: far → torso/head overwrites → near overwrites. Far remains only outside the torso rectangle.
- Docs: `side_view.md` / `overview.md` occlusion note. Regenerated `output/base_idle/` and vagabond character base (walk/fight/samurai_attack not regenerated).

### Base idle: stacked shoulder / hip (profile fix)

- Rewrote naked side-view `base_idle` so far/near arms share one shoulder attachment XY and far/near legs share one hip XY (no left/right A-pose joint split).
- Depth remains draw order only: far behind torso, near in front; far limbs may read slightly shorter/darker.
- Pivots: `shoulder_far`/`shoulder_near` and `hip_far`/`hip_near` forced equal via `side_view_stack` anchors (`SHOULDER_XY`, `HIP_XY`). Elbows/knees still per-limb.
- Docs: `side_view.md` / `overview.md` attachment lock. Regenerated `output/base_idle/` and vagabond character base (walk/fight/samurai_attack not regenerated this pass).

### Animation planning gate

- New `plan_animation` / `get_animation_plan` API + MCP tools. Agent must author a structured plan (`beats` ×8, `silhouette_story`, `differs_from`, `weapon_or_style_notes`); tool validates, stores `anims/<anim>/plan.json`, returns `user_facing_summary` that **must** be shown to the user before frames.
- `build_frame_animation` / `finish_frame_animation` refuse without a valid plan (optional `plan_id` match). Docs and MCP instructions: plan → show summary → (user OK) → build/finish ×8.
- Rejects empty/generic plans and sword-style names that clone bare-knuckle fight language.

### MCP server for Cursor

- Added `spritemcp.mcp_server` (FastMCP / official `mcp` SDK, stdio transport).
- Tools mirror `api.py`: `generate_character`, `plan_animation`, `get_animation_plan`, `build_frame_animation`, `finish_frame_animation`, getters, `show_reference_grid`, `generate_base_idle`, `generate_pivots`, `run_demo`.
- Optional dep: `pip install -e ".[mcp]"` or `requirements-mcp.txt`; entrypoint `spritemcp-mcp`.
- Registered as `spritemcp` in repo `.cursor/mcp.json` and `.mcp.json`.

### Design lock: side view + far-behind layering

- Characters are **always side view** (lateral). Docs and pose logic assume facing `+X` only — no front/¾.
- `*_far` = behind torso; `*_near` = in front. Compose order locked as far limbs → torso/head → near limbs (`DRAW_ORDER` in `base_idle`, used by pose rasterize).
- Base idle paints in that order; far/near limbs share stacked shoulder and hip attachment XY (see “stacked shoulder / hip” note above).
- Legs stack under the torso column (overlap in X) so rest reads as side profile, not ¾; near overwrites far on shared cells.
- `generate_character` / `part_map.json` now include `view`, `layering`, `draw_order`. API: `get_draw_order()`, `get_view_lock()`.
- See `docs/side_view.md`.

### Walk fix (vagabond)

- Root cause of broken walk: **negative knee angles** (hyperextension / bird-leg). Rotation math already matched docs (negative hip = forward).
- Regenerated 8-frame walk with positive knee flexion; contact sheets rewritten under `output/characters/vagabond/anims/walk/`.
- README examples updated to correct hip/knee signs.
