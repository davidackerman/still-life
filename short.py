#!/usr/bin/env python3
"""Render a vertical Short (1080x1920, about 40 s; the daily recap about 20 s) from the live universe.

    python short.py                                   # out/short.mp4 from state/
    python short.py --preview out/short_frames        # also save a PNG every second

Everything on screen is real: the seed, every logged comment at the generation
it landed, the board as it stands now, and its future if nobody comments (the
same forecast the title uses). Re-render whenever there is news, e.g. when the
universe gets stuck and the 24-hour countdown starts.

The whole board sits the right way up in a band across the middle (as in the
main video's thumbnail), with text above and below, so a comment's x,y and
direction match what appears on screen. For each comment the view zooms in on
where it landed.

Upload it by hand in YouTube Studio and set its "Related video" to the main
video. Then add its ID to the EXTRA_VIDEO_IDS secret so its comments play too.
"""

from __future__ import annotations

import argparse
import datetime as dt
import copy
import json
import os
import subprocess

import numpy as np
from PIL import Image, ImageDraw

import commands
import explainer as ex
import life
import render
from render import BG, GRID, HIGHLIGHT, PHASE_COLOR
from store import read_log
from universe import DIED, Universe, move_input

PW, PH = 1080, 1920
CELL = 15  # board image: 128 x 72 cells -> 1920 x 1080
BW, BH = life.W * CELL, life.H * CELL
BAND_W, BAND_H = PW, round(PW * BH / BW)  # the whole board across the screen: 1080 x 608
BAND_Y = 600  # top of the board on screen
TOP_Y = 365  # centre of the text area above the board
BOTTOM_Y = 1330  # centre of the text area below it, clear of the Shorts title and buttons
CX = 520  # text centre, nudged left of the Shorts buttons
TEXT_W = 900
WRAP_DIM = 0.8  # the torus continues above and below the board: shown faintly (0 = off)
FPS = ex.FPS
FORECAST_GENS = 30000
TRAVEL_GENS = 24  # between the recap's moves, it plays at most this many generations (cuts past the rest)
# Shorts viewers read fast: 17 characters/s (the top of the subtitle guidelines
# explainer.py cites), less time to look, shorter fades.
CPS, LOOK_S, MIN_S, FADE = 17, 0.8, 1.8, 8


def rf(*lines: str) -> int:
    return int(round(max(MIN_S, sum(map(len, lines)) / CPS + LOOK_S) * FPS)) + 2 * FADE


def fade(i: int, n: int) -> float:
    return ex.fade(i, n, FADE)


