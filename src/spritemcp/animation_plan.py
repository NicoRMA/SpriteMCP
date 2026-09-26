"""Animation planning gate — agents must reason before drafting frames.

Mandatory flow
--------------
1. ``plan_animation(name, animation_name, intent, plan, …)`` — validate + store
   ``anims/<animation_name>/plan.json``, return ``user_facing_summary``.
2. Agent **shows** ``user_facing_summary`` to the user and waits for OK.
3. ``build_frame_animation`` / ``finish_frame_animation`` — refuse without a
   valid stored plan (optional ``plan_id`` match).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .character import (
    FRAME_COUNT,
    anim_dir,
    character_base_dir,
    character_dir,
    validate_animation_name,
    validate_character_name,
)
from .pose import JOINT_ORDER, JOINT_BY_NAME

PLAN_SCHEMA = "spritemcp.animation_plan.v1"
PLAN_FILENAME = "plan.json"

REQUIRED_PLAN_KEYS = (
    "beats",
    "silhouette_story",
    "differs_from",
    "weapon_or_style_notes",
)

_MIN_POSE_DESC = 16
_MIN_SILHOUETTE = 48
_MIN_WEAPON_NOTES = 16
_MIN_DIFFERS = 24

_GENERIC_POSE = re.compile(
    r"^(pose|frame|same|idle|attack|punch|hit|swing|anim|default"
    r"|fight|like fight|same as fight|copy|todo|tbd|\.+)$",
    re.IGNORECASE,
)
_GENERIC_DIFFERS = re.compile(
    r"^(same|similar|like|alike|identical|copy|n/?a|none|todo|tbd"
    r"|same as .+|similar to .+|like .+)$",
    re.IGNORECASE,
)
_BLADE_HINT = re.compile(
    r"\b(sword|katana|blade|cut|slash|draw|sheath|iai|thrust|edge|weapon)\b",
    re.IGNORECASE,
)
_SWORD_ANIM = re.compile(
    r"(samurai|sword|katana|slash|cut|iai|blade)",
    re.IGNORECASE,
)
_PUNCH_HINT = re.compile(r"\b(punch|fist|boxing|jab|hook|uppercut)\b", re.IGNORECASE)


def plan_path(
    name: str,
    animation_name: str,
    output_dir: Path | str | None = None,
) -> Path:
    return anim_dir(name, animation_name, output_dir) / PLAN_FILENAME


def list_sibling_animations(
    name: str,
    animation_name: str,
    *,
    output_dir: Path | str | None = None,
) -> list[str]:
    """Animation folder names under the character that already exist."""
    name = validate_character_name(name)
    animation_name = validate_animation_name(animation_name)
    root = character_dir(name, output_dir) / "anims"
    if not root.is_dir():
        return []
    siblings: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        other = child.name
        if other == animation_name:
            continue
        # Count as existing if it has a plan, any frame, or draft/poses.
        if (child / PLAN_FILENAME).is_file() or any(child.glob("frame_*.png")):
            siblings.append(other)
            continue
        if (child / "poses").is_dir() and any((child / "poses").glob("frame_*.json")):
            siblings.append(other)
    return siblings


def _as_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = " ".join(value.split()).strip()
    if not text:
        raise ValueError(f"{field} must be non-empty")
    return text


def _normalize_differs_from(
    raw: Any,
) -> dict[str, str]:
    """Accept map ``{anim: explanation}`` or list of maps / {name, difference}."""
    if isinstance(raw, Mapping):
        out: dict[str, str] = {}
        for key, val in raw.items():
            name = _as_nonempty_str(str(key), "differs_from key")
            out[name] = _as_nonempty_str(val, f"differs_from[{name}]")
        return out
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        out = {}
        for i, item in enumerate(raw):
            if isinstance(item, Mapping):
                anim = item.get("name") or item.get("animation") or item.get("animation_name")
                diff = (
                    item.get("difference")
                    or item.get("differs")
                    or item.get("explanation")
                    or item.get("text")
                )
                if anim is None or diff is None:
                    raise ValueError(
                        f"differs_from[{i}] must include name + difference "
                        "(or use a map of animation_name → explanation)"
                    )
                key = _as_nonempty_str(str(anim), f"differs_from[{i}].name")
                out[key] = _as_nonempty_str(diff, f"differs_from[{i}].difference")
            else:
                raise ValueError(
                    "differs_from list items must be objects with name + difference"
                )
        return out
    raise ValueError(
        "differs_from must be a map {animation_name: explanation} "
        "or a list of {name, difference} objects"
    )


def _validate_beats(beats: Any) -> list[dict[str, Any]]:
    if not isinstance(beats, Sequence) or isinstance(beats, (str, bytes)):
        raise ValueError("plan.beats must be a list of 8 beat objects")
    if len(beats) != FRAME_COUNT:
        raise ValueError(
            f"plan.beats must have exactly {FRAME_COUNT} entries "
            f"(frame_index 0..{FRAME_COUNT - 1}), got {len(beats)}"
        )

    seen: set[int] = set()
    normalized: list[dict[str, Any]] = []
    for i, beat in enumerate(beats):
        if not isinstance(beat, Mapping):
            raise ValueError(f"plan.beats[{i}] must be an object")
        if "frame_index" not in beat:
            raise ValueError(f"plan.beats[{i}].frame_index is required")
        fi = beat["frame_index"]
        if not isinstance(fi, int) or isinstance(fi, bool):
            raise ValueError(f"plan.beats[{i}].frame_index must be an int")
        if fi < 0 or fi >= FRAME_COUNT:
            raise ValueError(
                f"plan.beats[{i}].frame_index must be 0..{FRAME_COUNT - 1}, got {fi}"
            )
        if fi in seen:
            raise ValueError(f"Duplicate frame_index {fi} in plan.beats")
        seen.add(fi)

        pose = _as_nonempty_str(
            beat.get("pose_description", ""),
            f"plan.beats[{i}].pose_description",
        )
        if len(pose) < _MIN_POSE_DESC:
            raise ValueError(
                f"plan.beats[{i}].pose_description too short "
                f"(min {_MIN_POSE_DESC} chars) — describe the silhouette change"
            )
        if _GENERIC_POSE.match(pose):
            raise ValueError(
                f"plan.beats[{i}].pose_description is too generic ({pose!r}). "
                "Describe the specific pose for this beat."
            )

        joints_raw = beat.get("primary_joints")
        if not isinstance(joints_raw, Sequence) or isinstance(joints_raw, (str, bytes)):
            raise ValueError(
                f"plan.beats[{i}].primary_joints must be a non-empty list of joint names"
            )
        joints: list[str] = []
        for jname in joints_raw:
            j = str(jname).strip()
            if j not in JOINT_BY_NAME:
                raise ValueError(
                    f"plan.beats[{i}].primary_joints: unknown joint {j!r}. "
                    f"Valid: {', '.join(JOINT_ORDER)}"
                )
            if j not in joints:
                joints.append(j)
        if not joints:
            raise ValueError(f"plan.beats[{i}].primary_joints must list at least one joint")

        normalized.append(
            {
                "frame_index": fi,
                "pose_description": pose,
                "primary_joints": joints,
            }
        )

    if seen != set(range(FRAME_COUNT)):
        missing = sorted(set(range(FRAME_COUNT)) - seen)
        raise ValueError(f"plan.beats missing frame_index values: {missing}")

    normalized.sort(key=lambda b: b["frame_index"])

    # Reject near-duplicate pose stories (copy-paste across all beats).
    descs = [b["pose_description"].casefold() for b in normalized]
    unique = set(descs)
    if len(unique) < 4:
        raise ValueError(
            "plan.beats pose_descriptions are too repetitive — "
            "each beat needs a distinct silhouette moment (at least 4 unique descriptions)"
        )

    return normalized


def _reject_fight_clone(
    animation_name: str,
    silhouette: str,
    weapon_notes: str,
    differs: dict[str, str],
    beats: list[dict[str, Any]],
) -> None:
    """Catch plans that rename fight without reasoning about a distinct clip."""
    blob = " ".join(
        [
            silhouette,
            weapon_notes,
            " ".join(differs.values()),
            " ".join(b["pose_description"] for b in beats),
        ]
    ).casefold()

    if _SWORD_ANIM.search(animation_name) or _SWORD_ANIM.search(silhouette):
        if not _BLADE_HINT.search(weapon_notes) and not _BLADE_HINT.search(silhouette):
            raise ValueError(
                f"Animation '{animation_name}' reads as a weapon/sword clip, but "
                "weapon_or_style_notes / silhouette_story never mention blade, cut, "
                "slash, draw, or similar. Do not reuse a bare-knuckle fight plan."
            )
        if _PUNCH_HINT.search(blob) and not _BLADE_HINT.search(weapon_notes):
            raise ValueError(
                "Plan mixes punch/fist language with a sword-style animation name "
                "without weapon_or_style_notes that specify a cut/slash. "
                "Differentiate from fight (jab/punch) explicitly."
            )

    fight_diff = differs.get("fight")
    if fight_diff is not None:
        if len(fight_diff) < _MIN_DIFFERS or _GENERIC_DIFFERS.match(fight_diff):
            raise ValueError(
                "differs_from['fight'] must explain a concrete silhouette/timing "
                "difference vs the fight clip (not 'same' / 'similar')."
            )


def validate_animation_plan(
    plan: Mapping[str, Any],
    *,
    animation_name: str,
    contrast_with: Sequence[str] | None = None,
    required_contrast: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate agent-authored plan fields; return a normalized plan dict."""
    if not isinstance(plan, Mapping):
        raise ValueError("plan must be an object with required keys: "
                         + ", ".join(REQUIRED_PLAN_KEYS))

    missing = [k for k in REQUIRED_PLAN_KEYS if k not in plan]
    if missing:
        raise ValueError(
            "plan is missing required keys: "
            + ", ".join(missing)
            + ". Required: beats (8), silhouette_story, differs_from, weapon_or_style_notes"
        )

    beats = _validate_beats(plan["beats"])
    silhouette = _as_nonempty_str(plan["silhouette_story"], "silhouette_story")
    if len(silhouette) < _MIN_SILHOUETTE:
        raise ValueError(
            f"silhouette_story too short (min {_MIN_SILHOUETTE} chars) — "
            "write a user-facing paragraph of how the clip reads as a whole"
        )

    weapon_notes = _as_nonempty_str(
        plan["weapon_or_style_notes"], "weapon_or_style_notes"
    )
    if len(weapon_notes) < _MIN_WEAPON_NOTES:
        raise ValueError(
            f"weapon_or_style_notes too short (min {_MIN_WEAPON_NOTES} chars) — "
            "e.g. sword cut arc vs jab/punch, stance, recovery"
        )

    differs = _normalize_differs_from(plan["differs_from"])
    if not differs:
        raise ValueError(
            "differs_from must explain how this clip differs from other animations "
            "(walk/run/fight/…) — empty plans are rejected"
        )
    for key, text in differs.items():
        if len(text) < _MIN_DIFFERS or _GENERIC_DIFFERS.match(text):
            raise ValueError(
                f"differs_from[{key!r}] is too short or generic. "
                "Explain a concrete silhouette / timing / intent difference."
            )

    must_cover = []
    for src in (contrast_with, required_contrast):
        if not src:
            continue
        for name in src:
            n = validate_animation_name(str(name))
            if n != animation_name and n not in must_cover:
                must_cover.append(n)

    missing_contrast = [n for n in must_cover if n not in differs]
    if missing_contrast:
        raise ValueError(
            "differs_from must cover these existing/contrast animations: "
            + ", ".join(missing_contrast)
        )

    _reject_fight_clone(animation_name, silhouette, weapon_notes, differs, beats)

    return {
        "beats": beats,
        "silhouette_story": silhouette,
        "differs_from": differs,
        "weapon_or_style_notes": weapon_notes,
    }


