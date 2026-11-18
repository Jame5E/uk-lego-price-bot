from __future__ import annotations

import struct
import zlib

# Minimal 3x5 pixel font for digits, '.', and '£'
_FONT: dict[str, tuple[str, ...]] = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    ".": ("000", "000", "000", "000", "010"),
    "£": ("011", "100", "110", "100", "111"),
}


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    chunk = chunk_type + data
    return struct.pack("!I", len(data)) + chunk + struct.pack("!I", zlib.crc32(chunk) & 0xFFFFFFFF)


def _encode_png(width: int, height: int, pixels: bytearray) -> bytes:
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        start = y * stride
        raw.extend(pixels[start : start + stride])

    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def _set_px(pixels: bytearray, width: int, height: int, x: int, y: int, rgb: tuple[int, int, int]) -> None:
    if 0 <= x < width and 0 <= y < height:
        idx = (y * width + x) * 3
        pixels[idx] = rgb[0]
        pixels[idx + 1] = rgb[1]
        pixels[idx + 2] = rgb[2]


def _draw_line(
    pixels: bytearray,
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    rgb: tuple[int, int, int],
    thickness: int = 1,
) -> None:
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy

    while True:
        for ox in range(-thickness + 1, thickness):
            for oy in range(-thickness + 1, thickness):
                _set_px(pixels, width, height, x0 + ox, y0 + oy, rgb)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x0 += sx
        if e2 < dx:
            err += dx
            y0 += sy


def _draw_circle(
    pixels: bytearray,
    width: int,
    height: int,
    cx: int,
    cy: int,
    radius: int,
    rgb: tuple[int, int, int],
) -> None:
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy <= radius * radius:
                _set_px(pixels, width, height, cx + dx, cy + dy, rgb)


def _draw_text(
    pixels: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    text: str,
    rgb: tuple[int, int, int],
    scale: int = 2,
) -> None:
    cursor = x
    for ch in text:
        glyph = _FONT.get(ch)
        if glyph:
            for row_i, row in enumerate(glyph):
                for col_i, bit in enumerate(row):
                    if bit == "1":
                        for sy in range(scale):
                            for sx in range(scale):
                                _set_px(
                                    pixels,
                                    width,
                                    height,
                                    cursor + col_i * scale + sx,
                                    y + row_i * scale + sy,
                                    rgb,
                                )
        cursor += 4 * scale


def generate_price_chart(
    prices_gbp: list[float],
    rrp_gbp: float,
    *,
    width: int = 560,
    height: int = 220,
) -> bytes:
    """Generate a Discord-friendly dark-mode PNG price history chart (pure Python)."""
    bg = (43, 45, 49)
    grid = (64, 68, 75)
    rrp_col = (231, 76, 60)
    line_col = (46, 204, 113)
    dot_col = (255, 255, 255)
    text_col = (210, 214, 220)

    pixels = bytearray(bg * (width * height))

    if not prices_gbp:
        return _encode_png(width, height, pixels)

    left, right, top, bottom = 78, width - 20, 20, height - 22
    plot_w = right - left
    plot_h = bottom - top

    all_vals = list(prices_gbp) + ([rrp_gbp] if rrp_gbp > 0 else [])
    min_p = min(all_vals) * 0.95
    max_p = max(all_vals) * 1.03
    if max_p <= min_p:
        max_p = min_p + 1.0

    def to_y(val: float) -> int:
        ratio = (val - min_p) / (max_p - min_p)
        return int(bottom - ratio * plot_h)

    # Horizontal grid lines
    for frac in (0.0, 0.5, 1.0):
        gy = int(top + frac * plot_h)
        _draw_line(pixels, width, height, left, gy, right, gy, grid)

    # Labels (max, mid, min)
    _draw_text(pixels, width, height, 8, top - 4, f"£{max_p:.0f}", text_col, scale=2)
    _draw_text(pixels, width, height, 8, bottom - 8, f"£{min_p:.0f}", text_col, scale=2)

    # RRP dashed line
    if rrp_gbp > 0:
        ry = to_y(rrp_gbp)
        for x in range(left, right, 12):
            _draw_line(pixels, width, height, x, ry, min(x + 6, right), ry, rrp_col)

    # Price series
    n = len(prices_gbp)
    pts: list[tuple[int, int]] = []
    for idx, p in enumerate(prices_gbp):
        px = left + (plot_w // 2 if n == 1 else int((idx / (n - 1)) * plot_w))
        py = to_y(p)
        pts.append((px, py))

    for i in range(len(pts) - 1):
        _draw_line(pixels, width, height, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], line_col, thickness=2)

    for px, py in pts:
        _draw_circle(pixels, width, height, px, py, 4, dot_col)

    return _encode_png(width, height, pixels)
