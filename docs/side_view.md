# Side-view + limb layering (design lock)

Every character in spritemcp is **true side view** (lateral profile facing `+X`). Do not author front or ¾ poses.

## Far / near

| Tag | Meaning | Depth |
|-----|---------|--------|
| `*_far` | Away from camera | **Behind** the torso |
| `*_near` | Toward camera | **In front** of the torso |

Far limbs are shaded one step darker in silhouettes so depth reads without a second camera angle.

## Full separate layers (storage)

Base idle geometry is stored as **full separate layers** under `layers/`. Far arms/legs keep their full length even where they sit under the torso. Occlusion is **not** baked into layer files.

Export layer groups (lower limb layers include hand/foot):

1. `upper_arm_far`, `lower_arm_far` (+ `hand_far`)
2. `upper_leg_far`, `lower_leg_far` (+ `foot_far`)
3. `torso`, `head`
4. `upper_leg_near`, `lower_leg_near` (+ `foot_near`)
5. `upper_arm_near`, `lower_arm_near` (+ `hand_near`)

## Compose order (`DRAW_ORDER`)

Permanent paint + pose rasterize order (later overwrites earlier on collision):

1. Far arm + far leg (+ hands/feet)
2. Torso, then head
3. Near leg (+ foot), then near arm (+ hand)

So when a far limb swings across the body, the torso occludes it; near limbs draw on top, with the near arm in front of the near thigh. This happens at **compose** time (`compose_preview.png`, `part_map` pixels, pose rasterize) — not by deleting far pixels from layer storage.

`part_map.json` includes composed `pixels` plus `parts_full` (per-part coords from layers) so articulation keeps under-torso far geometry.

Source of truth: `spritemcp.base_idle.DRAW_ORDER` / `EXPORT_LAYERS` (imported by `pose.apply_pose`). API: `get_draw_order()` / `get_view_lock()`.

## Shoulder / hip attachments

The authored **90×128** armature uses **lateral** per-limb shoulder and hip pivots
(far/near masses are separate on X). Depth is still z-order via `DRAW_ORDER`.

Knees use designed shin-column center X (`FAR_LEG_COLUMN` / `NEAR_LEG_COLUMN`)
and share one anatomic knee-line Y.

## Base idle

`build_base_idle_layers` imports each export layer from `spritemcp/base/`.
`compose()` applies `DRAW_ORDER` for previews / pivots. `generate_character`
writes `view`, `layering`, `draw_order`, and `storage` into return JSON /
`part_map.json`. Open `layers_grid.png` + `layers_strip.png` first when inspecting.

## Walk / angles (still side-only)

For `+X` facing: negative hip/shoulder ≈ forward; positive ≈ back; positive knee ≈ flexion. See package README.
