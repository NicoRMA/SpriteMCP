# SpriteMCP — Overview

**SpriteMCP** is a standalone package for pixel sprite authoring: canvas, palette, export, shared base idle, pivots, and an agent character / outfit / shading / animation pipeline. The MCP server (`spritemcp.mcp_server`) exposes the same API over stdio for Cursor and other MCP clients.

## Design locks

- **Side view only** — every character is a lateral profile (facing `+X`). No front or ¾.
- **Far behind / near in front** — `*_far` limbs sit behind the torso; `*_near` in front. Compose order: far limbs → torso/head → near legs → near arms (`DRAW_ORDER`). Full separate layers keep far geometry under the torso; occlusion is compose-only.
- **Lateral attachments** — `shoulder_far`/`shoulder_near` and `hip_far`/`hip_near` are independent per-limb pivots on the authored **90×128** silhouette. Depth is always z-order via `DRAW_ORDER`.

Details: [`side_view.md`](side_view.md). Recent changes: [`CHANGELOG.md`](CHANGELOG.md).

## Defaults

- **Canvas / armature:** authored **90×128** from repo `base/` (checkout) or packaged `spritemcp/authored_base/` (uvx/wheel) → `output/base_idle_90x128/`
- **Eyes:** `eyes.png` is required face detail on the head (not a part id). Import always composites it onto `layers/head.png`, compose, silhouette, and posed frames; outfit design on `head` may cover eyes (helmets), but naked base never drops them.
- **Output:** **no implicit default.** Agents must call ``set_output_root(<agent_project>/output)`` before any write (or set ``SPRITE_GEN_OUTPUT_ROOT`` at MCP start, or pass ``output_dir`` per call). Writes never use the package install path. Curated public examples live in `demo/` (see root README).
- **Keep in sync:** when editing armature PNGs under `base/`, copy them into `src/spritemcp/authored_base/` before a release so `uvx` picks up the same masks.
- **Session root:** `set_output_root(path)` (MCP/API) is required for writes that omit `output_dir`; creates dirs; absolute paths. Per-call `output_dir` still wins. `clear_output_root` clears the session (writes refuse until set again).
- **Style ref (optional):** if you keep a reference PNG for `show_reference_grid`, pass `--ref` / `style_ref=...`. The package default may point at a sibling game-repo path that does not exist in a standalone clone — override when needed.
- **Characters:** `<output_root>/characters/<name>/`

## Agent pipeline

**Mandatory flow** (do not skip plan steps):

1. **Required:** **`set_output_root(<agent_project>/output)`** before any write (or env / per-call `output_dir`)
2. **`generate_character(name)`** — rest idle + pivots under `characters/<name>/base/`
3. **`plan_outfit(name, brief, plan)`** — agent authors slots from a free brief; writes `design/plan.json` + **`user_facing_summary`**
4. **Show outfit summary** → per export layer: **`prepare_outfit_slot_reference`** → paint `design/layers/<layer>.png` (flat local colors; no shading bake in `fill_parts_on_slot`)
5. **Recommended shading:** optional **`suggest_shade_regions`** → **`plan_shading`** → show summary → paint one-step-darker hard shadows on design layers (`get_shade_palette`) → **`compose_character`**
6. **Human edit gate:** after compose, **ask** if the user wants manual pixel edits. If yes → **`open_pixel_editor(name)`** (local browser UI; Apply writes `design/layers/*.png` only) → re-**`compose_character`**. If no → continue.
7. **`plan_animation(name, animation_name, intent, plan)`** — beats / silhouette / differs_from / weapon notes; writes `anims/<animation_name>/plan.json` + **`user_facing_summary`**
8. **Show animation summary** and wait for OK
9. **`build_frame_animation(...)`** — draft frame `0..7` (**refuses without plan**; **dressed** when design layers exist)
10. **`finish_frame_animation(...)`** — lock draft; finishing frame 7 also writes contact sheet + preview GIF

Outfit design layers match the 10 `EXPORT_LAYER_NAMES` / `base/layers/`: `upper_arm_far`, `lower_arm_far`, `upper_leg_far`, `lower_leg_far`, `torso`, `head`, `upper_leg_near`, `lower_leg_near`, `upper_arm_near`, `lower_arm_near` (`lower_*` includes hand/foot).

For +X-facing idle, **negative hip/shoulder ≈ forward**, **positive ≈ back**; **positive knee ≈ flexion** (shin toward −X).

## Manual pixel editor

Human exception path after shading + compose. **`open_pixel_editor(name)`** starts a tiny localhost web app (stdlib HTTP + HTML/JS; no extra deps) with the 10 design layers, pencil / eyedropper / eraser, and a body underlay for alignment. **Apply** saves `design/layers/<layer>.png` only — never `base/`. Then re-run **`compose_character`**.

CLI: `python -m spritemcp pixel-editor <name> [--out-dir PATH]`.

## MCP

`src/spritemcp/api.py` is the stable public surface. `mcp_server.py` wraps those functions as stdio MCP tools. Example Cursor config: [`mcp.example.json`](mcp.example.json).

Expected live tool count: **46** (including paint tools, `open_pixel_editor`, and `show_preview`). After server code changes, restart the MCP so the client catalog refreshes.

**Vision QA:** `compose_character`, `finish_frame_animation`, and `export_animation_preview` default `preview=True` and embed a nearest-neighbor scaled PNG (typically ×8) as MCP ImageContent. Use `show_preview(kind=compose|layer|frame|contact_sheet)` for on-demand checks. Paint tools return path strings only — do not Cursor-Read after every stroke.
