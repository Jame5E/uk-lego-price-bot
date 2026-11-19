from __future__ import annotations

import re

# Keywords indicating accessories, display mounts, lighting kits, parts, used items, or clones
REJECTED_TITLE_PHRASES: tuple[str, ...] = (
    "light kit",
    "lighting kit",
    "led light",
    "led kit",
    "briksmax",
    "lightailing",
    "game of bricks",
    "yeabricks",
    "display case",
    "display stand",
    "display frame",
    "acrylic case",
    "acrylic stand",
    "wall mount",
    "3d wall",
    "mounting bracket",
    "dust cover",
    "upgrade kit",
    "rubber realistic wheel",
    "sticker sheet",
    "stickers only",
    "replacement parts",
    "spare parts",
    "instructions only",
    "manual only",
    "empty box",
    "box only",
    "no box",
    "no minifigures",
    "minifigure only",
    "minifigures only",
    "incomplete",
    "damaged box",
    "pre-owned",
    "preowned",
    "second hand",
    "refurbished",
    "used condition",
    "compatible with lego",
    "moc ",
    " moc",
)


def is_valid_lego_set_listing(
    title: str,
    set_number: str | None,
    price_gbp: float,
    rrp_gbp: float | None = None,
) -> bool:
    """Validate that a product title & price represent a genuine, complete, brand-new LEGO set."""
    if price_gbp < 5.0:
        return False

    clean_title = f" {title.strip().lower()} "
    if not clean_title.strip():
        return False

    if set_number:
        clean_num = set_number.strip().split("-")[0]
        if clean_num and not re.search(rf"\b{re.escape(clean_num)}\b", clean_title):
            return False

    for phrase in REJECTED_TITLE_PHRASES:
        if phrase in clean_title:
            return False

    # Reject "for LEGO 75192" accessory phrasing
    if re.search(r"\bfor\s+lego\b", clean_title):
        return False

    # Reject generic "building blocks" knockoffs that omit the LEGO brand
    if "building blocks" in clean_title and "lego" not in clean_title:
        return False

    if rrp_gbp is not None and rrp_gbp > 0:
        min_plausible = max(5.0, round(rrp_gbp * 0.40, 2))
        max_plausible = round(rrp_gbp * 1.35, 2)
        if not (min_plausible <= price_gbp <= max_plausible):
            return False

    return True
