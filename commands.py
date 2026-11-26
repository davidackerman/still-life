"""Turn a comment into the cells it injects.

Grammar (case-insensitive; the earliest valid command anywhere in the text wins):

    glider x,y [ne|nw|se|sw]     default direction se
    block x,y | blinker x,y | rpent x,y | acorn x,y
    cells x,y x,y ...            1 to 25 cells
    random x,y                   a 5x5 random patch (below), its top-left at x,y

A pattern's x,y may also be written "x y", "x x y", "x.y" or "(x, y)" ("acorn 15x15",
"glider 10 20 ne"), since people write them that way; `cells` needs commas.
`random` also takes "random at x,y" and the move list's own "random 5x5 at x,y";
"random 5x5" alone is the size, not a spot.

(x, y) is the top-left of the pattern's bounding box; x in 0..W-1, y in 0..H-1
(0..127 and 0..71 on the first universe's 128x72 board), origin top-left. Patterns that run past an edge wrap. A command with any
coordinate out of range, or a `cells` list longer than 25, is invalid and
ignored.

A comment with no valid command but a place on its own, "x,y" with a comma
("8, 56 red", "(8,56)"; not "1,000", "-1,5" or "1,2,3"), puts its move there:
the pattern named anywhere in it ("glider red 8,56", "a block at 10,20"), or
else its random patch (below). People write a place like that and expect it
to count (Nov 25: "8, 56 red" and "1,1 red" in the live chat).
A comment with no valid command but a pattern's name on its own, with no
number after it ("block", "glider ne", "acorn red"), places that pattern (the
first such name) at its place, or the spot (x, y) below; a glider heads se unless
a direction follows its name. A comment with neither injects a 5x5 random patch,
at its place, or at (x, y):

    d = BLAKE2b-256((comment_id + text).encode("utf-8"))
    x = int.from_bytes(d[0:2], "big") % W     (W x H: the board size, 128 x 72 at first)
    y = int.from_bytes(d[2:4], "big") % H
    bits = int.from_bytes(d[4:8], "big")
    cell (i % 5, i // 5) of the patch is alive iff bit i of `bits` is 1, i = 0..24

`text` is the comment text exactly as the API returned it (textFormat=plainText).
"random x,y" makes the same patch from the same bits, at the x,y it names.

A time anywhere in the text ("in 1h", "1 hr", "in 30 min", "90 minutes"; the
first one wins, at most MAX_DELAY_S) schedules the move to land that long after
the comment was posted; it is taken out before the command is read, so
"rpent 1 hr" is an rpent.

A color word anywhere in the text (red orange yellow blue purple pink cyan;
the first one wins) paints the move's cells. Paint is only how cells look: it
never changes which cells live (see life.paint_step). Unpainted cells are
green; dead ones are the dark background.

"black" with a named pattern ("glider 40,20 black", "block black") erases
instead: the living cells under the shape die (they are only dead: life can
grow back into them). The five patterns erase, and so does a random patch
when the comment says "random" ("random 40,20 black", "random black": its
live cells, about 12 of the 25). A comment that says black but names none
(a plain message's patch, or "cells") adds cells as usual, so nobody erases by
accident or a 25-cell swath of their choosing at once.
(admission also lets each person land one eraser per generation.)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace

import life

MAX_CELLS = 25
_COORD = r"([0-9]+)\s*,\s*([0-9]+)"
_PATTERN_COORD = r"(?:\(\s*)?([0-9]+)\s*(?:,|\.|x|×|\s)\s*([0-9]+)(?:\s*\))?"  # "15,15", "15x15", "15 15", "15.15", "(15, 15)"
_PATTERN_RE = re.compile(
    rf"\b(glider|block|blinker|rpent|acorn)\s+{_PATTERN_COORD}(?:\s+(ne|nw|se|sw)\b)?",
    re.IGNORECASE | re.ASCII,
)
# "random 46 56", "random at 46,56", "random 5x5 at 46,56" (the move list's words); "random 5x5" alone is no spot
_RANDOM_RE = re.compile(rf"\brandom\s+(?:5\s*[x×]\s*5\s+(?:at\s+)?|at\s+)?(?!5\s*[x×]\s*5\b){_PATTERN_COORD}",
                        re.IGNORECASE | re.ASCII)
_CELLS_RE = re.compile(rf"\bcells((?:\s+{_COORD})+)", re.IGNORECASE | re.ASCII)
# a pattern's name with no number after it (any digits, so "block ١,٢" is not one, nor "acorn -1,5")
_BARE_RE = re.compile(r"\b(glider|block|blinker|rpent|acorn)\b(?!\s*[-+]?\s*\d)(?:\s+(ne|nw|se|sw)\b)?", re.IGNORECASE)
# a place on its own, "8,56" or "8, 56" or "(8, 56)": not part of a longer number or list ("1,000", "-1,5", "1,2,3")
_NUM = r"(0|[1-9][0-9]{0,2})"
_PLACE_RE = re.compile(rf"(?<![\w.,+\-]){_NUM}\s*,\s*{_NUM}(?![\w.,])", re.ASCII)
_PAIR_RE = re.compile(_COORD, re.ASCII)
COLORS = ("red", "orange", "yellow", "blue", "purple", "pink", "cyan")  # paint 1..7 (0: unpainted)
_COLOR_RE = re.compile(rf"\b({'|'.join(COLORS)})\b", re.IGNORECASE | re.ASCII)
ERASE = "black"  # with a named pattern: its cells die instead (Command.erase)
ERASERS = ("glider", "block", "blinker", "rpent", "acorn")  # what "black" works with, and "random" said in words (not cells)
_RANDOM_WORD_RE = re.compile(r"\brandom\b", re.IGNORECASE | re.ASCII)
_ERASE_RE = re.compile(rf"\b{ERASE}\b", re.IGNORECASE | re.ASCII)
MAX_DELAY_S = 24 * 3600  # a move can be scheduled up to a day ahead
_DELAY_RE = re.compile(r"\b(?:in\s+)?([0-9]{1,4})\s*(hours?|hrs?|h|minutes?|mins?|m)\b", re.IGNORECASE | re.ASCII)


@dataclass(frozen=True)
class Command:
    kind: str  # glider | block | blinker | rpent | acorn | cells | random
    cells: tuple[tuple[int, int], ...]  # absolute, wrapped, sorted, unique
    desc: str  # canonical text, e.g. "glider 10,20 se" or "random 5x5 at 3,4"
    color: str | None = None  # a word from COLORS, or None (unpainted)
    delay_s: int = 0  # scheduled to land this long after it was posted (0: at once)
    erase: bool = False  # "black": the cells under it die instead of coming alive


def _in_range(x: int, y: int, shape) -> bool:
    h, w = shape
    return 0 <= x < w and 0 <= y < h


def _norm(cells) -> tuple[tuple[int, int], ...]:
    return tuple(sorted(set(cells)))


def _candidates(text: str, shape, comment_id: str = "", whole: str = ""):
    """Yield (start_offset, Command | None) for every command-shaped match.
    `whole`: the comment's full text, which a random patch's bits come from."""
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
    for m in _RANDOM_RE.finditer(text):
        x, y = int(m.group(1)), int(m.group(2))
        yield m.start(), (random_patch(comment_id, whole or text, shape, at=(x, y)) if _in_range(x, y, shape) else None)


def _spot(comment_id: str, text: str, shape) -> tuple[int, int, bytes]:
    """Where a comment without coordinates lands, and the digest it came from."""
    d = hashlib.blake2b((comment_id + text).encode("utf-8"), digest_size=32).digest()
    h, w = shape
    return int.from_bytes(d[0:2], "big") % w, int.from_bytes(d[2:4], "big") % h, d


def place(text: str, shape=(life.H, life.W)) -> tuple[int, int] | None:
    """The first place written on its own ("8, 56") that is on the board, or None."""
    for m in _PLACE_RE.finditer(text):
        x, y = int(m.group(1)), int(m.group(2))
        if _in_range(x, y, shape):
            return x, y
    return None


def bare_pattern(comment_id: str, text: str, shape=(life.H, life.W), at: tuple[int, int] | None = None) -> Command | None:
    """The first pattern named without coordinates, placed at `at` or else the comment's spot, or None."""
    m = _BARE_RE.search(text)
    if not m:
        return None
    kind = m.group(1).lower()
    x, y = at if at is not None else _spot(comment_id, text, shape)[:2]
    if kind == "glider":
        direction = (m.group(2) or "se").lower()
        return Command(kind, _norm(life.place(life.PATTERNS[f"glider_{direction}"], x, y, shape)),
                       f"glider {x},{y} {direction}")
    return Command(kind, _norm(life.place(life.PATTERNS[kind], x, y, shape)), f"{kind} {x},{y}")


