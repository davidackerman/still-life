"""The live stream, drawn tall (720 x 1280) for YouTube's vertical feed (Dual stream).

    python stream.py --tall --preview out/tall.png          # one frame
    STREAM_KEY=<the vertical key> python stream.py --tall    # live, as its own process

The same Screen as the wide stream (stream.py) runs it: the same board, moves,
replays, rewinds and future, so the two never disagree. Only the drawing
differs: the countdown on top, the board across the middle (the wide board,
scaled), then what's happening and how to play, sized for a phone.

It runs as a separate process with its own stream key, so it can fail, stall
or restart without touching the wide stream (deploy/setup-vertical.sh).
"""

from __future__ import annotations

import math
import re

import numpy as np
from PIL import Image, ImageDraw

import commands
import render
import stream as st
from render import PHASE_COLOR
from universe import ALIVE, CONTESTED, DIED, FLATLINING

TW, TH = 720, 1280
M = 24  # side margin
BW = TW - 2 * M  # the board's width: 672 px, 5.25 px a cell
BY = 312  # the board's top
HOW_Y = 985  # how to play starts here; the latest moves fit above it
TEXT = TW - 2 * M
CODE = st.CODE + ("vertical.py",)  # a change to these restarts the tall stream (not the wide one)


def _scale(shape) -> tuple[float, int]:
    """(px per cell, the board's height) on the tall frame."""
    h, w = shape
    s = BW / w
    return s, int(round(h * s))


def _to_tall(px: float, py: float, shape) -> tuple[float, float]:
    """A point on the wide board (stream.geometry) to the tall frame."""
    cell, left, top = st.geometry(shape)
    s = BW / (shape[1] * cell)
    return M + (px - left) * s, BY + (py - top) * s


def _banner_lines(screen, now: float):
    """What the wide stream's banner says right now (rewind, replay, future), or None."""
    shape = screen.snap.u.grid.shape
    got: dict = {}
    screen._banner = lambda d, sh, y0, line1, line2, frac: got.update(line1=line1, line2=line2, frac=frac)
    try:
        if screen.rewind:
            screen._rewind_banner(None, now, shape)
        elif screen.replay:
            screen._replay_banner(None, now, shape, 0)
        elif screen.future:
            screen._future_banner(None, now, shape)
    finally:
        del screen._banner  # back to the class's own
    return got or None


def _chrome(screen, now: float) -> np.ndarray:
    """Everything but the board, the labels and the banner: drawn once a second."""
    snap = screen.panel()
    u = snap.u
    im = Image.new("RGB", (TW, TH), st.SCREEN)
    d = ImageDraw.Draw(im, "RGBA")
    f = st.font
    # the top: live, which universe, the generation
    y = 30
    d.ellipse((M, y + 4, M + 18, y + 22), fill=(240, 60, 60))
    d.text((M + 28, y + 13), "LIVE", font=f("bold", 28), fill=st.WHITE, anchor="lm")
    d.text((M + 112, y + 13), f"universe #{snap.universe_no}", font=f("regular", 24), fill=st.MUTED, anchor="lm")
    d.text((TW - M, y + 13), f"gen {u.generation:,}", font=f("regular", 24), fill=st.MUTED, anchor="rm")
    # the fate, big
    stuck = snap.forecast["stuck_gen"] if snap.forecast and u.phase == ALIVE else None
    big, small = render.headline(u, snap.gen_seconds, stuck, st.FORECAST_GENS if snap.forecast else None, snap.launch)
    small = re.sub(r" · gen [\d,]+$", "", small)
    size = st.fit(d, big, "bold", 68, TEXT, 40)
    d.text((M, 78), big, font=f("bold", size), fill=PHASE_COLOR[u.phase], anchor="lt")
    count = st.countdown_text(snap, now)
    if count:
        d.text((M, 160), count, font=f("bold", 58), fill=st.WHITE, anchor="lt")
    d.text((M, 232), small, font=f("regular", 28), fill=st.LIGHT, anchor="lt")
    if u.phase != DIED:  # the next generation, filling up
        nxt = snap.launch + (u.generation + 1) * snap.gen_seconds
        frac = 1 - min(1.0, max(0.0, (nxt - now) / snap.gen_seconds))
        d.rounded_rectangle((M, 280, TW - M, 285), radius=3, fill=(40, 44, 54))
        d.rounded_rectangle((M, 280, M + max(6, int(TEXT * frac)), 285), radius=3, fill=PHASE_COLOR[u.phase])
    # the board's frame (red-edged while it's in trouble) and where to aim
    shape = u.grid.shape
    cell_px, bh = _scale(shape)
    alarm = u.phase in (FLATLINING, CONTESTED)
    d.rectangle((M - 2, BY - 2, M + BW + 1, BY + bh + 1), outline=PHASE_COLOR[u.phase] if alarm else (42, 46, 58),
                width=3 if alarm else 1)
    for x in range(0, shape[1], 32):
        d.text((M + (x + 0.5) * cell_px, BY + bh + 6), str(x), font=f("regular", 14), fill=st.MUTED, anchor="mt")
    for yy in range(0, shape[0], 16):
        d.text((TW - M + 2, BY + (yy + 0.5) * cell_px), str(yy), font=f("regular", 13), fill=st.MUTED, anchor="lm")
    # what just happened (the banner covers it while the past or the future plays)
    if not (screen.rewind or screen.replay or screen.future):
        _latest(d, snap, now, BY + bh + 44)
    _how(d, u, HOW_Y)
    return np.asarray(im)


