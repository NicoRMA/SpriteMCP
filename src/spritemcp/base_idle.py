"""Base character idle template — naked/base body part-id map (side-view).

Design lock
-----------
- Characters are **always true side view** (lateral profile), never front or ¾.
- ``*_far`` = away from camera (**behind** the torso). ``*_near`` = toward camera
  (**in front** of the torso).
- Geometry is stored as **full separate layers** (far limbs keep pixels under the
  torso). Occlusion happens only at **compose** time via ``DRAW_ORDER``.
- Compose / paint / pose rasterize order is always: far limbs → torso/head →
  near legs → near arms (see ``DRAW_ORDER``). Near arms stay in front of
  near thighs.

Canvas is authored 90×128 from ``spritemcp/base/`` (masks + eyes detail).
Does NOT copy clothed references; clothes/equipment come later.

Run:
  python -m spritemcp base-idle
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from PIL import Image, ImageDraw, ImageFont

from .config import MINI_PROJECT_ROOT, authored_base_dir, default_output_dir, subdir
from .export import grid_to_image, save_grid
from .palette import NEAR_BLACK, OFF_WHITE, SHADOW_BEIGE, TRANSPARENT, Color
from .show_ref_grid import render_ref_grid

# Authored 90×128 armature (only canvas).
CANVAS_W = 90
CANVAS_H = 128
TARGET_HEIGHT_PX = 105
OPAQUE_HEIGHT_LO = 100
OPAQUE_HEIGHT_HI = 110
ARMATURE_NAME = "standard_90x128"

# Hand-authored 90×128 Aseprite export (masks + eyes detail).
# Resolved at call time via authored_base_dir() so uvx/wheel installs work.
AUTHORED_BASE_SOURCE_DIR = authored_base_dir()
EYES_DETAIL_RGBA: Color = (255, 255, 255, 255)

# CLI argparse suggestion only — real writes use resolve_output_dir / session root.
DEFAULT_OUT_DIR = default_output_dir() / "base_idle_90x128"

# Knee columns on the authored silhouette (shin widths at the knee band).
# Far shin x=47..52 (w=6); near shin x=36..41 (w=6).
FAR_LEG_COLUMN: tuple[float, int] = (47.0, 6)
NEAR_LEG_COLUMN: tuple[float, int] = (36.0, 6)


def default_base_idle_out_dir(output_dir: Path | str | None = None) -> Path:
    """Shared naked base idle folder under the active output root."""
    return subdir("base_idle_90x128", output_dir)

# ---------------------------------------------------------------------------
# Part ids (machine + human readable)
# ---------------------------------------------------------------------------

PART_EMPTY = 0
PART_HEAD = 1
PART_TORSO = 2
PART_UPPER_ARM_FAR = 3
PART_LOWER_ARM_FAR = 4
PART_HAND_FAR = 5
PART_UPPER_ARM_NEAR = 6
PART_LOWER_ARM_NEAR = 7
PART_HAND_NEAR = 8
PART_UPPER_LEG_FAR = 9
PART_LOWER_LEG_FAR = 10
PART_FOOT_FAR = 11
PART_UPPER_LEG_NEAR = 12
PART_LOWER_LEG_NEAR = 13
PART_FOOT_NEAR = 14

# Stable name → id (JSON / docs). "far" = away from camera, "near" = toward camera.
PART_NAMES: dict[str, int] = {
    "empty": PART_EMPTY,
    "head": PART_HEAD,
    "torso": PART_TORSO,
    "upper_arm_far": PART_UPPER_ARM_FAR,
    "lower_arm_far": PART_LOWER_ARM_FAR,
    "hand_far": PART_HAND_FAR,
    "upper_arm_near": PART_UPPER_ARM_NEAR,
    "lower_arm_near": PART_LOWER_ARM_NEAR,
    "hand_near": PART_HAND_NEAR,
    "upper_leg_far": PART_UPPER_LEG_FAR,
    "lower_leg_far": PART_LOWER_LEG_FAR,
    "foot_far": PART_FOOT_FAR,
    "upper_leg_near": PART_UPPER_LEG_NEAR,
    "lower_leg_near": PART_LOWER_LEG_NEAR,
    "foot_near": PART_FOOT_NEAR,
}

PART_ID_TO_NAME: dict[int, str] = {v: k for k, v in PART_NAMES.items()}

# Permanent z-order for side-view compose (paint + pose rasterize).
# Later names overwrite earlier ones where pixels collide.
# Near legs before near arms so the near arm stays in front of the thigh.
DRAW_ORDER: tuple[str, ...] = (
    "upper_arm_far",
    "lower_arm_far",
    "hand_far",
    "upper_leg_far",
    "lower_leg_far",
    "foot_far",
    "torso",
    "head",
    "upper_leg_near",
    "lower_leg_near",
    "foot_near",
    "upper_arm_near",
    "lower_arm_near",
    "hand_near",
)

# Export layer groups (user-confirmed). lower_* layers include hand/foot parts.
# Order matches compose: far limbs → torso/head → near legs → near arms.
EXPORT_LAYERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("upper_arm_far", ("upper_arm_far",)),
    ("lower_arm_far", ("lower_arm_far", "hand_far")),
    ("upper_leg_far", ("upper_leg_far",)),
    ("lower_leg_far", ("lower_leg_far", "foot_far")),
    ("torso", ("torso",)),
    ("head", ("head",)),
    ("upper_leg_near", ("upper_leg_near",)),
    ("lower_leg_near", ("lower_leg_near", "foot_near")),
    ("upper_arm_near", ("upper_arm_near",)),
    ("lower_arm_near", ("lower_arm_near", "hand_near")),
)

EXPORT_LAYER_NAMES: tuple[str, ...] = tuple(name for name, _parts in EXPORT_LAYERS)
EXPORT_LAYER_PARTS: dict[str, tuple[str, ...]] = dict(EXPORT_LAYERS)

VIEW_MODE = "side"
FACING_DEFAULT = "+X"
LAYERING_RULE = (
    "side_view: *_far behind torso, *_near in front; "
    "full separate layers (far limbs keep geometry under torso); "
    "occlusion only at compose via DRAW_ORDER "
    "(far limbs -> torso/head -> near legs -> near arms); "
    "shoulder_far/near and hip_far/near are independent lateral attachments "
    "on each limb mass (depth is always z-order via DRAW_ORDER)"
)

# Distinct viz colors (not clothing palette — for part-map readability only).
PART_VIZ_COLORS: dict[int, Color] = {
    PART_EMPTY: TRANSPARENT,
    PART_HEAD: (240, 210, 120, 255),  # warm yellow (distinct from torso)
    PART_TORSO: (210, 168, 140, 255),  # flesh mid
    PART_UPPER_ARM_FAR: (120, 170, 220, 255),
    PART_LOWER_ARM_FAR: (80, 140, 200, 255),
    PART_HAND_FAR: (40, 110, 180, 255),
    PART_UPPER_ARM_NEAR: (220, 120, 100, 255),
    PART_LOWER_ARM_NEAR: (200, 80, 70, 255),
    PART_HAND_NEAR: (180, 50, 50, 255),
    PART_UPPER_LEG_FAR: (140, 200, 120, 255),
    PART_LOWER_LEG_FAR: (90, 170, 90, 255),
    PART_FOOT_FAR: (50, 130, 60, 255),
    PART_UPPER_LEG_NEAR: (200, 160, 80, 255),
    PART_LOWER_LEG_NEAR: (180, 130, 50, 255),
    PART_FOOT_NEAR: (150, 100, 30, 255),
}

# Silhouette preview: flat flesh from style lock (unclothed base, not kimono).
SILHOUETTE_FILL: Color = OFF_WHITE
SILHOUETTE_SHADE: Color = SHADOW_BEIGE  # far limbs only (one darker step)


@dataclass
class PartMap:
    """Labeled pixel grid: each cell is a part id (usually composed view)."""

    width: int
    height: int
    labels: list[list[int]]  # [y][x]
    pose: str = "side_idle_apose"
    facing: str = "+X"
    # Full per-part pixel coords from layered storage (before compose occlusion).
    # When set, pose/articulation uses these instead of composed labels.
    parts_full: dict[str, list[tuple[int, int]]] | None = field(default=None)
    # White eye pixels from authored base/eyes.png (detail on head, not a part id).
    eyes_detail: list[tuple[int, int]] | None = field(default=None)

    def flat(self) -> list[int]:
        return [self.labels[y][x] for y in range(self.height) for x in range(self.width)]

    def used_parts(self) -> list[str]:
        present = {cell for row in self.labels for cell in row if cell != PART_EMPTY}
        return [PART_ID_TO_NAME[i] for i in sorted(present)]

    def opaque_bbox(self) -> tuple[int, int, int, int] | None:
        """Exclusive (x0, y0, x1, y1) of non-empty cells."""
        min_x, min_y = self.width, self.height
        max_x, max_y = -1, -1
        for y in range(self.height):
            for x in range(self.width):
                if self.labels[y][x] != PART_EMPTY:
                    min_x = min(min_x, x)
                    min_y = min(min_y, y)
                    max_x = max(max_x, x)
                    max_y = max(max_y, y)
        if max_x < 0:
            return None
        return (min_x, min_y, max_x + 1, max_y + 1)

    def height_px(self) -> int:
        box = self.opaque_bbox()
        if box is None:
            return 0
        return box[3] - box[1]

    def part_pixels(self, part_name: str) -> list[tuple[int, int]]:
        """Pixels for a part: prefer full layered geometry when available."""
        if self.parts_full is not None and part_name in self.parts_full:
            return list(self.parts_full[part_name])
        pid = PART_NAMES[part_name]
        return [
            (x, y)
            for y in range(self.height)
            for x in range(self.width)
            if self.labels[y][x] == pid
        ]


@dataclass(frozen=True)
class LayerStack:
    """Full separate layers; occlusion only when composed."""

    width: int
    height: int
    # layer_name → part-id grid (empty=0). Far layers keep under-torso pixels.
    layers: dict[str, list[list[int]]]
    pose: str = "side_idle_apose"
    facing: str = "+X"

    def compose(self) -> PartMap:
        """Flatten with DRAW_ORDER; later parts overwrite earlier on collision."""
        labels = _blank(self.width, self.height)
        layer_of_part = {
            part: layer_name
            for layer_name, parts in EXPORT_LAYERS
            for part in parts
        }
        for part_name in DRAW_ORDER:
            layer_name = layer_of_part.get(part_name)
            if layer_name is None:
                continue
            grid = self.layers[layer_name]
            pid = PART_NAMES[part_name]
            for y in range(self.height):
                for x in range(self.width):
                    if grid[y][x] == pid:
                        labels[y][x] = pid
        return PartMap(
            width=self.width,
            height=self.height,
            labels=labels,
            pose=self.pose,
            facing=self.facing,
            parts_full=self.parts_full_coords(),
        )

    def parts_full_coords(self) -> dict[str, list[tuple[int, int]]]:
        out: dict[str, list[tuple[int, int]]] = {
            name: [] for name in PART_NAMES if name != "empty"
        }
        for layer_name, parts in EXPORT_LAYERS:
            grid = self.layers[layer_name]
            wanted = {PART_NAMES[p] for p in parts}
            for y in range(self.height):
                for x in range(self.width):
                    pid = grid[y][x]
                    if pid in wanted:
                        out[PART_ID_TO_NAME[pid]].append((x, y))
        return out

    def layer_color_grid(self, layer_name: str) -> list[list[Color]]:
        grid = self.layers[layer_name]
        return [
            [PART_VIZ_COLORS.get(grid[y][x], NEAR_BLACK) for x in range(self.width)]
            for y in range(self.height)
        ]


def _blank(w: int, h: int) -> list[list[int]]:
    return [[PART_EMPTY for _ in range(w)] for _ in range(h)]



# Atomic Aseprite PNGs under AUTHORED_BASE_SOURCE_DIR → (export_layer, part_name).
# Hands/feet merge into lower_* export layers (pipeline EXPORT_LAYERS rule).
_AUTHORED_ATOMICS: tuple[tuple[str, str, str], ...] = (
    ("head.png", "head", "head"),
    ("torso.png", "torso", "torso"),
    ("upper_arm_far.png", "upper_arm_far", "upper_arm_far"),
    ("lower_arm_far.png", "lower_arm_far", "lower_arm_far"),
    ("hand_far.png", "lower_arm_far", "hand_far"),
    ("upper_leg_far.png", "upper_leg_far", "upper_leg_far"),
    ("lower_leg_far.png", "lower_leg_far", "lower_leg_far"),
    ("foot_far.png", "lower_leg_far", "foot_far"),
    ("upper_arm_near.png", "upper_arm_near", "upper_arm_near"),
    ("lower_arm_near.png", "lower_arm_near", "lower_arm_near"),
    ("hand_near.png", "lower_arm_near", "hand_near"),
    ("upper_leg_near.png", "upper_leg_near", "upper_leg_near"),
    ("lower_leg_near.png", "lower_leg_near", "lower_leg_near"),
    ("foot_near.png", "lower_leg_near", "foot_near"),
)

_AUTHORED_EYES_FILE = "eyes.png"


def authored_base_source_dir() -> Path:
    """Directory with the user-authored 90×128 part PNGs.

    Checkout: ``<repo>/base/``. Installed (uvx/wheel): packaged
    ``spritemcp/authored_base/``.
    """
    return authored_base_dir()


def _rgba_opaque_coords(path: Path) -> list[tuple[int, int]]:
    img = Image.open(path).convert("RGBA")
    w, h = img.size
    return [
        (x, y)
        for y in range(h)
        for x in range(w)
        if img.getpixel((x, y))[3] > 0
    ]


def load_authored_eyes_coords(
    source_dir: Path | None = None,
) -> list[tuple[int, int]]:
    """Opaque pixels from authored ``eyes.png`` (required visual detail on head).

    Eyes are not a skeleton part id — they are always composited onto the head
    layer / face previews. Raises if ``eyes.png`` is missing or has no opaque
    pixels (import must never silently drop the face detail).
    """
    root = Path(source_dir) if source_dir is not None else authored_base_source_dir()
    eyes_path = root / _AUTHORED_EYES_FILE
    if not eyes_path.is_file():
        raise FileNotFoundError(
            f"Required authored face detail missing: {eyes_path}. "
            "Export eyes.png (white eye pixels) with the rest of spritemcp/base/."
        )
    coords = _rgba_opaque_coords(eyes_path)
    if not coords:
        raise ValueError(
            f"Authored {eyes_path} has no opaque pixels; eyes must be present "
            "on the face/head."
        )
    return coords


def eyes_coords_from_part_map_payload(
    payload: Mapping,
    *,
    source_dir: Path | None = None,
) -> list[tuple[int, int]]:
    """Eyes from a saved ``part_map.json``, else mandatory authored ``eyes.png``."""
    raw = payload.get("eyes_detail") if isinstance(payload, Mapping) else None
    if isinstance(raw, Mapping):
        pixels = raw.get("pixels")
        if isinstance(pixels, list) and pixels:
            return [(int(p[0]), int(p[1])) for p in pixels]
    return load_authored_eyes_coords(source_dir)


def authored_source_ready(source_dir: Path | None = None) -> bool:
    """True when every atomic body PNG and required ``eyes.png`` are present."""
    root = Path(source_dir) if source_dir is not None else authored_base_source_dir()
    if not (root / _AUTHORED_EYES_FILE).is_file():
        return False
    return all((root / filename).is_file() for filename, _layer, _part in _AUTHORED_ATOMICS)


def load_authored_layers(source_dir: Path | None = None) -> LayerStack:
    """Import user Aseprite part masks into a part-id ``LayerStack``.

    Opaque pixels become part ids (source colors are ignored). ``eyes.png`` is
    required (not a part id): those pixels are forced onto the head mask and
    stamped as white detail onto head layer / compose / silhouette later.
    """
    root = Path(source_dir) if source_dir is not None else authored_base_source_dir()
    width, height = CANVAS_W, CANVAS_H
    if not authored_source_ready(root):
        missing = [
            filename
            for filename, _layer, _part in _AUTHORED_ATOMICS
            if not (root / filename).is_file()
        ]
        if not (root / _AUTHORED_EYES_FILE).is_file():
            missing.append(_AUTHORED_EYES_FILE)
        raise FileNotFoundError(
            f"Authored 90×128 base incomplete under {root}: missing {missing}"
        )

    layers = {name: _blank(width, height) for name in EXPORT_LAYER_NAMES}
    for filename, layer_name, part_name in _AUTHORED_ATOMICS:
        path = root / filename
        img = Image.open(path).convert("RGBA")
        if img.size != (width, height):
            raise ValueError(
                f"{path} size {img.size} does not match canvas {width}x{height}"
            )
        pid = PART_NAMES[part_name]
        grid = layers[layer_name]
        for y in range(height):
            for x in range(width):
                if img.getpixel((x, y))[3] > 0:
                    grid[y][x] = pid

    # Eyes are painted detail on head (already inside head mask). Ensure head
    # still owns those pixels if a future export ever separates them.
    for x, y in load_authored_eyes_coords(root):
        if 0 <= x < width and 0 <= y < height:
            layers["head"][y][x] = PART_HEAD

    return LayerStack(
        width=width,
        height=height,
        layers=layers,
        pose="side_idle_apose",
        facing=FACING_DEFAULT,
    )


def _stamp_eyes_detail(
    image: Image.Image,
    eye_coords: list[tuple[int, int]],
    *,
    scale: int = 1,
) -> Image.Image:
    """Paint white eye pixels onto an RGBA preview (native or nearest-scaled)."""
    if not eye_coords:
        return image
    out = image.convert("RGBA").copy()
    px = out.load()
    assert px is not None
    for x, y in eye_coords:
        if scale <= 1:
            if 0 <= x < out.width and 0 <= y < out.height:
                px[x, y] = EYES_DETAIL_RGBA
            continue
        for dy in range(scale):
            for dx in range(scale):
                sx = x * scale + dx
                sy = y * scale + dy
                if 0 <= sx < out.width and 0 <= sy < out.height:
                    px[sx, sy] = EYES_DETAIL_RGBA
    return out


def build_base_idle_layers(
    *,
    width: int | None = None,
    height: int | None = None,
) -> LayerStack:
    """Import authored 90×128 layers from ``spritemcp/base/``.

    Explicit ``width``/``height`` must match the canvas when provided.
    """
    if width is not None and width != CANVAS_W:
        raise ValueError(f"width={width} does not match canvas_w={CANVAS_W}")
    if height is not None and height != CANVAS_H:
        raise ValueError(f"height={height} does not match canvas_h={CANVAS_H}")
    return load_authored_layers()


def _warn_opaque_height(part_map: PartMap) -> None:
    h_px = part_map.height_px()
    if not (OPAQUE_HEIGHT_LO <= h_px <= OPAQUE_HEIGHT_HI):
        print(
            f"Warning: opaque height is {h_px} px "
            f"(authored armature prefers ~{OPAQUE_HEIGHT_LO}–{OPAQUE_HEIGHT_HI}).",
            file=sys.stderr,
        )


def build_base_idle_part_map(
    *,
    width: int | None = None,
    height: int | None = None,
) -> PartMap:
    """Compose layered idle into a part-id map (occlusion at compose only)."""
    stack = build_base_idle_layers(width=width, height=height)
    part_map = stack.compose()
    _warn_opaque_height(part_map)
    return part_map


def part_map_to_color_grid(
    part_map: PartMap,
    colors: Mapping[int, Color] | None = None,
) -> list[list[Color]]:
    palette = colors or PART_VIZ_COLORS
    return [
        [palette.get(part_map.labels[y][x], NEAR_BLACK) for x in range(part_map.width)]
        for y in range(part_map.height)
    ]


def part_map_to_silhouette_grid(part_map: PartMap) -> list[list[Color]]:
    """Flat unclothed preview: light flesh; far limbs one shadow step."""
    far_parts = {
        PART_UPPER_ARM_FAR,
        PART_LOWER_ARM_FAR,
        PART_HAND_FAR,
        PART_UPPER_LEG_FAR,
        PART_LOWER_LEG_FAR,
        PART_FOOT_FAR,
    }
    out: list[list[Color]] = []
    for y in range(part_map.height):
        row: list[Color] = []
        for x in range(part_map.width):
            pid = part_map.labels[y][x]
            if pid == PART_EMPTY:
                row.append(TRANSPARENT)
            elif pid in far_parts:
                row.append(SILHOUETTE_SHADE)
            else:
                row.append(SILHOUETTE_FILL)
        out.append(row)
    return out


def _hex_rgb(color: Color) -> str:
    return f"#{color[0]:02X}{color[1]:02X}{color[2]:02X}"



def part_map_to_json_dict(part_map: PartMap) -> dict:
    legend = {
        name: {
            "id": pid,
            "color": _hex_rgb(PART_VIZ_COLORS[pid]) if pid != PART_EMPTY else None,
        }
        for name, pid in PART_NAMES.items()
    }
    box = part_map.opaque_bbox()
    parts_full = part_map.parts_full or {}
    layering = (
        "side_view: *_far behind torso, *_near in front; "
        "full separate layers (far limbs keep geometry under torso); "
        "occlusion only at compose via DRAW_ORDER "
        "(far limbs -> torso/head -> near legs -> near arms); "
        "shoulder_far/near and hip_far/near are independent "
        "lateral attachments on each limb mass (not a shared stack XY)"
    )
    description = (
        "Side-view (lateral only) naked/base idle (slight A-pose, upright "
        "torso). Geometry is stored as full separate layers; *_far limbs keep "
        "pixels under the torso. Occlusion is apply-only at compose via "
        "DRAW_ORDER (far limbs → torso/head → near legs → near arms). "
        "Authored 90×128 silhouette uses per-limb shoulder and hip pivots "
        "(laterally separated far/near masses). Eyes from base/eyes.png are "
        "mandatory white detail on the head (not a part id). No hat, kimono, "
        "hakama, armor, or weapon. Clothes/equipment layer on later."
    )
    return {
        "width": part_map.width,
        "height": part_map.height,
        "armature": ARMATURE_NAME,
        "pose": part_map.pose,
        "facing": part_map.facing,
        "view": VIEW_MODE,
        "storage": "layered",
        "layering": layering,
        "draw_order": list(DRAW_ORDER),
        "export_layers": [
            {"name": name, "parts": list(parts)} for name, parts in EXPORT_LAYERS
        ],
        "description": description,
        "stacked_joints": None,
        "opaque_bbox": (
            None
            if box is None
            else {"x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]}
        ),
        "opaque_height_px": part_map.height_px(),
        "parts": {name: pid for name, pid in PART_NAMES.items()},
        "legend": legend,
        "pixels": part_map.flat(),
        "parts_full": {
            name: [[x, y] for x, y in coords]
            for name, coords in parts_full.items()
            if coords
        },
    }


def layers_to_json_dict(stack: LayerStack) -> dict:
    return {
        "width": stack.width,
        "height": stack.height,
        "pose": stack.pose,
        "facing": stack.facing,
        "view": VIEW_MODE,
        "draw_order": list(DRAW_ORDER),
        "export_layers": [
            {"name": name, "parts": list(parts)} for name, parts in EXPORT_LAYERS
        ],
        "layers": {
            name: {
                "parts": list(EXPORT_LAYER_PARTS[name]),
                "pixels": [
                    stack.layers[name][y][x]
                    for y in range(stack.height)
                    for x in range(stack.width)
                ],
            }
            for name in EXPORT_LAYER_NAMES
        },
    }


def render_legend_image(
    *,
    cell: int = 14,
    padding: int = 8,
) -> Image.Image:
    """Vertical swatch list for part ids (excludes empty)."""
    entries = [(name, pid) for name, pid in PART_NAMES.items() if pid != PART_EMPTY]
    try:
        font = ImageFont.load_default()
    except OSError:
        font = None

    row_h = cell + 4
    text_w = 140
    img_w = padding * 2 + cell + 8 + text_w
    img_h = padding * 2 + row_h * len(entries) + 18
    img = Image.new("RGBA", (img_w, img_h), (32, 32, 32, 255))
    draw = ImageDraw.Draw(img)
    draw.text((padding, 4), "part id legend", fill=(220, 220, 220, 255), font=font)

    for i, (name, pid) in enumerate(entries):
        y = padding + 14 + i * row_h
        color = PART_VIZ_COLORS[pid]
        draw.rectangle(
            [padding, y, padding + cell - 1, y + cell - 1],
            fill=color,
            outline=(20, 20, 20, 255),
        )
        draw.text(
            (padding + cell + 8, y + 2),
            f"{pid:02d}  {name}",
            fill=(220, 220, 220, 255),
            font=font,
        )
    return img


def render_part_map_grid_with_legend(
    part_color_img: Image.Image,
    *,
    scale: int = 8,
) -> Image.Image:
    """Grid + axes (show_ref_grid style) with a legend strip on the right."""
    grid_img = render_ref_grid(
        part_color_img,
        scale=scale,
        grid_line_width=1,
        label_every=5,
        draw_bbox=True,
        grid_rgba=(40, 40, 40, 160),
    )
    legend = render_legend_image(cell=max(12, scale + 4))

    gap = 12
    out_w = grid_img.width + gap + legend.width
    out_h = max(grid_img.height, legend.height + 16)
    out = Image.new("RGBA", (out_w, out_h), (32, 32, 32, 255))
    out.paste(grid_img, (0, 0), grid_img)
    legend_y = 16
    out.paste(legend, (grid_img.width + gap, legend_y), legend)
    return out


def render_layers_strip(
    stack: LayerStack,
    *,
    scale: int = 8,
    gap: int = 8,
) -> Image.Image:
    """Horizontal strip: each export layer scaled with a name label."""
    try:
        font = ImageFont.load_default()
    except OSError:
        font = None

    thumbs: list[tuple[str, Image.Image]] = []
    for name in EXPORT_LAYER_NAMES:
        color_grid = stack.layer_color_grid(name)
        native = grid_to_image(color_grid)
        scaled = native.resize(
            (native.width * scale, native.height * scale),
            Image.Resampling.NEAREST,
        )
        thumbs.append((name, scaled))

    label_h = 14
    cell_w = thumbs[0][1].width if thumbs else 0
    cell_h = thumbs[0][1].height if thumbs else 0
    n = len(thumbs)
    out_w = gap + n * (cell_w + gap)
    out_h = gap + label_h + cell_h + gap
    out = Image.new("RGBA", (out_w, out_h), (28, 28, 32, 255))
    draw = ImageDraw.Draw(out)

    for i, (name, thumb) in enumerate(thumbs):
        x = gap + i * (cell_w + gap)
        y = gap + label_h
        # Checker-ish dark panel behind transparent pixels.
        draw.rectangle(
            [x, y, x + cell_w - 1, y + cell_h - 1],
            fill=(40, 40, 48, 255),
            outline=(60, 60, 70, 255),
        )
        out.paste(thumb, (x, y), thumb)
        draw.text((x, gap), name, fill=(200, 200, 200, 255), font=font)
    return out



def build_outputs(
    out_dir: Path,
    *,
    scale: int = 8,
) -> tuple[PartMap, dict[str, Path]]:
    """Write layered PNGs + composed part map / previews under ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stack = build_base_idle_layers()
    part_map = stack.compose()
    _warn_opaque_height(part_map)

    # Mandatory: eyes.png must exist and have opaque pixels.
    eye_coords = load_authored_eyes_coords()
    part_map.eyes_detail = list(eye_coords)

    json_path = out_dir / "part_map.json"
    part_map_payload = part_map_to_json_dict(part_map)
    part_map_payload["eyes_detail"] = {
        "part": "head",
        "note": (
            "White eye pixels are painted detail on the head (not a part id). "
            "Always composited from authored base/eyes.png onto head layer, "
            "compose, and silhouette. Outfit design on head may cover them."
        ),
        "pixels": [[x, y] for x, y in eye_coords],
    }
    json_path.write_text(
        json.dumps(part_map_payload, indent=2) + "\n",
        encoding="utf-8",
    )

    layers_dir = out_dir / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)
    layers_json = layers_dir / "layers.json"
    layers_json.write_text(
        json.dumps(layers_to_json_dict(stack), indent=2) + "\n",
        encoding="utf-8",
    )

    layer_png_paths: dict[str, Path] = {}
    for name in EXPORT_LAYER_NAMES:
        color_grid = stack.layer_color_grid(name)
        if name == "head":
            head_img = _stamp_eyes_detail(grid_to_image(color_grid), eye_coords)
            path = layers_dir / f"{name}.png"
            head_img.save(path)
            layer_png_paths[name] = path
        else:
            path = save_grid(color_grid, layers_dir / f"{name}.png")
            layer_png_paths[name] = path

    color_grid = part_map_to_color_grid(part_map)
    sil_grid = part_map_to_silhouette_grid(part_map)

    part_map_png = save_grid(color_grid, out_dir / "part_map.png")
    silhouette_img = _stamp_eyes_detail(grid_to_image(sil_grid), eye_coords)
    silhouette_png = out_dir / "silhouette.png"
    silhouette_img.save(silhouette_png)
    compose_img = _stamp_eyes_detail(grid_to_image(color_grid), eye_coords)
    compose_preview_png = out_dir / "compose_preview.png"
    compose_img.save(compose_preview_png)

    color_img = _stamp_eyes_detail(grid_to_image(color_grid), eye_coords)
    grid_with_legend = render_part_map_grid_with_legend(color_img, scale=scale)
    grid_path = out_dir / "part_map_grid.png"
    grid_with_legend.save(grid_path)
    layers_grid_path = out_dir / "layers_grid.png"
    grid_with_legend.save(layers_grid_path)

    legend_path = out_dir / "legend.png"
    render_legend_image().save(legend_path)

    strip = render_layers_strip(stack, scale=scale)
    try:
        head_index = EXPORT_LAYER_NAMES.index("head")
    except ValueError:
        head_index = -1
    if head_index >= 0:
        gap = 8
        label_h = 14
        cell_w = part_map.width * scale
        cell_h = part_map.height * scale
        x0 = gap + head_index * (cell_w + gap)
        y0 = gap + label_h
        head_thumb = strip.crop((x0, y0, x0 + cell_w, y0 + cell_h))
        head_thumb = _stamp_eyes_detail(head_thumb, eye_coords, scale=scale)
        strip.paste(head_thumb, (x0, y0), head_thumb)
    strip_path = out_dir / "layers_strip.png"
    strip.save(strip_path)

    eyes_path = out_dir / "eyes_detail.png"
    eyes_img = Image.new("RGBA", (part_map.width, part_map.height), TRANSPARENT)
    eyes_img = _stamp_eyes_detail(eyes_img, eye_coords)
    eyes_img.save(eyes_path)

    source_meta = {
        "armature": ARMATURE_NAME,
        "source_dir": str(authored_base_source_dir()),
        "canvas": {"w": part_map.width, "h": part_map.height},
        "eyes_pixels": len(eye_coords),
        "note": (
            "Geometry imported from user-authored base/ PNGs; "
            "eyes.png is required and always composited onto the head "
            "(visual detail, not a skeleton part id)."
        ),
    }
    source_json = out_dir / "authored_source.json"
    source_json.write_text(
        json.dumps(source_meta, indent=2) + "\n",
        encoding="utf-8",
    )

    paths: dict[str, Path] = {
        "part_map_json": json_path,
        "part_map": part_map_png,
        "part_map_grid": grid_path,
        "layers_grid": layers_grid_path,
        "compose_preview": compose_preview_png,
        "silhouette": silhouette_png,
        "legend": legend_path,
        "layers_json": layers_json,
        "layers_strip": strip_path,
        "layers_dir": layers_dir,
        "eyes_detail": eyes_path,
        "authored_source_json": source_json,
    }
    for name, path in layer_png_paths.items():
        paths[f"layer_{name}"] = path
    return part_map, paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build naked/base side-view idle as full separate layers "
            "(compose occlusion; no clothes/hat/weapon)."
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
        help="Nearest-neighbor upscale for grid / strip PNGs",
    )
    args = parser.parse_args(argv)

    out_dir = args.out_dir if args.out_dir is not None else default_base_idle_out_dir()
    part_map, paths = build_outputs(out_dir, scale=args.scale)
    box = part_map.opaque_bbox()

    print(f"Armature: {ARMATURE_NAME}")
    print(f"Canvas: {part_map.width}x{part_map.height}")
    print(f"Pose: {part_map.pose}  facing {part_map.facing}")
    print(
        f"Opaque height: {part_map.height_px()} px "
        f"(target ~{TARGET_HEIGHT_PX})"
    )
    if box is not None:
        print(f"BBox: x={box[0]}..{box[2] - 1}  y={box[1]}..{box[3] - 1}")
    print(f"Parts used ({len(part_map.used_parts())}): {', '.join(part_map.used_parts())}")
    print(f"Export layers: {', '.join(EXPORT_LAYER_NAMES)}")
    print("Wrote:")
    for key in (
        "part_map_json",
        "part_map",
        "part_map_grid",
        "layers_grid",
        "compose_preview",
        "silhouette",
        "layers_strip",
        "layers_json",
        "legend",
    ):
        print(f"  {paths[key]}")
    print(f"  layers/: {paths['layers_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
