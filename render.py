"""What the video shows: the status title and the 1280x720 thumbnail."""

from __future__ import annotations

import math
import os

import numpy as np

import life
from universe import ALIVE, CONTESTED, DIED, FLATLINING, GEN_SECONDS, Universe

TITLE_BASE = "This video dies when its universe repeats"
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
    secs = gens * gen_seconds
    if secs >= 3600:
        return f"{math.ceil(secs / 3600)}h left"
    return f"{max(1, math.ceil(secs / 60))}m left"


def title(u: Universe, gen_seconds: int = GEN_SECONDS) -> str:
    if u.phase == DIED:
        return f"{TITLE_BASE} · DIED at gen {u.died_gen:,}"
    parts = [TITLE_BASE, f"Gen {u.generation:,}", u.phase]
    if u.phase in (FLATLINING, CONTESTED):
        parts.append(time_left(u.gens_left(), gen_seconds))
    return " · ".join(parts)


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
