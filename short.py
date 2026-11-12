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
    """The part of the board image on screen: (x0, y0, w, h)."""
    w, h = BW / f.zoom, BH / f.zoom
    fx, fy = f.focus
    return min(max(fx - w / 2, 0), BW - w), min(max(fy - h / 2, 0), BH - h), w, h


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
EXAMPLES = [("hi!", "random", None, "adds a random patch"),
            ("glider {at} se blue", "glider_se", "blue", "adds a blue glider that flies off"),
            ("block {at}", "block", None, "adds a block that just sits"),
            ("blinker {at} cyan", "blinker", "cyan", "adds a cyan blinker that blinks"),
            ("rpent {at} yellow", "rpent", "yellow", "adds a yellow rpent: 5 cells of chaos"),
            ("acorn {at} red", "acorn", "red", "adds a red acorn that grows for ages")]
CYCLE = [EXAMPLES[k] for k in (0, 1, 5, 4)]  # the bubble's turns: hi!, glider, acorn, rpent
CHIP_Y, CHIP_BOX, CHIP_CELL, CHIP_EVERY = 1516, 9, 10, 3  # the bubble's centre, below the captions; a generation every 3 frames
STACK_Y, STACK_SCALE, STACK_GAP = 536, 0.9, 14  # the ending's list: its top, its bubbles' size, the gap between them


def _quote(say: str, at: str) -> str:
    return "\u201c" + say.format(at=at) + "\u201d"


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
        say, pattern, paint, adds = CYCLE[turn % len(CYCLE)]
        say = _quote(say, at)
        film = render.demo(pattern, CHIP_BOX)
        k = min(len(film) - 1, int((i - turn * per) * len(film) / per))
        w = _bubble_width(d, say, adds)
        _bubble(d, int(CX - w / 2), CHIP_Y, w, say, adds, film[k], paint, alpha)
    return draw


def comment_stack(i: int, n: int, alpha: float, at: str):
    """Frame i of the n-frame ending: every example, one under another, their
    films starting together and playing through once."""
    def draw(d, to_canvas):
        s = STACK_SCALE
        rows = [(_quote(say, at), pattern, paint, adds) for say, pattern, paint, adds in EXAMPLES]
        w = max(_bubble_width(d, say, adds, s) for say, _, _, adds in rows)
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
    x0, y0, w, h = view(f)
    resample = Image.NEAREST if f.zoom * BAND_W / BW >= 1 else Image.LANCZOS
    img = board(f)
    if WRAP_DIM < 1:
        # The whole screen, top to bottom, at the band's scale. Rows outside the
        # board wrap around (it is a torus), so this is what really lies beyond its edges.
        k = h / BAND_H  # board px per screen px
        top, bottom = y0 - BAND_Y * k, y0 + (PH - BAND_Y) * k
        r0, r1 = int(np.floor(top)), int(np.ceil(bottom))
        tall = Image.fromarray(np.asarray(img)[np.arange(r0, r1) % BH])
        canvas = tall.resize((PW, PH), resample, box=(x0, top - r0, x0 + w, bottom - r0)).convert("RGBA")
        veil = Image.new("RGBA", (PW, PH), (0, 0, 0, int(255 * WRAP_DIM)))
        veil.paste((0, 0, 0, 0), (0, BAND_Y, PW, BAND_Y + BAND_H))
        canvas = Image.alpha_composite(canvas, veil)
    else:
        canvas = Image.new("RGBA", (PW, PH), (0, 0, 0, 255))
        canvas.paste(img.resize((BAND_W, BAND_H), resample, box=(x0, y0, x0 + w, y0 + h)), (0, BAND_Y))
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    edge = (70, 76, 92, 255)
    d.line((0, BAND_Y - 1, PW, BAND_Y - 1), fill=edge, width=2)
    d.line((0, BAND_Y + BAND_H, PW, BAND_Y + BAND_H), fill=edge, width=2)
    if f.dim:
        d.rectangle((0, BAND_Y, PW, BAND_Y + BAND_H), fill=(5, 6, 9, int(255 * f.dim)))

    def to_canvas(bx, by):
        return (bx - x0) * BAND_W / w, BAND_Y + (by - y0) * BAND_H / h

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
def build(state_dir: str, since_gen: int | None = None) -> list:
    """The whole life so far, or with `since_gen`, a recap of what happened
    since then (the daily Short): its moves, the biggest ones up close, and
    how its expected death moved."""
    h = History(state_dir)
    frames = []
    whole = (BW / 2, BH / 2)

    def add(u, texts=(), zoom=1.0, focus=whole, extras=(), **kw):
        f = ex.Frame(u, texts=texts, hud=False, zoom=zoom, dim=kw.pop("dim", 0.0), extras=extras, **kw)
        f.focus = focus
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
    F = now.flatline_gens
    stuck_now = now.forecast_stuck(FORECAST_GENS)
    stuck_seed = h.fresh().forecast_stuck(FORECAST_GENS)
    moves = sum(len(b) for b in h.batches.values())

    # 1. hook over the live board (its real future, one generation per frame). With
    # HOOK_ZOOM > 1 it opens close up on the patch that changes most, then pulls back.
    # Measured: the teaser (whole board) kept 52.7% of viewers; Day 2 (4x close up)
    # kept 44.5%. So it opens on the whole board again.
    n = int(2.7 * FPS)  # on screen from the very first frame: it is the thumbnail
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
        History.advance(u, u.generation + 1)
        a = 1.0 if i < n - FADE else fade(i, n)
        zoom = Z if i < hold else 1 + (Z - 1) * (1 - ease((i - hold) / (n - 1 - hold)))
        (add_chip if since_gen is not None else add)(
            u, block([("THIS SHORT", "bold", 112), ("DIES", "bold", 196)], TOP_Y, a)
            + block([("if nobody comments", "demi", 80)], BOTTOM_Y, a), zoom=zoom, focus=busy)

    if since_gen is not None:  # the daily recap: day N, the day's moves, how its death moved
        _recap(h, now, since_gen, add, stuck_now, add_chip, end_chips)
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


