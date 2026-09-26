"""Articulation pivots for the base idle part-id map.

Computes joint positions at parent/child part boundaries on the hierarchical
2D skeleton (neck, shoulders, elbows, hips, knees). Visualization + JSON only —
no animation, no Godot.

Run:
  python -m spritemcp pivots
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

from .base_idle import (
    ARMATURE_NAME,
    FAR_LEG_COLUMN,
    NEAR_LEG_COLUMN,
    PART_NAMES,
    PartMap,
    build_base_idle_part_map,
    default_base_idle_out_dir,
    load_authored_eyes_coords,
    part_map_to_color_grid,
    part_map_to_silhouette_grid,
    render_part_map_grid_with_legend,
)
from .export import grid_to_image

# ---------------------------------------------------------------------------
# Hierarchical joints (parent = rootward bone; child rotates about the pivot)
# ---------------------------------------------------------------------------

# (name, parent_part, child_part) — matches user-confirmed skeleton.
JOINT_SPECS: tuple[tuple[str, str, str], ...] = (
    ("neck", "torso", "head"),
    ("shoulder_far", "torso", "upper_arm_far"),
    ("shoulder_near", "torso", "upper_arm_near"),
    ("elbow_far", "upper_arm_far", "lower_arm_far"),
    ("elbow_near", "upper_arm_near", "lower_arm_near"),
    ("hip_far", "torso", "upper_leg_far"),
    ("hip_near", "torso", "upper_leg_near"),
    ("knee_far", "upper_leg_far", "lower_leg_far"),
    ("knee_near", "upper_leg_near", "lower_leg_near"),
)

_NEIGHBORS_4: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))

# Bright marker colors for overlays (high contrast on silhouette / part map).
_MARKER_FILL = (255, 40, 40, 255)
_MARKER_OUTLINE = (255, 255, 255, 255)
_LABEL_FILL = (255, 255, 80, 255)
_LABEL_SHADOW = (0, 0, 0, 220)


@dataclass(frozen=True)
class Pivot:
    """One articulation point in part-map pixel space (origin top-left)."""

    name: str
    parent: str
    child: str
    x: float
    y: float
    method: str
    contact_pairs: int


def _part_pixels(part_map: PartMap, part_id: int) -> list[tuple[int, int]]:
    """Pixels from the composed part map (occlusion already applied)."""
    return [
        (x, y)
        for y in range(part_map.height)
        for x in range(part_map.width)
        if part_map.labels[y][x] == part_id
    ]


def _adjacent_contact_midpoints(
    part_map: PartMap,
    parent_id: int,
    child_id: int,
) -> list[tuple[float, float]]:
    """Midpoints of 4-adjacent parent/child pixel pairs (pixel-center space)."""
    labels = part_map.labels
    w, h = part_map.width, part_map.height
    mids: list[tuple[float, float]] = []
    seen: set[tuple[int, int, int, int]] = set()

    for y in range(h):
        for x in range(w):
            if labels[y][x] != parent_id:
                continue
            for dx, dy in _NEIGHBORS_4:
                nx, ny = x + dx, y + dy
                if not (0 <= nx < w and 0 <= ny < h):
                    continue
                if labels[ny][nx] != child_id:
                    continue
                key = (min(x, nx), min(y, ny), max(x, nx), max(y, ny))
                if key in seen:
                    continue
                seen.add(key)
                # Midpoint of pixel centers.
                mids.append(((x + nx) * 0.5 + 0.5, (y + ny) * 0.5 + 0.5))
    return mids


def _nearest_pair_midpoint(
    part_map: PartMap,
    parent_id: int,
    child_id: int,
) -> tuple[float, float, int]:
    """Fallback when parts do not share a 4-edge: nearest pixel pair midpoint."""
    parents = _part_pixels(part_map, parent_id)
    children = _part_pixels(part_map, child_id)
    if not parents or not children:
        raise ValueError(
            f"Missing pixels for parent_id={parent_id} or child_id={child_id}"
        )

    best_dist = float("inf")
    best_pairs: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for px, py in parents:
        for cx, cy in children:
            dist = (px - cx) * (px - cx) + (py - cy) * (py - cy)
            if dist < best_dist:
                best_dist = dist
                best_pairs = [((px, py), (cx, cy))]
            elif dist == best_dist:
                best_pairs.append(((px, py), (cx, cy)))

    sx = sy = 0.0
    for (px, py), (cx, cy) in best_pairs:
        sx += (px + cx) * 0.5 + 0.5
        sy += (py + cy) * 0.5 + 0.5
    n = len(best_pairs)
    return (sx / n, sy / n, n)


def compute_pivot(
    part_map: PartMap,
    name: str,
    parent: str,
    child: str,
) -> Pivot:
    parent_id = PART_NAMES[parent]
    child_id = PART_NAMES[child]
    mids = _adjacent_contact_midpoints(part_map, parent_id, child_id)
    if mids:
        x = sum(m[0] for m in mids) / len(mids)
        y = sum(m[1] for m in mids) / len(mids)
        return Pivot(
            name=name,
            parent=parent,
            child=child,
            x=round(x, 2),
            y=round(y, 2),
            method="boundary_centroid",
            contact_pairs=len(mids),
        )

    x, y, n = _nearest_pair_midpoint(part_map, parent_id, child_id)
    return Pivot(
        name=name,
        parent=parent,
        child=child,
        x=round(x, 2),
        y=round(y, 2),
        method="nearest_pair",
        contact_pairs=n,
    )


def _child_top_center_xy(part_map: PartMap, child: str) -> tuple[float, float]:
    """Center of the child's topmost painted row (full layer when available).

    Used for laterally separated far/near shoulder and hip masses on the
    authored 90×128 silhouette.
    """
    pixels = part_map.part_pixels(child)
    if not pixels:
        raise ValueError(f"Missing pixels for child part {child!r}")
    ymin = min(y for _, y in pixels)
    xs = [x for x, y in pixels if y == ymin]
    x0, x1 = min(xs), max(xs)
    cx = _leg_column_center_x(float(x0), x1 - x0 + 1)
    cy = round(ymin + 0.5, 2)
    return (cx, cy)


def _apply_lateral_limb_attachment(
    pivots: list[Pivot],
    part_map: PartMap,
    *,
    name: str,
    child: str,
) -> None:
    """Place a shoulder/hip pivot on that limb's top mass center (in place)."""
    cx, cy = _child_top_center_xy(part_map, child)
    for i, p in enumerate(pivots):
        if p.name == name:
            pivots[i] = Pivot(
                name=p.name,
                parent=p.parent,
                child=p.child,
                x=cx,
                y=cy,
                method="lateral_limb_top",
                contact_pairs=p.contact_pairs,
            )


