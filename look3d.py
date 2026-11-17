"""The Shorts' 3D look: the same real board, drawn as lit blocks on a tilted floor.

Only the drawing changes. The camera looks at the flat frame's focus with the
board's top edge tipped away (so up on screen is still up on the board, and a
glider heading "ne" still heads up and right), from as far back as the flat
frame's zoom shows. New cells rise out of the floor over a generation's frames and
dying ones sink back into it (add_rise), instead of blinking; the tails
(short.add_trails) glow on the floor. Text, the scrim and rings are the flat
Short's, placed with this camera.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

import commands
import life
import render
import short
from render import HIGHLIGHT, PHASE_COLOR
from universe import DIED

TILT = math.radians(52)  # from straight down
DEPTH_PER_COL = 1.53  # camera distance per board column the flat frame shows across the screen
FPX = 1500.0  # focal length, screen px
HORIZON_Y = 0.47  # the target's height on screen (fraction of PH)
HEIGHT, MARGIN = 0.85, 0.06  # a block's height and its gap to the next, in cells
GLOW, GLOW_BLUR = 0.55, 18
RISE_FRAMES = 8  # a new cell takes at most this many frames to rise (a dying one, to sink)
FLOOR = (40, 46, 62, 120)
BG = (6, 7, 11)


def add_rise(frames: list) -> None:
    """Give each frame of one continuous shot `before` (the board it changed from)
    and `rise` (0..1, how far that change has played): over min(RISE_FRAMES, the
    frames the new board holds) frames. A shot's first frame has nothing to rise from."""
    n, i, before = len(frames), 0, None
    while i < n:
        j = i
        while j + 1 < n and np.array_equal(frames[j + 1].grid, frames[i].grid):
            j += 1
        span = min(RISE_FRAMES, j - i + 1)
        for k in range(i, j + 1):
            frames[k].before = frames[i].grid if before is None else before
            frames[k].rise = 1.0 if before is None else min(1.0, (k - i + 1) / span)
        before, i = frames[i].grid, j + 1


class Camera:
    def __init__(self, f):
        s = f.zoom * short.BAND_W / short.BW  # screen px per board px, as the flat frame
        cols = short.PW / s / short.CELL
        self.tx, self.ty = f.focus[0] / short.CELL, f.focus[1] / short.CELL
        self.d = DEPTH_PER_COL * cols
        self.c = np.array([self.tx, self.ty + self.d * math.sin(TILT), self.d * math.cos(TILT)])
        self.fwd = np.array([0.0, -math.sin(TILT), -math.cos(TILT)])
        self.up = np.array([0.0, -math.cos(TILT), math.sin(TILT)])
        self.cols = cols

    def project(self, pts) -> tuple[np.ndarray, np.ndarray]:
        """World points (N, 3) -> screen (N, 2) and depth (N,)."""
        v = np.asarray(pts, float) - self.c
        z = v @ self.fwd
        zs = np.where(z > 1e-3, z, 1e-3)
        return np.stack([short.PW / 2 + FPX * v[:, 0] / zs, short.PH * HORIZON_Y - FPX * (v @ self.up) / zs], 1), z

    def near(self, x: float, y: float) -> tuple[float, float]:
        """A board position as the copy nearest the target (the board is a torus)."""
        W, H = life.W, life.H
        return self.tx + (x - self.tx + W / 2) % W - W / 2, self.ty + (y - self.ty + H / 2) % H - H / 2


def _colors(f, grid, cells_y, cells_x, before_paint=None) -> np.ndarray:
    base = np.array(PHASE_COLOR[f.phase], float)
    lut = np.array([base] + [np.array(render.PAINT[c], float) for c in commands.COLORS])
    paint = getattr(f, "paint", None)
    k = paint[cells_y, cells_x] if paint is not None else np.zeros(len(cells_y), int)
    if before_paint is not None:
        k = np.where(k > 0, k, before_paint)
    col = lut[k]
    if f.grey:
        col = col * (1 - f.grey) + np.array(PHASE_COLOR[DIED], float) * f.grey
    return col


