"""The live stream's board in 3D (STREAM_LOOK=3d): lit blocks on a floor tipped 25 degrees away.

Only the drawing changes. The same board, colors, tails, lit cells and eraser
outlines as stream.board_rgb, seen by a perspective camera that looks at the
board's centre with its top edge tipped away, so up is still up and the
coordinates read the same way. The whole board shows exactly once (no torus
wrap), fitted inside the stream's board area with room for the numbers on all
four edges. New cells rise out of the floor and dying ones sink back into it
(`rise`), instead of blinking.

View.at() is where a board point lands on the frame: the move labels, ripples
and banners use it, so they land on their cells.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

import commands
import render
from render import BG, GRID, HIGHLIGHT, PHASE_COLOR
from universe import DIED

TILT = 25  # degrees from straight down
DIST = 1.3  # the camera's distance, in board widths: the perspective's strength
AREA = (24, 30, 1478, 870)  # (x0, y0, x1, y1) of the frame the 3D board may use (the column starts at 1500)
PAD = 30  # inside AREA: room for the numbers
HEIGHT, MARGIN = 0.8, 0.12  # a block's height and its gap to the next, in cells (a clear gap: crisp, not blobs)
FRONT, SIDE = 0.55, 0.38  # the front and side faces' shade (the top is lit): contrast keeps the blocks crisp
TRAIL_INSET, TRAIL_DIM = 0.14, 0.75  # a tail's floor tile: its inset in the cell, and its brightness
GLOW, GLOW_BLUR = 0.42, 4  # the bloom under the blocks: its strength, and its blur (at half size); more reads as blur
SCREEN = (5, 6, 9)
EDGE = (42, 46, 58)
MUTED = (125, 131, 145)
GHOST = (150, 156, 172)
XS, YS = 16, 8  # a number every this many columns / rows
SS = 2  # drawn this many times bigger, then shrunk: smooth edges (Pillow's polygons have no anti-aliasing)
EYES = 0.25  # a face on every block: each eye's radius, as a fraction of its top's width (0: none). As the Shorts' creatures
EYE_WHITE, EYE_RIM, PUPIL = (255, 255, 255), (20, 40, 30), (12, 12, 16)


def ease(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


class View:
    """The camera for a board of `shape` (h, w), fitted inside `area`."""

    def __init__(self, shape, area=AREA, tilt: float = TILT, origin=(0, 0), full=None, steps=(None, None)):
        """`origin`, `full`, `steps`: for a window onto a bigger board (the stream's creature cam). This board's
        (0, 0) is the full board's `origin` (x, y), on a board of `full` (h, w) that wraps; the edge numbers
        count the full board's columns and rows, every `steps` (x, y) of them (default XS, YS)."""
        h, w = shape
        self.shape, self.area = shape, area
        self.origin, self.full = origin, tuple(full or shape)
        self.steps = (steps[0] or XS, steps[1] or YS)
        t = math.radians(tilt)
        self.c = np.array([w / 2, h / 2 + DIST * w * math.sin(t), DIST * w * math.cos(t)])
        self.fwd = np.array([0.0, -math.sin(t), -math.cos(t)])
        self.up = np.array([0.0, -math.cos(t), math.sin(t)])
        self.f, self.ox, self.oy = 1.0, 0.0, 0.0
        p = self._raw([(x, y, z) for x in (0, w) for y in (0, h) for z in (0, HEIGHT)])
        (x0, y0), (x1, y1) = p.min(0), p.max(0)
        ax0, ay0, ax1, ay1 = area
        self.f = min((ax1 - ax0 - 2 * PAD) / (x1 - x0), (ay1 - ay0 - 2 * PAD) / (y1 - y0))
        self.ox = (ax0 + ax1) / 2 - self.f * (x0 + x1) / 2
        self.oy = (ay0 + ay1) / 2 - self.f * (y0 + y1) / 2
        self.size = (ax1 - ax0, ay1 - ay0)
        corners = self.at([(0, 0), (w, 0), (w, h), (0, h)])
        self.edge = [tuple(map(float, p)) for p in corners]  # the board's outline: far left, far right, near right, near left
        self.top, self.bottom = float(corners[:, 1].min()), float(corners[:, 1].max())
        self.cx = float(corners[:, 0].mean())
        self._floors: dict[int, Image.Image] = {}

    def _raw(self, pts) -> np.ndarray:
        v = np.asarray(pts, float).reshape(-1, 3) - self.c
        z = v @ self.fwd
        return np.stack([v[:, 0] / z, -(v @ self.up) / z], 1)

    def at(self, pts) -> np.ndarray:
        """Board points (x, y) or (x, y, z), in cells (a cell's centre is +0.5), to frame pixels (N, 2)."""
        pts = np.asarray(pts, float)
        pts = pts.reshape(-1, pts.shape[-1])
        if pts.shape[1] == 2:
            pts = np.concatenate([pts, np.zeros((len(pts), 1))], 1)
        r = self._raw(pts)
        return np.stack([self.ox + self.f * r[:, 0], self.oy + self.f * r[:, 1]], 1)

    def local(self, pts) -> np.ndarray:
        """As at(), on the board's own image (AREA's corner at 0, 0)."""
        return self.at(pts) - np.array(self.area[:2], float)

    def floor(self, s: int = 1) -> Image.Image:
        """The floor and its grid, `s` times bigger: drawn once."""
        if s not in self._floors:
            h, w = self.shape
            img = Image.new("RGB", (self.size[0] * s, self.size[1] * s), SCREEN)
            d = ImageDraw.Draw(img)
            q = [tuple(p) for p in self.local([(0, 0), (w, 0), (w, h), (0, h)]) * s]
            d.polygon(q, fill=BG)
            for y in range(h + 1):
                a, b = self.local([(0, y), (w, y)]) * s
                d.line([tuple(a), tuple(b)], fill=GRID, width=s)
            for x in range(w + 1):
                a, b = self.local([(x, 0), (x, h)]) * s
                d.line([tuple(a), tuple(b)], fill=GRID, width=s)
            d.line(q + q[:1], fill=EDGE, width=s)
            self._floors[s] = img
        return self._floors[s]

    def numbers(self, d, font) -> None:
        """The coordinates along all four edges (drawn at the frame's own size: sharp)."""
        h, w = self.shape
        (ox, oy), (fh, fw), (sx, sy) = self.origin, self.full, self.steps
        small, near = font("regular", 15), font("regular", 17)
        for x in range(w):  # x along the far (top) edge and the near (bottom) edge
            if (x + ox) % sx:
                continue
            a, b = self.local([(x + 0.5, -1.0), (x + 0.5, h + 1.0)])
            d.text(tuple(a), str((x + ox) % fw), font=small, fill=MUTED, anchor="ms")
            d.text(tuple(b), str((x + ox) % fw), font=near, fill=MUTED, anchor="mt")
        for y in range(h):  # y down both sides
            if (y + oy) % sy:
                continue
            a, b = self.local([(-0.9, y + 0.5), (w + 0.9, y + 0.5)])
            d.text(tuple(a), str((y + oy) % fh), font=small, fill=MUTED, anchor="rm")
            d.text(tuple(b), str((y + oy) % fh), font=small, fill=MUTED, anchor="lm")
        d.text(tuple(self.local([(w, -1.0)])[0]), "x", font=small, fill=MUTED, anchor="ls")

    def _bloom(self, scr, col, hgt, order, s: int) -> Image.Image:
        """The glow: each block's top (scr: `s` times bigger), drawn at half the frame's size,
        blurred and brought up to the drawing's size (added in Pillow's C, saturating)."""
        half = (self.size[0] // 2, self.size[1] // 2)
        tops = Image.new("RGB", half, (0, 0, 0))
        td = ImageDraw.Draw(tops)
        for i in order:
            td.polygon([(x / (2 * s), y / (2 * s)) for x, y in scr[i][8:12]],
                       fill=tuple(int(v * GLOW * min(1.0, hgt[i] / HEIGHT)) for v in col[i]))
        return tops.filter(ImageFilter.GaussianBlur(GLOW_BLUR)).resize((self.size[0] * s, self.size[1] * s),
                                                                       Image.BILINEAR)

    def render(self, grid, phase, lit=None, paint=None, trail=None, ghost=None, ghost_a: float = 1.0,
               before=None, rise: float = 1.0, font=None, glow: bool = True, ss: int | None = None) -> np.ndarray:
        """The board's image (AREA's size): as stream.board_rgb, in blocks. With `before`
        (the board it changed from), new cells stand `rise` (0..1) of the way up and
        dying ones as far down. Drawn `ss` (default SS) times bigger and shrunk."""
        h, w = grid.shape
        s = SS if ss is None else ss
        img = self.floor(s).copy()
        d = ImageDraw.Draw(img)
        alive = grid == 1
        was = alive if before is None else before == 1
        e = ease(rise)
        lut = np.array([PHASE_COLOR[phase]] + [render.PAINT[c] for c in commands.COLORS], float)

        def quads(ys, xs, inset, z):  # floor or top squares of cells, (N, 4, 2) on the image
            pts = np.stack([np.stack([xs + dx, ys + dy, np.full(len(xs), z, float)], 1)
                            for dx, dy in ((inset, inset), (1 - inset, inset), (1 - inset, 1 - inset), (inset, 1 - inset))], 1)
            return self.local(pts.reshape(-1, 3)).reshape(-1, 4, 2) * s

        if trail is not None and phase != DIED:  # the tails: floor tiles fading into the floor
            a, col = trail
            ys, xs = np.nonzero((a > 0.004) & ~alive & ~was)
            if len(ys):
                aa = a[ys, xs, None] * TRAIL_DIM
                cols = np.array(BG, float) * (1 - aa) + lut[col[ys, xs]] * aa
                for q, c in zip(quads(ys, xs, TRAIL_INSET, 0.0), cols.astype(int)):
                    d.polygon([tuple(p) for p in q], fill=tuple(c))
        if ghost is not None and ghost_a > 0:  # an eraser's shape, outlined on the floor where it killed
            ink = tuple(int(v) for v in np.array(BG, float) + (np.array(GHOST, float) - BG) * min(1.0, ghost_a))
            ys, xs = np.nonzero((ghost == 1) & ~alive)
            for q in quads(ys, xs, 0.1, 0.0):
                d.line([tuple(p) for p in q] + [tuple(q[0])], fill=ink, width=s)

        heights = np.zeros(grid.shape)
        heights[alive & was] = HEIGHT
        heights[alive & ~was] = HEIGHT * e
        heights[was & ~alive] = HEIGHT * (1 - e)
        ys, xs = np.nonzero(heights > 0.02)
        if not len(ys):
            return self._finish(img, s, font)
        if paint is None or phase == DIED or not paint.any():
            k = np.zeros(len(ys), int)
        else:
            k = paint[ys, xs].astype(int)
        dying = ~alive[ys, xs]
        if trail is not None and dying.any():  # a dying cell keeps the color it had
            k[dying] = trail[1][ys[dying], xs[dying]]
        col = lut[k]
        if lit is not None:
            m = (lit[ys, xs] == 1) & alive[ys, xs]
            if paint is not None and phase != DIED:
                m &= paint[ys, xs] == 0  # a color shows from the moment it lands
            col[m] = HIGHLIGHT
        hgt = heights[ys, xs]
        x0, x1 = xs + MARGIN, xs + 1 - MARGIN
        y0, y1 = ys + MARGIN, ys + 1 - MARGIN
        sx = np.where(xs + 0.5 < w / 2, x1, x0)  # the side facing the camera
        zero = np.zeros(len(xs))
        pts = np.stack([np.stack(p, 1) for p in (
            (x0, y1, zero), (x1, y1, zero), (x1, y1, hgt), (x0, y1, hgt),  # front (the nearer edge)
            (sx, y0, zero), (sx, y1, zero), (sx, y1, hgt), (sx, y0, hgt),  # side
            (x0, y0, hgt), (x1, y0, hgt), (x1, y1, hgt), (x0, y1, hgt))], 1)  # top
        scr = self.local(pts.reshape(-1, 3)).reshape(-1, 12, 2) * s
        v = np.stack([xs + 0.5, ys + 0.5, hgt / 2], 1) - self.c
        order = np.argsort(-(v @ self.fwd))  # far to near
        if EYES:  # two eyes on each top, toward its front edge (the camera looks down at the tops), if big enough to read
            eye_xy = np.stack([self.local(np.stack([x0 + u * (x1 - x0), y0 + 0.6 * (y1 - y0), hgt], 1)) * s
                               for u in (0.27, 0.73)], 1)
            eye_r = EYES * np.linalg.norm(scr[:, 9] - scr[:, 8], axis=1)
            eyed = (hgt > 0.5 * HEIGHT) & (eye_r > 0.75 * s)
        if glow:  # the tops' light, blurred, on the floor first: the blocks go over it and stay sharp
            img = ImageChops.add(img, self._bloom(scr, col, hgt, order, s))
            d = ImageDraw.Draw(img)
        for i in order:
            p = [tuple(q) for q in scr[i]]
            c = col[i]
            d.polygon(p[4:8], fill=tuple(int(v) for v in c * SIDE))
            d.polygon(p[0:4], fill=tuple(int(v) for v in c * FRONT))
            d.polygon(p[8:12], fill=tuple(int(min(255, v * 1.06 + 10)) for v in c))
            if EYES and eyed[i]:
                r = eye_r[i]
                for ex, ey in eye_xy[i]:
                    if phase == DIED:  # a dead universe's creatures: eyes shut
                        d.line((ex - r, ey, ex + r, ey), fill=EYE_RIM, width=max(1, s))
                        continue
                    d.ellipse((ex - r, ey - r * 1.15, ex + r, ey + r * 1.15), fill=EYE_WHITE, outline=EYE_RIM,
                              width=max(1, s // 2))
                    pr = r * 0.62  # the pupil, a little low: looking up at you
                    d.ellipse((ex - pr, ey - pr + r * 0.25, ex + pr, ey + pr + r * 0.25), fill=PUPIL)
        return self._finish(img, s, font)

    def _finish(self, img: Image.Image, s: int, font) -> np.ndarray:
        if s > 1:
            img = img.reduce(s)  # a box filter: each pixel the mean of s x s
        if font is not None:
            self.numbers(ImageDraw.Draw(img), font)
        return np.asarray(img)
