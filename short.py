#!/usr/bin/env python3
"""Render a vertical Short (1080x1920, about half a minute) from the live universe.

    python short.py                                   # out/short.mp4 from state/
    python short.py --preview out/short_frames        # also save a PNG every second

Everything on screen is real: the seed, every logged comment at the generation
it landed, the board as it stands now, and its future if nobody comments (the
same forecast the title uses). Re-render whenever there is news, e.g. when the
universe gets stuck and the 24-hour countdown starts.

The board is shown rotated 90 degrees so that its 128 x 72 cells fill a phone
screen at 15 px each; a small caption says so.

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

import explainer as ex
import life
import render
from render import BG, GRID, HIGHLIGHT, PHASE_COLOR
from store import read_log
from universe import ALIVE, DIED, FLATLINING, Universe

PW, PH = 1080, 1920
CELL = PH // life.W  # 15: the rotated board (72 wide, 128 tall) fills the screen exactly
FPS = ex.FPS
CX = 500  # text centre: the Shorts buttons cover the right edge
TEXT_W = 880  # widest text line
FORECAST_GENS = 30000
# Shorts viewers read fast: 17 characters/s (the top of the subtitle guidelines
# explainer.py cites), less time to look, shorter fades.
CPS, LOOK_S, MIN_S, FADE = 17, 0.8, 1.8, 8


def rf(*lines: str) -> int:
    return int(round(max(MIN_S, sum(map(len, lines)) / CPS + LOOK_S) * FPS)) + 2 * FADE


def fade(i: int, n: int) -> float:
    return ex.fade(i, n, FADE)


# ---------------------------------------------------------------- geometry
def to_screen(x: float, y: float) -> tuple[float, float]:
    """Grid (x, y) -> screen pixels of the cell centre, for the clockwise rotation below."""
    return (life.H - 1 - y + 0.5) * CELL, (x + 0.5) * CELL


_tile = None


def board(f) -> Image.Image:
    global _tile
    g = np.rot90(f.grid, -1)  # clockwise: grid x runs down the screen
    if _tile is None:
        inner = np.zeros((CELL, CELL), bool)
        inner[1:-1, 1:-1] = True
        _tile = np.tile(inner, g.shape)
    big = g.repeat(CELL, 0).repeat(CELL, 1).astype(bool) & _tile
    colour = np.array(PHASE_COLOR[f.phase], float)
    if f.grey:
        colour = colour * (1 - f.grey) + np.array(PHASE_COLOR[DIED], float) * f.grey
    img = np.empty(_tile.shape + (3,), np.uint8)
    img[:] = GRID
    img[_tile] = BG
    img[big] = colour.astype(np.uint8)
    if f.highlight is not None:
        lit = np.rot90(f.grid & f.highlight, -1).repeat(CELL, 0).repeat(CELL, 1).astype(bool) & _tile
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
    return out + [line] if line else out


def block(lines, y0: int, alpha: float):
    """[(text, weight, size)] -> explainer-style text tuples, wrapped and centred on CX."""
    out, y = [], y0
    for s, weight, size in lines:
        for ln in wrap(s, weight, size):
            out.append((ln, weight, size, (CX, y), "mm", alpha))
            y += int(size * 1.3)
        y += int(size * 0.25)
    return out


def tag(cells, caption: str, alpha: float, y: int = 560):
    """A ring around freshly injected cells, and a caption at a fixed spot joined to it."""
    x0, y0 = cells[0]  # a pattern can straddle the torus edge: unwrap it around its first cell
    unwrapped = [(x0 + (x - x0 + life.W // 2) % life.W - life.W // 2,
                  y0 + (y - y0 + life.H // 2) % life.H - life.H // 2) for x, y in cells]
    pts = np.array([to_screen(x, y) for x, y in unwrapped])
    cx, cy = pts.mean(axis=0)
    r = np.hypot(*(pts - (cx, cy)).T).max() + 34

    def draw(d):
        a = int(255 * alpha)
        f = ex.font("demi", 50)
        w = d.textlength(caption, font=f)
        x0 = CX - w / 2
        top, bottom = y - 40, y + 40
        ring_y = cy - r if cy > bottom else cy + r  # the side of the ring facing the caption
        d.line((CX, bottom if cy > bottom else top, cx, ring_y), fill=(255, 255, 255, a), width=4)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, a), width=6)
        d.rounded_rectangle((x0 - 22, top, x0 + w + 22, bottom), radius=16, fill=(255, 255, 255, int(240 * alpha)))
        ex.text(d, (CX, y), caption, "demi", 50, alpha, fill=(10, 12, 16), anchor="mm", outline=False)
    return draw


def arrow_down(x: int, y: int, alpha: float):
    def draw(d):
        a = int(255 * alpha)
        d.rectangle((x - 9, y - 70, x + 9, y), fill=(255, 255, 255, a))
        d.polygon([(x - 34, y - 8), (x + 34, y - 8), (x, y + 36)], fill=(255, 255, 255, a))
    return draw


def compose(f) -> bytes:
    base = board(f)
    if f.zoom > 1:
        fx, fy = f.focus
        w, h = PW / f.zoom, PH / f.zoom
        x0 = min(max(fx - w / 2, 0), PW - w)
        y0 = min(max(fy - h / 2, 0), PH - h)
        base = base.resize((PW, PH), Image.NEAREST, box=(x0, y0, x0 + w, y0 + h))
    base = base.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if f.texts:
        a = max(t[5] for t in f.texts)
        d.rectangle((0, 0, PW, PH), fill=(5, 6, 9, int(255 * f.dim * a)))
    for s, weight, size, xy, anchor, alpha in f.texts:
        ex.text(d, xy, s, weight, size, alpha, anchor=anchor)
    for extra in f.extras:
        extra(d)
    return Image.alpha_composite(base, layer).convert("RGB").tobytes()


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

    def add(u, texts=(), **kw):
        focus = kw.pop("focus", (PW / 2, PH / 2))
        f = ex.Frame(u, texts=texts, hud=False, **kw)
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
    hook = [("THIS SHORT", "bold", 124), ("DIES", "bold", 210), ("if nobody comments", "demi", 80)]
    u = copy.deepcopy(now)
    n = int(2.7 * FPS)  # on screen from the very first frame: it is the thumbnail
    for i in range(n):
        u.advance_to(u.generation + 1)
        add(u, block(hook, 700, 1.0 if i < n - FADE else fade(i, n)), dim=0.55)

    # 2. the acorn, up close
    u = h.fresh()
    acorn = life.centered(h.genesis["seed"])
    focus = (np.mean([to_screen(x, y)[0] for x, y in acorn]), np.mean([to_screen(x, y)[1] for x, y in acorn]))
    lines = [("It started as 7 cells.", "bold", 88)]
    n = int(2.5 * FPS)
    for i in range(n):
        if i > n // 2 and i % 6 == 0:
            h.goto(u, u.generation + 1)
        add(u, block(lines, 420, fade(i, n)), zoom=6.0, focus=focus, dim=0.2)

    # 3. pull back as it grows
    lines = [("Conway's Game of Life.", "bold", 80), ("1 generation every 2 minutes.", "demi", 62),
             ("(shown sideways to fit your phone)", "medium", 40)]
    n = rf(*[s for s, _, _ in lines])
    for i in range(n):
        h.goto(u, u.generation + 1 + i // 30)
        z = 1 + 5.0 * (1 - min(1.0, i / 60)) ** 2
        add(u, block(lines, 700, fade(i, n)), zoom=z, focus=focus, dim=0.45)

    # 4. the real history, slowing down for each comment
    head = [("Every comment adds cells.", "bold", 84)]
    shown = sorted(h.batches)[-4:]  # name the latest few; earlier ones still land and light up
    lit = None
    seg_frames = max(24, int(4.5 * FPS) // (len(shown) + 1))
    targets = shown + [h.now_gen]
    for k, target in enumerate(targets):
        start = u.generation
        for i in range(seg_frames):
            cells = h.goto(u, start + round((target - start) * ((i + 1) / seg_frames) ** 1.4))
            if cells:
                lit = life.mask(cells)
            add(u, block(head, 330, 1.0), highlight=lit, dim=0.25)
        if target in shown:
            batch = h.batches[target]
            cells = [tuple(c) for e in batch for c in e["cells"]]
            lit = life.mask(cells)
            caption = f"a comment: {batch[-1]['command']}" + (f" (+{len(batch) - 1} more)" if len(batch) > 1 else "")
            hold = int(1.8 * FPS)
            for i in range(hold):
                if i and i % 3 == 0:
                    h.goto(u, u.generation + 1)
                cue = ("inject", float(np.mean([c[0] for c in cells]))) if i == 0 else None
                add(u, block(head, 330, 1.0), highlight=lit, cue=cue, dim=0.25,
                    extras=[tag(cells, caption, min(1.0, (i + 1) / 6, (hold - i) / 6))])

    # 5. what the comments did
    if stuck_seed is not None and moves:
        before = days(stuck_seed + F, h.gen_seconds)
        after = (f"{days(stuck_now + F, h.gen_seconds)}" if stuck_now is not None
                 else f"{days(FORECAST_GENS + F, h.gen_seconds)}+")
        what = "stretched" if stuck_now is None or stuck_now > stuck_seed else "changed"
        noun = "comment" if moves == 1 else "comments"
        lines = [(f"{moves} {noun} so far {what} its life from {before} days to {after}.", "bold", 80)]
        n = rf(lines[0][0])
        for i in range(n):
            if i % 4 == 0:
                h.goto(u, u.generation + 1)
            add(u, block(lines, 760, fade(i, n)), dim=0.6)

    # 6. the future if nobody comments: stuck, countdown, death
    fut = copy.deepcopy(now)
    if stuck_now is not None:
        lines = [("Left alone, it gets stuck in a loop.", "bold", 80)]
        n = rf(lines[0][0])
        start = fut.generation
        for i in range(n):
            p = min(1.0, (i + 1) / (n * 0.55))
            if fut.generation < stuck_now:  # ease out into the loop, then one generation per 2 frames
                fut.advance_to(start + round((stuck_now - start) * (1 - (1 - p) ** 2)))
            elif i % 2 == 0:
                fut.advance_to(fut.generation + 1)
            add(fut, block(lines, 760, fade(i, n)), dim=0.5)
        lines = [("Then it has 24 hours.", "bold", 84),
                 ("If nobody saves it, this Short goes private. For good.", "demi", 62)]
        n = rf(*[s for s, _, _ in lines])
        for i in range(n):
            if i % 2 == 0:
                fut.advance_to(fut.generation + 1)
            add(fut, block(lines, 700, fade(i, n)), dim=0.55, grey=max(0.0, (i - n * 0.55) / (n * 0.3)))

    # 7. the call to action over the present board
    u = copy.deepcopy(now)
    pick = render.suggestion(u)
    lines = [("Comment anything to add cells.", "bold", 84), (f"or aim one: {pick}", "demi", 62)]
    small = [("Live countdown: the linked video", "demi", 52)]
    n = max(rf(*[s for s, _, _ in lines + small]), 5 * FPS)
    for i in range(n):
        if i % 6 == 0:
            u.advance_to(u.generation + 1)
        a = min(1.0, (i + 1) / FADE)
        add(u, block(lines, 640, a) + block(small, 1180, a), dim=0.55,
            extras=[arrow_down(CX, 1330, a)])
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