def _latest(d, snap, now: float, y: int) -> None:
    f = st.font
    d.text((M, y), "Latest moves", font=f("bold", 28), fill=st.WHITE, anchor="lt")
    m = snap.comments_seen
    if m.get("total"):
        who = f" comments from {snap.people:,} people" if snap.people else " so far"
        d.text((TW - M, y + 6), f"{m['total']:,}{who}", font=f("regular", 22), fill=st.MUTED, anchor="rt")
    y += 44
    for r in snap.recent[:3]:
        if y + 56 > HOW_Y - 34:  # room for both its lines above the rule
            break
        what, _, effect = r.partition(" · ")
        what = re.sub(r"^gen [\d,]+: ", "", what)
        g = render.line_gen(r)
        age = st.ago(now - snap.launch - g * snap.gen_seconds) if g is not None else ""
        d.text((M, y), st.tidy(what), font=f("regular", 24), fill=st.LIGHT, anchor="lt")
        line = st.tidy(effect)
        d.text((M, y + 30), line, font=f("regular", 22), fill=st.effect_color(effect), anchor="lt")
        if age:
            d.text((M + d.textlength(line, font=f("regular", 22)) + 14, y + 30), age, font=f("regular", 22),
                   fill=st.MUTED, anchor="lt")
        y += 68
    if not snap.recent:
        d.text((M, y), "none yet: be the first!", font=f("regular", 24), fill=st.LIGHT, anchor="lt")


def _how(d, u, y: int) -> None:
    """How to play, for someone who has never seen it."""
    f = st.font
    d.line((M, y - 22, TW - M, y - 22), fill=(32, 36, 46), width=1)
    # every line on a baseline ("ls"), so words with and without capitals sit level
    d.text((M, y + 36), "Type in the chat to play", font=f("bold", 38), fill=st.WHITE, anchor="ls")
    d.text((M, y + 84), "Say anything: cells land on the board.", font=f("regular", 26), fill=st.LIGHT, anchor="ls")
    aim = f"Or aim: {render.suggestion(u)}" if u.phase != DIED else "Or aim: glider 64,36 ne"
    d.text((M, y + 120), aim, font=f("regular", 26), fill=st.LIGHT, anchor="ls")
    d.text((M, y + 154), "glider · block · blinker · rpent · acorn · random  x,y", font=f("regular", 22), fill=st.MUTED, anchor="ls")
    # the paints, each in its own color, then black: the eraser
    x, base, f22 = M, y + 194, f("regular", 22)
    d.text((x, base), "add a color: ", font=f22, fill=st.MUTED, anchor="ls")
    x += d.textlength("add a color: ", font=f22)
    for c in commands.COLORS:
        d.text((x, base), c, font=f22, fill=render.PAINT[c], anchor="ls")
        x += d.textlength(c + " ", font=f22)
    base += 38
    tw = d.textlength(commands.ERASE, font=f22)
    d.rounded_rectangle((M, base - 21, M + tw + 12, base + 7), radius=5, fill=st.ERASE_INK, outline=st.CHIP_EDGE)
    d.text((M + 6, base), commands.ERASE, font=f22, fill=st.WHITE, anchor="ls")
    d.text((M + tw + 20, base), "erases instead:  glider 40,20 black", font=f22, fill=st.MUTED, anchor="ls")
    d.text((M, base + 40), f"every move so far: {render.LIVE_ADDRESS}", font=f("regular", 20), fill=st.MUTED,
           anchor="ls")