def _leg_column_center_x(column_x0: float, column_w: int) -> float:
    """Pixel-center X of a contiguous leg column ``[x0, x0+w)``."""
    return round(column_x0 + column_w / 2.0, 2)


def _apply_knee_leg_column_center(
    pivots: list[Pivot],
    *,
    name: str,
    column_x0: float,
    column_w: int,
) -> None:
    """Snap knee X to the designed leg-column centerline; keep boundary Y for now.

    After DRAW_ORDER occlusion, far upper/lower often share only a 1px peek
    edge, so raw boundary_centroid sits on that edge instead of the middle of
    the painted far leg column (same idea as hip_far on the stack XY).
    Shared anatomic Y is applied afterward by ``_align_knee_heights``.
    """
    cx = _leg_column_center_x(column_x0, column_w)
    for i, p in enumerate(pivots):
        if p.name == name:
            pivots[i] = Pivot(
                name=p.name,
                parent=p.parent,
                child=p.child,
                x=cx,
                y=p.y,
                method="leg_column_center",
                contact_pairs=p.contact_pairs,
            )


def _align_knee_heights(
    pivots: list[Pivot],
    *,
    far_name: str = "knee_far",
    near_name: str = "knee_near",
) -> None:
    """Force far/near knees onto one anatomic knee line (shared Y).

    Boundary centroids can differ by ~1px (far peek vs near column). Use the
    mean of the two boundary Y values so both sit on the same horizontal line
    while each keeps its leg-column-center X.
    """
    by_name = {p.name: (i, p) for i, p in enumerate(pivots)}
    if far_name not in by_name or near_name not in by_name:
        return
    _fi, far = by_name[far_name]
    _ni, near = by_name[near_name]
    shared_y = round((far.y + near.y) / 2.0, 2)
    for name in (far_name, near_name):
        i, p = by_name[name]
        pivots[i] = Pivot(
            name=p.name,
            parent=p.parent,
            child=p.child,
            x=p.x,
            y=shared_y,
            method="leg_column_center",
            contact_pairs=p.contact_pairs,
        )


def compute_all_pivots(
    part_map: PartMap | None = None,
    specs: Sequence[tuple[str, str, str]] = JOINT_SPECS,
) -> list[Pivot]:
    pm = part_map or build_base_idle_part_map()
    pivots = [compute_pivot(pm, name, parent, child) for name, parent, child in specs]
    # Laterally separated far/near masses: per-limb pivots at each limb's
    # top-row center (full layered geometry when available).
    _apply_lateral_limb_attachment(
        pivots, pm, name="shoulder_far", child="upper_arm_far"
    )
    _apply_lateral_limb_attachment(
        pivots, pm, name="shoulder_near", child="upper_arm_near"
    )
    _apply_lateral_limb_attachment(
        pivots, pm, name="hip_far", child="upper_leg_far"
    )
    _apply_lateral_limb_attachment(
        pivots, pm, name="hip_near", child="upper_leg_near"
    )
    # Knees: X on designed shin-column centerline; shared anatomic Y.
    far_x0, far_w = FAR_LEG_COLUMN
    near_x0, near_w = NEAR_LEG_COLUMN
    _apply_knee_leg_column_center(
        pivots, name="knee_far", column_x0=far_x0, column_w=far_w
    )
    _apply_knee_leg_column_center(
        pivots, name="knee_near", column_x0=near_x0, column_w=near_w
    )
    _align_knee_heights(pivots)
    return pivots


