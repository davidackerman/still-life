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
from universe import DIED, Universe

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
    d.rounded_rectangle((x0, yc - h / 2, x0 + w, yc + h / 2), radius=26 * s, fill=(245, 246, 250, int(238 * alpha)))
    d.ellipse((x0 + 26 * s, yc - 22 * s, x0 + 70 * s, yc + 22 * s), fill=(150, 156, 170, a))  # a commenter
    tx = x0 + 88 * s
    ex.text(d, (tx, yc - 18 * s), say, "demi", round(46 * s), alpha, fill=(12, 14, 18), anchor="lm", outline=False)
    ex.text(d, (tx, yc + 28 * s), adds, "demi", round(30 * s), alpha, fill=(90, 96, 110), anchor="lm", outline=False)
    bx, by = int(x0 + w - 14 * s - box), int(yc - box / 2)
    d.rounded_rectangle((bx - 4, by - 4, bx + box + 4, by + box + 4), radius=10, fill=(16, 18, 24, a))
    fill = tuple(render.PAINT[paint] if paint else PHASE_COLOR["ALIVE"]) + (a,)
    for y, x in np.argwhere(cells):
        px, py = bx + x * cell, by + y * cell
        d.rectangle((px + 1, py + 1, px + cell - 2, py + cell - 2), fill=fill)


def chip_turn() -> int:
    """Frames a bubble is held at the films' own pace: its film, played through once."""
    return len(render.demo(CYCLE[0][1], CHIP_BOX)) * CHIP_EVERY


