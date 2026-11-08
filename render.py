"""What the video shows: the status title and the 1280x720 thumbnail."""

from __future__ import annotations

import math
import os

import numpy as np

import life
from universe import ALIVE, CONTESTED, DIED, FLATLINING, GEN_SECONDS, Universe

TITLE_BASE = "This video dies when its universe gets stuck in a loop"
CELL = 10  # px per cell: 128x72 cells -> 1280x720

BG = (10, 12, 16)
GRID = (22, 25, 32)
HIGHLIGHT = (255, 255, 255)  # cells injected since the last render
PHASE_COLOR = {
    ALIVE: (120, 230, 160),
    FLATLINING: (240, 90, 90),
    CONTESTED: (250, 200, 80),
    DIED: (120, 122, 135),
}


def time_left(gens: int, gen_seconds: int = GEN_SECONDS) -> str:
    """Short form for the thumbnail: '14h left', '45m left'."""
    secs = gens * gen_seconds
    if secs >= 3600:
        return f"{math.ceil(secs / 3600)}h left"
    return f"{max(1, math.ceil(secs / 60))}m left"


def duration(secs: float) -> str:
    """Countdown wording for titles: '2 days, 14 hours', '14 hours', '45 minutes'."""
    def n(k, unit):
        return f"{k} {unit}" + ("" if k == 1 else "s")
    secs = max(0, int(secs))
    days, hours = secs // 86400, secs % 86400 // 3600
    if days:
        return n(days, "day") + (f", {n(hours, 'hour')}" if hours else "")
    if hours:
        return n(hours, "hour")
    return n(max(1, math.ceil(secs / 60)), "minute")


VERB = "dies"  # one place to change the title's verb (e.g. "self-destructs")


def title(u: Universe, gen_seconds: int = GEN_SECONDS, stuck_gen: int | None = None,
          horizon_gens: int | None = None) -> str:
    """The video title: a countdown to death.

    ALIVE needs the forecast (`stuck_gen`: when it would get stuck if nobody
    comments; None with `horizon_gens` = not within that many generations)."""
    if u.phase == DIED:
        return f"This video died: its universe got stuck in a loop"
    if u.phase == FLATLINING:
        return f"This video {VERB} in {duration(u.gens_left() * gen_seconds)}: its universe is stuck in a loop"
    if u.phase == CONTESTED:
        return f"This video might die in {duration(u.gens_left() * gen_seconds)}: a comment broke the loop"
    if stuck_gen is not None:
        when = duration((stuck_gen + u.flatline_gens - u.generation) * gen_seconds)
    elif horizon_gens is not None:
        when = f"{(horizon_gens + u.flatline_gens) * gen_seconds // 86400}+ days"
    else:
        return f"{TITLE_BASE} · Game of Life"
    return f"This video {VERB} in {when} if nobody comments · Game of Life"


_BRAILLE_BITS = ((0, 0, 0x01), (0, 1, 0x02), (0, 2, 0x04), (1, 0, 0x08),
                 (1, 1, 0x10), (1, 2, 0x20), (0, 3, 0x40), (1, 3, 0x80))


def braille(grid: np.ndarray) -> str:
    """The grid as Unicode braille, 2x4 cells per character (128x72 -> 64x18).

    About 3.5 KB of UTF-8, small enough for a video description (5000 bytes)."""
    h, w = grid.shape
    ph, pw = -h % 4, -w % 2
    g = np.pad(grid, ((0, ph), (0, pw)))
    code = np.zeros((g.shape[0] // 4, g.shape[1] // 2), dtype=np.int32)
    for dx, dy, bit in _BRAILLE_BITS:
        code += g[dy::4, dx::2].astype(np.int32) * bit
    return "\n".join("".join(chr(0x2800 + c) for c in row) for row in code)


def _font(size: int):
    from PIL import ImageFont

    return ImageFont.load_default(size=size)


def thumbnail(u: Universe, path: str, highlight=(), gen_seconds: int = GEN_SECONDS) -> int:
    """Write the thumbnail PNG. `highlight` is an iterable of (x, y) cells to
    emphasize if alive (cells injected since the last render). Returns bytes."""
    from PIL import Image, ImageDraw

    h, w = u.grid.shape
    color = np.array(PHASE_COLOR[u.phase], np.uint8)
    hl = life.mask(highlight, (h, w)) if highlight else np.zeros((h, w), np.uint8)

    inner = np.zeros((CELL, CELL), bool)
    inner[1:-1, 1:-1] = True
    tile = np.tile(inner, (h, w))
    alive = np.kron(u.grid, np.ones((CELL, CELL), np.uint8)).astype(bool) & tile
    lit = np.kron(u.grid & hl, np.ones((CELL, CELL), np.uint8)).astype(bool) & tile

    img = np.empty((h * CELL, w * CELL, 3), np.uint8)
    img[:] = GRID
    img[tile] = BG
    img[alive] = color
    img[lit] = HIGHLIGHT

    im = Image.fromarray(img).convert("RGBA")
    band = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(band)
    W, H = im.size
    d.rectangle([0, H - 130, W, H], fill=(0, 0, 0, 200))
    status = "DIED" if u.phase == DIED else u.phase
    d.text((40, H - 118), status, font=_font(96), fill=PHASE_COLOR[u.phase] + (255,))
    if u.phase == DIED:
        right = f"at gen {u.died_gen:,}"
    elif u.phase in (FLATLINING, CONTESTED):
        right = f"Gen {u.generation:,} · {time_left(u.gens_left(), gen_seconds)}"
    else:
        right = f"Gen {u.generation:,}"
    font = _font(56)
    tw = d.textlength(right, font=font)
    d.text((W - 40 - tw, H - 96), right, font=font, fill=(235, 235, 240, 255))
    im = Image.alpha_composite(im, band).convert("RGB")

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    im.save(path, "PNG", optimize=True)
    size = os.path.getsize(path)
    if size >= 2 * 1024 * 1024:
        raise RuntimeError(f"thumbnail {path} is {size} bytes; must be under 2 MB")
    return size