def pivots_to_json_dict(
    pivots: Sequence[Pivot],
    part_map: PartMap,
) -> dict:
    far_col = FAR_LEG_COLUMN
    near_col = NEAR_LEG_COLUMN
    by_name = {p.name: p for p in pivots}
    coordinate_space = (
        "part_map pixel space; origin top-left; "
        "x/y are midpoints of parent/child boundary pixel centers "
        "(shoulder/hip far+near use per-limb lateral_limb_top on each "
        "limb's top-row center; knees use leg_column_center X on designed "
        "far/near shin columns and a shared anatomic knee-line Y)"
    )
    description = (
        "Hierarchical 2D articulation pivots for base idle. "
        "Parent is rootward; rotating about the pivot moves the child and its descendants. "
        "Authored 90×128 silhouette: shoulder_far/near and hip_far/near are "
        "independent lateral attachments (top mass center of each limb), "
        "not a shared stack XY. "
        "knee_far/near X sits on the designed shin-column centerline at the "
        "knee band; both knees share one Y (mean of upper/lower boundary heights). "
        "Wrist/ankle not included in this pass."
    )
    attachment_meta = {
        "lateral_joints": {
            "shoulder_far": {
                "x": by_name["shoulder_far"].x,
                "y": by_name["shoulder_far"].y,
            },
            "shoulder_near": {
                "x": by_name["shoulder_near"].x,
                "y": by_name["shoulder_near"].y,
            },
            "hip_far": {"x": by_name["hip_far"].x, "y": by_name["hip_far"].y},
            "hip_near": {"x": by_name["hip_near"].x, "y": by_name["hip_near"].y},
        }
    }
    return {
        "width": part_map.width,
        "height": part_map.height,
        "armature": ARMATURE_NAME,
        "pose": part_map.pose,
        "facing": part_map.facing,
        "coordinate_space": coordinate_space,
        "description": description,
        **attachment_meta,
        "leg_columns": {
            "far": {"x0": far_col[0], "width": far_col[1]},
            "near": {"x0": near_col[0], "width": near_col[1]},
        },
        "skeleton": [
            {"name": p.name, "parent": p.parent, "child": p.child} for p in pivots
        ],
        "joints": [asdict(p) for p in pivots],
    }


def _load_font(size_hint: int = 10) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype("arial.ttf", max(10, size_hint))
    except OSError:
        try:
            return ImageFont.load_default()
        except OSError:
            return ImageFont.load_default()


def _draw_cross(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    *,
    half: int = 5,
    fill=_MARKER_FILL,
    outline=_MARKER_OUTLINE,
) -> None:
    x, y = int(round(cx)), int(round(cy))
    # White outline then bright core for readability on any backdrop.
    for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1), (0, 0)):
        draw.line([(x - half + ox, y + oy), (x + half + ox, y + oy)], fill=outline, width=1)
        draw.line([(x + ox, y - half + oy), (x + ox, y + half + oy)], fill=outline, width=1)
    draw.line([(x - half, y), (x + half, y)], fill=fill, width=1)
    draw.line([(x, y - half), (x, y + half)], fill=fill, width=1)
    r = 2
    draw.ellipse([x - r, y - r, x + r, y + r], fill=fill, outline=outline)


def _label_offsets(name: str) -> tuple[int, int]:
    """Nudge labels so stacked near/far pairs stay readable."""
    offsets: dict[str, tuple[int, int]] = {
        "neck": (6, -12),
        "shoulder_far": (-58, -14),
        "shoulder_near": (8, 2),
        "elbow_far": (-48, 4),
        "elbow_near": (8, 4),
        "hip_far": (-44, -10),
        "hip_near": (8, 8),
        "knee_far": (-40, 6),
        "knee_near": (8, 6),
    }
    return offsets.get(name, (6, -10))