def random_patch(comment_id: str, text: str, shape=(life.H, life.W), at: tuple[int, int] | None = None) -> Command:
    """The comment's 5x5 random patch, at its spot or at `at` ("random x,y")."""
    x, y, d = _spot(comment_id, text, shape)
    if at is not None:
        x, y = at
    bits = int.from_bytes(d[4:8], "big")
    offsets = [(i % 5, i // 5) for i in range(25) if (bits >> i) & 1]
    return Command("random", _norm(life.place(offsets, x, y, shape)), f"random 5x5 at {x},{y}")


def parse(comment_id: str, text: str, shape=(life.H, life.W)) -> Command:
    """The command a comment injects. Never fails: falls back to a random patch."""
    delay, m = 0, _DELAY_RE.search(text)
    words = text
    if m:  # the time comes out first, so its number is never read as a coordinate
        n, unit = int(m.group(1)), m.group(2).lower()
        delay = min(MAX_DELAY_S, n * (3600 if unit.startswith("h") else 60))
        words = text[:m.start()] + " " + text[m.end():]
    valid = [(pos, cmd) for pos, cmd in _candidates(words, shape, comment_id, text) if cmd is not None]
    if valid:
        cmd = min(valid, key=lambda pc: pc[0])[1]
    else:  # no command: at the place it names, if any ("8, 56 red"), the pattern it names or its random patch
        at = place(words, shape)
        cmd = bare_pattern(comment_id, words, shape, at) or random_patch(comment_id, text, shape, at=at)
    named = cmd.kind in ERASERS or (cmd.kind == "random" and _RANDOM_WORD_RE.search(words))
    if named and _ERASE_RE.search(words):  # only a pattern named in words erases: never by accident
        return replace(cmd, erase=True, delay_s=delay)
    c = _COLOR_RE.search(text)
    return replace(cmd, color=c.group(1).lower() if c else None, delay_s=delay)