def ease(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return p * p * (3 - 2 * p)


# ---------------------------------------------------------------- geometry
def cell_px(x: float, y: float) -> tuple[float, float]:
    """Grid cell centre in board-image pixels."""
    return (x + 0.5) * CELL, (y + 0.5) * CELL


def main_piece(cells) -> list[tuple[int, int]]:
    """A pattern can straddle the torus edge (glider 64,71 nw wraps to the top
    rows). Return its largest connected-across-the-edge part, to ring and zoom on."""
    x0, y0 = cells[0]
    parts: dict[tuple[int, int], list] = {}
    for x, y in cells:
        ux = x0 + (x - x0 + life.W // 2) % life.W - life.W // 2
        uy = y0 + (y - y0 + life.H // 2) % life.H - life.H // 2
        parts.setdefault((ux // life.W, uy // life.H), []).append((x, y))
    return max(parts.values(), key=len)


def centre(cells) -> tuple[float, float]:
    pts = np.array([cell_px(x, y) for x, y in cells])
    return tuple(pts.mean(axis=0))


HOOK_ZOOM = 1.0  # the opening's zoom (1 = the whole board); see build()


def busiest(weight, zoom: float) -> tuple[float, float]:
    """Centre (board-image px) of the on-screen window at `zoom` holding the most `weight`."""
    h, w = weight.shape
    wh, ww = max(1, round(h / zoom)), max(1, round(w / zoom))
    c = np.pad(weight.astype(np.int64).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    counts = c[wh:, ww:] - c[:-wh, ww:] - c[wh:, :-ww] + c[:-wh, :-ww]
    y, x = np.unravel_index(int(np.argmax(counts)), counts.shape)
    return (x + ww / 2) * CELL, (y + wh / 2) * CELL


def view(f) -> tuple[float, float, float, float]:
    """The part of the board image on screen: (x0, y0, w, h). A frame can set
    `band` (its board's top and height on screen; the whole screen in the daily
    Short's opening) and `free` (a window anywhere on the torus, wrapping)."""
    _, bh = getattr(f, "band", (BAND_Y, BAND_H))
    if bh == BAND_H:
        w, h = BW / f.zoom, BH / f.zoom
    else:
        s = f.zoom * BAND_W / BW  # screen px per board px
        w, h = PW / s, bh / s
    fx, fy = f.focus
    if getattr(f, "free", False):
        return fx - w / 2, fy - h / 2, w, h
    return min(max(fx - w / 2, 0), BW - w), min(max(fy - h / 2, 0), BH - h), w, h


SCRIM_MAX, SCRIM_SOLID, SCRIM_CLEAR = 0.6, 560, 860  # behind the text over a full-screen board: dark down to y, clear by y
_scrim_alpha = np.clip((SCRIM_CLEAR - np.arange(PH)) / (SCRIM_CLEAR - SCRIM_SOLID), 0, 1) * SCRIM_MAX


def scrim(amount: float) -> Image.Image:
    a = np.zeros((PH, PW, 4), np.uint8)
    a[..., 3] = (255 * amount * _scrim_alpha)[:, None].astype(np.uint8)
    return Image.fromarray(a, "RGBA")


# ---------------------------------------------------------------- drawing
_tile = None


def board(f) -> Image.Image:
    global _tile
    if _tile is None:
        inner = np.zeros((CELL, CELL), bool)
        inner[1:-1, 1:-1] = True
        _tile = np.tile(inner, (life.H, life.W))
    big = f.grid.repeat(CELL, 0).repeat(CELL, 1).astype(bool) & _tile
    color = np.array(PHASE_COLOR[f.phase], float)
    if f.grey:
        color = color * (1 - f.grey) + np.array(PHASE_COLOR[DIED], float) * f.grey
    img = np.empty(_tile.shape + (3,), np.uint8)
    img[:] = GRID
    img[_tile] = BG
    trail = getattr(f, "trail", None)
    if trail is not None and f.grey < 1:  # the tails (add_trails), greying with the rest when it dies
        a, col = trail
        on = a > 0.004
        if on.any():
            lut = np.array([color] + [np.array(render.PAINT[c], float) * (1 - f.grey) + np.array(PHASE_COLOR[DIED], float)
                                      * f.grey for c in commands.COLORS], np.float32)
            rgb = np.zeros(f.grid.shape + (3,), np.uint8)
            rgb[on] = (np.array(BG, np.float32) * (1 - a[on, None]) + lut[col[on]] * a[on, None]).astype(np.uint8)
            m = on.repeat(CELL, 0).repeat(CELL, 1) & _tile
            img[m] = rgb.repeat(CELL, 0).repeat(CELL, 1)[m]
    img[big] = color.astype(np.uint8)
    paint = getattr(f, "paint", None)
    if paint is not None and f.grey < 1 and paint.any():  # paint fades to grey with the rest when it dies
        for k, name in enumerate(commands.COLORS, 1):
            mine = (f.grid & (paint == k)).repeat(CELL, 0).repeat(CELL, 1).astype(bool) & _tile
            if mine.any():
                img[mine] = (np.array(render.PAINT[name], float) * (1 - f.grey)
                             + np.array(PHASE_COLOR[DIED], float) * f.grey).astype(np.uint8)
    if f.highlight is not None:
        mask = f.grid & f.highlight
        if paint is not None and f.grey < 1:
            mask = mask & (paint == 0)  # a color shows from the moment it lands
        lit = mask.repeat(CELL, 0).repeat(CELL, 1).astype(bool) & _tile
        img[lit] = HIGHLIGHT
    return Image.fromarray(img)


def wrap(s: str, weight: str, size: int, width: int = TEXT_W) -> list[str]:
    d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    out, line = [], ""
    for w in s.split():
        trial = f"{line} {w}".strip()
        if line and d.textlength(trial, font=ex.font(weight, size)) > width:
            out.append(line)
            line = w
        else:
            line = trial
    out = out + [line] if line else out
    if 2 <= len(out) <= 3:  # balance: "Every comment / adds cells.", not "Every comment adds / cells."
        from itertools import combinations
        words = s.split()

        def width(ws):
            return d.textlength(" ".join(ws), font=ex.font(weight, size))
        best = min(combinations(range(1, len(words)), len(out) - 1),
                   key=lambda cuts: max(width(words[a:b]) for a, b in zip((0,) + cuts, cuts + (len(words),))))
        out = [" ".join(words[a:b]) for a, b in zip((0,) + best, best + (len(words),))]
    return out


def block(lines, yc: int, alpha: float):
    """[(text, weight, size)] -> explainer-style text tuples, wrapped, centred on (cx, yc).
    Above the board there are no Shorts buttons, so text there uses the full width."""
    above = yc < BAND_Y
    cx, width = (PW // 2, PW - 90) if above else (CX, TEXT_W)
    rows = [(ln, weight, size) for s, weight, size in lines for ln in wrap(s, weight, size, width)]
    steps = [int(size * 1.25) for _, _, size in rows]
    y = yc - (sum(steps) - steps[-1]) / 2 if rows else yc
    out = []
    for (ln, weight, size), step in zip(rows, steps):
        out.append((ln, weight, size, (cx, round(y)), "mm", alpha))
        y += step
    return out


def ring(cells, alpha: float, caption: str | None = None, dashed: bool = False):
    """A ring around cells on the board, optionally joined to a caption below the board."""
    def draw(d, to_canvas):
        a = int(255 * alpha)
        pts = np.array([to_canvas(*cell_px(x, y)) for x, y in cells])
        cx, cy = pts.mean(axis=0)
        r = max(38.0, np.hypot(*(pts - (cx, cy)).T).max() + 24)
        cx = min(max(cx, r + 4), PW - r - 4)  # on screen; it may overhang the board's edge
        if dashed:
            for k in range(0, 360, 20):
                d.arc((cx - r, cy - r, cx + r, cy + r), k, k + 11, fill=(255, 255, 255, a), width=5)
        else:
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, a), width=6)
        if caption:  # one or two lines, in a white pill below the board
            lines = caption.split("\n")
            size = 50 if len(lines) == 1 else 44
            f = ex.font("demi", size)
            w = max(d.textlength(ln, font=f) for ln in lines)
            step = int(size * 1.3)
            top = BOTTOM_Y - 40
            bottom = top + 24 + step * len(lines)
            d.line((cx, cy + r, CX, top), fill=(255, 255, 255, a), width=4)
            d.rounded_rectangle((CX - w / 2 - 22, top, CX + w / 2 + 22, bottom), radius=16,
                                fill=(255, 255, 255, int(240 * alpha)))
            for k, ln in enumerate(lines):
                ex.text(d, (CX, top + 12 + step * k + step // 2), ln, "demi", size, alpha, fill=(10, 12, 16),
                        anchor="mm", outline=False)
    return draw


# Wherever the daily Short says "comment", a comment bubble below the board shows
# one you could write and, beside it, a little film (render.demo) of exactly what it
# adds. They take turns from the first frame until the ending, each held until its
# film has played through; the ending then lists them all, one under another.
EXAMPLES = [("hi!|wow|save it", "random", None, "any message adds a random patch"),  # any words: a few, in turn
            ("glider {at} se blue", "glider_se", "blue", "adds a blue glider that flies off"),
            ("block {at}", "block", None, "adds a block that just sits"),
            ("blinker {at} cyan", "blinker", "cyan", "adds a cyan blinker that blinks"),
            ("rpent {at} yellow", "rpent", "yellow", "adds a yellow rpent: 5 cells of chaos"),
            ("acorn {at} red", "acorn", "red", "adds a red acorn that grows for ages")]
CYCLE = [EXAMPLES[k] for k in (0, 1, 5, 4)]  # the bubble's turns: hi!, glider, acorn, rpent
CHIP_Y, CHIP_BOX, CHIP_CELL, CHIP_EVERY = 1516, 9, 10, 3  # the bubble's centre, below the captions; a generation every 3 frames
STACK_Y, STACK_SCALE, STACK_GAP = 536, 0.9, 14  # the ending's list: its top, its bubbles' size, the gap between them


SAY_TURN = int(1.1 * FPS)  # frames each of an example's messages shows ("hi!|wow|save it")


def _quote(say: str, at: str, i: int = 0) -> str:
    """The example in quotes; one of its "|"-separated messages, in turn by frame i."""
    opts = say.split("|")
    return "\u201c" + opts[(i // SAY_TURN) % len(opts)].format(at=at) + "\u201d"


def _widest(d, say: str, at: str, adds: str, s: float = 1.0) -> float:
    return max(_bubble_width(d, _quote(o, at), adds, s) for o in say.split("|"))


def _bubble_width(d, say: str, adds: str, s: float = 1.0) -> float:
    tw = max(d.textlength(say, font=ex.font("demi", round(46 * s))), d.textlength(adds, font=ex.font("demi", round(30 * s))))
    return 128 * s + tw + CHIP_BOX * round(CHIP_CELL * s)


def _bubble(d, x0: int, yc: float, w: float, say: str, adds: str, cells, paint, alpha: float, s: float = 1.0):
    """One comment bubble at scale s: a commenter, what they wrote, what it adds, and a frame of its film."""
    a = int(255 * alpha)
    cell = round(CHIP_CELL * s)
    box, h = CHIP_BOX * cell, 112 * s
    fill = tuple(render.PAINT[paint] if paint else PHASE_COLOR["ALIVE"]) + (a,)  # the color of the cells it adds
    d.rounded_rectangle((x0, yc - h / 2, x0 + w, yc + h / 2), radius=26 * s, fill=(245, 246, 250, int(238 * alpha)))
    d.ellipse((x0 + 26 * s, yc - 22 * s, x0 + 70 * s, yc + 22 * s), fill=fill)  # a commenter, in that color
    tx = x0 + 88 * s
    ex.text(d, (tx, yc - 18 * s), say, "demi", round(46 * s), alpha, fill=(12, 14, 18), anchor="lm", outline=False)
    ex.text(d, (tx, yc + 28 * s), adds, "demi", round(30 * s), alpha, fill=(90, 96, 110), anchor="lm", outline=False)
    bx, by = int(x0 + w - 14 * s - box), int(yc - box / 2)
    d.rounded_rectangle((bx - 4, by - 4, bx + box + 4, by + box + 4), radius=10, fill=(16, 18, 24, a))
    for y, x in np.argwhere(cells):
        px, py = bx + x * cell, by + y * cell
        d.rectangle((px + 1, py + 1, px + cell - 2, py + cell - 2), fill=fill)


def chip_turn() -> int:
    """Frames a bubble is held at the films' own pace: its film, played through once."""
    return len(render.demo(CYCLE[0][1], CHIP_BOX)) * CHIP_EVERY


def _spot(at, k: int) -> str:
    """The k-th example's spot: `at` is one "x,y" or a list of them (each example its own)."""
    return at if isinstance(at, str) else at[k % len(at)]


def comment_chip(i: int, alpha: float, at, per: float | None = None, cycle=None):
    """Frame i (counted from the first bubble) of the comment bubbles, `per` frames to a turn."""
    per = per or chip_turn()
    cycle = cycle or CYCLE

    def draw(d, to_canvas):
        if alpha <= 0:
            return
        turn = int(i // per)
        raw, pattern, paint, adds = cycle[turn % len(cycle)]
        at1 = _spot(at, turn)
        say = _quote(raw, at1, i)
        film = render.demo(pattern, CHIP_BOX)
        k = min(len(film) - 1, int((i - turn * per) * len(film) / per))
        w = _widest(d, raw, at1, adds)  # steady while its messages take turns
        _bubble(d, int(CX - w / 2), CHIP_Y, w, say, adds, film[k], paint, alpha)
    return draw


def comment_stack(i: int, n: int, alpha: float, at, first: int | None = None):
    """Frame i of the n-frame ending: every example, one under another, their
    films starting together and playing through once (EXAMPLES[first] at the top)."""
    examples = EXAMPLES if first is None else [EXAMPLES[first]] + [x for k, x in enumerate(EXAMPLES) if k != first]

    def draw(d, to_canvas):
        s = STACK_SCALE
        rows = [(_quote(say, _spot(at, r), i), pattern, paint, adds) for r, (say, pattern, paint, adds) in enumerate(examples)]
        w = max(_widest(d, say, _spot(at, r), adds, s) for r, (say, _, _, adds) in enumerate(examples))
        h = 112 * s
        for r, (say, pattern, paint, adds) in enumerate(rows):
            film = render.demo(pattern, CHIP_BOX)
            k = min(len(film) - 1, i * len(film) // n)
            _bubble(d, int(CX - w / 2), STACK_Y + r * (h + STACK_GAP) + h / 2, w, say, adds, film[k], paint, alpha, s)
    return draw


def arrow_down(x: int, y: int, alpha: float):
    def draw(d, to_canvas):
        a = int(255 * alpha)
        d.rectangle((x - 8, y - 60, x + 8, y), fill=(255, 255, 255, a))
        d.polygon([(x - 30, y - 6), (x + 30, y - 6), (x, y + 32)], fill=(255, 255, 255, a))
    return draw


def compose(f) -> bytes:
    if getattr(f, "look3d", False):  # the same frame, drawn as blocks (look3d.py)
        import look3d
        return look3d.compose(f)
    band_y, band_h = getattr(f, "band", (BAND_Y, BAND_H))
    x0, y0, w, h = view(f)
    resample = Image.NEAREST if f.zoom * BAND_W / BW >= 1 else Image.LANCZOS
    img = board(f)
    if WRAP_DIM < 1:
        # The whole screen, top to bottom, at the band's scale. Rows outside the
        # board wrap around (it is a torus), so this is what really lies beyond its edges.
        k = h / band_h  # board px per screen px
        top, bottom = y0 - band_y * k, y0 + (PH - band_y) * k
        r0, r1 = int(np.floor(top)), int(np.ceil(bottom))
        rows, bx = np.asarray(img)[np.arange(r0, r1) % BH], x0
        c0, c1 = int(np.floor(x0)), int(np.ceil(x0 + w))
        if c0 < 0 or c1 > BW:  # a free window past a side edge: it wraps there too
            rows, bx = rows[:, np.arange(c0, c1) % BW], x0 - c0
        canvas = Image.fromarray(rows).resize((PW, PH), resample, box=(bx, top - r0, bx + w, bottom - r0)).convert("RGBA")
        if band_h < PH:
            veil = Image.new("RGBA", (PW, PH), (0, 0, 0, int(255 * WRAP_DIM)))
            veil.paste((0, 0, 0, 0), (0, band_y, PW, band_y + band_h))
            canvas = Image.alpha_composite(canvas, veil)
    else:
        canvas = Image.new("RGBA", (PW, PH), (0, 0, 0, 255))
        canvas.paste(img.resize((PW, band_h), resample, box=(x0, y0, x0 + w, y0 + h)), (0, band_y))
    if getattr(f, "scrim", 0):
        canvas = Image.alpha_composite(canvas, scrim(f.scrim))
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    edge = (70, 76, 92, 255)
    if band_y > 0:
        d.line((0, band_y - 1, PW, band_y - 1), fill=edge, width=2)
    if band_y + band_h < PH:
        d.line((0, band_y + band_h, PW, band_y + band_h), fill=edge, width=2)
    if f.dim:
        d.rectangle((0, band_y, PW, band_y + band_h), fill=(5, 6, 9, int(255 * f.dim)))

    def to_canvas(bx, by):
        return (bx - x0) * PW / w, band_y + (by - y0) * band_h / h

    for s, weight, size, xy, anchor, alpha in f.texts:
        ex.text(d, xy, s, weight, size, alpha, anchor=anchor)
    for extra in f.extras:
        extra(d, to_canvas)
    return Image.alpha_composite(canvas, layer).convert("RGB").tobytes()


# ---------------------------------------------------------------- the real history
class History:
    def __init__(self, state_dir: str):
        log = read_log(os.path.join(state_dir, "log.jsonl"))
        self.genesis = g = log[0]
        assert g["type"] == "genesis", "log must start with genesis"
        self.batches: dict[int, list[dict]] = {}
        self.effects: dict[tuple, dict] = {}  # (gen, command) -> what it did to the lifespan
        for e in log:
            if e["type"] == "injection":
                self.batches.setdefault(e["gen"], []).append(e)
            elif e["type"] == "effect":
                self.effects.setdefault((e["gen"], e["command"]), e)
        ticks = [e for e in log if e["type"] == "tick"]
        self.now_gen = ticks[-1]["gen_end"] if ticks else 0
        self.now_hash = ticks[-1]["state_hash"] if ticks else None
        self.gen_seconds = g["gen_seconds"]
        self.applied: set[int] = set()

    def fresh(self) -> Universe:
        """The universe at its first generation. It carries `paint`: the
        colors its cells have been given (see life.paint_step), kept up by goto."""
        self.applied = set()
        shape = tuple(self.genesis["shape"])
        u = Universe(life.mask(life.centered(self.genesis["seed"], shape), shape), self.genesis["flatline_gens"])
        u.paint = np.zeros(shape, np.uint8)
        return u

    @staticmethod
    def advance(u: Universe, target: int) -> None:
        """u.advance_to(target), keeping its paint up (one generation at a time, while it has any)."""
        paint = getattr(u, "paint", None)
        if paint is None or not paint.any():
            u.advance_to(target)
            return
        while u.generation < target and u.phase != DIED:
            before = u.grid
            u.advance_to(u.generation + 1)
            u.paint = life.paint_step(before, u.grid, u.paint)

    def goto(self, u: Universe, target: int):
        """Step to `target`, applying every logged injection at its generation.
        Returns the cells injected on the way (for highlighting)."""
        cells = []
        while True:
            due = [g for g in self.batches if g not in self.applied and u.generation <= g <= target]
            if not due:
                break
            g = min(due)
            self.advance(u, g)
            batch = self.batches[g]
            u.inject([move_input(e) for e in batch])
            paint = getattr(u, "paint", None)
            for e in batch:
                if paint is not None and (e.get("erase") or e.get("color") in commands.COLORS):
                    k = 0 if e.get("erase") else commands.COLORS.index(e["color"]) + 1  # an eraser's cells: dead, unpainted
                    for x, y in e["cells"]:
                        paint[y % paint.shape[0], x % paint.shape[1]] = k
            self.applied.add(g)
            cells += [tuple(c) for e in batch if not e.get("erase") for c in e["cells"]]  # new life, to light up
        self.advance(u, target)
        return cells


def days(gens: int, gen_seconds: int) -> int:
    return int(gens * gen_seconds // 86400)


# ---------------------------------------------------------------- the story
def add_trails(frames: list, primer=(), continuous: bool = False) -> None:
    """Give each frame its tails (life.Trail, as on the stream). A step is a frame
    whose board changed, so a hold keeps its tails, and a cut (back in time, or a
    jump far ahead) starts them afresh. `primer` is (grid, paint, generation) for the real generations
    just before the first frame, so even the first frame (the thumbnail) shows
    where things are moving. `continuous`: the frames are one shot whatever their
    generations (a rewind), so the tails follow what's on screen, like motion blur."""
    trail, prev, t, gen0 = None, None, 0, None
    for grid, paint, gen, f in [*((g, p, n, None) for g, p, n in primer),
                                *((f.grid, getattr(f, "paint", None), f.gen, f) for f in frames)]:
        cut = gen0 is not None and not 0 <= gen - gen0 <= 30 and not (continuous and f is not None)
        if trail is None or grid.shape != trail.last.shape or cut:
            trail, prev = life.Trail(grid.shape), None
        gen0 = gen
        if prev is not None and not np.array_equal(grid, prev):
            t += 1
        trail.see(grid, t, paint)
        prev = grid
        if f is not None:
            a, col = trail.layer(grid)
            f.trail = (a, col.copy()) if a.any() else None


def build(state_dir: str, since_gen: int | None = None) -> list:
    """The whole life so far, or with `since_gen`, a recap of what happened
    since then (the daily Short): its moves, the biggest ones up close, and
    how its expected death moved."""
    h = History(state_dir)
    frames = []
    whole = (BW / 2, BH / 2)

    def add(u, texts=(), zoom=1.0, focus=whole, extras=(), **kw):
        band, free, dark = kw.pop("band", None), kw.pop("free", False), kw.pop("scrim", 0.0)
        f = ex.Frame(u, texts=texts, hud=False, zoom=zoom, dim=kw.pop("dim", 0.0), extras=extras, **kw)
        f.focus, f.free, f.scrim = focus, free, dark
        if band:
            f.band = band
        paint = getattr(u, "paint", None)
        f.paint = paint.copy() if paint is not None and paint.any() else None
        frames.append(f)

    # the daily recap's comment bubbles (see comment_chip): from the first frame
    # until the ending, which lists them all (comment_stack)
    chipped: list = []
    chip_at: list = []  # the example spot: a quiet corner of the board as it is now

    def add_chip(u, texts=(), extras=(), **kw):
        if not chip_at:
            chip_at.append([f"{x},{y}" for x, y in render.quiet_spots(now.grid, len(EXAMPLES))])
        k = len(chipped)
        add(u, texts, extras=list(extras) + [comment_chip(k, min(1.0, (k + 1) / FADE), chip_at[0])], **kw)
        chipped.append(frames[-1])

    def end_chips():  # every turn a whole film, as many turns as fill the bubbles' time
        n = len(chipped)
        per = n / max(1, round(n / chip_turn()))
        for k in range(n):
            chipped[k].extras[-1] = comment_chip(k, min(1.0, (k + 1) / FADE, (n - k - 1) / FADE), chip_at[0], per)

    # where things stand now, and the forecasts
    now = h.fresh()
    h.goto(now, h.now_gen)
    if h.now_hash and now.hash.hex() != h.now_hash:
        raise SystemExit("replaying the log did not reproduce the current board; run replay.py")
    opening = opening_move(h, since_gen) if since_gen is not None else None
    first = open_first(opening[0]) if opening else h.now_gen  # the first frame's generation
    primer, pu = [], h.fresh()  # the real generations just before it: tails on the first frame
    h.goto(pu, max(0, first - life.TRAIL_GENS))
    while True:
        primer.append((pu.grid.copy(), None if pu.paint is None else pu.paint.copy(), pu.generation))
        if pu.generation >= first:
            break
        h.goto(pu, pu.generation + 1)
    F = now.flatline_gens
    stuck_now = now.forecast_stuck(FORECAST_GENS)
    stuck_seed = h.fresh().forecast_stuck(FORECAST_GENS)
    moves = sum(len(b) for b in h.batches.values())

    # 1. hook over the live board (its real future, one generation per frame). With
    # HOOK_ZOOM > 1 it opens close up on the patch that changes most, then pulls back.
    # Measured: the teaser (whole board) kept 52.7% of viewers; Day 2 (4x close up)
    # kept 44.5%. So it opens on the whole board again.
    if opening:  # the daily Short opens on what one real comment did (see _open_on)
        _open_on(h, opening, add)
    n = max(int((2.0 if opening else 2.7) * FPS), rf("THIS SHORT", "DIES", "if nobody comments"))  # without an opening,
    # on from the very first frame: the thumbnail
    Z, hold = HOOK_ZOOM, int(1.2 * FPS)
    busy = whole
    if Z > 1:
        u, prev, churn = copy.deepcopy(now), now.grid.copy(), np.zeros(now.grid.shape, np.int64)
        for _ in range(n):
            History.advance(u, u.generation + 1)
            churn += u.grid != prev
            prev = u.grid.copy()
        busy = busiest(churn, Z)
    u = copy.deepcopy(now)
    for i in range(n):
        if i % 3 == 0:  # ten generations a second: slow enough to see things move
            History.advance(u, u.generation + 1)
        a = (1.0 if i < n - FADE else fade(i, n)) * (min(1.0, (i + 1) / FADE) if opening else 1.0)
        zoom = Z if i < hold else 1 + (Z - 1) * (1 - ease((i - hold) / (n - 1 - hold)))
        (add_chip if since_gen is not None else add)(
            u, block([("THIS SHORT", "bold", 112), ("DIES", "bold", 196)], TOP_Y, a)
            + block([("if nobody comments", "demi", 80)], BOTTOM_Y, a), zoom=zoom, focus=busy)

    if since_gen is not None:  # the daily recap: day N, the day's moves, how its death moved
        _recap(h, now, since_gen, add, stuck_now, add_chip, end_chips, skip=opening[0] if opening else None)
    else:
        # 2. the acorn, up close
        u = h.fresh()
        acorn = centre(life.centered(h.genesis["seed"]))
        n = int(2.5 * FPS)
        for i in range(n):
            if i > n // 2 and i % 6 == 0:
                h.goto(u, u.generation + 1)
            add(u, block([("It started as 7 cells.", "bold", 88)], TOP_Y, fade(i, n)), zoom=8.0, focus=acorn)

        # 3. pull back as it grows, stopping short of the first comment we show
        shown = sorted(h.batches)[-4:]  # name the latest few; earlier ones still land and light up
        stop = (shown[0] - 15) if shown else h.now_gen
        lines = [("Conway's Game of Life.", "bold", 84), ("1 generation every 2 minutes.", "demi", 62)]
        n = rf(*[s for s, _, _ in lines])
        for i in range(n):
            h.goto(u, max(u.generation, min(u.generation + 1 + i // 30, stop)))
            add(u, block(lines, TOP_Y, fade(i, n)), zoom=1 + 7.0 * (1 - ease(i / 60)), focus=acorn)

        # 4. the real history; the view zooms in on each comment where it landed
        head = [("Every comment adds cells.", "bold", 84)]
        lit = None
        seg = max(24, int(4.5 * FPS) // (len(shown) + 1))
        for target in shown + [h.now_gen]:
            start = u.generation
            for i in range(seg):
                cells = h.goto(u, max(u.generation, start + round((target - start) * ((i + 1) / seg) ** 1.4)))
                if cells:
                    lit = life.mask(cells)
                add(u, block(head, TOP_Y, 1.0), highlight=lit)
            if target not in shown:
                continue
            batch = h.batches[target]
            cells = [tuple(c) for e in batch for c in e["cells"]]
            piece = main_piece([tuple(c) for c in batch[-1]["cells"]])
            lit = life.mask(cells)
            caption = f"a comment: {render.move_text(batch[-1])}" + (f" (+{len(batch) - 1} more)" if len(batch) > 1 else "")
            hold, z = int(2.4 * FPS), 2.6
            for i in range(hold):
                if i > 10 and i % 4 == 0:  # let it be seen where it landed, then let it move
                    h.goto(u, u.generation + 1)
                zoom = 1 + (z - 1) * min(ease(i / 14), ease((hold - i) / 14))
                alpha = min(1.0, (i + 1) / 6, (hold - i) / 6)
                cue = ("inject", float(np.mean([c[0] for c in cells]))) if i == 0 else None
                add(u, block(head, TOP_Y, 1.0), highlight=lit, cue=cue, zoom=zoom, focus=centre(piece),
                    extras=[ring(piece, alpha, caption)])

        # 5. what the comments did
        if stuck_seed is not None and moves:
            before = days(stuck_seed + F, h.gen_seconds)
            after = (f"{days(stuck_now + F, h.gen_seconds)}" if stuck_now is not None
                     else f"{days(FORECAST_GENS + F, h.gen_seconds)}+")
            what = "stretched" if stuck_now is None or stuck_now > stuck_seed else "changed"
            noun = "comment" if moves == 1 else "comments"
            text = f"{moves} {noun} so far {what} its life from {before} to {after} days."
            n = rf(text)
            for i in range(n):
                if i % 4 == 0:
                    h.goto(u, u.generation + 1)
                add(u, block([(text, "bold", 76)], TOP_Y, fade(i, n)))

    # 6. the future if nobody comments: stuck, countdown, death (the daily recap says
    # this in one beat of its own)
    fut = copy.deepcopy(now)
    if stuck_now is not None and since_gen is None:
        text = "Left alone, it gets stuck in a loop."
        n = rf(text)
        start = fut.generation
        for i in range(n):
            p = min(1.0, (i + 1) / (n * 0.55))
            if fut.generation < stuck_now:  # ease out into the loop, then one generation per 2 frames
                History.advance(fut, start + round((stuck_now - start) * (1 - (1 - p) ** 2)))
            elif i % 2 == 0:
                History.advance(fut, fut.generation + 1)
            add(fut, block([(text, "bold", 80)], TOP_Y, fade(i, n)))
        top = [("Then it has 24 hours.", "bold", 88)]
        bottom = [("If nobody saves it, this Short goes private. For good.", "demi", 62)]
        n = rf(*[s for s, _, _ in top + bottom])
        for i in range(n):
            if i % 2 == 0:
                History.advance(fut, fut.generation + 1)
            add(fut, block(top, TOP_Y, fade(i, n)) + block(bottom, BOTTOM_Y, fade(i, n)),
                grey=max(0.0, (i - n * 0.55) / (n * 0.3)))

    # 7. the call to action over the present board. The daily recap keeps it plain
    # (no coordinates) and short, so it loops sooner; the full Short marks a spot to aim at.
    u = copy.deepcopy(now)
    dim = 0.0
    if since_gen is not None:  # and what you can comment, each move shown doing its thing
        top = [("Comment anything.", "bold", 96), ("Like one of these:", "demi", 60)]
        bottom = [("Your cells could save it. Or kill it.", "demi", 62)]
        n, dim = max(rf(*[s for s, _, _ in top + bottom]), 6 * FPS), 0.82
        x, y = render.quiet_spot(u.grid)
        spots = [f"{x},{y}" for x, y in render.quiet_spots(u.grid, len(EXAMPLES))]
        extras = lambda a, i: [comment_stack(i, n, a, spots)]  # noqa: E731
        bottom_y = BOTTOM_Y + 24  # below the list
    else:
        pick = render.suggestion(u)
        spot = main_piece([tuple(c) for c in commands.parse("suggestion", pick).cells])
        top = [("Comment anything to add cells.", "bold", 84)]
        bottom = [(f"or aim one: {pick}", "demi", 62), ("Live countdown: the main video on this channel", "demi", 50)]
        n = max(rf(*[s for s, _, _ in top + bottom]), 5 * FPS)
        extras = lambda a, i: [ring(spot, a * 0.8, dashed=True), arrow_down(CX, 1470, a)]  # noqa: E731
        bottom_y = BOTTOM_Y - 20
    for i in range(n):
        if i % 6 == 0:
            History.advance(u, u.generation + 1)
        a = min(1.0, (i + 1) / FADE)
        add(u, block(top, TOP_Y, a) + block(bottom, bottom_y, a), extras=extras(a, i), dim=dim * a)
    add_trails(frames, primer)
    return frames


def _span(seconds: float) -> str:
    """'6 days', '1 day', '9 hours'."""
    if seconds >= 86400:
        n = round(seconds / 86400)
        return f"{n} day{'s' * (n != 1)}"
    n = max(1, round(seconds / 3600))
    return f"{n} hour{'s' * (n != 1)}"


def effect_words(before: int | None, after: int | None, gen_seconds: int) -> str:
    """What a move did to the universe's life, in plain words (None: no loop
    within the forecast, i.e. weeks to live)."""
    if before is None and after is None:
        return "it still has weeks to live"
    if after is None:
        return "gave it weeks more to live"
    if before is None:
        return "took weeks off its life"
    d = (after - before) * gen_seconds
    if abs(d) < 1800:
        return "barely changed its fate"
    return f"gave it {_span(d).replace(' ', ' more ', 1)} to live" if d > 0 else f"took {_span(-d)} off its life"


def _impact(h: History, gen: int) -> float:
    """How much the comments at `gen` moved its death (generations; big if unknown)."""
    best = 0.0
    for e in h.batches[gen]:
        f = h.effects.get((gen, e["command"]))
        if f:
            b, a = f["before"], f["after"]
            best = max(best, abs((a if a is not None else b + 30000) - (b if b is not None else a + 30000)))
    return best


# The daily Short's opening: its length, when the move lands, frames per generation
# (from the first frame: it moves at once), when its second lines appear, the pull
# back to the board's band at its end, a slow push in, and where the comment sits on screen.
# Measured: Day 5 (board in a band, still for its first 0.7 s, four lines of text at
# once) had 57.3% swipe away. So it opens on a full screen of board, already moving,
# with one sentence.
OPEN_S, OPEN_LAND_S, OPEN_STEP, OPEN_MORE_S, OPEN_OUT_S = 3.4, 0.0, 5, 1.0, 0.6  # it has landed on the first
# frame (the thumbnail): never an empty ring
OPEN_PUSH, OPEN_PIECE_Y = 1.12, 1000


def open_first(g: int) -> int:
    """The opening's first generation, for a comment landing at `g`: a few before, so it moves from frame one."""
    return max(0, g - 1 - int(OPEN_LAND_S * FPS) // OPEN_STEP)


OPENING_LEAD = "alternate"  # the opening's comment: "save", "kill", or "alternate" (kills on even days, saves on odd)


def opening_move(h: History, since_gen: int, lead: str | None = None):
    """The day's comment for the opening, as (generation, the logged move, its
    effect): the one that took the most life from it ("kill") or gave it the
    most ("save"), by `lead` (OPENING_LEAD; "alternate" by the day's parity, an
    experiment: which keeps more viewers). Falls back to the other kind when the
    day has none; None when nothing moved it much."""
    lead = lead or OPENING_LEAD
    if lead == "alternate":
        lead = "kill" if (int(h.now_gen * h.gen_seconds // 86400) + 1) % 2 == 0 else "save"
    saves, kills = [], []
    for g in sorted(x for x in h.batches if since_gen < x <= h.now_gen):
        for e in h.batches[g]:
            f = h.effects.get((g, e["command"]))
            if not f or not e.get("cells") or (f["before"] is None and f["after"] is None):
                continue
            b, a = f["before"], f["after"]
            d = (a if a is not None else b + FORECAST_GENS) - (b if b is not None else a + FORECAST_GENS)
            if effect_words(b, a, h.gen_seconds) != "barely changed its fate":
                # the biggest first; between near-equals, one with a pattern's name reads better than a random patch
                key = (round(abs(d) / 30), not e["command"].startswith("random"))
                (saves if d > 0 else kills).append((key, g, e, f))
    order = (kills, saves) if lead == "kill" else (saves, kills)
    want = OPEN_PICK.get(int(h.now_gen * h.gen_seconds // 86400) + 1)
    for t in (t for pick in order for t in pick if want and t[2]["command"] == want):
        return t[1:]
    clear = _lands_clear(h, [t[1:3] for pick in order for t in pick], since_gen)
    for pick in order:  # the biggest that looks like what it says; then the other kind's; then the biggest anyway
        seen = [t for t in pick if clear.get((t[1], t[2]["command"]))]
        if seen:
            return max(seen, key=lambda t: t[0])[1:]
    for pick in order:
        if pick:
            return max(pick, key=lambda t: t[0])[1:]
    return None


# The opening names its comment ("glider 18,55 se blue"), so that comment has to show as
# what it says: landing in the clear (no live cell within OPEN_CLEAR cells of it), and a
# glider flying OPEN_GLIDER_GENS before it hits anything. Day 6's biggest kill was a glider
# dropped onto a blinker: a blue blob, never a glider.
OPEN_CLEAR, OPEN_GLIDER_GENS = 2, 8
OPEN_PICK: dict = {6: "glider 20,23 se"}  # {day: a logged command}: pins that day's opening comment, if it moved the forecast


def _lands_clear(h: History, moves, since_gen: int) -> dict:
    """{(gen, command): True if that move lands in the clear (and, a glider, flies on)}."""
    flown = {(g, e["command"]): n for n, g, e, _ in flights(h, since_gen)}
    out, u = {}, h.fresh()
    for g, e in sorted(moves, key=lambda m: m[0]):
        h.goto(u, g - 1)  # the board just before it lands
        near = np.zeros(u.grid.shape, bool)
        for x, y in e["cells"]:
            ys = np.arange(y - OPEN_CLEAR, y + OPEN_CLEAR + 1) % u.grid.shape[0]
            xs = np.arange(x - OPEN_CLEAR, x + OPEN_CLEAR + 1) % u.grid.shape[1]
            near[np.ix_(ys, xs)] = True
        flies = not e["command"].startswith("glider") or flown.get((g, e["command"]), 0) >= OPEN_GLIDER_GENS
        out[(g, e["command"])] = flies and not (u.grid[near] > 0).any()
    return out


def _open_on(h: History, move, add) -> None:
    """The daily Short's first beat: what one real comment did. "One comment gave this
    Short 6 more days to live", over a full screen of board already moving, with that
    comment's words in a bubble where it lands; then it pulls back into the band."""
    import render as rd
    g, e, f = move
    G, launch = h.gen_seconds, h.genesis["launch_time"]
    u = h.fresh()
    h.goto(u, open_first(g))
    cells = [tuple(c) for c in e["cells"]]
    piece = main_piece(cells)
    day = rd._at(launch, g, G, "%Y-%m-%d")
    days_ago = (dt.date.fromisoformat(rd._at(launch, h.now_gen, G, "%Y-%m-%d")) - dt.date.fromisoformat(day)).days
    said = {0: "today", 1: "yesterday"}.get(days_ago, rd._at(launch, g, G, "%A"))
    words = effect_words(f["before"], f["after"], G)  # "gave it 9 more days to live", "took 9 days off its life"
    if words.startswith("gave it "):  # naming "it": the Short; one sentence, split where it reads
        lines = ["One comment", "gave this Short", words.removeprefix("gave it ") + "."]
    else:
        lines = ["One comment", "took " + words.removeprefix("took ").removesuffix(" off its life") + " off",
                 "this Short's life."]
    main = [(s, "bold", 92) for s in lines]
    more = [(f"{said} at {rd._at(launch, g, G, '%I:%M %p').lstrip('0')}", "demi", 46)]
    if words.startswith("took"):  # a kill: turn it toward the viewer
        more.append(("Yours could give them back.", "demi", 56))
    rows = block(main + more, TOP_Y, 1.0)  # placed together, so nothing moves when the rest appears
    k = len(block(main, TOP_Y, 1.0))
    if e["command"].startswith("random"):  # what they typed isn't kept: say what it became
        caption = "someone's message\nbecame a random 5\u00d75 patch" + (f", {e['color']}" if e.get("color") else "")
    else:
        caption = "\u201c" + render.move_text(e) + "\u201d"
    n = max(int(OPEN_S * FPS), rf(*lines, *[t for t, _, _ in more]))  # every word of it long enough to read
    land, lit = int(OPEN_LAND_S * FPS), None
    out, later = int(OPEN_OUT_S * FPS), int(OPEN_MORE_S * FPS)
    px, py = centre(piece)
    s0 = PH / BH  # screen px per board px with the board's whole height filling the screen
    start = (px, py + (PH / 2 - OPEN_PIECE_Y) / s0)  # the comment at OPEN_PIECE_Y, below the words
    heading = np.array(GLIDER_DIRS.get(e["command"].split()[-1], (1, 1))) if e["command"].startswith("glider") else None
    far = 0  # a glider's ring goes with it, as far as it flies
    for i in range(n):
        cue = None
        if i == land:  # it lands
            h.goto(u, g)
            lit = life.mask(cells, u.grid.shape)
            cue = ("inject", float(np.mean([c[0] for c in cells])))
            if heading is not None:
                far = _flew(h, u, e)
        elif i % OPEN_STEP == 0 and i and (i > land or u.generation < g - 1):  # moving, slowly enough to follow
            h.goto(u, u.generation + 1)
        # the pull back: the band shrinks to its place while the view widens to the whole
        # board, always showing all of its rows (so nothing repeats on the torus)
        q = ease((i - (n - out)) / (out - 1)) if i >= n - out else 0.0
        band_h, band_y = round(PH + (BAND_H - PH) * q), round(BAND_Y * q)
        push = 1 + (OPEN_PUSH - 1) * ease(i / (n - out)) * (1 - q)
        zoom = band_h / BH * push * BW / BAND_W
        focus = (start[0] + (BW / 2 - start[0]) * q, start[1] + (BH / 2 - start[1]) * q)
        a = 1.0 if i < n - FADE else fade(i, n)  # the first frame is the thumbnail: the sentence there
        b = a * ease((i - later) / 10)  # the rest, a beat later
        texts = [r[:5] + (a,) for r in rows[:k]] + [r[:5] + (b,) for r in rows[k:]]
        at = piece if heading is None else [(x + heading[0] * min(max(0, u.generation - g), far) / 4,
                                             y + heading[1] * min(max(0, u.generation - g), far) / 4) for x, y in piece]
        gone = 0.0 if heading is None or i <= land else ease((u.generation - g - far) / 3)  # its glider has hit something
        add(u, texts, highlight=lit, cue=cue, zoom=zoom, focus=focus, extras=[ring(at, a * (1 - gone), caption)],
            band=(band_y, band_h), free=True, scrim=1 - q)


def _moves(h: History, u: Universe, shown: list[int], head, add, until: int, effect_text):
    """Replay up to `until`, zooming in on each generation in `shown`."""
    lit = None

    def caption_for(target):  # plain words, not "glider 67,69 ne": most viewers of a daily Short are new
        batch = h.batches[target]
        return (f"{len(batch)} comments landed here" if len(batch) > 1 else "a comment landed here") + effect_text(target, batch)
    captions = {t: caption_for(t) for t in shown}
    holds = {t: max(int(2.6 * FPS), rf(c.replace("\n", " "))) for t, c in captions.items()}
    # long enough to read it all: the words at the top, and each caption while it's held
    need = rf(*[t for t, _, _ in head], *[c.replace("\n", " ") for c in captions.values()])
    seg = max(18, int(2.4 * FPS) // (len(shown) + 1), -(-(need - sum(holds.values())) // (len(shown) + 1)))
    for target in shown + [until]:
        if target - u.generation > TRAVEL_GENS:  # a long way: cut to just before, then play it at a followable pace
            cells = h.goto(u, target - TRAVEL_GENS)
            if cells:
                lit = life.mask(cells, u.grid.shape)
        start = u.generation
        for i in range(seg):
            cells = h.goto(u, max(u.generation, start + round((target - start) * ((i + 1) / seg))))
            if cells:
                lit = life.mask(cells, u.grid.shape)
            add(u, block(head, TOP_Y, 1.0), highlight=lit)
        if target not in shown:
            continue
        batch = h.batches[target]
        cells = [tuple(c) for e in batch for c in e["cells"]]
        piece = main_piece([tuple(c) for c in batch[-1]["cells"]])
        lit = life.mask(cells, u.grid.shape)
        caption, hold, z = captions[target], holds[target], 3.2
        for i in range(hold):
            if i > 12 and i % 6 == 0:  # let it be seen where it landed, then let it move, slowly
                h.goto(u, u.generation + 1)
            zoom = 1 + (z - 1) * min(ease(i / 24), ease((hold - i) / 24))
            alpha = min(1.0, (i + 1) / 6, (hold - i) / 6)
            cue = ("inject", float(np.mean([c[0] for c in cells]))) if i == 0 else None
            add(u, block(head, TOP_Y, 1.0), highlight=lit, cue=cue, zoom=zoom, focus=centre(piece),
                extras=[ring(piece, alpha, caption)])
    return u


def _recap(h: History, now: Universe, since_gen: int, add, stuck_now, add_chip=None, end_chips=None,
           skip: int | None = None) -> list:
    """The daily Short after its hook. Most of its viewers have never seen this
    universe, so it says what it is as it goes: what a comment does (the day's
    two biggest moves, in plain words), then what "dies" means, shown on its
    real future."""
    import render as rd
    G, F = h.gen_seconds, now.flatline_gens
    launch = h.genesis["launch_time"]
    day = int(h.now_gen * G // 86400) + 1
    gens = sorted(g for g in h.batches if since_gen < g <= h.now_gen)
    count = sum(len(h.batches[g]) for g in gens)

    # the board as it was a day ago
    u = h.fresh()
    h.goto(u, since_gen)

    head = [("Every comment adds cells to this Game of Life.", "bold", 76),
            (f"Day {day}: {count} comment{'s' * (count != 1)} in the last day" if count
             else f"Day {day}: no comments in the last day", "demi", 54)]

    def effect_text(gen, batch):  # "\ngave it 6 more days to live"
        effs = [h.effects[(gen, e["command"])] for e in batch if (gen, e["command"]) in h.effects]
        return "\n" + effect_words(effs[0]["before"], effs[-1]["after"], G) if effs else ""
    # the day's biggest moves (one, when the opening already showed the biggest)
    shown = sorted(sorted((g for g in gens if g != skip), key=lambda g: -_impact(h, g))[:1 if skip is not None else 2])
    say = add_chip or add  # the comment bubbles carry on under the moves and the stakes
    _moves(h, u, shown, head, say, h.now_gen, effect_text)

    _stakes(h, now, stuck_now, u, say)
    if end_chips:
        end_chips()


def _stakes(h: History, now: Universe, stuck_now, u: Universe, say) -> None:
    """Where its death stands now. Alive: its real future, fast, into the loop and
    grey ("and dies Sat Nov 21"). Stuck, or a loop broken: the deadline, over `u`
    playing on."""
    import render as rd
    G, F = h.gen_seconds, now.flatline_gens
    launch = h.genesis["launch_time"]

    def death_day(stuck):
        return rd._at(launch, stuck + F, G, "%a %b %d")
    if now.phase == "ALIVE" and stuck_now is not None:  # its real future: into the loop, then grey
        ahead = _span((stuck_now - now.generation) * G)
        top = [("Left alone, it gets stuck in a loop", "bold", 76), (f"fast-forward: the next {ahead}", "demi", 46)]
        bottom = [(f"and dies {death_day(stuck_now)}.", "bold", 80), ("Then this Short goes private.", "demi", 58)]
        fut = copy.deepcopy(now)
        start, n = fut.generation, rf(*[t for t, _, _ in top + bottom])
        for i in range(n):
            p = min(1.0, (i + 1) / (n * 0.55))
            if fut.generation < stuck_now:
                History.advance(fut, start + round((stuck_now - start) * (1 - (1 - p) ** 2)))
            elif i % 2 == 0:
                History.advance(fut, fut.generation + 1)
            say(fut, block(top, TOP_Y, fade(i, n)) + block(bottom, BOTTOM_Y, fade(i, n)),
                grey=max(0.0, (i - n * 0.6) / (n * 0.3)))
    else:
        if now.phase == "FLATLINING":
            top = [("It's stuck in a loop.", "bold", 84)]
            bottom = [(f"It dies {rd._at(launch, now.deadline_gen, G, '%a %I:%M %p ET')} unless a comment breaks it.",
                       "demi", 60)]
        elif now.phase == "CONTESTED":
            top = [("A comment broke the loop.", "bold", 84)]
            bottom = [(f"It lives if it's still changing {rd._at(launch, now.deadline_gen, G, '%a %I:%M %p ET')}.",
                       "demi", 60)]
        else:  # alive, and no loop within the forecast
            top = [("It won't get stuck in a loop for weeks.", "bold", 80)]
            bottom = [("Unless a comment changes that.", "demi", 60)]
        n = rf(*[t for t, _, _ in top + bottom])
        for i in range(n):
            if i % 4 == 0:
                h.goto(u, u.generation + 1)
            say(u, block(top, TOP_Y, fade(i, n)) + block(bottom, BOTTOM_Y, fade(i, n)))


# ---------------------------------------------------------------- the stranger and the countdown
# Two daily Shorts made for people who have never seen this universe (almost all of a
# Short's viewers: 657 new to 3 returning on the teaser). Short, one idea each, and
# ending on "comment, and watch it land live" (the Related video link is the stream).
# The stranger (noon): what one real comment did. The countdown (8 PM): how long it has.


def stranger_title(move, gen_seconds: int) -> str:
    """'One comment gave this universe 9 more days to live' (or took them): the stranger Short's headline."""
    _, _, f = move
    words = effect_words(f["before"], f["after"], gen_seconds)
    if words.startswith("gave it "):
        return "One comment gave this universe " + words.removeprefix("gave it ")
    return "One comment took " + words.removeprefix("took ").removesuffix(" off its life") + " off this universe's life"


def _frames(add_to: list):
    whole = (BW / 2, BH / 2)

    def add(u, texts=(), zoom=1.0, focus=whole, extras=(), **kw):
        band, free, dark = kw.pop("band", None), kw.pop("free", False), kw.pop("scrim", 0.0)
        f = ex.Frame(u, texts=texts, hud=False, zoom=zoom, dim=kw.pop("dim", 0.0), extras=extras, **kw)
        f.focus, f.free, f.scrim = focus, free, dark
        if band:
            f.band = band
        paint = getattr(u, "paint", None)
        f.paint = paint.copy() if paint is not None and paint.any() else None
        add_to.append(f)
    return add


STRANGER_WHAT = [("This is Conway's Game of Life.", "bold", 80), ("Every comment adds cells to it.", "demi", 64)]
WATCH_LIVE = ("Live now: the link below", "demi", 50)  # the Short's Related video link: the stream


def build_stranger(state_dir: str, since_gen: int) -> list | None:
    """The noon Short, about 15 s: the period's biggest real save or kill ("One comment
    gave this Short 9 more days to live", over a full screen of board, the comment where
    it landed), what this is, and "Your turn". None if nothing moved its fate much."""
    h = History(state_dir)
    move = opening_move(h, since_gen)
    if move is None:
        return None
    frames: list = []
    add = _frames(frames)
    _open_on(h, move, add)
    u = h.fresh()
    h.goto(u, frames[-1].gen)
    n = rf(*[t for t, _, _ in STRANGER_WHAT])
    for i in range(n):
        if i % 4 == 0:
            h.goto(u, u.generation + 1)
        add(u, block(STRANGER_WHAT, TOP_Y, fade(i, n)))
    top = [("Your turn.", "bold", 104), ("Comment anything.", "bold", 84)]
    bottom = [("Then watch it land live.", "demi", 64), WATCH_LIVE]
    n = rf(*[t for t, _, _ in top + bottom])
    for i in range(n):
        if i % 4 == 0:
            h.goto(u, u.generation + 1)
        a = min(1.0, (i + 1) / FADE)  # no fade out: it loops
        add(u, block(top, TOP_Y, a) + block(bottom, BOTTOM_Y, a))
    primer, pu = [], h.fresh()
    h.goto(pu, max(0, frames[0].gen - life.TRAIL_GENS))
    while pu.generation < frames[0].gen:
        primer.append((pu.grid.copy(), None if pu.paint is None else pu.paint.copy(), pu.generation))
        h.goto(pu, pu.generation + 1)
    add_trails(frames, primer)
    return frames


def time_left(seconds: float) -> str:
    """'4 days', '30 hours', '45 minutes': rounded down, so a countdown never claims more time than it has."""
    if seconds >= 2 * 86400:
        return f"{int(seconds // 86400)} days"
    if seconds >= 2 * 3600:
        return f"{int(seconds // 3600)} hours"
    n = max(1, int(seconds // 60))
    return f"{n} minute{'s' * (n != 1)}"


def countdown(h: History, now: Universe | None = None, stuck_now=None) -> tuple[list, str] | None:
    """The countdown Short's hook lines and headline, from where its death stands now:
    'This universe has 4 days left' (alive), 'dies in 7 hours' (stuck), 'could die in
    7 hours' (a comment broke its loop). None when no loop is in sight within the
    forecast: no urgency to claim."""
    if now is None:
        now = h.fresh()
        h.goto(now, h.now_gen)
        stuck_now = now.forecast_stuck(FORECAST_GENS) if now.phase == "ALIVE" else None
    G = h.gen_seconds
    if now.phase == "ALIVE":
        if stuck_now is None:
            return None
        left = time_left((stuck_now + now.flatline_gens - now.generation) * G)
        return [("This universe has", "bold", 96), (f"{left} left.", "bold", 150)], f"This universe has {left} left"
    if now.phase in ("FLATLINING", "CONTESTED") and now.deadline_gen is not None:
        left = time_left(max(1, now.deadline_gen - now.generation) * G)
        verb = "dies" if now.phase == "FLATLINING" else "could die"
        return [(f"This universe {verb}", "bold", 96), (f"in {left}.", "bold", 150)], f"This universe {verb} in {left}"
    return None


COUNTDOWN_LOOK = "3d"  # "flat", or "3d": drawn as lit blocks (look3d.py), as the made Short
COUNTDOWN_3D_ZOOM = 1.5  # in 3D: most of the board, wrapping on beyond it to fill the screen
COUNTDOWN_3D_STEP = 6  # in 3D, frames per generation while it plays (flat: 3): time for each block to rise and bounce
COUNTDOWN_3D_DRIFT = 1.2  # in 3D, the camera drifts across the board this many cells a second


def players(state_dir: str) -> int:
    """How many different people have moved this universe (the bot's scores: one per person)."""
    try:
        with open(os.path.join(state_dir, "state.json")) as f:
            return len(json.load(f).get("scores") or {})
    except (OSError, ValueError):
        return 0


def build_countdown(state_dir: str, look: str | None = None) -> list | None:
    """The 8 PM Short, about 20 s: how long it has, over the whole board moving
    (its band, as the teaser that kept the most viewers); its real future, into the
    loop and grey; then "Your comment changes that", and the live chat."""
    h = History(state_dir)
    now = h.fresh()
    h.goto(now, h.now_gen)
    stuck_now = now.forecast_stuck(FORECAST_GENS) if now.phase == "ALIVE" else None
    hook = countdown(h, now, stuck_now)
    if hook is None:
        return None
    lines, _ = hook
    frames: list = []
    add = _frames(frames)
    sub_line = [("if nobody comments", "demi", 80)] if now.phase == "ALIVE" else [("unless a comment saves it", "demi", 72)]
    n = max(int(2.7 * FPS), rf(*[t for t, _, _ in lines + sub_line]))
    u = copy.deepcopy(now)
    step = COUNTDOWN_3D_STEP if (look or COUNTDOWN_LOOK) == "3d" else 3
    for i in range(n):
        if i % step == 0 and i:  # moving from the first frame (the thumbnail), slowly enough to see
            History.advance(u, u.generation + 1)
        a = 1.0 if i < n - FADE else fade(i, n)
        add(u, block(lines, TOP_Y, a) + block(sub_line, BOTTOM_Y, a))
    cut1 = len(frames)
    _stakes(h, now, stuck_now, copy.deepcopy(now), add)
    cut2 = len(frames)
    top = [("Your comment", "bold", 104), ("changes that.", "bold", 104)]
    people = players(state_dir)
    bottom = [("Hundreds of people already have." if people >= 200 else "Comment, or join the live chat.", "demi", 64),
              WATCH_LIVE]
    n = rf(*[t for t, _, _ in top + bottom])
    u = copy.deepcopy(now)
    for i in range(n):
        if i and i % step == 0:
            History.advance(u, u.generation + 1)
        a = min(1.0, (i + 1) / FADE)  # no fade out: it loops back to the hook's board
        add(u, block(top, TOP_Y, a) + block(bottom, BOTTOM_Y, a))
    primer, pu = [], h.fresh()
    h.goto(pu, max(0, now.generation - life.TRAIL_GENS))
    while pu.generation < now.generation:
        primer.append((pu.grid.copy(), None if pu.paint is None else pu.paint.copy(), pu.generation))
        h.goto(pu, pu.generation + 1)
    add_trails(frames, primer)
    if (look or COUNTDOWN_LOOK) == "3d":
        import look3d
        for shot in (frames[:cut1], frames[cut1:cut2], frames[cut2:]):
            look3d.add_rise(shot)
        for i, f in enumerate(frames):
            f.look3d, f.zoom = True, COUNTDOWN_3D_ZOOM
            f.focus = (BW / 2 + i * COUNTDOWN_3D_DRIFT * CELL / FPS, BH / 2)
    return frames


# ---------------------------------------------------------------- a glider's flight
# A second kind of daily Short: one comment's glider as the main character. The
# camera follows it across the board until it hits something, slows down for the
# crash, then shows what that comment did to the universe's life. Everything in it
# comes from the log.
GLIDER_DIRS = {"ne": (1, -1), "nw": (-1, -1), "se": (1, 1), "sw": (-1, 1)}
FLIGHT_START_GPS, FLIGHT_MIN_S, FLIGHT_AFTER = 10, 4.0, 24  # its first pace (generations a second), the shortest
# flight in seconds, and the generations it plays on after the crash, at its top speed (it never slows for it)
FLIGHT_MAX_AVG_GPS = 30  # a long flight takes longer instead of racing past
FLIGHT_ZOOM, FLIGHT_Y, FLIGHT_LEAD = 1.25, 950, 110  # a bit closer than the board's full height (cells ~33 px); its height on
# screen, shifted back along its path by FLIGHT_LEAD px so more of what lies ahead shows
FLIGHT_MIN_GENS, FLIGHT_MIN_GPS = 40, 6  # a shorter trip isn't a journey; never slower than this while it flies
NOW_LINES = ("This universe is driven by your comments.", "Some save it. Some kill it.")  # the glider Short's
NOW_MORE_S, NOW_STEP, NOW_PUSH = 1.2, 5, 1.08  # first beat: the board as it is now, full screen, its second line in
# from NOW_MORE_S, a generation every NOW_STEP frames, closing in a little (and back out by the end, so it loops)
# the ending's example comment lands in a quiet spot in view, clear of the words above, the
# bubble below and the Shorts buttons on the right: columns from the view's left edge, rows
END_COLS, END_ROWS, END_RING = (6, 28), (22, 46), 72  # ...and its ring's radius (px)
END_BUBBLE_S = 0.5  # the bubble comes in this long after the words
END_ADDS = "adds a blue glider at the ring"  # what the bubble's example does (EXAMPLES[1]), tied to the ring


def _flew(h: History, u: Universe, e: dict) -> int:
    """How many generations a glider that has just landed (u: the board as it lands)
    flies as a lone glider before it hits something, later comments included.
    Checked every 4 generations."""
    d = np.array(GLIDER_DIRS.get(e["command"].split()[-1], (1, 1)))
    cells, w = np.array(e["cells"]), copy.deepcopy(u)
    H, W = u.grid.shape
    applied = set(h.applied)  # goto marks what it applies; u's own replay must still get it
    try:
        return _fly(h, w, cells, d, H, W)
    finally:
        h.applied = applied


def _fly(h, w, cells, d, H, W) -> int:
    for k in range(1, 2000):
        h.goto(w, w.generation + 4)
        want = (cells + d * k) % (W, H)
        rel = (want - want[0] + (W // 2, H // 2)) % (W, H) - (W // 2, H // 2)  # unwrapped around its first cell
        xs, ys = rel[:, 0] - rel[:, 0].min() + 1, rel[:, 1] - rel[:, 1].min() + 1
        x0, y0 = want[0][0] + rel[:, 0].min() - 1, want[0][1] + rel[:, 1].min() - 1
        win = w.grid[np.ix_(np.arange(y0, y0 + 5) % H, np.arange(x0, x0 + 5) % W)]
        alone = np.zeros((5, 5), bool)
        alone[ys, xs] = True
        if not (win.astype(bool) == alone).all() or w.phase == DIED:
            return 4 * k
    return 8000


def flights(h: History, since_gen: int = 0) -> list:
    """Every logged glider after `since_gen`, as (generations flown, gen, move, effect), longest first."""
    out, u = [], h.fresh()
    for g in sorted(x for x in h.batches if since_gen < x <= h.now_gen):
        movers = [e for e in h.batches[g] if e["command"].startswith("glider") and e.get("cells") and not e.get("erase")]
        if movers:
            h.goto(u, g)
            out += [(_flew(h, u, e), g, e, h.effects.get((g, e["command"]))) for e in movers]
    return sorted(out, key=lambda t: -t[0])


def _glider_shapes() -> dict:
    """Every 3x3 glider phase in every direction: {bytes: direction}."""
    def step(p):
        q = np.pad(p, 2)
        n = sum(np.roll(np.roll(q, i, 0), j, 1) for i in (-1, 0, 1) for j in (-1, 0, 1)) - q
        return ((n == 3) | ((q == 1) & (n == 2))).astype(np.uint8)

    def crop(p):
        ys, xs = np.nonzero(p)
        return p[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    out, p = {}, crop(np.array(life.mask(life.PATTERNS["glider_se"], (8, 8)), np.uint8))
    for _ in range(4):
        for name, (fx, fy) in GLIDER_DIRS.items():
            q = p[:, ::-1] if fx < 0 else p
            q = q[::-1, :] if fy < 0 else q
            out[q.tobytes()] = name
        p = crop(step(p))
    return out


def wild_gliders(h: History) -> list:
    """Every lone glider in the universe's history that no comment placed (born in a
    collision), checked every 4 generations: (generations flown, gen, move-like dict, None).
    The dict has its cells as first seen and its color: a glider's paint always came
    from some comment's color."""
    shapes, H, W = _glider_shapes(), life.H, life.W

    def box(a, n):
        c = np.pad(np.pad(a, n // 2, mode="wrap").astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
        return c[n:, n:] - c[:-n, n:] - c[n:, :-n] + c[:-n, :-n]
    placed = {(g, x, y) for g, b in h.batches.items() for e in b if e["command"].startswith("glider")
              for x, y in e.get("cells", [])}
    u, live, done = h.fresh(), {}, []
    for gen in range(0, h.now_gen + 1, 4):
        h.goto(u, gen)
        gr = u.grid.astype(np.uint8)
        b5, b3 = box(gr, 5), box(gr, 3)
        now = {}
        for y, x in zip(*np.nonzero((b5 == 5) & (b3 == 5))):
            ys, xs = np.arange(y - 1, y + 2) % H, np.arange(x - 1, x + 2) % W
            win = gr[np.ix_(ys, xs)]
            way = shapes.get(win.tobytes())
            if not way:
                continue
            dx, dy = GLIDER_DIRS[way]
            trip = live.pop((way, (x - dx) % W, (y - dy) % H), None)
            if trip is None:
                cells = [[int(xs[j]), int(ys[i])] for i, j in zip(*np.nonzero(win))]
                paint = u.paint[np.ix_(ys, xs)][win == 1] if u.paint is not None else np.zeros(1, int)
                k = int(np.bincount(paint).argmax())
                trip = {"start": gen, "move": {"command": f"glider {x},{y} {way}", "cells": cells, "wild": True,
                                               "color": commands.COLORS[k - 1] if k else None}}
            trip["end"] = gen
            now[(way, x, y)] = trip
        done += live.values()
        live = now
    done += live.values()
    out = []
    for t in done:
        g = t["start"]
        if any((gg, cx, cy) in placed for gg in range(max(0, g - 8), g + 1) for cx, cy in t["move"]["cells"]):
            continue  # a comment's own glider: flights() has it
        out.append((t["end"] - g + 4, g, t["move"], None))
    return sorted(out, key=lambda t: -t[0])


def tag(centre, r: float, alpha: float, text: str | None = None):
    """A ring around a point on the board (cells, unwrapped) and a pill with `text` under it."""
    def draw(d, to_canvas):
        a = int(255 * alpha)
        cx, cy = to_canvas(*cell_px(*centre))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, a), width=6)
        if text:
            f = ex.font("demi", 44)
            w = d.textlength(text, font=f)
            top = cy + r + 26
            d.rounded_rectangle((cx - w / 2 - 22, top, cx + w / 2 + 22, top + 70), radius=16, fill=(255, 255, 255, int(240 * alpha)))
            ex.text(d, (cx, top + 35), text, "demi", 44, alpha, fill=(10, 12, 16), anchor="mm", outline=False)
    return draw


MAP_X, MAP_Y, MAP_W = 40, 1250, 330  # the whole universe, small, at the bottom left


def minimap(grid, path, alpha: float, label: float = 1.0, paint=None):
    """The whole board, small, in its colors, with the glider's path so far (cells, unwrapped) and a ring where it is."""
    Hh, Ww = grid.shape
    c = MAP_W / Ww
    mh = round(Hh * c)
    on = grid > 0
    layers = [(on if paint is None else on & (paint == 0), tuple(PHASE_COLOR["ALIVE"]), 150)]
    if paint is not None:
        layers += [(on & (paint == k), tuple(render.PAINT[name]), 235) for k, name in enumerate(commands.COLORS, 1)]
    layers = [(Image.fromarray(m.astype(np.uint8) * 255).resize((MAP_W, mh), Image.NEAREST), col, op)
              for m, col, op in layers if m.any()]

    def draw(d, to_canvas):
        if alpha <= 0.01:
            return
        a = int(255 * alpha)
        d.rounded_rectangle((MAP_X - 8, MAP_Y - 8, MAP_X + MAP_W + 8, MAP_Y + mh + 8), radius=12,
                            fill=(6, 8, 10, int(215 * alpha)), outline=(70, 76, 92, a), width=2)
        for mask, col, op in layers:
            d.bitmap((MAP_X, MAP_Y), mask, fill=col + (int(op * alpha),))
        pts = [(MAP_X + (x % Ww + 0.5) * c, MAP_Y + (y % Hh + 0.5) * c) for x, y in path]
        for px, py in pts[:-1]:
            d.ellipse((px - 1.8, py - 1.8, px + 1.8, py + 1.8), fill=(255, 255, 255, int(170 * alpha)))
        px, py = pts[-1]
        d.ellipse((px - 8, py - 8, px + 8, py + 8), outline=(255, 255, 255, a), width=3)
        if label > 0:  # named while it's new, then just the map
            ex.text(d, (MAP_X - 4, MAP_Y - 32), "the whole universe", "demi", 30, alpha * label * 0.9, anchor="lm")
    return draw


def pick_flight(h: History, pick: str | None = None, wild: bool = False):
    """The flight to show (or the move `pick`): the longest colored glider a comment placed
    that flew FLIGHT_MIN_GENS, else the longest plain one. Its crash must be in the log. None
    when none flew far enough. Gliders born in collisions only with `wild`: so much of the
    history leads to one that no single comment can be credited."""
    trips = [t for t in flights(h) + (wild_gliders(h) if wild else [])
             if t[0] >= FLIGHT_MIN_GENS and t[1] + t[0] <= h.now_gen]
    trips.sort(key=lambda t: (bool(t[2].get("color")), t[0]), reverse=True)
    if pick:
        trips = [t for t in trips if t[2]["command"] == pick] or trips
    return trips[0] if trips else None


def flight_title(trip) -> str:
    """What the glider Short says it is."""
    return "This glider was born in a collision" if trip[2].get("wild") else "Someone's comment became this glider"


def build_flight(state_dir: str, pick: str | None = None) -> list | None:
    """The glider Short (pick_flight's glider, or the logged move `pick`): the universe as it
    is now, then that glider from where it landed until it crashed, quicker and quicker, what
    it did, and the challenge. None when no glider flew FLIGHT_MIN_GENS."""
    import render as rd
    h = History(state_dir)
    trip = pick_flight(h, pick)
    if trip is None:
        return None
    flown, g, e, eff = trip
    wild = bool(e.get("wild"))
    if wild:  # what it did: the universe's forecast with it, against the same moment without it
        w = h.fresh()
        h.goto(w, g)
        without = copy.deepcopy(w)
        without.grid = without.grid.copy()
        for x, y in e["cells"]:
            without.grid[y, x] = 0
        eff = {"before": without.forecast_stuck(FORECAST_GENS), "after": w.forecast_stuck(FORECAST_GENS)}
    G, launch = h.gen_seconds, h.genesis["launch_time"]
    H, W = life.H, life.W
    d = np.array(GLIDER_DIRS.get(e["command"].split()[-1], (1, 1)), float)
    cells = np.array(e["cells"])
    rel = (cells - cells[0] + (W // 2, H // 2)) % (W, H) - (W // 2, H // 2)
    c0 = cells[0] + rel.mean(axis=0)  # its centre as it lands (cells)

    def at(t: float) -> np.ndarray:  # its centre t generations later (cells, unwrapped: the camera never jumps)
        return c0 + d * min(t, flown) / 4

    s_full = PH / BH * FLIGHT_ZOOM  # screen px per board px
    zoom_full = s_full * BW / BAND_W

    gx, gy = PW / 2 - d[0] * FLIGHT_LEAD, FLIGHT_Y - d[1] * FLIGHT_LEAD  # where the glider sits on screen

    def follow(t: float) -> tuple[float, float]:  # the camera: the glider at (gx, gy) on screen
        x, y = cell_px(*at(t))
        return x + (PW / 2 - gx) / s_full, y + (PH / 2 - gy) / s_full

    frames: list = []

    def add(u, texts=(), zoom=1.0, focus=(BW / 2, BH / 2), extras=(), band=None, free=False, dark=0.0, **kw):
        f = ex.Frame(u, texts=texts, hud=False, zoom=zoom, dim=kw.pop("dim", 0.0), extras=extras, **kw)
        f.focus, f.free, f.scrim = focus, free, dark
        if band:
            f.band = band
        paint = getattr(u, "paint", None)
        f.paint = paint.copy() if paint is not None and paint.any() else None
        frames.append(f)

    def near(t: float) -> np.ndarray:  # the glider's neighbourhood, to light it up
        cx, cy = np.round(at(t)).astype(int)
        return life.mask([((cx + i) % W, (cy + j) % H) for i in range(-2, 3) for j in range(-2, 3)], (H, W))

    # first, the universe as it is now (every color in it is some comment's): a full screen
    # of its most colorful part, already moving; one sentence, then what comments do
    tonight = h.fresh()
    h.goto(tonight, h.now_gen)
    main, more = [(NOW_LINES[0], "bold", 92)], [(NOW_LINES[1], "demi", 62)]
    open_rows, k_main = block(main + more, TOP_Y, 1.0), len(block(main, TOP_Y, 1.0))  # placed together: nothing moves
    n_open = max(rf(*NOW_LINES), int(NOW_MORE_S * FPS) + rf(NOW_LINES[1]))
    open_gen = max(0, h.now_gen - n_open // NOW_STEP)
    s0 = PH / BH  # screen px per board px with the board's whole height filling the screen
    across = int(PW / s0 / CELL)  # cells on screen, side to side
    weight = tonight.grid > 0
    if tonight.paint is not None and (weight & (tonight.paint > 0)).any():
        weight = weight & (tonight.paint > 0)
    cols = weight.sum(0)
    x0 = max(range(W), key=lambda x: int(cols.take(range(x, x + across), mode="wrap").sum()))
    look = ((x0 + across / 2) * CELL, BH / 2)
    primer_now, ou = [], h.fresh()
    h.goto(ou, max(0, open_gen - life.TRAIL_GENS))
    while ou.generation < open_gen:
        primer_now.append((ou.grid.copy(), None if ou.paint is None else ou.paint.copy(), ou.generation))
        h.goto(ou, ou.generation + 1)
    for k in range(n_open):
        if k and k % NOW_STEP == 0 and ou.generation < h.now_gen:
            h.goto(ou, ou.generation + 1)
        a = 1.0 if k < n_open - FADE else fade(k, n_open)  # the first frame is the thumbnail: the sentence there
        b = a * ease((k - NOW_MORE_S * FPS) / 10)
        add(ou, [r[:5] + (a,) for r in open_rows[:k_main]] + [r[:5] + (b,) for r in open_rows[k_main:]],
            zoom=s0 * BW / BAND_W * (1 + (NOW_PUSH - 1) * ease(k / n_open)),
            focus=look, band=(0, PH), free=True, dark=1.0)
    k_flight = len(frames)

    # then a cut to the glider as it lands; the real generations before: tails on its first frame
    primer, pu = [], h.fresh()
    h.goto(pu, max(0, g - life.TRAIL_GENS))
    while pu.generation < g:
        primer.append((pu.grid.copy(), None if pu.paint is None else pu.paint.copy(), pu.generation))
        h.goto(pu, pu.generation + 1)
    u = h.fresh()
    h.goto(u, g)

    words = effect_words(eff["before"], eff["after"], G) if eff else "barely changed its fate"
    result = (("It " if wild else "That comment ") + words.replace("gave it", "gave this universe").replace(" to live", "").replace("its life", "this universe's life") + "."
              if words != "barely changed its fate" else "It barely changed the universe's fate.")
    said = None if wild else f"\u201c{render.move_text(e)}\u201d"
    lead = ([("This glider was born", "bold", 84), ("in a collision.", "bold", 84)] if wild
            else [("Someone's comment", "bold", 84), ("became this glider.", "bold", 84)])
    question = [("Will it save this universe, or kill it?", "demi", 56)]  # the reason to stay: answered at the end
    rows = block(lead + question, TOP_Y, 1.0)  # placed together, so the question stays put when the lead goes
    k_lead = len(block(lead, TOP_Y, 1.0))
    # every line on screen long enough to read (rf): the lead, then the question (in from 1 s,
    # and kept through the crash)
    X = flown
    lead_s = max(3.0, rf(*[t for t, _, _ in lead]) / FPS - 0.5)
    want = max(lead_s + 0.5, 0.5 + rf(question[0][0]) / FPS, FLIGHT_MIN_S, X / FLIGHT_MAX_AVG_GPS)
    # its pace: slower at first (to see it), then quicker and quicker right into the hit:
    # g0 (1 + k x/X) generations a second after x of them, k chosen so the flight takes `want`
    g0 = FLIGHT_START_GPS
    if X / g0 <= want:  # too short to speed up
        g0, k = max(FLIGHT_MIN_GPS, X / want), 0.0
    else:
        lo, hi = 1e-6, 500.0
        for _ in range(60):
            k = (lo + hi) / 2
            lo, hi = (k, hi) if X / (g0 * k) * np.log1p(k) > want else (lo, k)
    pace = [g0 * (1 + k * min(x, X) / X) for x in range(flown + 1)]  # pace[flown]: its top speed, on through the crash

    def counter(n: int, a: float):
        return [(f"{n:,}", "bold", 120, (MAP_X + MAP_W + 330, MAP_Y + 70), "mm", a),
                ("generations flown", "demi", 40, (MAP_X + MAP_W + 330, MAP_Y + 160), "mm", a)]

    r = 15 * s_full * 2.2  # ring radius on screen: a bit more than the glider
    hx, hy = np.round(at(flown)).astype(int)
    hit = life.mask([((hx + i) % W, (hy + j) % H) for i in range(-5, 6) for j in range(-5, 6)], (H, W))
    t, i = 0.0, 0
    crash_at, crash_hold = None, rf("Crash.") - FADE  # it plays on at top speed until "Crash." has been read
    while t < flown + FLIGHT_AFTER or crash_at is None or i - crash_at < crash_hold:
        t += pace[min(flown, int(t))] / FPS
        h.goto(u, g + int(t))
        sec = i / FPS
        a_lead = 1.0 if sec < lead_s else max(0.0, 1 - (sec - lead_s) * 2)
        a_q = min(1.0, max(0.0, (sec - 1.0) * 3))
        texts = [x[:5] + (a_lead,) for x in rows[:k_lead] if a_lead > 0] + [x[:5] + (a_q,) for x in rows[k_lead:]]
        if t >= flown:
            if crash_at is None:
                crash_at = i
            texts += block([("Crash.", "bold", 110)], round(np.mean([x[3][1] for x in rows[:k_lead]])),  # where the lead was
                           min(1.0, (i - crash_at + 1) / 6))
        texts += counter(min(int(t), flown), 1.0)
        boom = 1.0 if t < flown else 1 + min(1.0, (t - flown) / 10) * 1.6  # the ring opens out at the crash
        ring_a = 1.0 if t < flown else max(0.35, 1 - (t - flown) / 14)
        map_a = min(1.0, max(0.0, (sec - lead_s) * 2))
        add(u, texts, zoom=zoom_full, focus=follow(t), band=(0, PH), free=True, dark=1.0,
            highlight=near(t) if t < flown else (hit if t < flown + 10 else None),  # the impact flashes
            cue=("inject", float(c0[0])) if i == 0 else ("inject", float(at(flown)[0])) if i == crash_at else None,
            extras=[tag(at(t), r * boom, ring_a, said if t < flown else None),
                    minimap(u.grid, [at(x) for x in range(0, int(min(t, flown)) + 1, 4)], map_a,
                            label=min(1.0, max(0.0, (lead_s + 5.0 - sec) * 2)), paint=u.paint)])
        i += 1

    # what it did, pulling back to the whole board
    moved = abs((eff["after"] or 0) - (eff["before"] or 0)) * G if eff else 0  # how much (if both are known)
    big = moved >= 86400 or not eff or eff.get("after") is None or eff.get("before") is None
    answer = (("It saved it." if big else "It helped, a little.") if words.startswith("gave")
              else ("It hurt it." if big else "It hurt it, a little.") if words.startswith("took") else "It barely changed a thing.")
    without = "Without it" if wild else "Without that comment"
    if eff and eff.get("before") is not None and eff.get("after") is not None and words != "barely changed its fate":
        due = rd._at(launch, eff["before"] + u.flatline_gens, G, "%a %b %d")  # the forecast then, without it
        moved_by = _span(abs(eff["after"] - eff["before"]) * G)
        if eff["after"] < eff["before"]:
            result = f"{without}, it had {moved_by.replace(' ', ' more ', 1)} to live."
        elif eff["before"] + u.flatline_gens < h.now_gen:  # it would be gone by now: the date says so
            result = f"{without}, it was due to die {due}. It got {moved_by.replace(' ', ' more ', 1)}."
        else:
            result = f"{without}, it was due to die {moved_by} sooner."
    top = [(answer, "bold", 96), (result, "demi", 52)]  # the counter already said how far it flew

    # back to the universe now, in the opening's view: what it did, then the challenge and the
    # one comment that takes it up, ringed where it would land; the board bright and moving
    # throughout, easing back out to where the Short began, so it loops
    now = tonight
    k_end = len(frames)
    who = "Someone's glider" if not wild else "This glider"
    dare = [(f"{who} flew {flown:,} generations.", "demi", 52), ("Can yours fly farther?", "bold", 88)]
    view = np.full(now.grid.shape, 1000, np.int32)  # everywhere but the quiet part of the view is taken
    view[np.ix_(range(*END_ROWS), [(x0 + c) % W for c in range(*END_COLS)])] = 0
    sx, sy = render.quiet_spot(view + now.grid, 10, 8)  # once: it stays put
    spot = f"{sx},{sy}"
    pts = np.array(commands.parse("example", EXAMPLES[1][0].format(at=spot)).cells, float)
    rel = (pts - pts[0] + (W // 2, H // 2)) % (W, H) - (W // 2, H // 2)
    ring = pts[0] + rel.mean(axis=0)
    ring[0] = x0 + (ring[0] - x0) % W  # in the view's columns, wherever the board wraps
    n_res = max(int(3.0 * FPS), rf(*[t for t, _, _ in top]))
    example = (EXAMPLES[1][0], EXAMPLES[1][1], EXAMPLES[1][2], END_ADDS)
    n_end = max(int(4.0 * FPS), rf(*[t for t, _, _ in dare], _quote(example[0], spot), END_ADDS) + int(END_BUBBLE_S * FPS))
    total = n_res + n_end
    for k in range(total):
        if k % NOW_STEP == 0:
            History.advance(now, now.generation + 1)
        zoom = s0 * BW / BAND_W * (1 + (NOW_PUSH - 1) * (1 - ease(k / (total - 1))))
        if k < n_res:
            a = min(1.0, (k + 1) / FADE, (n_res - k) / FADE)
            add(now, block(top[:1], TOP_Y - 80, a) + block(top[1:], TOP_Y + 65, a), zoom=zoom, focus=look,
                band=(0, PH), free=True, dark=1.0, cue=("saved",) if k == FADE and words.startswith("gave") else None)
        else:
            j = k - n_res
            a = min(1.0, (j + 1) / FADE)
            b = min(1.0, max(0.0, (j + 1 - END_BUBBLE_S * FPS) / FADE))
            add(now, block(dare[:1], TOP_Y - 95, a) + block(dare[1:], TOP_Y + 5, a),  # apart: sizes differ
                extras=[tag(ring, END_RING, b, spot), comment_chip(j, b, spot, cycle=[example])],
                zoom=zoom, focus=look, band=(0, PH), free=True, dark=1.0)
    add_trails(frames[:k_flight], primer_now)
    add_trails(frames[k_flight:k_end], primer)
    add_trails(frames[k_end:], [(f.grid, f.paint, f.gen) for f in frames[:k_flight]])  # it carries on from the opening
    return frames


# ---------------------------------------------------------------- the rewind Short
# The whole board from the first frame (the teaser and Day 3, which opened that way, kept
# 52.8% and 51.2% of viewers; Day 7's sparse, still opening kept 20.9%), then the week
# backward to the 7-cell acorn, the universe it would have been with no comments, and
# the real one again. About 20 s: the Shorts that held best were 20 s long.
REWIND_S = 5.0  # the whole week (or weeks), backward
NUMBERS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}
REWIND_STEP = 3  # the hook and the ending: a generation every 3 frames, as in the recap
ACORN_ZOOM = 6.0
LONE_PULL_S = 1.4  # pulling back from the acorn while the comment-free universe grows
REWIND_LOOK = "3d"  # "flat", or "3d": lit blocks on a tilted floor (look3d.py), as the countdown (from Nov 22)
REWIND_3D_ZOOM = COUNTDOWN_3D_ZOOM  # in 3D, every shot this much closer: most of the board, wrapping on to fill the screen


def people(state_dir: str) -> int:
    """People who have made moves: the per-person scores (HMAC'd IDs, one entry each).
    A few early moves have no author, so this never overcounts."""
    import json
    with open(os.path.join(state_dir, "state.json")) as fh:
        return len(json.load(fh).get("scores") or {})


def at_least(n: int) -> str:
    """'Over 200', '150': a round floor that stays true as more people play."""
    step = 100 if n >= 200 else 50 if n >= 100 else 10 if n >= 20 else 1
    return f"Over {n // step * step}" if n % step else f"{n}"


def lone_death(h: History) -> tuple[int, int] | None:
    """(stuck, died) generations of the seed left alone: no comments at all."""
    stuck = h.fresh().forecast_stuck(FORECAST_GENS)
    return None if stuck is None else (stuck, stuck + h.genesis["flatline_gens"])


def build_rewind(state_dir: str, look: str | None = None) -> list:
    h = History(state_dir)
    G, launch = h.gen_seconds, h.genesis["launch_time"]
    whole = (BW / 2, BH / 2)
    frames: list = []

    def frame(u, texts=(), zoom=1.0, focus=whole, **kw):
        f = ex.Frame(u, texts=texts, hud=False, zoom=zoom, dim=0.0, **kw)
        f.focus, f.free, f.scrim = focus, False, 0.0
        paint = getattr(u, "paint", None)
        f.paint = paint.copy() if paint is not None and paint.any() else None
        return f

    now_gen = h.now_gen
    lone = lone_death(h)
    n_people = people(state_dir)

    # 1. the hook, over the whole board as it was a minute ago, playing up to now
    hook_top = [("THIS SHORT", "bold", 112), ("DIES", "bold", 196)]
    hook_bottom = [("if nobody comments", "demi", 80)]
    n_hook = max(int(2.7 * FPS), rf("THIS SHORT", "DIES", "if nobody comments"))
    hook0 = max(0, now_gen - n_hook // REWIND_STEP)

    # 2. the week backward: slow at first (you see it un-happen), fast, slow again into the acorn
    n_rw = int(REWIND_S * FPS)
    rw_gens = [round(now_gen * (1 - ease(i / (n_rw - 1)))) for i in range(n_rw)]
    weeks = (now_gen * G // 86400 + 1) // 7  # day 8: the week; day 15: two weeks
    rw_top = [("Rewinding the week" if weeks <= 1 else f"Rewinding {NUMBERS.get(weeks, weeks)} weeks", "bold", 92)]

    def day_label(g: int) -> list:
        return [(f"Day {g * G // 86400 + 1} · {render._at(launch, g, G, '%a %b %d').replace(' 0', ' ')}",
                 "demi", 72)]

    # every frame of 1 and 2 is the real history: one pass forward, collected by generation
    want: dict[int, list[int]] = {}
    hook_gens = [min(now_gen, hook0 + i // REWIND_STEP) for i in range(n_hook)]
    for k, g in enumerate(hook_gens + rw_gens):
        want.setdefault(g, []).append(k)
    slots: list = [None] * (n_hook + n_rw)
    u = h.fresh()
    for g in sorted(want):
        h.goto(u, g)
        for k in want[g]:
            if k < n_hook:
                a = 1.0 if k < n_hook - FADE else fade(k, n_hook)  # the first frame is the thumbnail
                slots[k] = frame(u, block(hook_top, TOP_Y, a) + block(hook_bottom, BOTTOM_Y, a))
            else:
                i = k - n_hook
                a = fade(i, n_rw)
                slots[k] = frame(u, block(rw_top, TOP_Y, a) + block(day_label(g), BOTTOM_Y, a))
    frames += slots
    k_rw = n_hook

    # 3. the acorn it started as; then that acorn left alone, growing with no comments,
    # stuck, and dead (the universe's own colors: red when it's stuck, grey when it dies)
    start = [("It started as 7 cells.", "bold", 92)]
    n_acorn = rf(start[0][0])
    acorn = centre(life.centered(h.genesis["seed"]))
    lu = h.fresh()
    k_lone = len(frames)
    for i in range(n_acorn):
        if i > n_acorn // 2 and i % 4 == 0:
            History.advance(lu, lu.generation + 1)
        zoom = 1 + (ACORN_ZOOM - 1) * ease(i / 18)
        frames.append(frame(lu, block(start, TOP_Y, fade(i, n_acorn)), zoom=zoom, focus=acorn))
    if lone is not None:
        stuck, died = lone
        when = render._at(launch, died, G, "%b %d").replace(" 0", " ")  # a date: "Wednesday" read as the coming one
        alone = [("Left alone, it would", "bold", 88), (f"have died {when}.", "bold", 88)]
        n_alone = rf(*[t for t, _, _ in alone]) + int(0.6 * FPS)  # and a beat on it, dead
        g0, pull = lu.generation, int(LONE_PULL_S * FPS)
        run = n_alone - int(0.9 * FPS)  # it reaches its death with time to see it
        for i in range(n_alone):
            p = min(1.0, i / run)
            History.advance(lu, g0 + round((died - g0) * p ** 1.6))
            zoom = 1 + (ACORN_ZOOM - 1) * (1 - ease(i / pull))
            focus = (acorn[0] + (whole[0] - acorn[0]) * ease(i / pull), acorn[1] + (whole[1] - acorn[1]) * ease(i / pull))
            frames.append(frame(lu, block(alone, TOP_Y, fade(i, n_alone)), zoom=zoom, focus=focus))
    k_end = len(frames)

    # 4. the real universe again, now, full of color, and the call to action
    # not "kept it alive": some of their moves took time off its life
    top = [(f"{at_least(n_people)} people have played.", "bold", 92), ("It's still alive.", "bold", 92)]
    bottom = [("Comment anything to join them.", "demi", 64), ("Live now on this channel", "demi", 50)]
    n_end = rf(*[t for t, _, _ in top + bottom])
    nu = h.fresh()
    h.goto(nu, now_gen)
    for i in range(n_end):
        if i and i % REWIND_STEP == 0:
            History.advance(nu, nu.generation + 1)
        a = min(1.0, (i + 1) / FADE)  # no fade out: it loops back to the hook's board
        frames.append(frame(nu, block(top, TOP_Y, a) + block(bottom, BOTTOM_Y, a),
                            cue=("saved",) if i == FADE else None))

    primer, pu = [], h.fresh()
    h.goto(pu, max(0, hook0 - life.TRAIL_GENS))
    while pu.generation < hook0:
        primer.append((pu.grid.copy(), None if pu.paint is None else pu.paint.copy(), pu.generation))
        h.goto(pu, pu.generation + 1)
    add_trails(frames[:k_rw], primer)
    add_trails(frames[k_lone:k_end])
    add_trails(frames[k_end:], [(f.grid, f.paint, f.gen) for f in frames[:k_rw]])
    if (look or REWIND_LOOK) == "3d":
        import look3d
        look3d.add_rise(frames[k_end:] + frames[:k_rw])  # one shot across the loop: the end plays into the hook
        look3d.add_rise(frames[k_lone:k_end])  # (not the rewind: hundreds of generations a frame, its blocks just stand)
        for f in frames:
            f.look3d, f.zoom = True, f.zoom * REWIND_3D_ZOOM
    return frames


REWIND_TITLE = "It started as 7 cells"


# ---------------------------------------------------------------- "1,000 comments built this"
# A Short about the one thing a stranger can't tell from a picture of the board: that
# people made it. About 20 s, every beat on a full screen of board, the cells big:
#   1. "1,000 comments built this simulation." over the universe playing up to now (the
#      thumbnail first), then rewinding: the whole universe folds back into its seed
#   2. the seed, close up: "It started as 7 cells."
#   3. "Every comment adds more.": one real comment landing (a glider, flying), its words in a pill
#   4. "It's still alive. Comment to keep it going." over the board just before the hook's: a seamless loop
# Every count in it is the real one, rounded down (comments_made).
MADE_FULL = PH / BH * BW / BAND_W  # the board's whole height fills the screen: cells ~27 px
MADE_ACORN = 3.5  # times MADE_FULL, on the seed
MADE_ACORN_3D = 1.7  # and this much closer again in 3D
MADE_PUSH = 1.6  # times MADE_FULL, on the comment
MADE_STEP = 10  # frames per generation on the live board (3 a second): slow enough to watch blocks rise and sink
MADE_COMMENT_STEP = 3  # and as the comment lands: its glider flies
MADE_CRASH_S = 1.5  # a glider's beat runs on this long after it hits something
MADE_PLAY_S = 4.0  # the hook's board playing, before it rewinds
MADE_RW_S = 7.0  # the rewind: at the play's pace at both ends, a blur between (rewind_curve)
MADE_END_MORE_S = 2.5  # the ending held this much longer than its words need, to watch it
MADE_SEED_MORE_S = 1.5  # the seed held longer too, growing
MADE_COMMENT_S = 4.5  # the comment's beat, at least
MADE_LAND_S = 0.5  # the comment lands this far into its beat
MADE_HOOK_Y = 380  # the count's centre: clear of the Shorts player's top bar


def comments_made(n: int) -> str:
    """'1,000', '1,200', 'Almost 1,000', '800': the comments that landed, never more than really did."""
    if 950 <= n < 1000:
        return "Almost 1,000"
    return f"{n // 100 * 100:,}" if n >= 100 else f"{n}"


def made_count(h: History) -> int:
    """Every move that landed on the board: one comment or live-chat message each."""
    return sum(len(b) for b in h.batches.values())


MADE_FLIGHT_GENS = 12  # a glider for the comment beat flies at least this far in the clear


def made_move(h: History, since_gen: int):
    """The made Short's comment, as (gen, move, effect): one that's good to watch. A glider
    that lands in the clear and flies MADE_FLIGHT_GENS (a colored one first, then the
    longest flight), else the period's biggest save or kill (opening_move). None if neither."""
    trips = [t for t in flights(h, since_gen) if t[0] >= MADE_FLIGHT_GENS]
    clear = _lands_clear(h, [(g, e) for _, g, e, _ in trips], since_gen) if trips else {}
    trips = [t for t in trips if clear.get((t[1], t[2]["command"]))]
    if trips:
        _, g, e, f = max(trips, key=lambda t: (bool(t[2].get("color")), t[0]))
        return g, e, f
    return opening_move(h, since_gen)


def made_title(h: History) -> str:
    return f"{comments_made(made_count(h))} comments built this simulation"


def _full(u, texts=(), zoom=MADE_FULL, focus=(BW / 2, BH / 2), scrim_amount=1.0, **kw):
    """One frame on a full screen of board (a window anywhere on the torus)."""
    f = ex.Frame(u, texts=texts, hud=False, zoom=zoom, dim=0.0, **kw)
    f.focus, f.free, f.scrim, f.band = focus, True, scrim_amount, (0, PH)
    paint = getattr(u, "paint", None)
    f.paint = paint.copy() if paint is not None and paint.any() else None
    return f


def _busiest_columns(grids, cols: int) -> float:
    """The x centre (board px) of the `cols`-wide window, wrapping, with the most life in `grids`."""
    w = sum(g.astype(np.int64) for g in grids).sum(axis=0)
    run = np.convolve(np.concatenate([w, w[:cols - 1]]), np.ones(cols, np.int64), "valid")[:len(w)]
    return (int(np.argmax(run)) + cols / 2) * CELL


MADE_LOOK = "3d"  # "flat", or "3d": the same frames drawn as lit blocks on a tilted floor (look3d.py)


def _soft_min(x: float, cap: float, w: float = 3.0) -> float:
    """min(x, cap) with the corner rounded over +-w: a camera that eases to a stop."""
    if x <= cap - w:
        return x
    if x >= cap + w:
        return cap
    return cap - (cap + w - x) ** 2 / (4 * w)


def rewind_curve(total: int, n: int, v0: float) -> list[float]:
    """How far back (generations) each of n frames of a rewind is: it leaves at v0
    generations a second and arrives at that pace too, accelerating exponentially to
    half way and slowing the same way after, so every order of magnitude gets its share."""
    if total <= 0:
        return [0.0] * n
    half, k0 = (n - 1) / FPS / 2, v0

    def reach(k):  # generations covered by the first half at rate k
        return v0 / k * np.expm1(k * half)
    lo, hi = 1e-6, 60.0
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if reach(mid) < total / 2 else (lo, mid)
    k0 = (lo + hi) / 2
    out = []
    for i in range(n):
        t = i / FPS
        r = v0 / k0 * np.expm1(k0 * t) if t <= half else total - v0 / k0 * np.expm1(k0 * (2 * half - t))
        out.append(float(min(total, max(0.0, r))))
    return out


MADE_FOLLOW = True  # in the comment's beat the camera follows its glider


def build_made(state_dir: str, since_gen: int | None = None, look: str | None = None,
               follow: bool | None = None) -> list:
    h = History(state_dir)
    G, launch = h.gen_seconds, h.genesis["launch_time"]
    now_gen = h.now_gen
    style = look or MADE_LOOK
    num = comments_made(made_count(h))
    hook = ([("Almost", "bold", 100)] if num.startswith("Almost") else []) + \
        [(num.removeprefix("Almost "), "bold", 230), ("comments built", "bold", 88), ("this simulation.", "bold", 88)]

    def hook_texts(a):  # placed by hand: rows this different in size don't stack evenly
        y = MADE_HOOK_Y
        out = block([hook[-3]], y, a) + block([hook[-2]], y + 165, a) + block([hook[-1]], y + 275, a)
        return (block([hook[0]], y - 165, a) if len(hook) == 4 else []) + out
    # 1. the hook over the universe playing up to now, slowly enough to watch it live;
    # then the whole of it rewinding: slow, fast, slow again as it folds back into the seed
    n_play = int(MADE_PLAY_S * FPS)
    n_rw = int(MADE_RW_S * FPS)
    n_hook = n_play + n_rw
    h0 = max(0, now_gen - (n_play - 1) // MADE_STEP)
    back = rewind_curve(now_gen, n_rw, FPS / MADE_STEP)

    def rewound(k: int) -> float:  # 0..1 of the way back, k frames into the rewind
        return back[k] / max(1, now_gen)
    hook_gens = [min(now_gen, h0 + k // MADE_STEP) for k in range(n_play)] + \
        [now_gen - round(back[k]) for k in range(n_rw)]
    n_words = max(n_play, rf(*[t for t, _, _ in hook]))  # the hook's words, then the days going by
    day_of = lambda g: g * G // 86400 + 1
    end = [("It's still alive.", "bold", 112), ("Comment to keep it going.", "bold", 70), WATCH_LIVE]
    n_end = rf(*[t for t, _, _ in end]) + int(MADE_END_MORE_S * FPS)
    e0 = max(0, h0 - (n_end + MADE_STEP - 1) // MADE_STEP)  # the board just before the hook's: it loops
    end_gens = [min(h0 - 1, e0 + i // MADE_STEP) if h0 > 0 else 0 for i in range(n_end)]

    # 1 and 4 are the real history: one pass forward, collected by generation
    snaps, u = {}, h.fresh()
    for g in sorted(set(hook_gens) | set(end_gens)):
        h.goto(u, g)
        snaps[g] = copy.deepcopy(u)
    fx = _busiest_columns([snaps[g].grid for g in {*hook_gens[:n_play], *end_gens}], round(PW / (PH / BH) / CELL))
    home = (fx, BH / 2)
    acorn = centre(life.centered(h.genesis["seed"]))

    hook_frames = []
    for k, g in enumerate(hook_gens):
        if k < n_words:
            texts = hook_texts(1.0 if k < n_words - FADE else fade(k, n_words))  # the first frame is the thumbnail
        else:  # the days ticking back to the first
            a = min(1.0, (k - n_words + 1) / FADE, (n_hook - k) / FADE)
            texts = block([("rewinding", "demi", 64)], MADE_HOOK_Y - 120, a) + \
                block([(f"Day {day_of(g)}", "bold", 170)], MADE_HOOK_Y + 20, a)
        p = ease(rewound(k - n_play)) if k >= n_play else 0.0
        focus = (home[0] + (acorn[0] - home[0]) * p, home[1] + (acorn[1] - home[1]) * p)
        hook_frames.append(_full(snaps[g], texts, focus=focus))
    end_frames = []
    for k, g in enumerate(end_gens):
        a = min(1.0, (k + 1) / FADE, (n_end - k) / (FADE * 0.75))  # out just before the loop's seam
        end_frames.append(_full(snaps[g], block(end, TOP_Y, a), focus=home,
                                cue=("saved",) if k == FADE else None))

    # 2b. the seed, close up, starting to grow
    n_seed = len(life.centered(h.genesis["seed"]))
    start = [(f"It started as {n_seed} cells.", "bold", 92)]
    n_acorn = rf(start[0][0]) + int(MADE_SEED_MORE_S * FPS)
    su, seed_frames = h.fresh(), []
    for i in range(n_acorn):
        if i > n_acorn // 3 and i % MADE_COMMENT_STEP == 0:  # still at first, then growing
            History.advance(su, su.generation + 1)
        close = MADE_ACORN * (MADE_ACORN_3D if style == "3d" else 1)  # a tilted floor shrinks it: closer
        zoom = MADE_FULL * (1 + (close - 1) * ease(i / 18))
        seed_frames.append(_full(su, block(start, TOP_Y, fade(i, n_acorn)), zoom=zoom, focus=acorn))

    # 3. one real comment landing (made_move): "Every comment adds more."
    since = since_gen if since_gen is not None else max(0, now_gen - 86400 // G)
    move = made_move(h, since)
    comment_frames, primer3 = [], []
    if move is not None:
        import render as rd
        g, e, f = move
        cells = [tuple(c) for c in e["cells"]]
        piece = main_piece(cells)
        said = ["Every comment adds more."]
        top = [(s, "bold", 88) for s in said]
        if e["command"].startswith("random"):
            caption = "someone's message\nbecame a random 5×5 patch" + (f", {e['color']}" if e.get("color") else "")
        else:
            caption = "“" + rd.move_text(e) + "”"
        n = max(int(MADE_COMMENT_S * FPS), rf(*said))
        land = int(MADE_LAND_S * FPS)
        first = max(0, g - 1 - land // MADE_COMMENT_STEP)
        if e["command"].startswith("glider"):  # on until it has hit something, and a beat more
            cu = h.fresh()  # (before the board below: fresh() restarts what goto has applied)
            h.goto(cu, g)
            n = max(n, land + _flew(h, cu, e) * MADE_COMMENT_STEP + int(MADE_CRASH_S * FPS))
        cu = h.fresh()
        h.goto(cu, max(0, first - life.TRAIL_GENS))
        while cu.generation < first:
            primer3.append((cu.grid.copy(), None if cu.paint is None else cu.paint.copy(), cu.generation))
            h.goto(cu, cu.generation + 1)
        heading = (np.array(GLIDER_DIRS.get(e["command"].split()[-1], (1, 1)))
                   if e["command"].startswith("glider") else None)
        px, py = centre(piece)
        lift = (PH / 2 - 980) / (MADE_FULL * MADE_PUSH * BAND_W / BW)  # the comment in the clear middle
        chase = (follow if follow is not None else MADE_FOLLOW) and heading is not None
        cam = np.array([px, py + lift])
        lit, far = None, 0
        for i in range(n):
            cue = None
            if i == land:
                h.goto(cu, g)
                lit = life.mask(cells, cu.grid.shape)
                cue = ("inject", float(np.mean([c[0] for c in cells])))
                if heading is not None:
                    far = _flew(h, cu, e)
            elif i and i % MADE_COMMENT_STEP == 0 and (i > land or cu.generation < g - 1):
                h.goto(cu, cu.generation + 1)
                if heading is not None and cu.generation == g + far:  # it hits
                    cue = ("inject", float((piece[0][0] + heading[0] * far / 4) % life.W))
            # where the glider is along its straight line: steady, easing to a stop where it hits
            flown = _soft_min(max(0.0, (i - land) / MADE_COMMENT_STEP), far) if i >= land else 0.0
            at = piece if heading is None else [(x + heading[0] * flown / 4, y + heading[1] * flown / 4)
                                                for x, y in piece]
            gone = 0.0  # a glider's ring stops where it hits, and stays: that's what it did
            a = fade(i, n)
            b = a * ease((i - land) / 6)  # the ring and the words come with it
            if chase:  # the camera rides the same straight line
                cam = np.array([px + heading[0] * flown / 4 * CELL, py + heading[1] * flown / 4 * CELL + lift])
            focus = (float(cam[0]), float(cam[1]))
            comment_frames.append(_full(cu, block(top, TOP_Y, b), zoom=MADE_FULL * MADE_PUSH, focus=focus,
                                        highlight=lit if i >= land else None, cue=cue,
                                        extras=[ring(at, b * (1 - gone), caption)] if i >= land else []))

    # trails: from the real generations before each run of frames (none on the rewind)
    pu, primer = h.fresh(), []
    h.goto(pu, max(0, e0 - life.TRAIL_GENS))
    while pu.generation < e0:
        primer.append((pu.grid.copy(), None if pu.paint is None else pu.paint.copy(), pu.generation))
        h.goto(pu, pu.generation + 1)
    add_trails(end_frames, primer)
    add_trails(hook_frames, [(f.grid, f.paint, f.gen) for f in end_frames], continuous=True)
    add_trails(seed_frames)
    if comment_frames:
        add_trails(comment_frames, primer3)
    shots = [end_frames, hook_frames, seed_frames, comment_frames]  # the end first: it plays into the hook
    if style == "3d":
        import look3d
        look3d.add_rise(end_frames + hook_frames)  # one shot across the loop
        look3d.add_rise(seed_frames)
        look3d.add_rise(comment_frames)
        for shot in shots:
            for f in shot:
                f.look3d = True
    return hook_frames + seed_frames + comment_frames + end_frames


LOOP_XFADE_S = 0.12


def looped_soundtrack(frames: list):
    """The soundtrack of a Short that loops, seamless when it plays again: made for
    two plays in a row and cut to the second, so its start already has what was still
    ringing at the end; its last LOOP_XFADE_S cross into the first play's end, which
    the second play's start continues sample for sample."""
    import numpy as np
    n = len(frames) * (ex.SR // FPS)
    both = ex.soundtrack(frames + frames)
    first, second = both[:, :n], both[:, n:2 * n].copy()
    k = int(LOOP_XFADE_S * ex.SR)
    ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)
    second[:, -k:] = second[:, -k:] * (1 - ramp) + first[:, -k:] * ramp
    return second


BLOCK_SOUNDS = False  # the 3D blocks' own bloops and pops (synthesized, they read as annoying: off until real samples)
TICK_MIN_FRAMES = 3  # at most one bloop (and one pop) this often: a fast-forward changes the board every frame
BLOOP_AMP, POP_AMP = 0.11, 0.05  # their loudest (for a lot of blocks at once)


def block_ticks(frames: list) -> np.ndarray:
    """The blocks' own sound in a 3D Short, soft and low: a "bloop" as new cells spring
    up (its note from where they are: high far away, low near; louder for more of them)
    and a quieter "pop" as dying ones burst. Panned to their side. Silence when nothing changes."""
    spf = ex.SR // FPS
    n = len(frames) * spf
    out = np.zeros((2, n), np.float32)
    notes = [v for v in ex.NOTES[0:7]]  # A3 up, pentatonic

    def put(sig, at, pan):
        if at >= n:
            return
        seg = sig[: n - at].astype(np.float32)
        ang = (pan + 1) * np.pi / 4
        out[0, at:at + len(seg)] += np.cos(ang) * seg
        out[1, at:at + len(seg)] += np.sin(ang) * seg

    def drop(f0, glide, decay, dur):  # a note that starts a little sharp and falls into place
        t = np.arange(int(dur * ex.SR)) / ex.SR
        f = f0 * (1 + glide * np.exp(-t / 0.025))
        ph = 2 * np.pi * np.cumsum(f) / ex.SR
        return (np.sin(ph) + 0.18 * np.sin(2 * ph)) * np.exp(-t / decay) * np.minimum(1.0, t / 0.003)

    last_b = last_d = -TICK_MIN_FRAMES
    for i in range(1, len(frames)):
        a, b = frames[i - 1].grid, frames[i].grid
        if a.shape != b.shape or frames[i].grey:
            continue
        gh, gw = b.shape
        born = np.argwhere((b == 1) & (a == 0)) if i - last_b >= TICK_MIN_FRAMES else ()
        if len(born):
            last_b = i
            loud = min(1.0, np.log2(len(born) + 1) / 7)
            f0 = notes[min(len(notes) - 1, int((1 - born[:, 0].mean() / gh) * len(notes)))]
            put(drop(f0, 0.5, 0.09, 0.3) * BLOOP_AMP * (0.4 + 0.6 * loud), i * spf, born[:, 1].mean() / (gw - 1) * 1.2 - 0.6)
        died = np.argwhere((b == 0) & (a == 1)) if i - last_d >= TICK_MIN_FRAMES else ()
        if len(died):
            last_d = i
            loud = min(1.0, np.log2(len(died) + 1) / 7)
            put(drop(notes[-1] * 2, 0.8, 0.025, 0.12) * POP_AMP * (0.4 + 0.6 * loud), (i + 2) * spf,
                died[:, 1].mean() / (gw - 1) * 1.2 - 0.6)
    return out


def render_video(state_dir: str, out: str, since_gen: int | None = None, preview: str | None = None,
                 kind: str = "recap", pick: str | None = None, look: str | None = None,
                 follow: bool | None = None) -> str:
    """Render the Short (or, with `since_gen`, the recap since then; with kind="flight",
    the glider Short) to an MP4."""
    ex.SCALE = 1  # explainer's text helpers draw in pixels here
    frames = None
    if kind == "flight":
        frames = build_flight(state_dir, pick)
    elif kind == "rewind":
        frames = build_rewind(state_dir, look)
    elif kind == "stranger" and since_gen is not None:
        frames = build_stranger(state_dir, since_gen)
    elif kind == "countdown":
        frames = build_countdown(state_dir, look)
    elif kind == "made":
        frames = build_made(state_dir, since_gen, look, follow)
    if frames is None:
        frames = build(state_dir, since_gen)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if preview:
        os.makedirs(preview, exist_ok=True)
    wav = os.path.splitext(out)[0] + ".wav"
    audio = looped_soundtrack(frames) if kind == "made" else ex.soundtrack(frames)
    if BLOCK_SOUNDS and kind == "countdown" and any(getattr(f, "look3d", False) for f in frames):
        audio = audio + block_ticks(frames)  # the blocks' own sound, as they rise
    ex.write_wav(wav, audio)
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{PW}x{PH}",
         "-r", str(FPS), "-i", "-", "-i", wav, "-shortest",
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", out],
        stdin=subprocess.PIPE)
    for i, f in enumerate(frames):
        data = compose(f)
        ff.stdin.write(data)
        if preview and i % FPS == FPS // 2:
            Image.frombytes("RGB", (PW, PH), data).save(os.path.join(preview, f"{i // FPS:03d}s.png"))
    ff.stdin.close()
    if ff.wait() != 0:
        raise RuntimeError("ffmpeg failed")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--out", default=os.path.join("out", "short.mp4"))
    ap.add_argument("--preview", help="also save one frame per second as PNG into this folder")
    ap.add_argument("--since-hours", type=float, help="a recap of the last N hours (the daily Short)")
    ap.add_argument("--flight", action="store_true", help="the glider Short: the longest recent glider flight in the log")
    ap.add_argument("--pick", help='with --flight, this logged move instead (e.g. "glider 66,8 se")')
    ap.add_argument("--rewind", action="store_true", help="the rewind Short: the week backward to the acorn")
    ap.add_argument("--stranger", action="store_true", help="with --since-hours: the noon Short (one comment's save or kill)")
    ap.add_argument("--countdown", action="store_true", help="the 8 PM Short: how long it has left")
    ap.add_argument("--look", choices=("flat", "3d"), help=f"with --made, --countdown or --rewind: how it's drawn (defaults {MADE_LOOK}, {COUNTDOWN_LOOK}, {REWIND_LOOK})")
    ap.add_argument("--fixed", action="store_true", help="with --made: the camera stays put as the comment lands")
    ap.add_argument("--made", action="store_true", help='"1,000 comments built this": the rewind to the seed, one comment')
    args = ap.parse_args()
    since = None
    if args.since_hours:
        h = History(args.state_dir)
        since = max(0, h.now_gen - int(args.since_hours * 3600 // h.gen_seconds))
    kind = ("flight" if args.flight else "rewind" if args.rewind else "stranger" if args.stranger
            else "countdown" if args.countdown else "made" if args.made else "recap")
    render_video(args.state_dir, args.out, since, args.preview, kind, args.pick, args.look, False if args.fixed else None)
    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
