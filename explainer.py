#!/usr/bin/env python3
"""Render the explainer video that hosts the universe (about 1:50, 4K, silent).

    python explainer.py                      # 4K: out/explainer.mp4
    python explainer.py --scale 1 --preview out/explainer_frames   # quick 1080p draft

Everything on screen is a real run of universe.Universe: the acorn evolving,
comments injecting cells, the loop that starts the countdown, a comment that
saves it, and (as a "what if") the same universe dying. Needs ffmpeg.

Layout is written in 1920x1080 design units and scaled at render time.
Text durations follow reading speed: subtitle guidelines cap reading at
17 characters/second (Netflix, children's programmes) and 130-160 words/minute
(DCMP). Viewers here also watch a moving picture, so each card gets
len(text) / 12 cps + 1.5 s, plus its fades, and at least 2.5 s.
"""

from __future__ import annotations

import argparse
import copy
import os
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import commands
import life
from render import BG, GRID, HIGHLIGHT, PHASE_COLOR, TITLE_BASE
from universe import ALIVE, CONTESTED, DIED, Universe, flatline_gens

FPS = 30
DW, DH = 1920, 1080  # design units
DCELL = DW // life.W  # 15 design px per cell
SCALE = 2  # set by --scale; 2 -> 3840x2160
LIVE_URL = "davidackerman.github.io/still-life"
FONT_FILE = "/System/Library/Fonts/Avenir Next.ttc"
FACE = {"bold": 0, "demi": 2, "medium": 5, "regular": 7}
CPS, LOOK_S, FADE, MIN_S = 12, 1.5, 12, 2.5  # reading speed, time for the picture, fade frames, minimum


def read_frames(*lines: str) -> int:
    """Frames a card needs: reading time + time to look + fades."""
    chars = sum(len(t) for t in lines)
    return int(round(max(MIN_S, chars / CPS + LOOK_S) * FPS)) + 2 * FADE


_fonts: dict = {}


def font(weight: str, size: float):
    key = (weight, int(size * SCALE))
    if key not in _fonts:
        try:
            _fonts[key] = ImageFont.truetype(FONT_FILE, key[1], index=FACE[weight])
        except OSError:
            _fonts[key] = ImageFont.load_default(size=key[1])
    return _fonts[key]


def S(*v):
    """Design units -> pixels."""
    return [int(round(x * SCALE)) for x in v]


# ---------------------------------------------------------------- frame model
class Frame:
    def __init__(self, u: Universe, highlight=None, texts=(), grey=0.0, hud=True, extras=(), zoom=1.0, dim=0.7):
        self.grid = u.grid.copy()
        self.phase, self.gen = u.phase, u.generation
        self.left = u.gens_left()
        self.died_gen = u.died_gen
        self.highlight = highlight
        self.texts = list(texts)  # (text, weight, size, (x, y), anchor, alpha), design units
        self.grey = grey  # 0..1 desaturation toward the DIED colour
        self.hud = hud
        self.extras = list(extras)  # callables(draw)
        self.zoom = zoom  # >1 magnifies the centre of the grid
        self.dim = dim  # how much the board darkens behind text


def fade(i: int, n: int, edge: int = FADE) -> float:
    return max(0.0, min(1.0, (i + 1) / edge, (n - i) / edge))


