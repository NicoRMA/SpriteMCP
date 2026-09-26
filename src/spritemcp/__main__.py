"""CLI entry: ``python -m spritemcp <command>``.

Commands map to the public API (future MCP tools).
"""

from __future__ import annotations

import argparse
import json
import sys


def _parse_rotations(raw: str | None) -> dict[str, float] | None:
    if raw is None or raw.strip() == "":
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise SystemExit("--rotations must be a JSON object, e.g. {\"hip_near\": 15}")
    return {str(k): float(v) for k, v in data.items()}


def _ensure_cli_output_root(out_dir: str | None = None) -> None:
    """CLI writes need an explicit session root (no silent package path)."""
    from .config import (
        default_output_dir,
        has_session_output_root,
        set_output_root,
    )

    if out_dir is not None:
        set_output_root(out_dir)
        return
    if not has_session_output_root():
        set_output_root(default_output_dir())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spritemcp",
        description="Pixel sprite toolkit mini-project (MCP-ready API).",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("demo", help="Write the tiny palette test pattern")
    sub.add_parser("show-ref-grid", help="Style-ref pixel grid visualization")
    sub.add_parser("base-idle", help="Naked/base idle part-id map")
    sub.add_parser("pivots", help="Articulation pivots on base idle")
    sub.add_parser("paths", help="Print default output / style-ref paths")
    sub.add_parser("part-ids", help="Print part name → id map")
    sub.add_parser("joints", help="Print joint hierarchy specs")
    sub.add_parser("joint-docs", help="Print joints + which parts they rotate")

    gen_c = sub.add_parser(
        "generate-character",
        help="Generate base idle + pivots for a named character",
    )
    gen_c.add_argument("name", help="Character name (folder under characters/)")
    gen_c.add_argument("--out-dir", type=str, default=None, help="Output root override")
    gen_c.add_argument("--scale", type=int, default=8)

    plan_c = sub.add_parser(
        "plan-animation",
        help="Validate + lock animation plan JSON before building frames",
    )
    plan_c.add_argument("name", help="Character name")
    plan_c.add_argument("animation_name", help="Animation name (e.g. walk)")
    plan_c.add_argument(
        "--intent",
        type=str,
        required=True,
        help="What the animation should convey",
    )
    plan_c.add_argument(
        "--plan",
        type=str,
        required=True,
        help="JSON object with beats, silhouette_story, differs_from, weapon_or_style_notes",
    )
    plan_c.add_argument(
        "--contrast-with",
        type=str,
        default=None,
        help='JSON list of other anim names, e.g. ["walk","fight"]',
    )
    plan_c.add_argument("--out-dir", type=str, default=None)

    get_plan_c = sub.add_parser(
        "get-animation-plan",
        help="Print locked plan.json for a character animation",
    )
    get_plan_c.add_argument("name", help="Character name")
    get_plan_c.add_argument("animation_name", help="Animation name")
    get_plan_c.add_argument("--out-dir", type=str, default=None)

    build_c = sub.add_parser(
        "build-frame",
        help="Draft one animation frame (joint rotations; not finalized)",
    )
    build_c.add_argument("name", help="Character name")
    build_c.add_argument("animation_name", help="Animation name (e.g. walk)")
    build_c.add_argument("frame_index", type=int, help="Frame index 0..7")
    build_c.add_argument(
        "--rotations",
        type=str,
        default=None,
        help='JSON object of joint→degrees, e.g. {"hip_near": 15, "knee_near": -20}',
    )
    build_c.add_argument("--plan-id", type=str, default=None)
    build_c.add_argument("--out-dir", type=str, default=None)
    build_c.add_argument("--scale", type=int, default=8)

    finish_c = sub.add_parser(
        "finish-frame",
        help="Finalize a drafted animation frame",
    )
    finish_c.add_argument("name", help="Character name")
    finish_c.add_argument("animation_name", help="Animation name")
    finish_c.add_argument("frame_index", type=int, help="Frame index 0..7")
    finish_c.add_argument("--plan-id", type=str, default=None)
    finish_c.add_argument("--out-dir", type=str, default=None)

    edit_c = sub.add_parser(
        "pixel-editor",
        help="Open the local manual pixel editor for design layers",
    )
    edit_c.add_argument("name", help="Character name")
    edit_c.add_argument("--out-dir", type=str, default=None, help="Output root override")
    edit_c.add_argument(
        "--port",
        type=int,
        default=0,
        help="TCP port (0 = ephemeral free port)",
    )
    edit_c.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the system browser",
    )

    # Allow bare ``python -m spritemcp`` → demo (matches prior habit).
    args, rest = parser.parse_known_args(argv)
    command = args.command

    if command is None:
        if rest:
            parser.print_help()
            return 2
        command = "demo"

    if command == "demo":
        from .demo import main as demo_main

        _ensure_cli_output_root()
        return demo_main()
    if command == "show-ref-grid":
        from .show_ref_grid import main as show_main

        _ensure_cli_output_root()
        return show_main(rest)
    if command == "base-idle":
        from .base_idle import main as idle_main

        _ensure_cli_output_root()
        return idle_main(rest)
    if command == "pivots":
        from .pivots import main as pivots_main

        _ensure_cli_output_root()
        return pivots_main(rest)
    if command == "paths":
        from .api import get_default_paths

        _ensure_cli_output_root()
        for key, value in get_default_paths().items():
            print(f"{key}: {value}")
        return 0
    if command == "part-ids":
        from .api import get_part_ids

        for name, pid in get_part_ids().items():
            print(f"{pid:2d}  {name}")
        return 0
    if command == "joints":
        from .api import get_joints

        for j in get_joints():
            print(f"{j['name']:16s}  {j['parent']:16s} -> {j['child']}")
        return 0
    if command == "joint-docs":
        from .api import get_joint_docs

        for j in get_joint_docs():
            print(
                f"{j['name']:16s}  {j['parent']:16s} -> {j['child']:16s}  "
                f"rotates={j['rotates']}"
            )
        return 0
    if command == "generate-character":
        from .api import generate_character

        _ensure_cli_output_root(args.out_dir)
        result = generate_character(
            args.name,
            output_dir=None,
            scale=args.scale,
        )
        print(json.dumps(result, indent=2))
        return 0
    if command == "plan-animation":
        from .api import plan_animation

        _ensure_cli_output_root(args.out_dir)
        plan_data = json.loads(args.plan)
        contrast = None
        if args.contrast_with:
            contrast = json.loads(args.contrast_with)
        result = plan_animation(
            args.name,
            args.animation_name,
            args.intent,
            plan_data,
            contrast_with=contrast,
            output_dir=None,
        )
        print(json.dumps(result, indent=2))
        return 0
    if command == "get-animation-plan":
        from .api import get_animation_plan

        _ensure_cli_output_root(args.out_dir)
        result = get_animation_plan(
            args.name,
            args.animation_name,
            output_dir=None,
        )
        print(json.dumps(result, indent=2))
        return 0
    if command == "build-frame":
        from .api import build_frame_animation

        _ensure_cli_output_root(args.out_dir)
        result = build_frame_animation(
            args.name,
            args.animation_name,
            args.frame_index,
            _parse_rotations(args.rotations),
            plan_id=args.plan_id,
            output_dir=None,
            scale=args.scale,
        )
        print(json.dumps(result, indent=2))
        return 0
    if command == "finish-frame":
        from .api import finish_frame_animation

        _ensure_cli_output_root(args.out_dir)
        result = finish_frame_animation(
            args.name,
            args.animation_name,
            args.frame_index,
            plan_id=args.plan_id,
            output_dir=None,
        )
        print(json.dumps(result, indent=2))
        return 0
    if command == "pixel-editor":
        from .api import open_pixel_editor

        _ensure_cli_output_root(args.out_dir)
        result = open_pixel_editor(
            args.name,
            output_dir=None,
            port=args.port,
            open_browser=not args.no_browser,
        )
        print(json.dumps(result, indent=2))
        if not args.no_browser:
            print(
                "Editor running. Press Ctrl+C to stop this process "
                "(the MCP tool keeps the server in the background).",
                file=sys.stderr,
            )
            try:
                import time

                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                print("Stopped.", file=sys.stderr)
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
