"""Rasterise the Motet mark (``motet-mark.svg``) to the PNGs the app icon and the feed use.

    uv run --with pillow brand/mark/render.py

Pillow rather than an SVG renderer, so regenerating needs nothing but ``uv``: the mark is
four cubic curves and one straight line, which is little enough to draw by hand. The
geometry below is the SVG's, and the two must be changed together. It draws at four times
the target size and downsamples, which is the antialiasing.

Outputs are opaque on purpose: an iOS app icon with an alpha channel is rejected by App
Store Connect, and podcast directories want a square RGB image of at least 1400 px.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

PARCHMENT = (0xF4, 0xEF, 0xE6)
INK = (0x1B, 0x1A, 0x2E)
VOICES = [
    ((0xD6, 0x4B, 0x2A), ((1, 3), (8, 3), (10, 11), (25, 11))),
    ((0xD9, 0xA4, 0x41), ((1, 8), (8, 8), (12, 11), (25, 11))),
    ((0x2A, 0x7F, 0x86), ((1, 14), (8, 14), (12, 11), (25, 11))),
    ((0x6B, 0x3E, 0x86), ((1, 19), (8, 19), (10, 11), (25, 11))),
]
INK_LINE = ((24.5, 11), (32, 11))
STROKE = 2.4
# The SVG's `translate(165 281) scale(21)` on a 1024 tile.
TILE, OFFSET, SCALE = 1024, (165, 281), 21

OUTPUTS = {
    "motet-mark-1024.png": 1024,
    "motet-mark-3000.png": 3000,
}


def bezier(p0, p1, p2, p3, steps=160):
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        yield (
            u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
        )


def stroke(draw: ImageDraw.ImageDraw, size: int, points, colour) -> None:
    """One round-capped stroke, stamped as overlapping discs along the path.

    Stamping rather than ``ImageDraw.line`` because a wide polyline leaves hairline slivers
    between its segments on a curve, and a disc every fraction of a stroke-width cannot.
    """
    k = size / TILE
    r = STROKE * SCALE * k / 2
    xy = [((OFFSET[0] + x * SCALE) * k, (OFFSET[1] + y * SCALE) * k) for x, y in points]
    for cx, cy in xy:
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=colour)


def render(size: int) -> Image.Image:
    big = size * 4
    tile = Image.new("RGB", (big, big), PARCHMENT)
    draw = ImageDraw.Draw(tile)
    # Painted in order, like the nav glyph: the four voices, then the one ink line they
    # resolve into, on top of the point where they meet.
    for colour, curve in VOICES:
        stroke(draw, big, list(bezier(*curve, steps=600)), colour)
    (x0, y), (x1, _) = INK_LINE
    stroke(draw, big, [(x0 + (x1 - x0) * i / 400, y) for i in range(401)], INK)
    return tile.resize((size, size), Image.Resampling.LANCZOS)


def main() -> int:
    here = Path(__file__).resolve().parent
    for name, size in OUTPUTS.items():
        render(size).save(here / name, optimize=True)
        sys.stderr.write(f"wrote {name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
