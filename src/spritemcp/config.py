"""Configurable paths for SpriteMCP.

Defaults
--------
- **Authored armature:** ``<repo>/base/`` in a source checkout; otherwise the
  packaged ``spritemcp/authored_base/`` copy (uvx / pip / wheel).
- **Output root:** always ``Path.cwd() / "output"`` when no session override
  and no per-call ``output_dir``. That is the MCP/agent working directory
  (usually the Cursor project), not the package install location.
- **Env seed:** if ``SPRITE_GEN_OUTPUT_ROOT`` is set when this module loads,
  it becomes the initial session output root (same as ``set_output_root``).

Session output root
-------------------
Call ``set_output_root(path)`` so subsequent API/MCP calls that omit
``output_dir`` write under that absolute directory (created if needed).
The override lasts for the process / MCP session only — it is not written
to disk. Per-call ``output_dir`` still wins when provided.
"""

from __future__ import annotations

import os
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent


def _detect_repo_root() -> Path | None:
    """Return the git/project root when running from a source checkout."""
    # Editable / src layout: <repo>/src/spritemcp/config.py
    if _PKG_DIR.parent.name == "src":
        candidate = _PKG_DIR.parents[1]
        if (candidate / "pyproject.toml").is_file():
            return candidate
    # Legacy flat layout: <repo>/spritemcp/config.py
    candidate = _PKG_DIR.parent
    if (candidate / "pyproject.toml").is_file() and (candidate / "base").is_dir():
        return candidate
    return None


_REPO_ROOT = _detect_repo_root()

# Public alias kept for callers that imported MINI_PROJECT_ROOT.
# Checkout root when editable; otherwise process CWD (legacy / demos).
MINI_PROJECT_ROOT = _REPO_ROOT if _REPO_ROOT is not None else Path.cwd()

# Optional sibling game repo (only meaningful in a monorepo checkout).
GAME_ROOT = MINI_PROJECT_ROOT.parent

_PACKAGED_BASE_DIR = _PKG_DIR / "authored_base"


def authored_base_dir() -> Path:
    """Directory with the 90×128 armature PNGs (eyes + part masks).

    Prefers ``<repo>/base`` in a checkout so artists edit one tree; falls back
    to the packaged ``spritemcp/authored_base`` copy shipped in the wheel.
    """
    if _REPO_ROOT is not None:
        checkout = _REPO_ROOT / "base"
        if (checkout / "eyes.png").is_file():
            return checkout
    if (_PACKAGED_BASE_DIR / "eyes.png").is_file():
        return _PACKAGED_BASE_DIR
    raise FileNotFoundError(
        "Authored base armature not found. Expected repo base/eyes.png or "
        f"packaged {_PACKAGED_BASE_DIR / 'eyes.png'}."
    )


def default_output_dir() -> Path:
    """Default output root: ``<cwd>/output`` (agent/MCP working directory).

    Never uses the package install path. When developing inside this repo with
    cwd set to the checkout, that is ``<repo>/output``.
    """
    return Path.cwd() / "output"


# Snapshot at import for legacy importers; prefer ``default_output_dir()`` /
# ``get_output_root()`` so cwd changes are reflected.
DEFAULT_OUTPUT_DIR = default_output_dir()
DEFAULT_STYLE_REF = (
    GAME_ROOT / "assets" / "art" / "style_refs" / "character_style_ref.png"
)

# Optional legacy game art path (not the default write target).
LEGACY_GAME_OUTPUT_DIR = GAME_ROOT / "assets" / "art" / "spritemcp"


def _session_root_from_env() -> Path | None:
    """If ``SPRITE_GEN_OUTPUT_ROOT`` is set, use it as the initial session root."""
    raw = os.environ.get("SPRITE_GEN_OUTPUT_ROOT", "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


# Process / MCP-session override (None → ``Path.cwd() / "output"``).
_session_output_root: Path | None = _session_root_from_env()


def set_output_root(path: Path | str) -> Path:
    """Set the session output root; create it if needed; return absolute path.

    Persists for this Python process (MCP server session). Does not change
    the cwd-based default. Pass an absolute or relative path — relative
    paths resolve against the current working directory.
    """
    global _session_output_root
    root = Path(path).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    _session_output_root = root
    return root


def clear_output_root() -> Path:
    """Clear the session override; subsequent calls use ``<cwd>/output``."""
    global _session_output_root
    _session_output_root = None
    return get_output_root()


def get_output_root() -> Path:
    """Absolute output root: session override if set, else ``<cwd>/output``."""
    if _session_output_root is not None:
        return _session_output_root
    return default_output_dir().resolve()


def has_session_output_root() -> bool:
    """True when ``set_output_root`` (or ``SPRITE_GEN_OUTPUT_ROOT``) is active."""
    return _session_output_root is not None


def resolve_output_dir(output_dir: Path | str | None = None) -> Path:
    """Return absolute output directory.

    - ``output_dir`` provided → that path (resolved, not auto-created here)
    - else → session ``set_output_root`` value if set
    - else → ``Path.cwd() / "output"``
    """
    if output_dir is None:
        return get_output_root()
    return Path(output_dir).expanduser().resolve()


def resolve_style_ref(style_ref: Path | str | None = None) -> Path:
    """Return absolute path to the character style reference PNG."""
    if style_ref is None:
        return DEFAULT_STYLE_REF
    return Path(style_ref).expanduser().resolve()


def subdir(name: str, output_dir: Path | str | None = None) -> Path:
    """``output_dir / name`` (e.g. ``base_idle``, ``demo``, ``reference_grid``)."""
    return resolve_output_dir(output_dir) / name
