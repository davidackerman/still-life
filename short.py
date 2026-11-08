#!/usr/bin/env python3
"""Render a vertical Short (1080x1920, about 40 s) from the live universe.

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
    colour = np.array(PHASE_COLOR[f.phase], float)
    if f.grey:
        colour = colour * (1 - f.grey) + np.array(PHASE_COLOR[DIED], float) * f.grey
    img = np.empty(_tile.shape + (3,), np.uint8)
    img[:] = GRID
    img[_tile] = BG
    img[big] = colour.astype(np.uint8)
    if f.highlight is not None:
        lit = (f.grid & f.highlight).repeat(CELL, 0).repeat(CELL, 1).astype(bool) & _tile
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
        if caption:
            f = ex.font("demi", 50)
            w = d.textlength(caption, font=f)
            top = BOTTOM_Y - 40
            d.line((cx, cy + r, CX, top), fill=(255, 255, 255, a), width=4)
            d.rounded_rectangle((CX - w / 2 - 22, top, CX + w / 2 + 22, BOTTOM_Y + 40), radius=16,
                                fill=(255, 255, 255, int(240 * alpha)))
            ex.text(d, (CX, BOTTOM_Y), caption, "demi", 50, alpha, fill=(10, 12, 16), anchor="mm", outline=False)
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
        for e in log:
            if e["type"] == "injection":
                self.batches.setdefault(e["gen"], []).append(e)
        ticks = [e for e in log if e["type"] == "tick"]
        self.now_gen = ticks[-1]["gen_end"] if ticks else 0
        self.now_hash = ticks[-1]["state_hash"] if ticks else None
        self.gen_seconds = g["gen_seconds"]
        self.applied: set[int] = set()

    def fresh(self) -> Universe:
        self.applied = set()
        return Universe(life.mask(life.centered(self.genesis["seed"])), self.genesis["flatline_gens"])

    def goto(self, u: Universe, target: int):
        """Step to `target`, applying every logged injection at its generation.
        Returns the cells injected on the way (for highlighting)."""
        cells = []
        while True:
            due = [g for g in self.batches if g not in self.applied and u.generation <= g <= target]
            if not due:
                break
            g = min(due)
            u.advance_to(g)
            batch = self.batches[g]
            u.inject([{"command": e["command"], "cells": e["cells"], "run_at": e["run_at"]} for e in batch])
            self.applied.add(g)
            cells += [tuple(c) for e in batch for c in e["cells"]]
        u.advance_to(target)
        return cells


def days(gens: int, gen_seconds: int) -> int:
    return int(gens * gen_seconds // 86400)


# ---------------------------------------------------------------- the story
def build(state_dir: str) -> list:
    h = History(state_dir)
    frames = []
    whole = (BW / 2, BH / 2)

    def add(u, texts=(), zoom=1.0, focus=whole, extras=(), **kw):
        f = ex.Frame(u, texts=texts, hud=False, zoom=zoom, dim=kw.pop("dim", 0.0), extras=extras, **kw)
        f.focus = focus
        frames.append(f)

    # where things stand now, and the forecasts
    now = h.fresh()
    h.goto(now, h.now_gen)
    if h.now_hash and now.hash.hex() != h.now_hash:
        raise SystemExit("replaying the log did not reproduce the current board; run replay.py")
    F = now.flatline_gens
    stuck_now = now.forecast_stuck(FORECAST_GENS)
    stuck_seed = h.fresh().forecast_stuck(FORECAST_GENS)
    moves = sum(len(b) for b in h.batches.values())

    # 1. hook over the live board (its real future, one generation per frame)
    u = copy.deepcopy(now)
    n = int(2.7 * FPS)  # on screen from the very first frame: it is the thumbnail
    for i in range(n):
        u.advance_to(u.generation + 1)
        a = 1.0 if i < n - FADE else fade(i, n)
        add(u, block([("THIS SHORT", "bold", 112), ("DIES", "bold", 196)], TOP_Y, a)
            + block([("if nobody comments", "demi", 80)], BOTTOM_Y, a))

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

    # 6. the future if nobody comments: stuck, countdown, death
    fut = copy.deepcopy(now)
    if stuck_now is not None:
        text = "Left alone, it gets stuck in a loop."
        n = rf(text)
        start = fut.generation
        for i in range(n):
            p = min(1.0, (i + 1) / (n * 0.55))
            if fut.generation < stuck_now:  # ease out into the loop, then one generation per 2 frames
                fut.advance_to(start + round((stuck_now - start) * (1 - (1 - p) ** 2)))
            elif i % 2 == 0:
                fut.advance_to(fut.generation + 1)
            add(fut, block([(text, "bold", 80)], TOP_Y, fade(i, n)))
        top = [("Then it has 24 hours.", "bold", 88)]
        bottom = [("If nobody saves it, this Short goes private. For good.", "demi", 62)]
        n = rf(*[s for s, _, _ in top + bottom])
        for i in range(n):
            if i % 2 == 0:
                fut.advance_to(fut.generation + 1)
            add(fut, block(top, TOP_Y, fade(i, n)) + block(bottom, BOTTOM_Y, fade(i, n)),
                grey=max(0.0, (i - n * 0.55) / (n * 0.3)))

    # 7. the call to action over the present board, with the suggested spot marked
    u = copy.deepcopy(now)
    pick = render.suggestion(u)
    spot = main_piece([tuple(c) for c in commands.parse("suggestion", pick).cells])
    top = [("Comment anything to add cells.", "bold", 84)]
    bottom = [(f"or aim one: {pick}", "demi", 62), ("Live countdown: the main video on this channel", "demi", 50)]
    n = max(rf(*[s for s, _, _ in top + bottom]), 5 * FPS)
    for i in range(n):
        if i % 6 == 0:
            u.advance_to(u.generation + 1)
        a = min(1.0, (i + 1) / FADE)
        add(u, block(top, TOP_Y, a) + block(bottom, BOTTOM_Y - 20, a),
            extras=[ring(spot, a * 0.8, dashed=True), arrow_down(CX, 1470, a)])
    return frames


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--out", default=os.path.join("out", "short.mp4"))
    ap.add_argument("--preview", help="also save one frame per second as PNG into this folder")
    args = ap.parse_args()
    ex.SCALE = 1  # explainer's text helpers draw in pixels here
    frames = build(args.state_dir)
    print(f"{len(frames)} frames, {len(frames) / FPS:.1f} s, {PW}x{PH}")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    if args.preview:
        os.makedirs(args.preview, exist_ok=True)
    wav = os.path.splitext(args.out)[0] + ".wav"
    ex.write_wav(wav, ex.soundtrack(frames))
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{PW}x{PH}",
         "-r", str(FPS), "-i", "-", "-i", wav, "-shortest",
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", args.out],
        stdin=subprocess.PIPE)
    for i, f in enumerate(frames):
        data = compose(f)
        ff.stdin.write(data)
        if args.preview and i % FPS == FPS // 2:
            Image.frombytes("RGB", (PW, PH), data).save(os.path.join(args.preview, f"{i // FPS:03d}s.png"))
    ff.stdin.close()
    if ff.wait() != 0:
        raise SystemExit("ffmpeg failed")
    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