def build_user_facing_summary(
    *,
    name: str,
    animation_name: str,
    intent: str,
    plan: Mapping[str, Any],
) -> str:
    """Short summary the calling agent MUST show the user before building frames."""
    lines: list[str] = [
        f"Animation plan for `{name}` / `{animation_name}`",
        f"Intent: {intent.strip()}",
        "",
        "How it will look:",
        plan["silhouette_story"],
        "",
        f"Style: {plan['weapon_or_style_notes']}",
        "",
        "8-beat outline:",
    ]
    for beat in plan["beats"]:
        joints = ", ".join(beat["primary_joints"])
        lines.append(
            f"  {beat['frame_index']}: {beat['pose_description']} "
            f"(joints: {joints})"
        )
    differs = plan["differs_from"]
    if differs:
        lines.append("")
        lines.append("Differs from other clips:")
        for other, text in differs.items():
            lines.append(f"  vs {other}: {text}")
    lines.append("")
    lines.append(
        "If this matches what you want, say OK and frames can be built; "
        "otherwise revise the plan first."
    )
    return "\n".join(lines)


def _new_plan_id(name: str, animation_name: str, normalized: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"name": name, "animation_name": animation_name, "plan": normalized},
        sort_keys=True,
        ensure_ascii=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"{name}-{animation_name}-{digest}-{uuid.uuid4().hex[:8]}"