def _labels(screen, d, now: float, shape, tags, life: float) -> None:
    """The moves' ripples and labels, as the wide stream draws them, at the tall board's size.
    `d` draws on a layer the board's size (0, 0 = the board's corner), so nothing spills off it."""
    cell_px, bh = _scale(shape)
    placed: list[tuple] = []
    fade = min(5.0, life / 3)
    fl = st.font("bold", 20)
    for t in tags:
        age = now - t.t0
        if age >= life:
            continue
        cx, cy = _to_tall(*st.centre_px(t.cells, shape), shape)
        cx, cy = cx - M, cy - BY  # on the board's own layer
        for k in range(3):
            p = (age - k * 0.4) / (st.RIPPLE_S - 0.8)
            if 0 < p < 1:
                r = 5 + p * 70
                d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, int(230 * (1 - p))), width=2)
        label = st.tidy(t.text)
        tw = d.textlength(label, font=fl)
        x0 = min(max(cx - tw / 2 - 8, 2), BW - tw - 18)
        y0 = cy - 38 if cy - 38 > 2 else cy + 12
        y0 = st.free_row(y0, x0, x0 + tw + 16, placed, 2, bh - 28, step=30)
        placed.append((x0, x0 + tw + 16, y0))
        alpha = int(255 * min(1.0, (life - age) / fade))
        d.rounded_rectangle((x0, y0, x0 + tw + 16, y0 + 26), radius=6, fill=(255, 255, 255, alpha))
        d.text((x0 + 8, y0 + 13), label, font=fl, fill=(10, 12, 16, alpha), anchor="lm")


def _banner(d, got: dict, y: int) -> None:
    f1, f2 = st.font("bold", 34), st.font("regular", 24)
    d.rounded_rectangle((M, y, TW - M, y + 128), radius=14, fill=(14, 16, 22), outline=(255, 255, 255, 90))
    size = st.fit(d, got["line1"], "bold", 34, TEXT - 40, 22)
    d.text((TW / 2, y + 38), got["line1"], font=st.font("bold", size), fill=st.WHITE, anchor="mm")
    line2 = got["line2"]
    for part in (line2, line2.rsplit(" · ", 1)[0]):  # the full line, or without its last part if too long
        if d.textlength(part, font=f2) <= TEXT - 40:
            line2 = part
            break
    d.text((TW / 2, y + 78), line2, font=f2, fill=st.LIGHT, anchor="mm")
    frac = min(1.0, max(0.0, got["frac"]))
    d.rounded_rectangle((M + 28, y + 106, TW - M - 28, y + 111), radius=2, fill=(40, 44, 54))
    d.rounded_rectangle((M + 28, y + 106, M + 28 + max(6, int((TEXT - 56) * frac)), y + 111), radius=2, fill=st.WHITE)


def compose(screen, now: float) -> Image.Image:
    """One tall frame of `screen` (a stream.Screen) at `now`."""
    u = screen.snap.u
    shape = u.grid.shape
    panel = screen.panel()
    playing = bool(screen.rewind or screen.replay or screen.future)
    k = (int(now), id(screen.snap), u.generation, u.phase, playing, id(panel), panel.u.generation)
    cache = getattr(screen, "_tall_chrome", None)
    if cache is None or cache[0] != k:
        screen._tall_chrome = cache = (k, _chrome(screen, now))
    img = cache[1].copy()
    b = screen.board(now)
    _, bh = _scale(shape)
    small = Image.fromarray(b).resize((BW, bh), Image.BOX)  # the wide board, scaled: the same cells, tails and glow
    img[BY:BY + bh, M:M + BW] = np.asarray(small)
    im = Image.fromarray(img)
    tags = screen.rewind.tags if screen.rewind else ([] if screen.future else screen.tags)
    life = st.REWIND_TAG_S if screen.rewind else st.TAG_S
    if any(now - t.t0 < life for t in tags):
        layer = Image.new("RGBA", (BW, bh), (0, 0, 0, 0))
        _labels(screen, ImageDraw.Draw(layer, "RGBA"), now, shape, tags, life)
        im.paste(layer, (M, BY), layer)
    d = ImageDraw.Draw(im, "RGBA")
    got = _banner_lines(screen, now) if playing else None
    if got:
        _banner(d, got, BY + bh + 44)
    return im