def centre_block(lines, alpha, y0):
    out, y = [], y0
    for text, weight, size in lines:
        out.append((text, weight, size, (DW // 2, y), "mm", alpha))
        y += int(size * 1.35)
    return out


# ---------------------------------------------------------------- drawing
_tile = None


def tile():
    global _tile
    if _tile is None:
        c = DCELL * SCALE
        inner = np.zeros((c, c), bool)
        b = max(1, SCALE)
        inner[b:-b, b:-b] = True
        _tile = np.tile(inner, (life.H, life.W))
    return _tile


def board_image(f: Frame) -> np.ndarray:
    c = DCELL * SCALE
    t = tile()
    big = f.grid.repeat(c, 0).repeat(c, 1).astype(bool) & t
    colour = np.array(PHASE_COLOR[f.phase], float)
    if f.grey:
        colour = colour * (1 - f.grey) + np.array(PHASE_COLOR[DIED], float) * f.grey
    img = np.empty(t.shape + (3,), np.uint8)
    img[:] = GRID
    img[t] = BG
    img[big] = colour.astype(np.uint8)
    if f.highlight is not None:
        lit = (f.grid & f.highlight).repeat(c, 0).repeat(c, 1).astype(bool) & t
        img[lit] = HIGHLIGHT
    return img


def text(d, xy, s, weight, size, alpha=1.0, fill=(255, 255, 255), anchor="mm", outline=True):
    kw = {}
    if outline:
        kw = {"stroke_width": max(3, int(size * SCALE) // 16), "stroke_fill": (0, 0, 0, int(255 * alpha))}
    d.text(S(*xy), s, font=font(weight, size), fill=tuple(fill) + (int(255 * alpha),), anchor=anchor, **kw)


def hud(d, f: Frame) -> None:
    d.rectangle(S(0, DH - 120, DW, DH), fill=(0, 0, 0, 190))
    phase = DIED if f.grey >= 1 else f.phase
    text(d, (48, DH - 60), phase, "demi", 72, fill=PHASE_COLOR[phase], anchor="lm", outline=False)
    if phase == DIED:
        right = f"at gen {f.died_gen or f.gen:,}"
    elif f.left is not None:
        hours = f.left * 120 / 3600
        right = f"Gen {f.gen:,} · {hours:.0f}h left" if hours >= 1 else f"Gen {f.gen:,} · <1h left"
    else:
        right = f"Gen {f.gen:,}"
    text(d, (DW - 48, DH - 60), right, "medium", 52, fill=(235, 235, 240), anchor="rm", outline=False)


def compose(f: Frame) -> bytes:
    base = Image.fromarray(board_image(f))
    W, H = base.size
    if f.zoom > 1:
        w, h = W / f.zoom, H / f.zoom
        base = base.resize((W, H), Image.NEAREST, box=((W - w) / 2, (H - h) / 2, (W + w) / 2, (H + h) / 2))
    base = base.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if f.texts:
        a = max(t[5] for t in f.texts)
        d.rectangle(S(0, 0, DW, DH - (120 if f.hud else 0)), fill=(5, 6, 9, int(255 * f.dim * a)))
    if f.hud:
        hud(d, f)
    for s, weight, size, xy, anchor, alpha in f.texts:
        text(d, xy, s, weight, size, alpha, anchor=anchor)
    for extra in f.extras:
        extra(d)
    return Image.alpha_composite(base, layer).convert("RGB").tobytes()


def label(cells, caption, alpha=1.0):
    """A caption beside injected cells, kept inside the frame (design units)."""
    xs, ys = [c[0] for c in cells], [c[1] for c in cells]

    def draw(d):
        f = font("demi", 40)
        w = d.textlength(caption, font=f) / SCALE
        x = (max(xs) + 2) * DCELL
        if x + w + 30 > DW:
            x = min(xs) * DCELL - 2 * DCELL - w
        x = max(30, min(x, DW - w - 30))
        y = max(20, min(min(ys) * DCELL, DH - 200))
        d.rounded_rectangle(S(x - 12, y - 8, x + w + 12, y + 52), radius=10 * SCALE,
                            fill=(255, 255, 255, int(235 * alpha)))
        text(d, (x, y + 22), caption, "demi", 40, alpha, fill=(10, 12, 16), anchor="lm", outline=False)
    return draw


def title_bar(title, alpha):
    def draw(d):
        x0, y0, x1 = 200, 640, DW - 200
        d.rounded_rectangle(S(x0, y0, x1, y0 + 96), radius=16 * SCALE, fill=(255, 255, 255, int(240 * alpha)))
        text(d, ((x0 + x1) // 2, y0 + 48), title, "demi", 36, alpha, fill=(15, 15, 15), outline=False)
    return draw


# ---------------------------------------------------------------- the story
def inject(u: Universe, s: str, cid: str):
    cmd = commands.parse(cid, s)
    u.inject([{"command": cmd.desc, "cells": [list(c) for c in cmd.cells], "run_at": 0}])
    return cmd


def babble_id() -> str:
    """A demo comment ID whose random patch lands in the lower middle of the grid."""
    for i in range(1000):
        cid = f"demo-babble-{i}"
        x, y = map(int, commands.random_patch(cid, "hello universe!").desc.rsplit(" ", 1)[1].split(","))
        if 40 <= x <= 80 and 38 <= y <= 52:
            return cid
    return "demo-babble-0"


def find_saver(u: Universe, deadline: int, keep_alive_gens: int) -> str:
    """A glider that breaks the loop and keeps the universe alive past the deadline."""
    for y in range(4, 68, 8):
        for x in range(4, 124, 8):
            for d in ("se", "sw", "ne", "nw"):
                trial = copy.deepcopy(u)
                inject(trial, f"glider {x},{y} {d}", "probe")
                if trial.phase != CONTESTED:
                    continue
                ev = trial.advance_to(deadline) + trial.advance_to(deadline + keep_alive_gens)
                if [e["reason"] for e in ev if e["type"] == "phase"] == ["survived_deadline"]:
                    return f"glider {x},{y} {d}"
    raise RuntimeError("no saving glider found; adjust the story")


def build() -> list[Frame]:
    frames: list[Frame] = []
    u = Universe(life.mask(life.centered("acorn")), flatline_gens())

    def card(lines, y0, step=None, n=None, **kw):
        """A centred text card held for its reading time; `step(i, n)` moves the universe."""
        n = n or read_frames(*[t for t, _, _ in lines])
        for i in range(n):
            if step:
                step(i, n)
            frames.append(Frame(u, texts=centre_block(lines, fade(i, n), y0), **kw))

    # 1. hook
    card([("This video dies when its universe", "bold", 100), ("gets stuck in a loop.", "bold", 100)],
         DH // 2 - 70, hud=False, dim=0.85)

    # 2. it starts with an acorn: zoom in on the 7 cells
    ZOOM = 5.0
    top, bottom = "It all starts with an acorn:", "seven living cells."
    n = read_frames(top, bottom)
    for i in range(n):
        z = 1 + (ZOOM - 1) * min(1.0, i / 40) ** 0.5
        frames.append(Frame(u, zoom=z, hud=False, dim=0.25, texts=[
            (top, "bold", 72, (DW // 2, 150), "mm", fade(i, n)),
            (bottom, "bold", 72, (DW // 2, 950), "mm", fade(i, n))]))

    # 3. the rule, up close: one generation every half second, in two cards
    for line, size in [("Each generation, every cell counts its 8 neighbours.", 62),
                       ("Born with exactly 3. Survives with 2 or 3. Otherwise it dies.", 60)]:
        n = read_frames(line)
        for i in range(n):
            if i and i % 15 == 0:
                u.advance_to(u.generation + 1)
            frames.append(Frame(u, zoom=ZOOM, hud=False, dim=0.25,
                                texts=[(line, "bold", size, (DW // 2, 960), "mm", fade(i, n))]))

    # 4. pull back as it grows into the whole grid
    lines = [("From seven cells, a whole world.", "bold", 72),
             ("128 × 72 cells · one generation every 2 minutes", "medium", 48)]
    n = read_frames(*[t for t, _, _ in lines])
    for i in range(n):
        u.advance_to(u.generation + 1 + i // 60)
        z = 1 + (ZOOM - 1) * (1 - min(1.0, i / 75)) ** 2
        frames.append(Frame(u, zoom=z, dim=0.45, texts=centre_block(lines, fade(i, n), 470)))

    # 5. thumbnail + title (2 s extra to read the title bar)
    lines = [("The thumbnail is its window.", "bold", 76), ("The title is its pulse.", "bold", 76)]
    n = read_frames(*[t for t, _, _ in lines]) + 2 * FPS
    for i in range(n):
        u.advance_to(u.generation + 1)
        a = fade(i, n)
        frames.append(Frame(u, texts=centre_block(lines, a, 360), extras=[title_bar(
            f"This video dies when its universe gets stuck in a loop · Gen {u.generation:,} · ALIVE", a)]))

    # 6. comments inject cells: three examples, each label up for 3 s
    head = "Every comment adds cells."
    cmds = "glider x,y   block x,y   blinker x,y   rpent x,y   acorn x,y   cells x,y …"
    rest = "anything else: a random 5×5 patch made from your comment"
    n = read_frames(head, cmds, rest)
    gap = (n - 30) // 3
    plan = {30: ("glider 14,40 se", "demo-1"), 30 + gap: ("rpent 96,50", "demo-2"),
            30 + 2 * gap: ("hello universe!", babble_id())}
    lit, labels = None, []
    for i in range(n):
        if i in plan:
            s, cid = plan[i]
            cmd = inject(u, s, cid)
            lit = life.mask(cmd.cells)
            labels = [(i, cmd.cells, s if cmd.kind != "random" else f"“{s}”: a random 5×5 patch")]
        u.advance_to(u.generation + 1)
        a = fade(i, n)
        extras = [label(c, t, min(1.0, (i - s0 + 1) / 8, (s0 + 90 - i) / 8))
                  for s0, c, t in labels if s0 <= i < s0 + 90]
        frames.append(Frame(u, highlight=lit, extras=extras, texts=[
            (head, "bold", 76, (DW // 2, 110), "mm", a), (cmds, "medium", 40, (DW // 2, 190), "mm", a),
            (rest, "medium", 40, (DW // 2, 245), "mm", a)]))

    # 7. stuck in a loop -> FLATLINING
    probe = copy.deepcopy(u)
    while probe.phase == ALIVE:
        probe.advance_to(probe.generation + 1)
    start, target = u.generation, probe.generation
    card([("Left alone, it always ends up", "bold", 76), ("stuck in a loop.", "bold", 76)], 430,
         step=lambda i, n: u.advance_to(start + round((target - start) * ((i + 1) / n) ** 1.6)))
    card([("Then a 24-hour countdown starts.", "bold", 76)], 500, step=lambda i, n: u.advance_to(u.generation + 2))
    flatlined = copy.deepcopy(u)

    # 8. a comment breaks the loop -> CONTESTED -> it lives
    deadline = u.deadline_gen
    tail = (read_frames("…and if it’s still changing when the countdown ends, it lives.") + 3 * FPS
            + 12 * FPS + 60)
    saver = find_saver(u, deadline, tail)
    card([("A comment can break the loop…", "bold", 76)], 500, step=lambda i, n: u.advance_to(u.generation + 1))
    cmd = inject(u, saver, "save")
    lit = life.mask(cmd.cells)
    for i in range(3 * FPS):
        u.advance_to(u.generation + 1)
        frames.append(Frame(u, highlight=lit, extras=[label(cmd.cells, saver, min(1.0, (i + 1) / 8))]))
    start = u.generation
    card([("…and if it’s still changing when", "bold", 70), ("the countdown ends, it lives.", "bold", 70)], 460,
         highlight=lit,
         step=lambda i, n: u.advance_to(start + round((deadline - start) * min(1.0, (i + 1) / max(1, n - 20)))))
    card([("Saved.", "bold", 110)], 500, n=int(MIN_S * FPS) + 2 * FADE, step=lambda i, n: u.advance_to(u.generation + 1))

    # 9. what if nobody had: it dies
    dead = flatlined
    dead.advance_to(deadline)
    lines = [("If nobody saves it, it dies.", "bold", 76), ("An obituary video is uploaded,", "medium", 52),
             ("and this video goes private.", "medium", 52)]
    n = read_frames(*[t for t, _, _ in lines])
    for i in range(n):
        frames.append(Frame(dead, grey=min(1.0, i / 45), texts=centre_block(lines, fade(i, n), 420)))

    # 10. end card over the saved universe: the call to action, held at full reading time
    end = [("Every comment changes this universe.", "bold", 80, 280), ("Some save it. Some kill it.", "bold", 80, 390),
           ("Comment below.", "demi", 56, 560),
           ("x is 0–127 and y is 0–71, from the top-left · one per channel per hour", "medium", 38, 650),
           ("Watch it live, generation by generation:", "medium", 40, 760), (LIVE_URL, "demi", 50, 820)]
    n = min(read_frames(*[t for t, *_ in end]), 12 * FPS)  # reference details; viewers can pause
    for i in range(n):
        u.advance_to(u.generation + 1)
        a = min(1.0, (i + 1) / 15)
        frames.append(Frame(u, texts=[(t, w, s, (DW // 2, y), "mm", a) for t, w, s, y in end]))
    return frames


def main() -> None:
    global SCALE
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=os.path.join("out", "explainer.mp4"))
    ap.add_argument("--scale", type=int, default=2, help="1 = 1080p, 2 = 4K (default)")
    ap.add_argument("--preview", help="also save one frame every 2 s as PNG into this folder")
    args = ap.parse_args()
    SCALE = args.scale
    frames = build()
    W, H = DW * SCALE, DH * SCALE
    print(f"{len(frames)} frames, {len(frames) / FPS:.1f} s, {W}x{H}")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    if args.preview:
        os.makedirs(args.preview, exist_ok=True)
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-movflags", "+faststart", args.out],
        stdin=subprocess.PIPE)
    for i, f in enumerate(frames):
        data = compose(f)
        ff.stdin.write(data)
        if args.preview and i % (2 * FPS) == FPS:
            Image.frombytes("RGB", (W, H), data).save(os.path.join(args.preview, f"{i // FPS:03d}s.png"))
    ff.stdin.close()
    if ff.wait() != 0:
        raise SystemExit("ffmpeg failed")
    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