def draw_pivots_on_image(
    base: Image.Image,
    pivots: Sequence[Pivot],
    *,
    scale: int = 1,
    label: bool = True,
    origin_xy: tuple[int, int] = (0, 0),
) -> Image.Image:
    """Overlay crosses + labels. ``origin_xy`` is where part-map (0,0) sits in ``base``."""
    out = base.convert("RGBA").copy()
    draw = ImageDraw.Draw(out)
    font = _load_font(max(10, scale + 2))
    ox, oy = origin_xy

    for p in pivots:
        cx = ox + p.x * scale
        cy = oy + p.y * scale
        half = max(4, scale // 2 + 2)
        _draw_cross(draw, cx, cy, half=half)
        if label:
            lx, ly = _label_offsets(p.name)
            tx = int(round(cx + lx * max(1, scale / 8)))
            ty = int(round(cy + ly * max(1, scale / 8)))
            text = f"{p.name} ({p.x:g},{p.y:g})"
            # Shadow for contrast.
            draw.text((tx + 1, ty + 1), text, fill=_LABEL_SHADOW, font=font)
            draw.text((tx, ty), text, fill=_LABEL_FILL, font=font)
    return out


def _grid_content_origin(scale: int, label_every: int = 5) -> tuple[int, int]:
    """Match show_ref_grid left/top margin so overlays align with part_map_grid."""
    margin = max(28, scale * 2 + 8 + label_every // 2)
    return (margin, margin)


def render_pivots_overlay(
    part_map: PartMap,
    pivots: Sequence[Pivot],
    *,
    scale: int = 8,
    use_silhouette: bool = True,
    eye_coords: Sequence[tuple[int, int]] | None = None,
) -> Image.Image:
    """Scaled silhouette (or part colors) with pivot markers — no grid axes."""
    from .base_idle import _stamp_eyes_detail

    grid = (
        part_map_to_silhouette_grid(part_map)
        if use_silhouette
        else part_map_to_color_grid(part_map)
    )
    native = grid_to_image(grid)
    if eye_coords:
        native = _stamp_eyes_detail(native, list(eye_coords))
    # Opaque dark backdrop so transparent pixels read clearly.
    w, h = native.size
    scaled = native.resize((w * scale, h * scale), Image.Resampling.NEAREST)
    canvas = Image.new("RGBA", scaled.size, (28, 28, 32, 255))
    canvas.paste(scaled, (0, 0), scaled)
    return draw_pivots_on_image(canvas, pivots, scale=scale, label=True)


def render_pivots_grid(
    part_map: PartMap,
    pivots: Sequence[Pivot],
    *,
    scale: int = 8,
    eye_coords: Sequence[tuple[int, int]] | None = None,
) -> Image.Image:
    """Part-map grid + legend (same layout as part_map_grid) with pivots overlaid."""
    from .base_idle import _stamp_eyes_detail

    color_img = grid_to_image(part_map_to_color_grid(part_map))
    if eye_coords:
        color_img = _stamp_eyes_detail(color_img, list(eye_coords))
    base = render_part_map_grid_with_legend(color_img, scale=scale)
    origin = _grid_content_origin(scale)
    return draw_pivots_on_image(base, pivots, scale=scale, label=True, origin_xy=origin)


def build_outputs(
    out_dir: Path,
    *,
    scale: int = 8,
    part_map: PartMap | None = None,
) -> tuple[PartMap, list[Pivot], dict[str, Path]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    pm = part_map or build_base_idle_part_map()
    pivots = compute_all_pivots(pm)

    eye_coords = load_authored_eyes_coords()

    json_path = out_dir / "pivots.json"
    json_path.write_text(
        json.dumps(pivots_to_json_dict(pivots, pm), indent=2) + "\n",
        encoding="utf-8",
    )

    overlay = render_pivots_overlay(
        pm, pivots, scale=scale, use_silhouette=True, eye_coords=eye_coords or None
    )
    overlay_path = out_dir / "pivots_overlay.png"
    overlay.save(overlay_path)

    grid_img = render_pivots_grid(
        pm, pivots, scale=scale, eye_coords=eye_coords or None
    )
    grid_path = out_dir / "pivots_grid.png"
    grid_img.save(grid_path)

    paths = {
        "pivots_json": json_path,
        "pivots_overlay": overlay_path,
        "pivots_grid": grid_path,
    }
    return pm, pivots, paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compute articulation pivots on the base idle part map "
            "and export JSON + overlay PNGs."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: output/base_idle_90x128/)",
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=8,
        help="Nearest-neighbor upscale for overlay PNGs",
    )
    args = parser.parse_args(argv)

    out_dir = args.out_dir if args.out_dir is not None else default_base_idle_out_dir()
    _pm, pivots, paths = build_outputs(out_dir, scale=args.scale)

    print(f"Armature: {ARMATURE_NAME}")
    print(f"Joints: {len(pivots)}")
    for p in pivots:
        print(
            f"  {p.name:16s}  parent={p.parent:16s}  child={p.child:16s}  "
            f"({p.x:g}, {p.y:g})  [{p.method}, n={p.contact_pairs}]"
        )
    print("Wrote:")
    for key in ("pivots_json", "pivots_overlay", "pivots_grid"):
        print(f"  {paths[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