def require_animation_plan(
    name: str,
    animation_name: str,
    *,
    plan_id: str | None = None,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Load stored plan or raise with a clear gate error."""
    name = validate_character_name(name)
    animation_name = validate_animation_name(animation_name)
    path = plan_path(name, animation_name, output_dir)
    if not path.is_file():
        raise FileNotFoundError(
            f"No valid animation plan for '{name}' / '{animation_name}' "
            f"(missing {path}). "
            "Call plan_animation with a complete plan first, show "
            "user_facing_summary to the user, then build frames. "
            "Never call build_frame_animation without a locked plan."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "plan" not in data or "plan_id" not in data:
        raise FileNotFoundError(
            f"Animation plan at {path} is incomplete or corrupt. "
            "Re-submit via plan_animation."
        )
    if plan_id is not None and data.get("plan_id") != plan_id:
        raise ValueError(
            f"plan_id mismatch for '{name}' / '{animation_name}': "
            f"got {plan_id!r}, stored {data.get('plan_id')!r}. "
            "Pass the plan_id returned by plan_animation, or omit plan_id."
        )
    return data


def get_animation_plan(
    name: str,
    animation_name: str,
    *,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Return the locked plan for a character animation (or raise if missing)."""
    data = require_animation_plan(name, animation_name, output_dir=output_dir)
    return {
        "schema": data.get("schema", PLAN_SCHEMA),
        "plan_id": data["plan_id"],
        "name": data.get("name", name),
        "animation_name": data.get("animation_name", animation_name),
        "intent": data.get("intent", ""),
        "plan": data["plan"],
        "user_facing_summary": data.get("user_facing_summary", ""),
        "paths": {"plan_json": str(plan_path(name, animation_name, output_dir))},
        "next_step": (
            "Show user_facing_summary to the user if not already shown, "
            "then build_frame_animation → finish_frame_animation for frames 0..7"
        ),
    }


def plan_animation(
    name: str,
    animation_name: str,
    intent: str,
    plan: Mapping[str, Any],
    *,
    contrast_with: Sequence[str] | None = None,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Validate + store an agent-authored animation plan; return summary + plan_id.

    The calling agent must author ``plan`` (beats, silhouette story, differs_from,
    weapon/style notes). This tool does **not** invent the creative plan — it
    gates completeness and locks the result under ``plan.json``.
    """
    name = validate_character_name(name)
    animation_name = validate_animation_name(animation_name)
    intent_text = _as_nonempty_str(intent, "intent")
    if len(intent_text) < 8:
        raise ValueError("intent must briefly say what the animation should convey")

    base = character_base_dir(name, output_dir)
    if not (base / "part_map.json").is_file() or not (base / "pivots.json").is_file():
        raise FileNotFoundError(
            f"Character '{name}' base assets missing under {base}. "
            "Call generate_character first."
        )

    siblings = list_sibling_animations(name, animation_name, output_dir=output_dir)
    contrast_list: list[str] = []
    if contrast_with:
        for item in contrast_with:
            contrast_list.append(validate_animation_name(str(item)))

    # Auto-require differentiation vs existing sibling clips.
    required = list(dict.fromkeys([*contrast_list, *siblings]))

    normalized = validate_animation_plan(
        plan,
        animation_name=animation_name,
        contrast_with=contrast_list or None,
        required_contrast=required or None,
    )

    plan_id = _new_plan_id(name, animation_name, normalized)
    summary = build_user_facing_summary(
        name=name,
        animation_name=animation_name,
        intent=intent_text,
        plan=normalized,
    )

    anim = anim_dir(name, animation_name, output_dir)
    anim.mkdir(parents=True, exist_ok=True)
    stored = {
        "schema": PLAN_SCHEMA,
        "plan_id": plan_id,
        "name": name,
        "animation_name": animation_name,
        "intent": intent_text,
        "contrast_with": contrast_list,
        "sibling_animations_considered": siblings,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plan": normalized,
        "user_facing_summary": summary,
    }
    path = plan_path(name, animation_name, output_dir)
    path.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")

    return {
        "schema": PLAN_SCHEMA,
        "plan_id": plan_id,
        "name": name,
        "animation_name": animation_name,
        "intent": intent_text,
        "plan": normalized,
        "user_facing_summary": summary,
        "paths": {"plan_json": str(path), "anim_dir": str(anim)},
        "next_step": (
            "Show user_facing_summary to the user, then after user OK "
            "call build_frame_animation / finish_frame_animation for frames 0..7. "
            "Never build frames before presenting the summary."
        ),
    }
