#!/usr/bin/env python3
"""Render the explainer video that hosts the universe (about a minute, 1080p, silent).

    python explainer.py [--out out/explainer.mp4] [--preview out/explainer_frames]

Everything on screen is a real run of universe.Universe: the acorn evolving,
comments injecting cells, the repeat that starts the countdown, a comment that
saves it, and (as a "what if") the same universe dying. Needs ffmpeg.
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
from render import BG, GRID, HIGHLIGHT, PHASE_COLOR
from universe import ALIVE, CONTESTED, DIED, FLATLINING, Universe, flatline_gens

CELL = 15  # 128x72 cells -> 1920x1080
FPS = 30
WIDTH, HEIGHT = life.W * CELL, life.H * CELL
LIVE_URL = "davidackerman.github.io/still-life"
FONT_FILE = "/System/Library/Fonts/Avenir Next.ttc"
FACE = {"bold": 0, "demi": 2, "medium": 5, "regular": 7}


def font(weight: str, size: int):
    try:
        return ImageFont.truetype(FONT_FILE, size, index=FACE[weight])
    except OSError:
        return ImageFont.load_default(size=size)


# ---------------------------------------------------------------- frame model
class Frame:
    def __init__(self, u: Universe, highlight=None, texts=(), grey=0.0, hud=True, extras=(), zoom=1.0, dim=0.7):
        self.zoom = zoom  # >1 magnifies the centre of the grid
        self.dim = dim  # how much the board darkens behind text (0..1)
        self.grid = u.grid.copy()
        self.phase, self.gen = u.phase, u.generation
        self.left = u.gens_left()
        self.died_gen = u.died_gen
        self.highlight = highlight
        self.texts = list(texts)  # (text, weight, size, (x, y), anchor, alpha)
        self.grey = grey  # 0..1 desaturation toward the DIED colour
        self.hud = hud
        self.extras = list(extras)  # callables(draw, alpha)


def fade(i: int, n: int, edge: int = 12) -> float:
    """Alpha for frame i of n: fade in and out over `edge` frames."""
    return max(0.0, min(1.0, (i + 1) / edge, (n - i) / edge))


def centre_block(lines, alpha, y0=HEIGHT // 2 - 40):
    """Centred lines: [(text, weight, size), ...] -> text specs."""
    out, y = [], y0
    for text, weight, size in lines:
        out.append((text, weight, size, (WIDTH // 2, y), "mm", alpha))
        y += int(size * 1.35)
    return out


# ---------------------------------------------------------------- drawing
_inner = np.zeros((CELL, CELL), bool)
_inner[1:-1, 1:-1] = True
TILE = np.tile(_inner, (life.H, life.W))


def board_image(f: Frame) -> np.ndarray:
    big = f.grid.repeat(CELL, 0).repeat(CELL, 1).astype(bool) & TILE
    colour = np.array(PHASE_COLOR[f.phase], float)
    if f.grey:
        colour = colour * (1 - f.grey) + np.array(PHASE_COLOR[DIED], float) * f.grey
    img = np.empty((HEIGHT, WIDTH, 3), np.uint8)
    img[:] = GRID
    img[TILE] = BG
    img[big] = colour.astype(np.uint8)
    if f.highlight is not None:
        lit = (f.grid & f.highlight).repeat(CELL, 0).repeat(CELL, 1).astype(bool) & TILE
        img[lit] = HIGHLIGHT
    return img


def hud(draw: ImageDraw.ImageDraw, f: Frame) -> None:
    draw.rectangle([0, HEIGHT - 120, WIDTH, HEIGHT], fill=(0, 0, 0, 190))
    phase = DIED if f.grey >= 1 else f.phase
    draw.text((48, HEIGHT - 60), phase, font=font("demi", 72), fill=PHASE_COLOR[phase] + (255,), anchor="lm")
    if phase == DIED:
        right = f"at gen {f.died_gen or f.gen:,}"
    elif f.left is not None:
        hours = f.left * 120 / 3600
        right = f"Gen {f.gen:,} · {hours:.0f}h left" if hours >= 1 else f"Gen {f.gen:,} · <1h left"
    else:
        right = f"Gen {f.gen:,}"
    draw.text((WIDTH - 48, HEIGHT - 60), right, font=font("medium", 52), fill=(235, 235, 240, 255), anchor="rm")


def compose(f: Frame) -> bytes:
    base = Image.fromarray(board_image(f))
    if f.zoom > 1:
        w, h = WIDTH / f.zoom, HEIGHT / f.zoom
        box = ((WIDTH - w) / 2, (HEIGHT - h) / 2, (WIDTH + w) / 2, (HEIGHT + h) / 2)
        base = base.resize((WIDTH, HEIGHT), Image.NEAREST, box=box)
    base = base.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if f.texts:
        a = max(t[5] for t in f.texts)
        d.rectangle([0, 0, WIDTH, HEIGHT - (120 if f.hud else 0)], fill=(5, 6, 9, int(255 * f.dim * a)))
    if f.hud:
        hud(d, f)
    for text, weight, size, xy, anchor, alpha in f.texts:
        d.text(xy, text, font=font(weight, size), fill=(255, 255, 255, int(255 * alpha)), anchor=anchor,
               stroke_width=max(3, size // 16), stroke_fill=(0, 0, 0, int(255 * alpha)))
    for extra in f.extras:
        extra(d)
    return Image.alpha_composite(base, layer).convert("RGB").tobytes()


def label(cells, text, alpha=1.0):
    """A caption beside injected cells, kept inside the frame."""
    xs, ys = [c[0] for c in cells], [c[1] for c in cells]

    def draw(d):
        f = font("demi", 40)
        w = d.textlength(text, font=f)
        x = (max(xs) + 2) * CELL
        if x + w + 30 > WIDTH:  # no room on the right: put it on the left
            x = min(xs) * CELL - 2 * CELL - w
        x = max(30, min(x, WIDTH - w - 30))
        y = max(20, min(min(ys) * CELL, HEIGHT - 200))
        d.rounded_rectangle([x - 12, y - 8, x + w + 12, y + 52], radius=10, fill=(255, 255, 255, int(235 * alpha)))
        d.text((x, y + 22), text, font=f, fill=(10, 12, 16, int(255 * alpha)), anchor="lm")
    return draw


def title_bar(title, alpha):
    def draw(d):
        x0, y0, x1 = 200, 640, WIDTH - 200
        d.rounded_rectangle([x0, y0, x1, y0 + 96], radius=16, fill=(255, 255, 255, int(240 * alpha)))
        d.text(((x0 + x1) // 2, y0 + 48), title, font=font("demi", 40), fill=(15, 15, 15, int(255 * alpha)),
               anchor="mm")
    return draw


# ---------------------------------------------------------------- the story
def inject(u: Universe, text: str, cid: str):
    cmd = commands.parse(cid, text)
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


def build() -> list[Frame]:
    frames: list[Frame] = []
    u = Universe(life.mask(life.centered("acorn")), flatline_gens())

    # 1. the hook (4 s)
    n = 4 * FPS
    for i in range(n):
        frames.append(Frame(u, texts=centre_block(
            [("This video dies", "bold", 110), ("when its universe repeats itself.", "bold", 110)],
            fade(i, n), y0=HEIGHT // 2 - 110), hud=False, dim=0.85))

    # 2. it starts with an acorn: zoom in on the 7 cells (4 s)
    ZOOM = 5.0
    n = 4 * FPS
    for i in range(n):
        z = 1 + (ZOOM - 1) * min(1.0, i / 40) ** 0.5
        frames.append(Frame(u, zoom=z, hud=False, dim=0.25, texts=[
            ("It all starts with an acorn:", "bold", 72, (WIDTH // 2, 150), "mm", fade(i, n)),
            ("seven living cells.", "bold", 72, (WIDTH // 2, 950), "mm", fade(i, n))]))

    # 3. the rule, watched up close: one generation every half second (6 s)
    n = 6 * FPS
    for i in range(n):
        if i and i % 15 == 0:
            u.advance_to(u.generation + 1)
        a = fade(i, n)
        frames.append(Frame(u, zoom=ZOOM, hud=False, dim=0.25, texts=[
            ("Each generation, every cell counts its 8 neighbours.", "bold", 60, (WIDTH // 2, 130), "mm", a),
            ("Born with exactly 3.  Survives with 2 or 3.  Otherwise it dies.", "demi", 52,
             (WIDTH // 2, 960), "mm", a)]))

    # 4. pull back as it grows into the whole grid (5 s)
    n = 5 * FPS
    for i in range(n):
        u.advance_to(u.generation + 1 + i // 50)
        z = 1 + (ZOOM - 1) * (1 - min(1.0, i / 75)) ** 2
        frames.append(Frame(u, zoom=z, dim=0.45, texts=centre_block(
            [("From seven cells, a whole world.", "bold", 72),
             ("128 × 72 cells, edges wrap around,", "medium", 46),
             ("one generation every 2 minutes, around the clock.", "medium", 46)], fade(i, n), y0=430)))

    # 5. thumbnail + title (6 s)
    n = 6 * FPS
    for i in range(n):
        u.advance_to(u.generation + 1)
        a = fade(i, n)
        frames.append(Frame(u, texts=centre_block(
            [("The thumbnail is its window.", "bold", 76), ("The title is its pulse.", "bold", 76)], a,
            y0=360), extras=[title_bar(f"This video dies when its universe repeats · Gen {u.generation:,} · ALIVE", a)]))

    # 6. comments inject cells (10 s)
    n = 10 * FPS
    plan = {15: ("glider 14,40 se", "demo-1"), 95: ("rpent 96,50", "demo-2"), 175: ("hello universe!", babble_id())}
    lit, labels = None, []
    for i in range(n):
        if i in plan:
            text, cid = plan[i]
            cmd = inject(u, text, cid)
            lit = life.mask(cmd.cells)
            shown = text if cmd.kind != "random" else f"“{text}”: a random 5×5 patch"
            labels = [(i, cmd.cells, shown)]
        u.advance_to(u.generation + 1)
        a = fade(i, n)
        extras = [label(c, t, min(1.0, (i - s + 1) / 8, (s + 80 - i) / 8))
                  for s, c, t in labels if s <= i < s + 80]
        frames.append(Frame(u, highlight=lit, extras=extras, texts=[
            ("Every comment adds cells.", "bold", 76, (WIDTH // 2, 110), "mm", a),
            ("glider x,y   block x,y   blinker x,y   rpent x,y   acorn x,y   cells x,y …", "medium", 40,
             (WIDTH // 2, 190), "mm", a),
            ("anything else becomes a random 5×5 patch made from your comment", "medium", 40,
             (WIDTH // 2, 245), "mm", a)]))

    # 7. it repeats -> FLATLINING (5 s fast-forward + 3 s countdown)
    probe = copy.deepcopy(u)
    while probe.phase == ALIVE:
        probe.advance_to(probe.generation + 1)
    start, target = u.generation, probe.generation
    n = 5 * FPS
    for i in range(n):
        u.advance_to(start + round((target - start) * ((i + 1) / n) ** 1.6))
        frames.append(Frame(u, texts=centre_block(
            [("Left alone, every universe", "bold", 76), ("eventually repeats itself.", "bold", 76)],
            fade(i, n, 15), y0=420)))
    n = 3 * FPS
    for i in range(n):
        u.advance_to(u.generation + 2)
        frames.append(Frame(u, texts=centre_block(
            [("Then a 24-hour countdown starts.", "bold", 76)], fade(i, n), y0=470)))
    flatlined = copy.deepcopy(u)

    # 8. a comment breaks the loop -> CONTESTED -> survives (search for a saving glider)
    deadline = u.deadline_gen
    saver = None
    for y in range(4, 68, 8):
        for x in range(4, 124, 8):
            for d in ("se", "sw", "ne", "nw"):
                trial = copy.deepcopy(u)
                inject(trial, f"glider {x},{y} {d}", "probe")
                if trial.phase != CONTESTED:
                    continue
                ev = trial.advance_to(deadline)
                # ...and stays ALIVE through the "Saved." beat and the end card (12 s, 1 gen/frame)
                ev += trial.advance_to(deadline + 10 * FPS + 30)
                if [e["reason"] for e in ev if e["type"] == "phase"] == ["survived_deadline"]:
                    saver = f"glider {x},{y} {d}"
                    break
            if saver:
                break
        if saver:
            break
    if saver is None:
        raise RuntimeError("no saving glider found; adjust the story")
    n = 3 * FPS
    for i in range(n):
        u.advance_to(u.generation + 1)
        frames.append(Frame(u, texts=centre_block([("A comment can break the loop…", "bold", 76)], fade(i, n),
                                                   y0=470)))
    cmd = inject(u, saver, "save")
    lit = life.mask(cmd.cells)
    n = 4 * FPS
    for i in range(n):
        u.advance_to(u.generation + 1)
        frames.append(Frame(u, highlight=lit, extras=[label(cmd.cells, saver, min(1.0, (i + 1) / 8))]))
    start = u.generation
    n = 4 * FPS
    for i in range(n):
        u.advance_to(start + round((deadline - start) * (i + 1) / n))
        a = fade(i, n) if i < n - 30 else 0.0
        frames.append(Frame(u, highlight=lit, texts=centre_block(
            [("…and if it’s still changing when", "bold", 70), ("the countdown ends, it lives.", "bold", 70)],
            a, y0=430)))
    n = 2 * FPS
    for i in range(n):
        u.advance_to(u.generation + 1)
        frames.append(Frame(u, texts=centre_block([("Saved.", "bold", 110)], fade(i, n), y0=470)))

    # 9. what if nobody had commented: it dies (5 s)
    dead = flatlined
    dead.advance_to(deadline)
    n = 5 * FPS
    for i in range(n):
        g = min(1.0, i / 45)
        frames.append(Frame(dead, grey=g, texts=centre_block(
            [("If nobody saves it, it dies.", "bold", 76),
             ("An obituary video is uploaded,", "medium", 52), ("and this video goes private.", "medium", 52)],
            fade(i, n), y0=400)))

    # 10. end card (8 s) over the live, saved universe
    n = 8 * FPS
    for i in range(n):
        u.advance_to(u.generation + 1)
        a = min(1.0, (i + 1) / 15)
        frames.append(Frame(u, texts=[
            ("Every comment changes this universe.", "bold", 80, (WIDTH // 2, 280), "mm", a),
            ("Some save it. Some kill it.", "bold", 80, (WIDTH // 2, 390), "mm", a),
            ("Comment below.", "demi", 56, (WIDTH // 2, 560), "mm", a),
            ("x is 0–127 and y is 0–71, from the top-left · one per channel per hour", "medium", 38,
             (WIDTH // 2, 650), "mm", a),
            ("Watch it live, generation by generation:", "medium", 40, (WIDTH // 2, 760), "mm", a),
            (LIVE_URL, "demi", 50, (WIDTH // 2, 820), "mm", a)]))
    return frames


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=os.path.join("out", "explainer.mp4"))
    ap.add_argument("--preview", help="also save every 2nd second as PNG into this folder")
    args = ap.parse_args()
    frames = build()
    print(f"{len(frames)} frames, {len(frames) / FPS:.1f} s")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    if args.preview:
        os.makedirs(args.preview, exist_ok=True)
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}",
         "-r", str(FPS), "-i", "-", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
         "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-movflags", "+faststart", args.out],
        stdin=subprocess.PIPE)
    for i, f in enumerate(frames):
        data = compose(f)
        ff.stdin.write(data)
        if args.preview and i % (2 * FPS) == FPS:
            Image.frombytes("RGB", (WIDTH, HEIGHT), data).save(os.path.join(args.preview, f"{i // FPS:03d}s.png"))
    ff.stdin.close()
    if ff.wait() != 0:
        raise SystemExit("ffmpeg failed")
    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
