"""Hierarchical skeleton pose + part-layer transforms around pivots.

Rotates labeled part layers about articulation pivots (parent rotates children).
Each part is a full-canvas RGBA bitmap; rotation uses Pillow ``Image.rotate`` on
the whole layer about the joint pivot (Photoshop Free Transform), not
forward-scattering of sparse opaque pixels.

Joint names (from pivots / JOINT_SPECS)
---------------------------------------
  neck, shoulder_far, shoulder_near, elbow_far, elbow_near,
  hip_far, hip_near, knee_far, knee_near

Angles are degrees from the rest (base idle) pose. Positive = counterclockwise
in part-map pixel space (origin top-left, Y down).

For a +X-facing rest pose that means:
  - hip / shoulder: negative ≈ swing limb forward (+X), positive ≈ back (−X)
  - knee: positive ≈ flexion (shin folds toward −X; kneecap faces +X)
  - elbow: negative ≈ natural fold (forearm toward +X)
Negative knee angles hyperextend / bird-leg the shin — avoid for walks.

Limitations of pixel-layer rotation
-----------------------------------
- Rigid layer rotate: native-canvas BICUBIC about each joint pivot, composed
  into **one** affine per part (Photoshop Free Transform / parenting), then
  DRAW_ORDER ``alpha_composite``. Soft resample alpha is kept — no
  supersample→threshold morph that changes silhouette thickness or punches holes.
- Overlaps: later draw-order parts overwrite earlier ones via alpha composite.
  Side-view lock: far limbs → torso/head → near legs → near arms
  (``base_idle.DRAW_ORDER``).
- Hands/feet have no wrist/ankle joints; they follow lower_arm / lower_leg.
- Drafts for agent iteration; final game art may still want manual cleanup.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from PIL import Image

from .base_idle import (
    DRAW_ORDER,
    PART_EMPTY,
    PART_FOOT_FAR,
    PART_HAND_FAR,
    PART_LOWER_ARM_FAR,
    PART_LOWER_LEG_FAR,
    PART_NAMES,
    PART_UPPER_ARM_FAR,
    PART_UPPER_LEG_FAR,
    PartMap,
    SILHOUETTE_FILL,
    SILHOUETTE_SHADE,
    _stamp_eyes_detail,
    load_authored_eyes_coords,
    part_map_to_silhouette_grid,
)
from .pivots import JOINT_SPECS, Pivot, compute_all_pivots

# Joint process order: rootward joints before distal (shoulders before elbows, etc.).
JOINT_ORDER: tuple[str, ...] = tuple(name for name, _p, _c in JOINT_SPECS)

JOINT_BY_NAME: dict[str, tuple[str, str]] = {
    name: (parent, child) for name, parent, child in JOINT_SPECS
}

# Parts that follow a parent part without their own articulation pivot.
_IMPLICIT_PART_CHILDREN: dict[str, tuple[str, ...]] = {
    "lower_arm_far": ("hand_far",),
    "lower_arm_near": ("hand_near",),
    "lower_leg_far": ("foot_far",),
    "lower_leg_near": ("foot_near",),
}

_TRANSPARENT = (0, 0, 0, 0)
# Label-map only: count a soft-edge pixel as occupied. Does not alter silhouette.
_LABEL_ALPHA = 1
_IDENTITY_3 = (
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
)

# Far-limb silhouette shade (matches base_idle.part_map_to_silhouette_grid).
_FAR_PART_IDS: frozenset[int] = frozenset(
    {
        PART_UPPER_ARM_FAR,
        PART_LOWER_ARM_FAR,
        PART_HAND_FAR,
        PART_UPPER_LEG_FAR,
        PART_LOWER_LEG_FAR,
        PART_FOOT_FAR,
    }
)


def part_children(part: str) -> list[str]:
    """Direct child parts in the articulation tree (including implicit hands/feet)."""
    kids: list[str] = []
    for _name, parent, child in JOINT_SPECS:
        if parent == part:
            kids.append(child)
    kids.extend(_IMPLICIT_PART_CHILDREN.get(part, ()))
    return kids


def descendant_parts(root_part: str) -> list[str]:
    """``root_part`` plus all descendants (depth-first)."""
    out: list[str] = []
    stack = [root_part]
    seen: set[str] = set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        for child in reversed(part_children(p)):
            stack.append(child)
    return out


def zero_pose() -> dict[str, float]:
    """Rest pose: every joint at 0 degrees."""
    return {name: 0.0 for name in JOINT_ORDER}


def normalize_pose(angles: Mapping[str, float] | None = None) -> dict[str, float]:
    """Return a full joint-angle dict; unknown keys raise ``ValueError``."""
    pose = zero_pose()
    if not angles:
        return pose
    unknown = [k for k in angles if k not in JOINT_BY_NAME]
    if unknown:
        raise ValueError(
            f"Unknown joint name(s): {unknown}. "
            f"Valid: {', '.join(JOINT_ORDER)}"
        )
    for name, deg in angles.items():
        pose[name] = float(deg)
    return pose


def merge_pose(
    base: Mapping[str, float],
    overrides: Mapping[str, float] | None,
) -> dict[str, float]:
    """Absolute angles: ``overrides`` replace listed joints; others keep ``base``."""
    pose = normalize_pose(base)
    if not overrides:
        return pose
    unknown = [k for k in overrides if k not in JOINT_BY_NAME]
    if unknown:
        raise ValueError(
            f"Unknown joint name(s): {unknown}. "
            f"Valid: {', '.join(JOINT_ORDER)}"
        )
    for name, deg in overrides.items():
        pose[name] = float(deg)
    return pose


@dataclass(frozen=True)
class DesignLayerInput:
    """One outfit design layer parented to a single body export layer.

    ``parent_parts`` must be a single export-layer name (same as
    ``EXPORT_LAYER_NAMES`` / ``base/layers/``). The whole design PNG
    (including overhang) rigid-rotates 1:1 with that part — no multi-parent
    mask split. ``draw_after`` inserts the posed layer in DRAW_ORDER.
    """

    slot: str
    parent_parts: tuple[str, ...]
    draw_after: str
    image: Image.Image


@dataclass(frozen=True)
class PoseResult:
    """Rasterized posed part map plus transformed pivot positions."""

    part_map: PartMap
    pivots: list[Pivot]
    angles: dict[str, float]
    # RGBA silhouette from layered rotate+compose (Photoshop-style stack).
    silhouette: Image.Image | None = None
    # Outfit slots composited into ``silhouette`` (empty when base-only).
    design_slots: tuple[str, ...] = ()


def _rotate_point(
    x: float,
    y: float,
    cx: float,
    cy: float,
    degrees: float,
) -> tuple[float, float]:
    if degrees == 0.0:
        return (x, y)
    rad = math.radians(degrees)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    dx = x - cx
    dy = y - cy
    return (cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a)


def _part_layer(part_map: PartMap, part_name: str) -> Image.Image:
    """Full-canvas RGBA layer for one part (contiguous bitmap, not sparse points)."""
    w, h = part_map.width, part_map.height
    img = Image.new("RGBA", (w, h), _TRANSPARENT)
    pix = img.load()
    assert pix is not None
    pid = PART_NAMES[part_name]
    color = SILHOUETTE_SHADE if pid in _FAR_PART_IDS else SILHOUETTE_FILL
    for x, y in part_map.part_pixels(part_name):
        if 0 <= x < w and 0 <= y < h:
            pix[x, y] = color
    return img


def _solidify_layer_rgb(img: Image.Image, rgba: tuple[int, int, int, int]) -> Image.Image:
    """Keep resampled alpha; restore solid part RGB (drop BICUBIC color fringe)."""
    alpha = img.getchannel("A")
    out = Image.new("RGBA", img.size, (rgba[0], rgba[1], rgba[2], 0))
    out.putalpha(alpha)
    return out


def _matmul3(
    a: tuple[tuple[float, float, float], ...],
    b: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    rows: list[tuple[float, float, float]] = []
    for i in range(3):
        rows.append(
            (
                a[i][0] * b[0][0] + a[i][1] * b[1][0] + a[i][2] * b[2][0],
                a[i][0] * b[0][1] + a[i][1] * b[1][1] + a[i][2] * b[2][1],
                a[i][0] * b[0][2] + a[i][1] * b[1][2] + a[i][2] * b[2][2],
            )
        )
    return (rows[0], rows[1], rows[2])


def _rot_about_fwd(
    cx: float,
    cy: float,
    degrees: float,
) -> tuple[tuple[float, float, float], ...]:
    """Forward affine: rotate by ``degrees`` about ``(cx, cy)`` (same as ``_rotate_point``)."""
    rad = math.radians(degrees)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    return (
        (cos_a, -sin_a, cx - cos_a * cx + sin_a * cy),
        (sin_a, cos_a, cy - sin_a * cx - cos_a * cy),
        (0.0, 0.0, 1.0),
    )


def _invert_affine(
    m: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    a, b, c = m[0]
    d, e, f = m[1]
    det = a * e - b * d
    if abs(det) < 1e-12:
        return _IDENTITY_3
    ia, ib = e / det, -b / det
    id_, ie = -d / det, a / det
    ic = -(ia * c + ib * f)
    iff = -(id_ * c + ie * f)
    return ((ia, ib, ic), (id_, ie, iff), (0.0, 0.0, 1.0))


def _joints_affecting(part: str) -> list[str]:
    """Joints in ``JOINT_ORDER`` whose subtree rotation includes ``part``."""
    out: list[str] = []
    for joint_name in JOINT_ORDER:
        _parent, child = JOINT_BY_NAME[joint_name]
        if part in descendant_parts(child):
            out.append(joint_name)
    return out


def _rotate_layer(
    img: Image.Image,
    degrees: float,
    cx: float,
    cy: float,
) -> Image.Image:
    """Rotate an entire RGBA part layer about pivot ``(cx, cy)`` in canvas space.

    Equivalent to Photoshop Free Transform around a custom pivot:
    ``Image.rotate(-degrees, BICUBIC, center=(cx, cy), expand=False)``.
    """
    if degrees == 0.0:
        return img
    return img.rotate(
        -degrees,
        resample=Image.Resampling.BICUBIC,
        center=(cx, cy),
        expand=False,
        fillcolor=_TRANSPARENT,
    )


def _apply_composed_rotate(
    img: Image.Image,
    ops: Sequence[tuple[float, float, float]],
) -> Image.Image:
    """Apply hierarchical rotations as one BICUBIC affine (single resample).

    ``ops`` is ``(degrees, cx, cy)`` in root→distal order — the same sequence as
    successive ``_rotate_layer`` calls, but without stacking resampling blur.
    """
    if not ops:
        return img
    if len(ops) == 1:
        deg, cx, cy = ops[0]
        return _rotate_layer(img, deg, cx, cy)

    fwd: tuple[tuple[float, float, float], ...] = _IDENTITY_3
    any_rot = False
    for deg, cx, cy in ops:
        if deg == 0.0:
            continue
        any_rot = True
        fwd = _matmul3(_rot_about_fwd(cx, cy, deg), fwd)
    if not any_rot:
        return img
    inv = _invert_affine(fwd)
    coeff = (inv[0][0], inv[0][1], inv[0][2], inv[1][0], inv[1][1], inv[1][2])
    return img.transform(
        img.size,
        Image.Transform.AFFINE,
        coeff,
        resample=Image.Resampling.BICUBIC,
        fillcolor=_TRANSPARENT,
    )


def _part_ops_for_pose(
    pose: Mapping[str, float],
    applied_centers: Mapping[str, tuple[float, float]],
    part_name: str,
) -> list[tuple[float, float, float]]:
    """``(degrees, cx, cy)`` ops that pose ``part_name`` (root→distal)."""
    ops: list[tuple[float, float, float]] = []
    for joint_name in _joints_affecting(part_name):
        deg = float(pose[joint_name])
        if deg == 0.0:
            continue
        cx, cy = applied_centers[joint_name]
        ops.append((deg, cx, cy))
    return ops


def _transform_pixel_coords(
    coords: Sequence[tuple[int, int]],
    ops: Sequence[tuple[float, float, float]],
) -> list[tuple[int, int]]:
    """Map rest-space pixel coords through the same hierarchy ops as a part layer."""
    if not ops:
        return [(int(x), int(y)) for x, y in coords]
    out: list[tuple[int, int]] = []
    for x, y in coords:
        fx = float(x) + 0.5
        fy = float(y) + 0.5
        for deg, cx, cy in ops:
            fx, fy = _rotate_point(fx, fy, cx, cy, deg)
        out.append((int(fx), int(fy)))
    return out


def _resolve_eyes_detail(rest_map: PartMap) -> list[tuple[int, int]]:
    """Eyes always belong on the head; load from part map or authored base."""
    if rest_map.eyes_detail:
        return list(rest_map.eyes_detail)
    return load_authored_eyes_coords()


def _pose_design_layer(
    design: Image.Image,
    parent_parts: Sequence[str],
    draw_after: str,
    rest_map: PartMap,
    pose: Mapping[str, float],
    applied_centers: Mapping[str, tuple[float, float]],
) -> Image.Image:
    """Rigid-rotate a design layer 1:1 with its single parent export layer.

    ``parent_parts`` must contain exactly one part name (the export layer).
    The whole design PNG — including overhang — follows that part's pivot ops.
    Multi-parent mask splits are not used (design files match ``base/layers/``).
    """
    del draw_after, rest_map  # single-parent; draw_after used only at compose insert
    parents = [p for p in parent_parts if p in PART_NAMES and p != "empty"]
    if not parents:
        return design
    if len(parents) > 1:
        raise ValueError(
            "Design layers must have a single parent export layer "
            f"(got {parents!r}). Split paint into per-layer PNGs matching "
            "EXPORT_LAYER_NAMES / base/layers/."
        )
    ops = _part_ops_for_pose(pose, applied_centers, parents[0])
    return _apply_composed_rotate(design, ops) if ops else design


def apply_pose(
    rest_map: PartMap,
    rest_pivots: Sequence[Pivot],
    angles: Mapping[str, float] | None = None,
    design_layers: Sequence[DesignLayerInput] | None = None,
) -> PoseResult:
    """Apply hierarchical joint angles to the rest part map.

    Joints are processed in ``JOINT_ORDER``. Each non-zero angle contributes a
    rotation about that joint's *current* world-space pivot (from pivots.json,
    then updated when parents rotate). Distal joints use pivots already moved
    by parents — same hierarchy as Photoshop parenting.

    Each part layer is resampled **once**: ancestor joint rotations are composed
    into a single BICUBIC affine (equivalent to successive Free Transform
    rotates, without stacking blur). Absolute joint angles are applied once per
    joint in the chain — no double-counting.

    Uses full layered part pixels when ``rest_map.parts_full`` is set so far
    limbs keep under-torso geometry; DRAW_ORDER still resolves occlusion on
    compose.

    When ``design_layers`` is provided, each outfit layer is rigid-rotated 1:1
    with its single parent export layer and inserted after ``draw_after`` in
    DRAW_ORDER (overhang preserved; body is not a clip mask).
    """
    pose = normalize_pose(angles)
    w, h = rest_map.width, rest_map.height

    # Full-canvas RGBA layers at native resolution (Photoshop layer stack).
    layers: dict[str, Image.Image] = {
        name: _part_layer(rest_map, name)
        for name in PART_NAMES
        if name != "empty"
    }

    pivots_xy: dict[str, tuple[float, float]] = {
        p.name: (p.x, p.y) for p in rest_pivots
    }
    pivot_meta = {p.name: p for p in rest_pivots}

    # Exact pivot FK first; record the center used for each joint when it fires.
    applied_centers: dict[str, tuple[float, float]] = {}
    for joint_name in JOINT_ORDER:
        deg = pose[joint_name]
        cx, cy = pivots_xy[joint_name]
        applied_centers[joint_name] = (cx, cy)
        if deg == 0.0:
            continue
        _parent, child = JOINT_BY_NAME[joint_name]
        moved = descendant_parts(child)
        for other_name, (_op, other_child) in JOINT_BY_NAME.items():
            if other_name == joint_name:
                continue
            if other_child in moved or other_child == child:
                ox, oy = pivots_xy[other_name]
                pivots_xy[other_name] = _rotate_point(ox, oy, cx, cy, deg)

    # One BICUBIC resample per part (composed hierarchy), not stacked rotates.
    for part_name in list(layers.keys()):
        ops = _part_ops_for_pose(pose, applied_centers, part_name)
        if ops:
            layers[part_name] = _apply_composed_rotate(layers[part_name], ops)

    # Pose design layers (same rigid ops as parents).
    design_by_after: dict[str, list[tuple[str, Image.Image]]] = {}
    used_slots: list[str] = []
    if design_layers:
        for spec in design_layers:
            if spec.image.size != (w, h):
                raise ValueError(
                    f"Design layer '{spec.slot}' size {spec.image.size} != "
                    f"character canvas {(w, h)}"
                )
            if spec.image.getchannel("A").getextrema()[1] == 0:
                continue
            posed_design = _pose_design_layer(
                spec.image,
                spec.parent_parts,
                spec.draw_after,
                rest_map,
                pose,
                applied_centers,
            )
            design_by_after.setdefault(spec.draw_after, []).append(
                (spec.slot, posed_design)
            )
            used_slots.append(spec.slot)

    # Solidify RGB (keep soft alpha), alpha-composite in DRAW_ORDER.
    # Design layers insert after draw_after (full layer — overhang OK).
    # Eyes are white detail on the head: stamp after solidify using the same
    # hierarchy ops as the head layer so they rotate with the face.
    eye_coords = _resolve_eyes_detail(rest_map)
    labels = [[PART_EMPTY for _ in range(w)] for _ in range(h)]
    canvas = Image.new("RGBA", (w, h), _TRANSPARENT)
    for part_name in DRAW_ORDER:
        pid = PART_NAMES[part_name]
        layer = layers.get(part_name)
        if layer is None:
            continue
        color = SILHOUETTE_SHADE if pid in _FAR_PART_IDS else SILHOUETTE_FILL
        native = _solidify_layer_rgb(layer, color)
        if part_name == "head":
            head_ops = _part_ops_for_pose(pose, applied_centers, "head")
            posed_eyes = _transform_pixel_coords(eye_coords, head_ops)
            native = _stamp_eyes_detail(native, posed_eyes)
        canvas = Image.alpha_composite(canvas, native)
        pix = native.load()
        assert pix is not None
        for y in range(h):
            for x in range(w):
                if pix[x, y][3] >= _LABEL_ALPHA:
                    labels[y][x] = pid
        for _slot, posed_design in design_by_after.get(part_name, ()):
            canvas = Image.alpha_composite(canvas, posed_design)

    posed_map = PartMap(
        width=w,
        height=h,
        labels=labels,
        pose=f"posed:{rest_map.pose}",
        facing=rest_map.facing,
    )
    posed_pivots = [
        Pivot(
            name=name,
            parent=pivot_meta[name].parent,
            child=pivot_meta[name].child,
            x=round(pivots_xy[name][0], 2),
            y=round(pivots_xy[name][1], 2),
            method=pivot_meta[name].method,
            contact_pairs=pivot_meta[name].contact_pairs,
        )
        for name in JOINT_ORDER
    ]
    return PoseResult(
        part_map=posed_map,
        pivots=posed_pivots,
        angles=pose,
        silhouette=canvas,
        design_slots=tuple(used_slots),
    )


def pose_to_json_dict(
    angles: Mapping[str, float],
    pivots: Sequence[Pivot],
    *,
    frame_index: int | None = None,
    animation_name: str | None = None,
    character: str | None = None,
    finalized: bool = False,
) -> dict:
    """Structured pose state for agents (joint angles + pivot xy)."""
    return {
        "character": character,
        "animation_name": animation_name,
        "frame_index": frame_index,
        "finalized": finalized,
        "frame_count": 8,
        "angle_units": "degrees",
        "angle_convention": (
            "Degrees from rest (base idle). Positive = counterclockwise "
            "in part-map pixel space (origin top-left, Y down). "
            "For +X-facing: negative hip/shoulder ≈ forward, positive ≈ back; "
            "positive knee ≈ flexion (shin toward -X)."
        ),
        "joint_names": list(JOINT_ORDER),
        "angles": {name: float(angles.get(name, 0.0)) for name in JOINT_ORDER},
        "joints": [
            {
                "name": p.name,
                "parent": p.parent,
                "child": p.child,
                "x": p.x,
                "y": p.y,
                "angle_deg": float(angles.get(p.name, 0.0)),
            }
            for p in pivots
        ],
        "limitations": (
            "Layer rotate via one Pillow BICUBIC affine per part (composed "
            "hierarchy, expand=False pivot-fixed), alpha-composite in "
            "DRAW_ORDER. Soft resample alpha is kept (no hard threshold morph). "
            "Hands/feet follow lower limbs (no wrist/ankle). "
            "When design layers exist, each export-layer PNG rigid-rotates 1:1 "
            "with its single parent (same name as base/layers/) and inserts "
            "after draw_after — body is not a clip; no multi-parent mask split."
        ),
    }


def rasterize_silhouette(part_map: PartMap) -> list[list[tuple[int, int, int, int]]]:
    """Silhouette color grid for the posed part map."""
    return part_map_to_silhouette_grid(part_map)


def rest_assets(part_map: PartMap | None = None) -> tuple[PartMap, list[Pivot]]:
    """Build rest part map + pivots (optionally reuse an existing map)."""
    from .base_idle import build_base_idle_part_map

    pm = part_map or build_base_idle_part_map()
    return pm, compute_all_pivots(pm)


def joint_docs() -> list[dict[str, str]]:
    """Human/agent documentation for each rotatable joint."""
    return [
        {
            "name": name,
            "parent": parent,
            "child": child,
            "rotates": ", ".join(descendant_parts(child)),
        }
        for name, parent, child in JOINT_SPECS
    ]
