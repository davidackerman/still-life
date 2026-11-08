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
    """Countdown clock for titles, rounded down to the quarter hour (the title
    changes at most every 15 minutes): '3d 13h 45m', '14h 30m', '45m', 'under 15m'."""
    q = max(0, int(secs)) // 900 * 900
    d, h, m = q // 86400, q % 86400 // 3600, q % 3600 // 60
    if d:
        return f"{d}d {h:02d}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m" if m else "under 15m"


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
        when = f"{(horizon_gens + u.flatline_gens) * gen_seconds // 86400}+ days"  # beyond the forecast
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
    # Everything sits bottom-LEFT: YouTube overlays the video length on the
    # bottom-right corner (and hover icons on the top-right).
    for i in range(70):  # soft fade into the band
        d.line([(0, H - 210 - i), (W, H - 210 - i)], fill=(0, 0, 0, int(200 * (1 - i / 70))))
    d.rectangle([0, H - 210, W, H], fill=(0, 0, 0, 200))
    status = "DIED" if u.phase == DIED else u.phase
    if u.phase == DIED:
        line = f"at generation {u.died_gen:,}"
    elif u.phase in (FLATLINING, CONTESTED):
        line = f"Gen {u.generation:,} · {time_left(u.gens_left(), gen_seconds)}"
    else:
        line = f"Gen {u.generation:,}"
    d.text((40, H - 150), line, font=_font(52), fill=(235, 235, 240, 255), anchor="ls")
    d.text((36, H - 36), status, font=_font(104), fill=PHASE_COLOR[u.phase] + (255,), anchor="ls")
    im = Image.alpha_composite(im, band).convert("RGB")

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    im.save(path, "PNG", optimize=True)
    size = os.path.getsize(path)
    if size >= 2 * 1024 * 1024:
        raise RuntimeError(f"thumbnail {path} is {size} bytes; must be under 2 MB")
    return size


# ---------------------------------------------------------------- pinned comment
PINNED_MARKER = "Latest changes to the universe"  # how the bot recognises the owner's comment
LIVE_ADDRESS = "davidackerman.github.io/still-life"
PHASE_WORDS = {
    "cycle_detected": "got stuck in a loop: the 24-hour countdown started",
    "new_cycle": "fell into a new loop: the countdown restarted",
    "reentered_cycle": "fell back into its loop: the countdown kept running",
    "injection": "a comment broke the loop",
    "survived_deadline": "saved: still changing when the countdown ran out",
    "deadline": "died",
}


def event_line(ev: dict) -> str | None:
    """One line of the pinned comment for a logged event (game data only)."""
    if ev["type"] == "injection":
        return f"gen {ev['gen']:,}: {ev['command']}" + ("" if ev["changed"] else " (changed nothing)")
    if ev["type"] == "phase":
        return f"gen {ev['gen']:,}: {PHASE_WORDS.get(ev['reason'], ev['to'].lower())}"
    return None


def pinned_text(recent: list[str], link: bool = True) -> str:
    lines = [f"{PINNED_MARKER} (this comment updates itself):"]
    lines += [f"• {r}" for r in recent] or ["• nothing yet. Be the first: comment something like glider 60,30"]
    lines.append(f"Watch it live and see exactly what each comment did: {LIVE_ADDRESS}" if link
                 else "Watch it live: the address is at the end of the video.")
    return "\n".join(lines)