def compose(f) -> bytes:
    cam = Camera(f)
    PW, PH = short.PW, short.PH
    W, H = life.W, life.H
    img = Image.new("RGB", (PW, PH), BG)
    d = ImageDraw.Draw(img, "RGBA")
    # the window: one whole board around the target (no cell twice), cut to what's in front
    x0, y0 = math.floor(cam.tx - W / 2), math.floor(cam.ty - H / 2)
    xs, ys = np.arange(x0, x0 + W), np.arange(y0, y0 + H)

    # the floor's grid lines, near ones only where they're in front of the camera
    for y in ys[::1]:
        (a, b), z = cam.project([(xs[0], y, 0), (xs[-1] + 1, y, 0)])
        if (z > 1).all():
            d.line([tuple(a), tuple(b)], fill=FLOOR, width=1)
    for x in xs:
        p, z = cam.project([(x, ys[0], 0), (x, ys[-1] + 1, 0)])
        if (z > 1).all():
            d.line([tuple(p[0]), tuple(p[1])], fill=FLOOR, width=1)

    grid = f.grid.astype(bool)
    before = getattr(f, "before", f.grid).astype(bool)
    rise = getattr(f, "rise", 1.0)
    e = short.ease(rise)

    # the tails: glowing floor tiles
    trail = getattr(f, "trail", None)
    if trail is not None and f.grey < 1:
        ta, tc = trail
        lut = np.array([PHASE_COLOR[f.phase]] + [render.PAINT[c] for c in commands.COLORS], float)
        yy, xx = np.nonzero((ta > 0.02) & ~grid & ~before)
        if len(yy):
            wx = cam.tx + (xx - cam.tx + W / 2) % W - W / 2
            wy = cam.ty + (yy - cam.ty + H / 2) % H - H / 2
            corners = np.stack([np.stack([wx + dx, wy + dy, np.full_like(wx, 0.01, float)], 1)
                                for dx, dy in ((.08, .08), (.92, .08), (.92, .92), (.08, .92))], 1)
            pts, z = cam.project(corners.reshape(-1, 3))
            pts, z = pts.reshape(-1, 4, 2), z.reshape(-1, 4)
            for q, zz, a, c in zip(pts, z, ta[yy, xx], lut[tc[yy, xx]]):
                if (zz > 1).all():
                    d.polygon([tuple(p) for p in q], fill=tuple(int(v) for v in c) + (int(200 * a),))

    # the blocks: standing, rising (new) and sinking (dying), far to near
    live_now, born, dying = grid & before, grid & ~before, before & ~grid
    heights = np.zeros(grid.shape)
    heights[live_now] = HEIGHT
    heights[born] = HEIGHT * e
    heights[dying] = HEIGHT * (1 - e)
    yy, xx = np.nonzero(heights > 0.02)
    if len(yy):
        trail_col = trail[1][yy, xx] if trail is not None else None
        col = _colors(f, grid, yy, xx, before_paint=trail_col)
        if f.highlight is not None:
            lit = (f.highlight & f.grid)[yy, xx].astype(bool)
            paint = getattr(f, "paint", None)
            if paint is not None:
                lit &= paint[yy, xx] == 0
            col[lit] = HIGHLIGHT
        hgt = heights[yy, xx]
        wx = cam.tx + (xx - cam.tx + W / 2) % W - W / 2
        wy = cam.ty + (yy - cam.ty + H / 2) % H - H / 2
        _, depth = cam.project(np.stack([wx + 0.5, wy + 0.5, hgt / 2], 1))
        order = np.argsort(-depth)
        tops = Image.new("RGB", (PW, PH), (0, 0, 0))
        td = ImageDraw.Draw(tops)
        for i in order:
            if depth[i] <= 1:
                continue
            X0, X1 = wx[i] + MARGIN, wx[i] + 1 - MARGIN
            Y0, Y1 = wy[i] + MARGIN, wy[i] + 1 - MARGIN
            h = hgt[i]
            side_x = X1 if wx[i] + 0.5 < cam.tx else X0  # the side facing the camera
            pts, _ = cam.project([(X0, Y1, 0), (X1, Y1, 0), (X1, Y1, h), (X0, Y1, h),  # front (nearer edge)
                                  (side_x, Y0, 0), (side_x, Y1, 0), (side_x, Y1, h), (side_x, Y0, h),  # side
                                  (X0, Y0, h), (X1, Y0, h), (X1, Y1, h), (X0, Y1, h)])  # top
            pts = [tuple(p) for p in pts]
            c = col[i]
            d.polygon(pts[4:8], fill=tuple(int(v) for v in c * 0.55))
            d.polygon(pts[0:4], fill=tuple(int(v) for v in c * 0.72))
            top = tuple(int(min(255, v * 1.08 + 12)) for v in c)
            d.polygon(pts[8:12], fill=top)
            td.polygon(pts[8:12], fill=tuple(int(v * min(1.0, h / HEIGHT)) for v in c))
        small = tops.resize((PW // 2, PH // 2), Image.BILINEAR).filter(ImageFilter.GaussianBlur(GLOW_BLUR / 2))
        glow = np.asarray(small.resize((PW, PH), Image.BILINEAR), np.float32) * GLOW
        img = Image.fromarray(np.clip(np.asarray(img, np.float32) + glow, 0, 255).astype(np.uint8))

    canvas = img.convert("RGBA")
    if getattr(f, "scrim", 0):
        canvas = Image.alpha_composite(canvas, short.scrim(f.scrim))
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    def to_canvas(bx, by):  # a board-image point (a cell's centre) on a block's top
        x, y = cam.near(bx / short.CELL - 0.5, by / short.CELL - 0.5)
        p, _ = cam.project([(x + 0.5, y + 0.5, HEIGHT)])
        return float(p[0, 0]), float(p[0, 1])

    for s, weight, size, xy, anchor, alpha in f.texts:
        short.ex.text(ld, xy, s, weight, size, alpha, anchor=anchor)
    for extra in f.extras:
        extra(ld, to_canvas)
    return Image.alpha_composite(canvas, layer).convert("RGB").tobytes()