def comment_chip(i: int, alpha: float, at: str, per: float | None = None):
    """Frame i (counted from the first bubble) of the comment bubbles, `per` frames to a turn."""
    per = per or chip_turn()

    def draw(d, to_canvas):
        if alpha <= 0:
            return
        turn = int(i // per)
        raw, pattern, paint, adds = CYCLE[turn % len(CYCLE)]
        say = _quote(raw, at, i)
        film = render.demo(pattern, CHIP_BOX)
        k = min(len(film) - 1, int((i - turn * per) * len(film) / per))
        w = _widest(d, raw, at, adds)  # steady while its messages take turns
        _bubble(d, int(CX - w / 2), CHIP_Y, w, say, adds, film[k], paint, alpha)
    return draw


def comment_stack(i: int, n: int, alpha: float, at: str):
    """Frame i of the n-frame ending: every example, one under another, their
    films starting together and playing through once."""
    def draw(d, to_canvas):
        s = STACK_SCALE
        rows = [(_quote(say, at, i), pattern, paint, adds) for say, pattern, paint, adds in EXAMPLES]
        w = max(_widest(d, say, at, adds, s) for say, _, _, adds in EXAMPLES)
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
            u.inject([{"command": e["command"], "cells": e["cells"], "run_at": e["run_at"]} for e in batch])
            paint = getattr(u, "paint", None)
            for e in batch:
                if paint is not None and e.get("color") in commands.COLORS:
                    k = commands.COLORS.index(e["color"]) + 1
                    for x, y in e["cells"]:
                        paint[y % paint.shape[0], x % paint.shape[1]] = k
            self.applied.add(g)
            cells += [tuple(c) for e in batch for c in e["cells"]]
        self.advance(u, target)
        return cells


def days(gens: int, gen_seconds: int) -> int:
    return int(gens * gen_seconds // 86400)


# ---------------------------------------------------------------- the story
def add_trails(frames: list, primer=()) -> None:
    """Give each frame its tails (life.Trail, as on the stream). A step is a frame
    whose board changed, so a hold keeps its tails, and a cut (back in time, or a
    jump far ahead) starts them afresh. `primer` is (grid, paint, generation) for the real generations
    just before the first frame, so even the first frame (the thumbnail) shows
    where things are moving."""
    trail, prev, t, gen0 = None, None, 0, None
    for grid, paint, gen, f in [*((g, p, n, None) for g, p, n in primer),
                                *((f.grid, getattr(f, "paint", None), f.gen, f) for f in frames)]:
        if trail is None or grid.shape != trail.last.shape or (gen0 is not None and not 0 <= gen - gen0 <= 30):
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
            chip_at.append("{},{}".format(*render.quiet_spot(now.grid)))
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
    n = int((2.0 if opening else 2.7) * FPS)  # without an opening, on from the very first frame: the thumbnail
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
            caption = f"a comment: {batch[-1]['command']}" + (f" (+{len(batch) - 1} more)" if len(batch) > 1 else "")
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
        extras = lambda a, i: [comment_stack(i, n, a, f"{x},{y}")]  # noqa: E731
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
OPEN_S, OPEN_LAND_S, OPEN_STEP, OPEN_MORE_S, OPEN_OUT_S = 3.4, 0.7, 5, 1.0, 0.6
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
    for pick in ((kills, saves) if lead == "kill" else (saves, kills)):
        if pick:
            return max(pick, key=lambda t: t[0])[1:]
    return None


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
    n, land, lit = int(OPEN_S * FPS), int(OPEN_LAND_S * FPS), None
    out, later = int(OPEN_OUT_S * FPS), int(OPEN_MORE_S * FPS)
    px, py = centre(piece)
    s0 = PH / BH  # screen px per board px with the board's whole height filling the screen
    start = (px, py + (PH / 2 - OPEN_PIECE_Y) / s0)  # the comment at OPEN_PIECE_Y, below the words
    for i in range(n):
        cue = None
        if i == land:  # it lands
            h.goto(u, g)
            lit = life.mask(cells, u.grid.shape)
            cue = ("inject", float(np.mean([c[0] for c in cells])))
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
        add(u, texts, highlight=lit, cue=cue, zoom=zoom, focus=focus, extras=[ring(piece, a, caption)],
            band=(band_y, band_h), free=True, scrim=1 - q)


def _moves(h: History, u: Universe, shown: list[int], head, add, until: int, effect_text):
    """Replay up to `until`, zooming in on each generation in `shown`."""
    lit = None
    seg = max(18, int(2.4 * FPS) // (len(shown) + 1))
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
        # plain words, not "glider 67,69 ne": most viewers of a daily Short are new
        caption = f"{len(batch)} comments landed here" if len(batch) > 1 else "a comment landed here"
        caption += effect_text(target, batch)
        hold, z = int(2.6 * FPS), 3.2
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
    total = sum(len(b) for g, b in h.batches.items() if g <= h.now_gen)

    # the board as it was a day ago
    u = h.fresh()
    h.goto(u, since_gen)

    head = [("Every comment adds cells to this Game of Life.", "bold", 76),
            (f"Day {day}: {count} comment{'s' * (count != 1)} in the last day" if count
             else f"Day {day}: no comments in the last day", "demi", 54)]
    if total > count:  # and how many it has had in all
        head.append((f"{total:,} since it began", "demi", 44))

    def effect_text(gen, batch):  # "\ngave it 6 more days to live"
        effs = [h.effects[(gen, e["command"])] for e in batch if (gen, e["command"]) in h.effects]
        return "\n" + effect_words(effs[0]["before"], effs[-1]["after"], G) if effs else ""
    # the day's biggest moves (one, when the opening already showed the biggest)
    shown = sorted(sorted((g for g in gens if g != skip), key=lambda g: -_impact(h, g))[:1 if skip is not None else 2])
    say = add_chip or add  # the comment bubbles carry on under the moves and the stakes
    _moves(h, u, shown, head, say, h.now_gen, effect_text)

    # the stakes: where its death stands now
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
    if end_chips:
        end_chips()


# ---------------------------------------------------------------- a glider's flight
# A second kind of daily Short: one comment's glider as the main character. The
# camera follows it across the board until it hits something, slows down for the
# crash, then shows what that comment did to the universe's life. Everything in it
# comes from the log.
GLIDER_DIRS = {"ne": (1, -1), "nw": (-1, -1), "se": (1, 1), "sw": (-1, 1)}
FLIGHT_GPS, FLIGHT_MAX_S = 16, 20  # generations a second while it flies (faster only to keep the flight under 20 s)
FLIGHT_SLOW_GPS, FLIGHT_SLOW_BEFORE, FLIGHT_SLOW_AFTER = 6, 8, 16  # the crash in slow motion: gens around it
FLIGHT_ZOOM, FLIGHT_Y, FLIGHT_LEAD = 1.0, 950, 110  # the board's full height (cells ~27 px); the glider's height on
# screen, shifted back along its path by FLIGHT_LEAD px so more of what lies ahead shows
FLIGHT_MIN_GENS = 40  # a shorter trip isn't a journey


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
        movers = [e for e in h.batches[g] if e["command"].startswith("glider") and e.get("cells")]
        if movers:
            h.goto(u, g)
            out += [(_flew(h, u, e), g, e, h.effects.get((g, e["command"]))) for e in movers]
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


def minimap(grid, path, alpha: float):
    """The whole board, small, with the glider's path so far (cells, unwrapped) and a ring where it is."""
    Hh, Ww = grid.shape
    c = MAP_W / Ww
    mh = round(Hh * c)
    mask = Image.fromarray((grid > 0).astype(np.uint8) * 255).resize((MAP_W, mh), Image.NEAREST)

    def draw(d, to_canvas):
        if alpha <= 0.01:
            return
        a = int(255 * alpha)
        d.rounded_rectangle((MAP_X - 8, MAP_Y - 8, MAP_X + MAP_W + 8, MAP_Y + mh + 8), radius=12,
                            fill=(6, 8, 10, int(215 * alpha)), outline=(70, 76, 92, a), width=2)
        d.bitmap((MAP_X, MAP_Y), mask, fill=tuple(PHASE_COLOR["ALIVE"]) + (int(150 * alpha),))
        pts = [(MAP_X + (x % Ww + 0.5) * c, MAP_Y + (y % Hh + 0.5) * c) for x, y in path]
        for px, py in pts[:-1]:
            d.ellipse((px - 1.8, py - 1.8, px + 1.8, py + 1.8), fill=(255, 255, 255, int(170 * alpha)))
        px, py = pts[-1]
        d.ellipse((px - 8, py - 8, px + 8, py + 8), outline=(255, 255, 255, a), width=3)
        ex.text(d, (MAP_X - 4, MAP_Y - 32), "the whole universe", "demi", 30, alpha * 0.9, anchor="lm")
    return draw


def build_flight(state_dir: str, pick: str | None = None) -> list | None:
    """The glider Short: the longest flight in the log (or the logged move `pick`).
    None when no glider flew FLIGHT_MIN_GENS."""
    import render as rd
    h = History(state_dir)
    trips = [t for t in flights(h) if t[0] >= FLIGHT_MIN_GENS and t[1] + t[0] + FLIGHT_SLOW_AFTER <= h.now_gen]  # crashed already
    if pick:
        trips = [t for t in trips if t[2]["command"] == pick] or trips
    if not trips:
        return None
    flown, g, e, eff = trips[0]
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

    # the real generations before it lands: tails on the first frame
    primer, pu = [], h.fresh()
    h.goto(pu, max(0, g - life.TRAIL_GENS))
    while pu.generation < g:
        primer.append((pu.grid.copy(), None if pu.paint is None else pu.paint.copy(), pu.generation))
        h.goto(pu, pu.generation + 1)
    u = h.fresh()
    h.goto(u, g)

    when = f"{rd._at(launch, g, G, '%A %b %d')}, {rd._at(launch, g, G, '%I:%M %p').lstrip('0')}"
    began = rd._at(launch, 0, G, "%A %b %d")
    span = None
    if eff and eff.get("before") is not None:  # `before`: the generation it would have got stuck at
        span = _span((eff["before"] - g) * G).replace("1 day", "a day").replace("1 hour", "an hour")
    words = effect_words(eff["before"], eff["after"], G) if eff else "barely changed its fate"
    result = ("That comment " + words.replace("gave it", "gave this universe").replace("its life", "this universe's life") + "."
              if words != "barely changed its fate" else "It barely changed the universe's fate.")
    said = f"\u201c{render.move_text(e)}\u201d"
    lead = [("Someone's comment", "bold", 84), ("became this glider.", "bold", 84)]
    question = [("How far will it get?", "demi", 60)]
    # on the way, what this universe is: in order, as many as the flight has time for
    beats = [(2, [(f"This universe has been running live on YouTube since {began}.", "demi", 50)]),
             (0, [("Every comment drops cells into it.", "demi", 50), ("Some save it. Some kill it.", "demi", 50)])]
    if span:
        beats.append((1, [(f"{when}: it was {span} from getting stuck.", "demi", 50),
                          ("Stuck means it dies, and these videos go private.", "demi", 44)]))
    gps = max(FLIGHT_GPS, (flown - FLIGHT_SLOW_BEFORE) / FLIGHT_MAX_S)
    lead_s = 4.0
    room = (flown - FLIGHT_SLOW_BEFORE) / gps - lead_s
    keep = sorted(b[0] for b in beats)[:max(0, min(len(beats), int(room // 3.5)))]  # by priority
    beats = [lines for rank, lines in beats if rank in keep]
    per = room / len(beats) if beats else 0

    def counter(n: int, a: float):
        return [(f"{n:,}", "bold", 120, (MAP_X + MAP_W + 330, MAP_Y + 70), "mm", a),
                ("generations flown", "demi", 40, (MAP_X + MAP_W + 330, MAP_Y + 160), "mm", a)]

    r = 15 * s_full * 2.2  # ring radius on screen: a bit more than the glider
    t, i = 0.0, 0
    crash_at = None
    while t < flown + FLIGHT_SLOW_AFTER:
        slow = t >= flown - FLIGHT_SLOW_BEFORE
        t += (FLIGHT_SLOW_GPS if slow else gps) / FPS
        h.goto(u, g + int(t))
        sec = i / FPS
        a_lead = 1.0 if sec < lead_s else max(0.0, 1 - (sec - lead_s) * 2)
        a_q = min(1.0, max(0.0, (sec - 1.0) * 3)) * (1.0 if t < flown else max(0.0, 1 - (t - flown) / 4))
        texts = []
        if a_lead > 0:
            texts += block(lead + question, TOP_Y, 1.0)
            texts = [x[:5] + (a_lead,) for x in texts[:2]] + [x[:5] + (a_q,) for x in texts[2:]]
        else:
            texts += [x[:5] + (a_q,) for x in block(question, TOP_Y - 140, 1.0)]
            j = int((sec - lead_s) // per) if per else len(beats)
            if t < flown and j < len(beats):
                t0 = lead_s + j * per
                texts += block(beats[j], TOP_Y + 40, min(1.0, (sec - t0) * 3, (t0 + per - sec) * 3))
        if t >= flown:
            if crash_at is None:
                crash_at = i
            texts += block([("Crash.", "bold", 110)], TOP_Y + 40, min(1.0, (i - crash_at + 1) / 6))
        texts += counter(min(int(t), flown), 1.0)
        boom = 1.0 if t < flown else 1 + min(1.0, (t - flown) / 10) * 1.6  # the ring opens out at the crash
        ring_a = 1.0 if t < flown else max(0.35, 1 - (t - flown) / 14)
        map_a = min(1.0, max(0.0, (sec - lead_s) * 2))
        add(u, texts, zoom=zoom_full, focus=follow(t), band=(0, PH), free=True, dark=1.0,
            highlight=near(t) if t < flown else None, cue=("inject", float(c0[0])) if i == 0 else None,
            extras=[tag(at(t), r * boom, ring_a, said if t < flown else None),
                    minimap(u.grid, [at(x) for x in range(0, int(min(t, flown)) + 1, 4)], map_a)])
        i += 1

    # what it did, pulling back to the whole board
    n, top = int(3.4 * FPS), [(f"{flown:,} generations, then it hit something.", "bold", 64), (result, "demi", 54)]
    end_focus = follow(flown)
    path = [at(x) for x in range(0, flown + 1, 4)]
    for k in range(n):
        if k % 5 == 0:
            h.goto(u, u.generation + 1)
        q = ease(k / (0.9 * FPS))
        band_h, band_y = round(PH + (BAND_H - PH) * q), round(BAND_Y * q)
        push = FLIGHT_ZOOM + (1 - FLIGHT_ZOOM) * q
        a = min(1.0, (k + 1) / FADE, (n - k) / FADE)
        add(u, block(top, TOP_Y, a), zoom=band_h / BH * push * BW / BAND_W, band=(band_y, band_h), free=True,
            focus=(end_focus[0] + (BW / 2 - end_focus[0]) * q, end_focus[1] + (BH / 2 - end_focus[1]) * q),
            dark=1 - q, cue=("saved",) if k == int(0.9 * FPS) and "gave" in result else None,
            extras=[tag(at(flown), r * 2.6 * (1 - 0.6 * q), 0.35 * (1 - q)), minimap(u.grid, path, 1 - q)])

    # the same universe tonight: still going, and what happens next if nobody comments
    now = h.fresh()
    h.goto(now, h.now_gen)
    F = now.flatline_gens
    if now.phase == "FLATLINING":
        fate = f"It's stuck in a loop: it dies {rd._at(launch, now.deadline_gen, G, '%a %I:%M %p ET')} unless a comment breaks it."
    elif now.phase == "CONTESTED":
        fate = f"A comment broke its loop: it lives if it's still changing {rd._at(launch, now.deadline_gen, G, '%a %I:%M %p ET')}."
    else:
        stuck = now.forecast_stuck(FORECAST_GENS)
        fate = (f"It dies {rd._at(launch, stuck + F, G, '%a %b %d')} unless someone comments." if stuck is not None
                else "Nothing is set to get it stuck for weeks. A comment could change that.")
    bottom = [(fate, "demi", 56), ("It's live on this channel.", "demi", 46)]
    n = int(4.5 * FPS)
    for k in range(n):
        if k % 6 == 0:
            History.advance(now, now.generation + 1)
        a = min(1.0, (k + 1) / FADE)
        add(now, block([("The same universe, right now:", "demi", 54)], TOP_Y - 80, a)
            + block([("Still alive.", "bold", 110)], TOP_Y + 30, a) + block(bottom, BOTTOM_Y, a))

    # what you can comment: yours could fly next
    x, y = render.quiet_spot(now.grid)
    top = [("Yours could fly next.", "bold", 90), ("Comment anything. Like one of these:", "demi", 54)]
    n = 5 * FPS
    for k in range(n):
        if k % 6 == 0:
            History.advance(now, now.generation + 1)
        a = min(1.0, (k + 1) / FADE)
        add(now, block(top, TOP_Y, a) + block([("Your cells could save it. Or kill it.", "demi", 62)], BOTTOM_Y + 24, a),
            extras=[comment_stack(k, n, a, f"{x},{y}")], dim=0.82 * a)
    add_trails(frames, primer)
    return frames


def render_video(state_dir: str, out: str, since_gen: int | None = None, preview: str | None = None,
                 kind: str = "recap") -> str:
    """Render the Short (or, with `since_gen`, the recap since then; with kind="flight",
    the glider Short) to an MP4."""
    ex.SCALE = 1  # explainer's text helpers draw in pixels here
    frames = build_flight(state_dir) if kind == "flight" else None
    if frames is None:
        frames = build(state_dir, since_gen)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if preview:
        os.makedirs(preview, exist_ok=True)
    wav = os.path.splitext(out)[0] + ".wav"
    ex.write_wav(wav, ex.soundtrack(frames))
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
    ap.add_argument("--flight", action="store_true", help="the glider Short: the longest glider flight in the log")
    args = ap.parse_args()
    since = None
    if args.since_hours:
        h = History(args.state_dir)
        since = max(0, h.now_gen - int(args.since_hours * 3600 // h.gen_seconds))
    render_video(args.state_dir, args.out, since, args.preview, "flight" if args.flight else "recap")
    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