def _moves(h: History, u: Universe, shown: list[int], head, add, until: int, effect_text):
    """Replay up to `until`, zooming in on each generation in `shown`."""
    lit = None
    seg = max(18, int(2.4 * FPS) // (len(shown) + 1))
    for target in shown + [until]:
        start = u.generation
        for i in range(seg):
            cells = h.goto(u, max(u.generation, start + round((target - start) * ((i + 1) / seg) ** 1.4)))
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
        hold, z = int(2.0 * FPS), 3.2
        for i in range(hold):
            if i > 10 and i % 4 == 0:  # let it be seen where it landed, then let it move
                h.goto(u, u.generation + 1)
            zoom = 1 + (z - 1) * min(ease(i / 14), ease((hold - i) / 14))
            alpha = min(1.0, (i + 1) / 6, (hold - i) / 6)
            cue = ("inject", float(np.mean([c[0] for c in cells]))) if i == 0 else None
            add(u, block(head, TOP_Y, 1.0), highlight=lit, cue=cue, zoom=zoom, focus=centre(piece),
                extras=[ring(piece, alpha, caption)])
    return u


def _recap(h: History, now: Universe, since_gen: int, add, stuck_now, add_chip=None, end_chips=None) -> list:
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
    shown = sorted(sorted(gens, key=lambda g: -_impact(h, g))[:2])
    say = add_chip or add  # the comment bubbles carry on under the moves and the stakes
    _moves(h, u, shown, head, say, h.now_gen, effect_text)

    # the stakes: where its death stands now
    def death_day(stuck):
        return rd._at(launch, stuck + F, G, "%a %b %d")
    if now.phase == "ALIVE" and stuck_now is not None:  # its real future: into the loop, then grey
        top = [("Left alone, it gets stuck in a loop", "bold", 76)]
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


def render_video(state_dir: str, out: str, since_gen: int | None = None, preview: str | None = None) -> str:
    """Render the Short (or, with `since_gen`, the recap since then) to an MP4."""
    ex.SCALE = 1  # explainer's text helpers draw in pixels here
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
    args = ap.parse_args()
    since = None
    if args.since_hours:
        h = History(args.state_dir)
        since = max(0, h.now_gen - int(args.since_hours * 3600 // h.gen_seconds))
    render_video(args.state_dir, args.out, since, args.preview)
    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
