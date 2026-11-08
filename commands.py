"""Turn a comment into the cells it injects.

Grammar (case-insensitive; the earliest valid command anywhere in the text wins):

    glider x,y [ne|nw|se|sw]     default direction se
    block x,y | blinker x,y | rpent x,y | acorn x,y
    cells x,y x,y ...            1 to 25 cells

(x, y) is the top-left of the pattern's bounding box; x in 0..127, y in 0..71,
origin top-left. Patterns that run past an edge wrap. A command with any
coordinate out of range, or a `cells` list longer than 25, is invalid and
ignored. A comment with no valid command injects a 5x5 random patch:

    d = BLAKE2b-256((comment_id + text).encode("utf-8"))
    x = int.from_bytes(d[0:2], "big") % 128
    y = int.from_bytes(d[2:4], "big") % 72
    bits = int.from_bytes(d[4:8], "big")
    cell (i % 5, i // 5) of the patch is alive iff bit i of `bits` is 1, i = 0..24

`text` is the comment text exactly as the API returned it (textFormat=plainText).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import life

MAX_CELLS = 25
_COORD = r"([0-9]+)\s*,\s*([0-9]+)"
_PATTERN_RE = re.compile(
    rf"\b(glider|block|blinker|rpent|acorn)\s+{_COORD}(?:\s+(ne|nw|se|sw)\b)?",
    re.IGNORECASE | re.ASCII,
)
_CELLS_RE = re.compile(rf"\bcells((?:\s+{_COORD})+)", re.IGNORECASE | re.ASCII)
_PAIR_RE = re.compile(_COORD, re.ASCII)


@dataclass(frozen=True)
class Command:
    kind: str  # glider | block | blinker | rpent | acorn | cells | random
    cells: tuple[tuple[int, int], ...]  # absolute, wrapped, sorted, unique
    desc: str  # canonical text, e.g. "glider 10,20 se" or "random 5x5 at 3,4"


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


def random_patch(comment_id: str, text: str, shape=(life.H, life.W)) -> Command:
    d = hashlib.blake2b((comment_id + text).encode("utf-8"), digest_size=32).digest()
    h, w = shape
    x = int.from_bytes(d[0:2], "big") % w
    y = int.from_bytes(d[2:4], "big") % h
    bits = int.from_bytes(d[4:8], "big")
    offsets = [(i % 5, i // 5) for i in range(25) if (bits >> i) & 1]
    return Command("random", _norm(life.place(offsets, x, y, shape)), f"random 5x5 at {x},{y}")


def parse(comment_id: str, text: str, shape=(life.H, life.W)) -> Command:
    """The command a comment injects. Never fails: falls back to a random patch."""
    valid = [(pos, cmd) for pos, cmd in _candidates(text, shape) if cmd is not None]
    if valid:
        return min(valid, key=lambda pc: pc[0])[1]
    return random_patch(comment_id, text, shape)
