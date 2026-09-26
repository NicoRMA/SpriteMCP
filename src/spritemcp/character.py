"""Per-character folder layout + agent animation frame pipeline.

Folder layout
-------------
::

    output/characters/<name>/
      base/                          # rest idle + pivots
        part_map.json, part_map.png, silhouette.png, …
        pivots.json, pivots_overlay.png, …
        layers/                      # naked body export layers
      design/                        # visual content on export body layers
        plan.json                    # required before painting layers
        layers_meta.json             # layer → parent / body_parts / draw hooks
        layers/<layer>.png           # same 10 names as base/layers/
        refs/<layer>_ref.png         # body under + design on top
        compose_preview.png          # dressed rest
      anims/<animation_name>/
        plan.json                    # required before build/finish
        frame_00.png … frame_07.png  # finalized frames
        <animation_name>_contact_sheet.png   # auto on finish frame 7
        <animation_name>_preview.gif         # auto on finish frame 7
        poses/frame_00.json …
        draft/                       # working drafts from build_
          frame_00.png, frame_00.json
          …

All animations are 8 frames (indices 0..7). Finishing frame 7 also writes
the contact sheet and looping preview GIF (re-runnable via
``export_animation_preview``).

Gate: call ``plan_animation`` first; ``build_frame_animation`` /
``finish_frame_animation`` refuse without a valid ``plan.json``.
When ``design/`` has painted outfit layers (or an outfit plan), call
``compose_character`` before frames — ``build_frame_animation`` composites
base + design (rigid-rotated with parent parts). No design → base-only as
before. Outfit: call ``plan_outfit`` before slot paint tools (see ``outfit``).
Clothing may overhang body silhouettes; body underlay is alignment, not a clip.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from . import base_idle as _base_idle
from . import pivots as _pivots
from .config import resolve_output_dir
from .export import (
    ANIMATION_PREVIEW_SCALE,
    DEFAULT_PREVIEW_DURATION_MS,
    export_animation_sheet_and_gif,
    save_grid,
)
from .pose import (
    JOINT_ORDER,
    apply_pose,
    descendant_parts,
    joint_docs,
    merge_pose,
    normalize_pose,
    pose_to_json_dict,
    rasterize_silhouette,
    zero_pose,
)

FRAME_COUNT = 8
_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


def validate_character_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            "Character name must be 1–64 chars, start with a letter, "
            "and contain only letters, digits, underscore, or hyphen."
        )
    return name


def validate_animation_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            "Animation name must be 1–64 chars, start with a letter, "
            "and contain only letters, digits, underscore, or hyphen."
        )
    return name


def validate_frame_index(frame_index: int) -> int:
    if not isinstance(frame_index, int) or isinstance(frame_index, bool):
        raise TypeError("frame_index must be an int")
    if frame_index < 0 or frame_index >= FRAME_COUNT:
        raise ValueError(f"frame_index must be 0..{FRAME_COUNT - 1}, got {frame_index}")
    return frame_index


def characters_root(output_dir: Path | str | None = None) -> Path:
    return resolve_output_dir(output_dir) / "characters"


def character_dir(name: str, output_dir: Path | str | None = None) -> Path:
    return characters_root(output_dir) / validate_character_name(name)


def character_base_dir(name: str, output_dir: Path | str | None = None) -> Path:
    return character_dir(name, output_dir) / "base"


def anim_dir(
    name: str,
    animation_name: str,
    output_dir: Path | str | None = None,
) -> Path:
    return (
        character_dir(name, output_dir)
        / "anims"
        / validate_animation_name(animation_name)
    )


def frame_stem(frame_index: int) -> str:
    return f"frame_{validate_frame_index(frame_index):02d}"


def _write_json(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_rest(name: str, output_dir: Path | str | None = None):
    base = character_base_dir(name, output_dir)
    part_json = base / "part_map.json"
    pivots_json = base / "pivots.json"
    if not part_json.is_file() or not pivots_json.is_file():
        raise FileNotFoundError(
            f"Character '{name}' base assets missing under {base}. "
            "Call generate_character first."
        )
    pm_data = _read_json(part_json)
    w, h = int(pm_data["width"]), int(pm_data["height"])
    flat = pm_data["pixels"]
    labels = [flat[y * w : (y + 1) * w] for y in range(h)]
    parts_full_raw = pm_data.get("parts_full") or {}
    parts_full: dict[str, list[tuple[int, int]]] | None = None
    if parts_full_raw:
        parts_full = {
            name: [(int(p[0]), int(p[1])) for p in coords]
            for name, coords in parts_full_raw.items()
        }
    part_map = _base_idle.PartMap(
        width=w,
        height=h,
        labels=labels,
        pose=pm_data.get("pose", "side_idle_apose"),
        facing=pm_data.get("facing", "+X"),
        parts_full=parts_full,
        eyes_detail=_base_idle.eyes_coords_from_part_map_payload(pm_data),
    )
    piv_data = _read_json(pivots_json)
    pivots = [
        _pivots.Pivot(
            name=j["name"],
            parent=j["parent"],
            child=j["child"],
            x=float(j["x"]),
            y=float(j["y"]),
            method=j.get("method", "boundary_centroid"),
            contact_pairs=int(j.get("contact_pairs", 0)),
        )
        for j in piv_data["joints"]
    ]
    return part_map, pivots


def _pose_path_final(anim: Path, frame_index: int) -> Path:
    return anim / "poses" / f"{frame_stem(frame_index)}.json"


def _pose_path_draft(anim: Path, frame_index: int) -> Path:
    return anim / "draft" / f"{frame_stem(frame_index)}.json"


def _png_path_final(anim: Path, frame_index: int) -> Path:
    return anim / f"{frame_stem(frame_index)}.png"


def _png_path_draft(anim: Path, frame_index: int) -> Path:
    return anim / "draft" / f"{frame_stem(frame_index)}.png"


def _load_angles_from_pose_file(path: Path) -> dict[str, float]:
    data = _read_json(path)
    return normalize_pose(data.get("angles") or {})


def resolve_start_pose(
    anim: Path,
    frame_index: int,
) -> tuple[dict[str, float], str]:
    """Pose inherited when building ``frame_index``.

    Frame 0 → rest (zeros).
    Frame N>0 → finished pose of N-1, else draft of N-1.
    """
    if frame_index == 0:
        return zero_pose(), "rest"
    prev = frame_index - 1
    final_p = _pose_path_final(anim, prev)
    if final_p.is_file():
        return _load_angles_from_pose_file(final_p), f"finished_frame_{prev:02d}"
    draft_p = _pose_path_draft(anim, prev)
    if draft_p.is_file():
        return _load_angles_from_pose_file(draft_p), f"draft_frame_{prev:02d}"
    raise FileNotFoundError(
        f"No pose for previous frame {prev} under {anim}. "
        "Finish or build the previous frame first."
    )


def generate_character(
    name: str,
    *,
    output_dir: Path | str | None = None,
    scale: int = 8,
) -> dict[str, Any]:
    """Generate fixed base idle + pivots for ``name``.

    Writes under ``output/characters/<name>/base/``.

    Always uses the authored 90×128 armature from ``spritemcp/base/``.

    Return schema (``spritemcp.generate_character.v1``)
    ---------------------------------------------------
    - ``name``, ``width``, ``height``, ``pose``, ``facing``
    - ``parts``: ``[{name, id}, …]``
    - ``joints``: ``[{name, parent, child, x, y, method, rotates}, …]``
    - ``hierarchy``: joint docs (what each joint rotates)
    - ``preview_paths``: silhouette / part_map / pivots_overlay image paths
    - ``paths``: all written file paths
    """
    name = validate_character_name(name)
    base = character_base_dir(name, output_dir)
    base.mkdir(parents=True, exist_ok=True)

    part_map, idle_paths = _base_idle.build_outputs(base, scale=scale)
    _pm, pivots, pivot_paths = _pivots.build_outputs(
        base, scale=scale, part_map=part_map
    )

    box = part_map.opaque_bbox()
    parts = [
        {"name": pname, "id": pid}
        for pname, pid in _base_idle.PART_NAMES.items()
        if pname != "empty"
    ]
    joints = [
        {
            "name": p.name,
            "parent": p.parent,
            "child": p.child,
            "x": p.x,
            "y": p.y,
            "method": p.method,
            "rotates": descendant_parts(p.child),
        }
        for p in pivots
    ]

    paths = {
        "part_map_json": str(idle_paths["part_map_json"]),
        "part_map": str(idle_paths["part_map"]),
        "part_map_grid": str(idle_paths["part_map_grid"]),
        "layers_grid": str(idle_paths["layers_grid"]),
        "compose_preview": str(idle_paths["compose_preview"]),
        "layers_strip": str(idle_paths["layers_strip"]),
        "layers_dir": str(idle_paths["layers_dir"]),
        "silhouette": str(idle_paths["silhouette"]),
        "legend": str(idle_paths["legend"]),
        "pivots_json": str(pivot_paths["pivots_json"]),
        "pivots_overlay": str(pivot_paths["pivots_overlay"]),
        "pivots_grid": str(pivot_paths["pivots_grid"]),
        "character_dir": str(character_dir(name, output_dir)),
        "base_dir": str(base),
    }

    return {
        "schema": "spritemcp.generate_character.v1",
        "name": name,
        "armature": _base_idle.ARMATURE_NAME,
        "width": part_map.width,
        "height": part_map.height,
        "pose": part_map.pose,
        "facing": part_map.facing,
        "view": _base_idle.VIEW_MODE,
        "layering": _base_idle.LAYERING_RULE,
        "draw_order": list(_base_idle.DRAW_ORDER),
        "opaque_height_px": part_map.height_px(),
        "opaque_bbox": (
            None
            if box is None
            else {"x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]}
        ),
        "parts": parts,
        "parts_used": part_map.used_parts(),
        "joints": joints,
        "joint_names": list(JOINT_ORDER),
        "hierarchy": joint_docs(),
        "preview_paths": {
            "silhouette": paths["silhouette"],
            "part_map": paths["part_map"],
            "part_map_grid": paths["part_map_grid"],
            "layers_grid": paths["layers_grid"],
            "layers_strip": paths["layers_strip"],
            "compose_preview": paths["compose_preview"],
            "pivots_overlay": paths["pivots_overlay"],
        },
        "paths": paths,
    }


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
    """Draft one animation frame from joint rotations (does not finalize).

    Requires a locked ``plan.json`` from ``plan_animation`` (optional
    ``plan_id`` must match when provided).

    ``rotations`` sets **absolute** degrees-from-rest for listed joints.
    Unspecified joints inherit the frame start pose (rest for frame 0;
    previous finished pose, else previous draft, for frame > 0).

    Re-calling on the same frame rebuilds the draft from that same start pose
    plus the new ``rotations`` (does not stack on the previous draft of this frame).

    Return schema (``spritemcp.build_frame_animation.v1``)
    ------------------------------------------------------
    - ``angles``: full joint → degrees map
    - ``joints``: name, parent, child, x, y, angle_deg
    - ``preview_paths.frame`` / ``frame_preview``
    - ``paths``: draft PNG + pose JSON
    - ``finalized``: always ``False``
    - ``plan_id``: locked plan id used for this draft
    """
    from .animation_plan import require_animation_plan

    name = validate_character_name(name)
    animation_name = validate_animation_name(animation_name)
    frame_index = validate_frame_index(frame_index)

    locked = require_animation_plan(
        name, animation_name, plan_id=plan_id, output_dir=output_dir
    )

    from .outfit import load_active_design_layers, require_compose_for_animation

    require_compose_for_animation(name, output_dir=output_dir)
    design_layers = load_active_design_layers(name, output_dir=output_dir)

    rest_map, rest_pivots = _load_rest(name, output_dir)
    anim = anim_dir(name, animation_name, output_dir)
    anim.mkdir(parents=True, exist_ok=True)
    (anim / "draft").mkdir(parents=True, exist_ok=True)
    (anim / "poses").mkdir(parents=True, exist_ok=True)

    start_angles, start_source = resolve_start_pose(anim, frame_index)
    pose_angles = merge_pose(start_angles, rotations)

    result = apply_pose(
        rest_map,
        rest_pivots,
        pose_angles,
        design_layers=design_layers or None,
    )
    draft_png = _png_path_draft(anim, frame_index)
    draft_png.parent.mkdir(parents=True, exist_ok=True)
    if result.silhouette is not None:
        result.silhouette.save(draft_png)
    else:
        save_grid(rasterize_silhouette(result.part_map), draft_png)

    native = Image.open(draft_png).convert("RGBA")
    preview = native.resize(
        (native.width * scale, native.height * scale),
        Image.Resampling.NEAREST,
    )
    preview_path = anim / "draft" / f"{frame_stem(frame_index)}_preview.png"
    preview.save(preview_path)

    pose_dict = pose_to_json_dict(
        result.angles,
        result.pivots,
        frame_index=frame_index,
        animation_name=animation_name,
        character=name,
        finalized=False,
    )
    pose_dict["start_pose_source"] = start_source
    pose_dict["schema"] = "spritemcp.frame_pose.v1"
    pose_dict["dressed"] = bool(result.design_slots)
    pose_dict["design_slots"] = list(result.design_slots)
    pose_json = _write_json(_pose_path_draft(anim, frame_index), pose_dict)

    return {
        "schema": "spritemcp.build_frame_animation.v1",
        "name": name,
        "animation_name": animation_name,
        "frame_index": frame_index,
        "frame_count": FRAME_COUNT,
        "finalized": False,
        "plan_id": locked["plan_id"],
        "dressed": bool(result.design_slots),
        "design_slots": list(result.design_slots),
        "start_pose_source": start_source,
        "joint_names": list(JOINT_ORDER),
        "angles": pose_dict["angles"],
        "joints": pose_dict["joints"],
        "preview_paths": {
            "frame": str(draft_png),
            "frame_preview": str(preview_path),
        },
        "paths": {
            "draft_png": str(draft_png),
            "draft_preview": str(preview_path),
            "draft_pose_json": str(pose_json),
            "anim_dir": str(anim),
        },
        "limitations": pose_dict["limitations"],
    }


def list_final_frame_paths(
    name: str,
    animation_name: str,
    *,
    output_dir: Path | str | None = None,
) -> list[Path]:
    """Return expected final ``frame_00.png`` … ``frame_07.png`` paths (may be missing)."""
    anim = anim_dir(name, animation_name, output_dir)
    return [_png_path_final(anim, i) for i in range(FRAME_COUNT)]


def missing_final_frames(
    name: str,
    animation_name: str,
    *,
    output_dir: Path | str | None = None,
) -> list[int]:
    """Frame indices whose final PNG is not on disk yet."""
    return [
        i
        for i, path in enumerate(
            list_final_frame_paths(name, animation_name, output_dir=output_dir)
        )
        if not path.is_file()
    ]


def export_animation_preview(
    name: str,
    animation_name: str,
    *,
    duration_ms: int = DEFAULT_PREVIEW_DURATION_MS,
    scale: int = ANIMATION_PREVIEW_SCALE,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Build contact sheet + looping preview GIF from finished frames 0..7.

    Writes ``<animation_name>_contact_sheet.png`` and
    ``<animation_name>_preview.gif`` under ``anims/<animation_name>/``.
    Requires all eight final frame PNGs.

    Preview GIF scale is locked to ``ANIMATION_PREVIEW_SCALE`` (native
    90×128). Passing any other ``scale`` raises ``ValueError``.

    Return schema (``spritemcp.export_animation_preview.v1``).
    """
    name = validate_character_name(name)
    animation_name = validate_animation_name(animation_name)
    if not isinstance(duration_ms, int) or isinstance(duration_ms, bool):
        raise TypeError("duration_ms must be an int")
    if duration_ms < 1:
        raise ValueError(f"duration_ms must be >= 1, got {duration_ms}")
    if scale != ANIMATION_PREVIEW_SCALE:
        raise ValueError(
            f"Animation preview GIF scale is locked to "
            f"{ANIMATION_PREVIEW_SCALE} (native frame size); got {scale}."
        )

    anim = anim_dir(name, animation_name, output_dir)
    frame_paths = list_final_frame_paths(name, animation_name, output_dir=output_dir)
    missing = [i for i, p in enumerate(frame_paths) if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Cannot export preview for {name}/{animation_name}: "
            f"missing final frames {missing}. Finish frames 0..{FRAME_COUNT - 1} first."
        )

    sheet_path = anim / f"{animation_name}_contact_sheet.png"
    gif_path = anim / f"{animation_name}_preview.gif"
    written = export_animation_sheet_and_gif(
        frame_paths,
        sheet_path=sheet_path,
        gif_path=gif_path,
        duration_ms=duration_ms,
        scale=ANIMATION_PREVIEW_SCALE,
    )
    return {
        "schema": "spritemcp.export_animation_preview.v1",
        "name": name,
        "animation_name": animation_name,
        "frame_count": FRAME_COUNT,
        "duration_ms": duration_ms,
        "scale": ANIMATION_PREVIEW_SCALE,
        "paths": {
            "contact_sheet": str(written["contact_sheet"]),
            "preview_gif": str(written["preview_gif"]),
            "anim_dir": str(anim),
            "frames": [str(p) for p in frame_paths],
        },
        "preview_paths": {
            "contact_sheet": str(written["contact_sheet"]),
            "preview_gif": str(written["preview_gif"]),
        },
    }


