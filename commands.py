"""Turn a comment into the cells it injects.

Grammar (case-insensitive; the earliest valid command anywhere in the text wins):

    glider x,y [ne|nw|se|sw]     default direction se
    block x,y | blinker x,y | rpent x,y | acorn x,y
    cells x,y x,y ...            1 to 25 cells

A pattern's x,y may also be written "x y", "x x y" or "(x, y)" ("acorn 15x15",
"glider 10 20 ne"), since people write them that way; `cells` needs commas.

(x, y) is the top-left of the pattern's bounding box; x in 0..W-1, y in 0..H-1
(0..127 and 0..71 on the first universe's 128x72 board), origin top-left. Patterns that run past an edge wrap. A command with any
coordinate out of range, or a `cells` list longer than 25, is invalid and
ignored.

A comment with no valid command but a pattern's name on its own, with no
number after it ("block", "glider ne", "acorn red"), places that pattern (the
first such name) at the spot (x, y) below; a glider heads se unless a direction
follows its name. A comment with neither injects a 5x5 random patch:

    d = BLAKE2b-256((comment_id + text).encode("utf-8"))
    x = int.from_bytes(d[0:2], "big") % W     (W x H: the board size, 128 x 72 at first)
    y = int.from_bytes(d[2:4], "big") % H
    bits = int.from_bytes(d[4:8], "big")
    cell (i % 5, i // 5) of the patch is alive iff bit i of `bits` is 1, i = 0..24

`text` is the comment text exactly as the API returned it (textFormat=plainText).

A color word anywhere in the text (red orange yellow blue purple pink cyan;
the first one wins) paints the move's cells. Paint is only how cells look: it
never changes which cells live (see life.paint_step). Unpainted cells are
green; dead ones are the dark background.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace

import life

MAX_CELLS = 25
_COORD = r"([0-9]+)\s*,\s*([0-9]+)"
_PATTERN_COORD = r"(?:\(\s*)?([0-9]+)\s*(?:,|x|×|\s)\s*([0-9]+)(?:\s*\))?"  # "15,15", "15x15", "15 15", "(15, 15)"
_PATTERN_RE = re.compile(
    rf"\b(glider|block|blinker|rpent|acorn)\s+{_PATTERN_COORD}(?:\s+(ne|nw|se|sw)\b)?",
    re.IGNORECASE | re.ASCII,
)
_CELLS_RE = re.compile(rf"\bcells((?:\s+{_COORD})+)", re.IGNORECASE | re.ASCII)
# a pattern's name with no number after it (any digits, so "block ١,٢" is not one, nor "acorn -1,5")
_BARE_RE = re.compile(r"\b(glider|block|blinker|rpent|acorn)\b(?!\s*[-+]?\s*\d)(?:\s+(ne|nw|se|sw)\b)?", re.IGNORECASE)
_PAIR_RE = re.compile(_COORD, re.ASCII)
COLORS = ("red", "orange", "yellow", "blue", "purple", "pink", "cyan")  # paint 1..7 (0: unpainted)
_COLOR_RE = re.compile(rf"\b({'|'.join(COLORS)})\b", re.IGNORECASE | re.ASCII)


@dataclass(frozen=True)
class Command:
    kind: str  # glider | block | blinker | rpent | acorn | cells | random
    cells: tuple[tuple[int, int], ...]  # absolute, wrapped, sorted, unique
    desc: str  # canonical text, e.g. "glider 10,20 se" or "random 5x5 at 3,4"
    color: str | None = None  # a word from COLORS, or None (unpainted)


def _in_range(x: int, y: int, shape) -> bool:
    h, w = shape
    return 0 <= x < w and 0 <= y < h


def _norm(cells) -> tuple[tuple[int, int], ...]:
    return tuple(sorted(set(cells)))


def _candidates(text: str, shape):
    """Yield (start_offset, Command | None) for every command-shaped match."""
    for m in _PATTERN_RE.finditer(text):
        kind, x, y = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        if not _in_range(x, y, shape):
            yield m.start(), None
            continue
        if kind == "glider":
            direction = (m.group(4) or "se").lower()
            cells = life.place(life.PATTERNS[f"glider_{direction}"], x, y, shape)
            yield m.start(), Command(kind, _norm(cells), f"glider {x},{y} {direction}")
        else:
            cells = life.place(life.PATTERNS[kind], x, y, shape)
            yield m.start(), Command(kind, _norm(cells), f"{kind} {x},{y}")
    for m in _CELLS_RE.finditer(text):
        pairs = [(int(a), int(b)) for a, b in _PAIR_RE.findall(m.group(1))]
        if len(pairs) > MAX_CELLS or not all(_in_range(x, y, shape) for x, y in pairs):
            yield m.start(), None
            continue
        desc = "cells " + " ".join(f"{x},{y}" for x, y in pairs)
        yield m.start(), Command("cells", _norm(pairs), desc)


def _spot(comment_id: str, text: str, shape) -> tuple[int, int, bytes]:
    """Where a comment without coordinates lands, and the digest it came from."""
    d = hashlib.blake2b((comment_id + text).encode("utf-8"), digest_size=32).digest()
    h, w = shape
    return int.from_bytes(d[0:2], "big") % w, int.from_bytes(d[2:4], "big") % h, d


def bare_pattern(comment_id: str, text: str, shape=(life.H, life.W)) -> Command | None:
    """The first pattern named without coordinates, placed at the comment's spot, or None."""
    m = _BARE_RE.search(text)
    if not m:
        return None
    kind = m.group(1).lower()
    x, y, _ = _spot(comment_id, text, shape)
    if kind == "glider":
        direction = (m.group(2) or "se").lower()
        return Command(kind, _norm(life.place(life.PATTERNS[f"glider_{direction}"], x, y, shape)),
                       f"glider {x},{y} {direction}")
    return Command(kind, _norm(life.place(life.PATTERNS[kind], x, y, shape)), f"{kind} {x},{y}")


def random_patch(comment_id: str, text: str, shape=(life.H, life.W)) -> Command:
    x, y, d = _spot(comment_id, text, shape)
    bits = int.from_bytes(d[4:8], "big")
    offsets = [(i % 5, i // 5) for i in range(25) if (bits >> i) & 1]
    return Command("random", _norm(life.place(offsets, x, y, shape)), f"random 5x5 at {x},{y}")


def parse(comment_id: str, text: str, shape=(life.H, life.W)) -> Command:
    """The command a comment injects. Never fails: falls back to a random patch."""
    valid = [(pos, cmd) for pos, cmd in _candidates(text, shape) if cmd is not None]
    cmd = (min(valid, key=lambda pc: pc[0])[1] if valid
           else bare_pattern(comment_id, text, shape) or random_patch(comment_id, text, shape))
    m = _COLOR_RE.search(text)
    return replace(cmd, color=m.group(1).lower()) if m else cmd
