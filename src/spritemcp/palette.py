"""Character style-lock palette for the sprite toolkit.

Colors match the game style bible (docs/art-character-style.md), sampled from
assets/art/style_refs/character_style_ref.png in the parent game repo.
Background grey from the ref is intentionally excluded — sprites ship as
transparent RGBA.
"""

from __future__ import annotations

from typing import Mapping

# RGBA tuples (0–255). Alpha 255 = opaque.
Color = tuple[int, int, int, int]


def _hex(hex_color: str, alpha: int = 255) -> Color:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r, g, b, alpha)


TRANSPARENT: Color = (0, 0, 0, 0)

# --- Locked roles (mandatory character palette) ---
NEAR_BLACK: Color = _hex("#0E151C")
NEAR_BLACK_ALT: Color = _hex("#17171D")
OFF_WHITE: Color = _hex("#DEC1B5")
SHADOW_BEIGE: Color = _hex("#A08884")
DEEP_RED: Color = _hex("#790F19")
MUTED_PLUM: Color = _hex("#291A1F")
MUTED_PLUM_ALT: Color = _hex("#3D2334")

# Ref field only — never paint this into game sprites.
REF_BACKGROUND: Color = _hex("#A6A6A6")

CHARACTER_PALETTE: Mapping[str, Color] = {
    "near_black": NEAR_BLACK,
    "near_black_alt": NEAR_BLACK_ALT,
    "off_white": OFF_WHITE,
    "shadow_beige": SHADOW_BEIGE,
    "deep_red": DEEP_RED,
    "muted_plum": MUTED_PLUM,
    "muted_plum_alt": MUTED_PLUM_ALT,
}

# Ordered list for demos / swatches (excludes optional alts that are rarely needed).
PRIMARY_ROLES: tuple[str, ...] = (
    "near_black",
    "off_white",
    "shadow_beige",
    "deep_red",
)

# One darker step for hard pixel shading (no gradients). Local flat fill → shade.
SHADE_STEP: Mapping[str, str] = {
    "off_white": "shadow_beige",
    "shadow_beige": "muted_plum",
    "deep_red": "muted_plum",
    "muted_plum": "muted_plum_alt",
    "muted_plum_alt": "near_black",
    "near_black_alt": "near_black",
    "near_black": "near_black",
}


def get_color(role: str) -> Color:
    """Return an RGBA color by style-lock role name."""
    try:
        return CHARACTER_PALETTE[role]
    except KeyError as exc:
        known = ", ".join(CHARACTER_PALETTE)
        raise KeyError(f"Unknown palette role {role!r}. Known: {known}") from exc


def get_shade_role(local_role: str) -> str:
    """Return the one-step-darker palette role for a local flat color role."""
    if local_role not in CHARACTER_PALETTE:
        known = ", ".join(CHARACTER_PALETTE)
        raise KeyError(f"Unknown palette role {local_role!r}. Known: {known}")
    return SHADE_STEP.get(local_role, "near_black")


def get_shade_color(local_role: str) -> Color:
    """RGBA for the one-step-darker shadow of ``local_role``."""
    return get_color(get_shade_role(local_role))


def list_shade_palette() -> list[dict[str, str]]:
    """Local → shade role pairs with hex (for agent shading plans)."""
    rows: list[dict[str, str]] = []
    for local_role, shade_role in SHADE_STEP.items():
        rows.append(
            {
                "local_role": local_role,
                "local_hex": as_hex(get_color(local_role)),
                "shade_role": shade_role,
                "shade_hex": as_hex(get_color(shade_role)),
            }
        )
    return rows


def as_hex(color: Color) -> str:
    """Format RGB as #RRGGBB (alpha ignored)."""
    return f"#{color[0]:02X}{color[1]:02X}{color[2]:02X}"