def finish_frame_animation(
    name: str,
    animation_name: str,
    frame_index: int,
    *,
    plan_id: str | None = None,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Lock the current draft of ``frame_index`` as the final frame.

    Requires a locked ``plan.json`` from ``plan_animation`` (optional
    ``plan_id`` must match when provided).

    When frame 7 is finished and all eight finals exist, also writes
    ``<animation_name>_contact_sheet.png`` and ``<animation_name>_preview.gif``.

    Return schema (``spritemcp.finish_frame_animation.v1``)
    --------------------------------------------------------
    - ``finalized``: ``True``
    - ``paths.frame_png``, ``paths.pose_json``
    - ``paths.contact_sheet`` / ``paths.preview_gif`` when generated
    - ``next_frame_index`` / ``ready_for_next_frame`` / ``animation_complete``
    - ``plan_id``: locked plan id
    """
    from .animation_plan import require_animation_plan

    name = validate_character_name(name)
    animation_name = validate_animation_name(animation_name)
    frame_index = validate_frame_index(frame_index)

    locked = require_animation_plan(
        name, animation_name, plan_id=plan_id, output_dir=output_dir
    )

    anim = anim_dir(name, animation_name, output_dir)
    draft_png = _png_path_draft(anim, frame_index)
    draft_pose = _pose_path_draft(anim, frame_index)
    if not draft_png.is_file() or not draft_pose.is_file():
        raise FileNotFoundError(
            f"No draft for frame {frame_index} under {anim / 'draft'}. "
            "Call build_frame_animation first."
        )

    if frame_index > 0:
        prev_final = _png_path_final(anim, frame_index - 1)
        if not prev_final.is_file():
            raise FileNotFoundError(
                f"Frame {frame_index - 1} is not finished yet "
                f"(missing {prev_final}). Finish frames in order."
            )

    final_png = _png_path_final(anim, frame_index)
    final_pose = _pose_path_final(anim, frame_index)
    final_png.parent.mkdir(parents=True, exist_ok=True)
    final_pose.parent.mkdir(parents=True, exist_ok=True)

    final_png.write_bytes(draft_png.read_bytes())
    pose_data = _read_json(draft_pose)
    pose_data["finalized"] = True
    pose_data["schema"] = "spritemcp.frame_pose.v1"
    _write_json(final_pose, pose_data)

    next_index = frame_index + 1
    ready_next = next_index < FRAME_COUNT
    animation_complete = frame_index == FRAME_COUNT - 1

    paths: dict[str, Any] = {
        "frame_png": str(final_png),
        "pose_json": str(final_pose),
        "anim_dir": str(anim),
    }
    preview_paths: dict[str, str] = {
        "frame": str(final_png),
    }
    export_note: str | None = None

    if animation_complete:
        missing = missing_final_frames(name, animation_name, output_dir=output_dir)
        if missing:
            export_note = (
                "animation_complete but contact sheet / preview GIF not written; "
                f"missing final frames {missing}"
            )
        else:
            exported = export_animation_preview(
                name, animation_name, output_dir=output_dir
            )
            paths["contact_sheet"] = exported["paths"]["contact_sheet"]
            paths["preview_gif"] = exported["paths"]["preview_gif"]
            preview_paths["contact_sheet"] = exported["paths"]["contact_sheet"]
            preview_paths["preview_gif"] = exported["paths"]["preview_gif"]
    else:
        export_note = (
            f"Contact sheet and preview GIF are written when frame "
            f"{FRAME_COUNT - 1} is finished (all frames 0..{FRAME_COUNT - 1} present)."
        )

    result: dict[str, Any] = {
        "schema": "spritemcp.finish_frame_animation.v1",
        "name": name,
        "animation_name": animation_name,
        "frame_index": frame_index,
        "frame_count": FRAME_COUNT,
        "finalized": True,
        "plan_id": locked["plan_id"],
        "angles": pose_data.get("angles", {}),
        "next_frame_index": next_index if ready_next else None,
        "ready_for_next_frame": ready_next,
        "animation_complete": animation_complete,
        "paths": paths,
        "preview_paths": preview_paths,
    }
    if export_note is not None:
        result["export_note"] = export_note
    return result
