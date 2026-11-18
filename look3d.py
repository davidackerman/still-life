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
BOUNCE = 1.2  # a new block overshoots its height a little and settles (0: no bounce)
TILE_UP, TILE_DOWN, TILE_SIDE = 5, 1, 1  # copies of the board drawn beyond it (it's a torus): the screen fills
FOG_NEAR, FOG_FAR = 1.15, 3.2  # copies fade into the dark between these depths (times the camera's distance)
BACKING = 0.62  # how dark the soft patch behind the words is
BEVEL, FOOT = 0.14, 0.35  # the top's bevel (of a block's width); the faces' darker foot (of its height)
SHADOW, SHADOW_BLUR = 0.75, 5  # contact shadows: how dark under a block, how soft (px, at half size)
FLOOR = (40, 46, 62, 120)
BG = (6, 7, 14)
PASTEL, GLOSS, EDGE = 0.0, 0, 70  # mixed toward white; a shine on each top (alpha); the bright far edge (alpha)
TRAILS = True  # the tails glowing on the floor (on a light floor they read as smudges)
REFLECT = 0.6  # a glossy floor: each block mirrored in it, this bright (0: a matte floor)
SHADES = (1.12, 14, 0.92, 0.74, 0.52, 0.55, 0.38)  # top (x, +), bevel, front, its foot, side, its foot
TEXT_FILL, TEXT_OUTLINE, BACKING_RGB, SHADOW_RGB = (255, 255, 255), True, (0, 0, 0), (0, 0, 0)
STYLES = {
    "night": {},  # lit blocks on a dark slate floor, glowing (as the live stream)
    "clay": dict(REFLECT=0.0, BG=(247, 238, 228), GROUND=(229, 215, 199), FLOOR=(120, 96, 84, 34), GLOW=0.0, PASTEL=0.42, EDGE=55,
                 SHADES=(1.0, 6, 0.93, 0.86, 0.75, 0.74, 0.64), SHADOW=0.42, SHADOW_BLUR=6, SHADOW_RGB=(92, 70, 66),
                 TEXT_FILL=(48, 40, 54), TEXT_OUTLINE=False, BACKING=0.55, BACKING_RGB=(250, 244, 236), TRAILS=False),
    "candy": dict(REFLECT=0.0, BG=(246, 233, 242), GROUND=(214, 200, 232), FLOOR=(110, 90, 140, 34), GLOW=0.0, PASTEL=0.12, GLOSS=170,
                  EDGE=120, SHADES=(1.04, 10, 0.9, 0.82, 0.7, 0.68, 0.58), SHADOW=0.45, SHADOW_BLUR=6,
                  SHADOW_RGB=(80, 58, 96), TEXT_FILL=(52, 38, 66), TEXT_OUTLINE=False, BACKING=0.55,
                  BACKING_RGB=(250, 242, 248), TRAILS=False),
}
_NIGHT = None


def use(style: str) -> None:
    """Draw from now on in one of STYLES (night: the default)."""
    global _NIGHT, _GROUND, _HORIZON
    g = globals()
    if _NIGHT is None:
        _NIGHT = {k: g[k] for k in set().union(*STYLES.values())}
    g.update(_NIGHT)
    g.update(STYLES[style])
    _GROUND = _HORIZON = None


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
    # how long ago each cell was born (it wobbles like jelly for a while) or died (it pops)
    born = died = None
    prev = None
    for k, f in enumerate(frames):
        g = f.grid.astype(bool)
        if born is None:
            born = np.full(g.shape, -10_000, np.int32)
            died = np.full(g.shape, -10_000, np.int32)
        elif prev is not None and not np.array_equal(g, prev):
            born[g & ~prev] = k
            died[prev & ~g] = k
        prev = g
        f.age = np.clip(k - born, 0, 255).astype(np.uint8)
        f.gone = np.clip(k - died, 0, 255).astype(np.uint8)


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


WOBBLE_TAU, WOBBLE_PERIOD = 4.0, 9.0  # frames: a new block springs up, overshoots and wobbles like jelly
POP_FRAMES = 5  # a dying block puffs up, brightens and pops over this many frames


def spring(age) -> np.ndarray:
    """A new block's height (times its full height) `age` frames after it's born: up from
    nothing past full, back, and settling, like jelly."""
    a = np.asarray(age, float)
    return 1 - np.exp(-a / WOBBLE_TAU) * np.cos(2 * np.pi * a / WOBBLE_PERIOD)


def bounce(t: float) -> float:
    """0 -> 1, overshooting a little before it settles (ease-out-back)."""
    t = min(1.0, max(0.0, t)) - 1
    return 1 + (BOUNCE + 1) * t ** 3 + BOUNCE * t ** 2


def _copies(cam, xx, yy):
    """Every copy of the cells (xx, yy) worth drawing: the one nearest the target, and
    the board repeated around it (the universe is a torus), as world x, y and copy index."""
    W, H = life.W, life.H
    wx = cam.tx + (xx - cam.tx + W / 2) % W - W / 2
    wy = cam.ty + (yy - cam.ty + H / 2) % H - H / 2
    offs = [(kx * W, ky * H) for ky in range(-TILE_UP, TILE_DOWN + 1) for kx in range(-TILE_SIDE, TILE_SIDE + 1)]
    ox = np.repeat([o[0] for o in offs], len(xx))
    oy = np.repeat([o[1] for o in offs], len(xx))
    idx = np.tile(np.arange(len(xx)), len(offs))
    return np.tile(wx, len(offs)) + ox, np.tile(wy, len(offs)) + oy, idx


def _fog(cam, depth) -> np.ndarray:
    """1 near the target, fading to 0 into the distance."""
    return np.clip((FOG_FAR * cam.d - depth) / ((FOG_FAR - FOG_NEAR) * cam.d), 0, 1)


def _on_screen(pts, margin=60) -> np.ndarray:
    x, y = pts[..., 0], pts[..., 1]
    return (x > -margin) & (x < short.PW + margin) & (y > -margin) & (y < short.PH + margin)


def _backing(texts) -> Image.Image | None:
    """A soft dark patch behind each line of words, so they read over the blocks."""
    if not texts:
        return None
    k = 4
    m = Image.new("L", (short.PW // k, short.PH // k), 0)
    d = ImageDraw.Draw(m)
    for s, weight, size, xy, anchor, alpha in texts:
        if alpha <= 0.01:
            continue
        tw = short.ex.font(weight, size).getlength(s)
        w = min(short.PW, tw + 1.6 * size)
        x = xy[0] if anchor[0] == "m" else xy[0] + tw / 2 if anchor[0] == "l" else xy[0] - tw / 2
        y = xy[1] if anchor[1] == "m" else xy[1] + size * 0.5 if anchor[1] in "ta" else xy[1] - size * 0.5
        a = int(255 * BACKING * min(1.0, alpha))
        d.rectangle(((x - w / 2) / k, (y - size * 0.95) / k, (x + w / 2) / k, (y + size * 0.95) / k), fill=a)
    m = m.filter(ImageFilter.GaussianBlur(14)).resize((short.PW, short.PH), Image.BILINEAR)
    out = Image.new("RGBA", (short.PW, short.PH), BACKING_RGB + (0,))
    out.putalpha(m)
    return out


_GROUND = None
GROUND = (17, 21, 38)  # the floor up close: a deep navy, so shadows and reflections show on it


def _ground() -> Image.Image:
    """The floor's shade: the dark of the distance at the top of the screen, slate up close."""
    global _GROUND
    if _GROUND is None:
        t = np.clip((np.arange(short.PH) - short.PH * 0.12) / (short.PH * 0.75), 0, 1) ** 1.3
        col = np.array(BG, float)[None] + (np.array(GROUND, float) - np.array(BG, float))[None] * t[:, None]
        _GROUND = Image.fromarray(np.repeat(col[:, None], short.PW, 1).astype(np.uint8), "RGB")
    return _GROUND


_HORIZON = None
HORIZON_FADE = 340  # px at the top of the screen where the far copies fade into the dark


def _horizon() -> Image.Image:
    global _HORIZON
    if _HORIZON is None:
        a = np.zeros((short.PH, short.PW, 4), np.uint8)
        a[..., :3] = BG
        ramp = np.clip(1 - np.arange(short.PH) / HORIZON_FADE, 0, 1) ** 1.5
        a[..., 3] = (255 * ramp)[:, None].astype(np.uint8)
        _HORIZON = Image.fromarray(a, "RGBA")
    return _HORIZON


def compose(f) -> bytes:
    cam = Camera(f)
    PW, PH = short.PW, short.PH
    W, H = life.W, life.H
    img = _ground().copy()
    d = ImageDraw.Draw(img, "RGBA")
    x0, y0 = math.floor(cam.tx - W / 2), math.floor(cam.ty - H / 2)

    # the floor's grid lines, the board's copies beyond it too, fading into the dark: rows whole,
    # columns (going into the distance) in pieces, each piece with its own fog; projected all at once
    ylo, yhi = y0 - TILE_UP * H, y0 + (TILE_DOWN + 1) * H
    xlo, xhi = x0 - TILE_SIDE * W, x0 + (TILE_SIDE + 1) * W
    step = 6
    rows = np.arange(ylo, yhi + 1, dtype=float)
    cols = np.arange(xlo, xhi + 1, dtype=float)
    ya = np.arange(ylo, yhi, step, dtype=float)
    cx, cy = np.meshgrid(cols, ya)
    cx, cy = cx.ravel(), cy.ravel()
    ends = np.concatenate([np.stack([np.full_like(rows, xlo), rows, 0 * rows], 1), np.stack([np.full_like(rows, xhi), rows, 0 * rows], 1),
                           np.stack([cx, cy, 0 * cx], 1), np.stack([cx, cy + step, 0 * cx], 1)])
    p, z = cam.project(ends)
    nr, nc = len(rows), len(cx)
    a, b = np.concatenate([p[:nr], p[2 * nr:2 * nr + nc]]), np.concatenate([p[nr:2 * nr], p[2 * nr + nc:]])
    za, zb = np.concatenate([z[:nr], z[2 * nr:2 * nr + nc]]), np.concatenate([z[nr:2 * nr], z[2 * nr + nc:]])
    fg = _fog(cam, (za + zb) / 2)
    ok = (za > 1) & (zb > 1) & (fg > 0.02) & (_on_screen(a, 5) | _on_screen(b, 5))
    ok[:nr] &= (a[:nr, 1] > -5) & (a[:nr, 1] < PH + 5)
    for (pa, pb, g) in zip(a[ok].tolist(), b[ok].tolist(), fg[ok].tolist()):
        d.line([tuple(pa), tuple(pb)], fill=FLOOR[:3] + (int(FLOOR[3] * g),), width=1)

    grid = f.grid.astype(bool)
    before = getattr(f, "before", f.grid).astype(bool)
    rise = getattr(f, "rise", 1.0)
    e = short.ease(rise)

    # the tails: glowing floor tiles
    trail = getattr(f, "trail", None)
    if trail is not None and f.grey < 1 and TRAILS:
        ta, tc = trail
        lut = np.array([PHASE_COLOR[f.phase]] + [render.PAINT[c] for c in commands.COLORS], float)
        yy, xx = np.nonzero((ta > 0.02) & ~grid & ~before)
        if len(yy):
            wx, wy, idx = _copies(cam, xx, yy)
            corners = np.stack([np.stack([wx + dx, wy + dy, np.full_like(wx, 0.01, float)], 1)
                                for dx, dy in ((.08, .08), (.92, .08), (.92, .92), (.08, .92))], 1)
            pts, z = cam.project(corners.reshape(-1, 3))
            pts, z = pts.reshape(-1, 4, 2), z.reshape(-1, 4)
            fog = _fog(cam, z.mean(1))
            keep = (z > 1).all(1) & _on_screen(pts).any(1) & (fog > 0.02)
            for q, a, c, fg in zip(pts[keep], ta[yy, xx][idx[keep]], lut[tc[yy, xx]][idx[keep]], fog[keep]):
                d.polygon([tuple(p) for p in q], fill=tuple(int(v) for v in c) + (int(200 * a * fg),))

    # the blocks: standing, rising (new, with a little bounce) and sinking (dying), far to near
    heights = np.zeros(grid.shape)
    scale = np.ones(grid.shape)  # a block's footprint (jelly keeps its volume: taller is thinner)
    popping = np.zeros(grid.shape)  # 0..1 through a dying block's pop
    age, gone = getattr(f, "age", None), getattr(f, "gone", None)
    if age is not None:
        hf = spring(age[grid])
        heights[grid] = HEIGHT * np.maximum(hf, 0)
        scale[grid] = 1 / np.sqrt(np.clip(hf, 0.55, 1.6))
        pop = ~grid & (gone < POP_FRAMES)
        q = (gone[pop].astype(float) + 1) / (POP_FRAMES + 1)
        heights[pop] = HEIGHT * (1 - q ** 1.5)
        scale[pop] = 1 + 0.55 * q
        popping[pop] = q
    else:
        live_now, born, dying = grid & before, grid & ~before, before & ~grid
        heights[live_now] = HEIGHT
        heights[born] = HEIGHT * bounce(rise)
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
        if PASTEL:
            col = col * (1 - PASTEL) + 255 * PASTEL
        wx, wy, idx = _copies(cam, xx, yy)
        hgt = heights[yy, xx][idx]
        centre, depth = cam.project(np.stack([wx + 0.5, wy + 0.5, hgt / 2], 1))
        fog = _fog(cam, depth)
        keep = np.nonzero((depth > 1) & _on_screen(centre, 80) & (fog > 0.02))[0]
        order = keep[np.argsort(-depth[keep])]
        tops = Image.new("RGB", (PW, PH), (0, 0, 0))
        td = ImageDraw.Draw(tops)
        bg = np.array(BG, float)
        # every block's corners in one projection: front (nearer edge), the side facing the camera,
        # the top, the top's bevel (inset), and where the faces' darker feet end
        half = (0.5 - MARGIN) * scale[yy, xx][idx[order]]
        X0, X1 = wx[order] + 0.5 - half, wx[order] + 0.5 + half
        Y0, Y1 = wy[order] + 0.5 - half, wy[order] + 0.5 + half
        hh, zero = hgt[order], np.zeros(len(order))
        sx = np.where(wx[order] + 0.5 < cam.tx, X1, X0)
        bv = BEVEL * (X1 - X0)
        foot = hh * FOOT
        corners = np.stack([np.stack(c, 1) for c in (
            (X0, Y1, zero), (X1, Y1, zero), (X1, Y1, hh), (X0, Y1, hh),  # 0-3 front
            (sx, Y0, zero), (sx, Y1, zero), (sx, Y1, hh), (sx, Y0, hh),  # 4-7 side
            (X0, Y0, hh), (X1, Y0, hh), (X1, Y1, hh), (X0, Y1, hh),  # 8-11 top
            (X0 + bv, Y0 + bv, hh), (X1 - bv, Y0 + bv, hh), (X1 - bv, Y1 - bv, hh), (X0 + bv, Y1 - bv, hh),  # 12-15 bevel
            (X1, Y1, foot), (X0, Y1, foot), (sx, Y1, foot), (sx, Y0, foot),  # 16-19 the feet
            (X0 + 2 * bv, Y0 + 2 * bv, hh), (X0 + 4 * bv, Y0 + 2 * bv, hh), (X0 + 3.4 * bv, Y0 + 3 * bv, hh),
            (X0 + 2 * bv, Y0 + 3.4 * bv, hh),  # 20-23 a shine on the top
            (X0, Y1, -hh * 0.5), (X1, Y1, -hh * 0.5), (X1, Y1, -hh), (X0, Y1, -hh),  # 24-27 the mirror image: front
            (sx, Y1, -hh * 0.5), (sx, Y0, -hh * 0.5), (sx, Y0, -hh), (sx, Y1, -hh))], 1)  # 28-31 and side
        pts, _ = cam.project(corners.reshape(-1, 3))
        pts = pts.reshape(-1, 32, 2)
        c = col[idx[order]]
        fg = fog[order][:, None]

        def tone(k, add=0.0):
            return (bg + (np.minimum(255, c * k + add) - bg) * fg).astype(int).tolist()
        kt, kadd, kb, kf, kff, ks, ksf = SHADES
        side, side_foot, front, front_foot = tone(ks), tone(ksf), tone(kf), tone(kff)
        rim, top = tone(kb), tone(kt, kadd)
        edge = np.minimum(255, 60 + c * 0.8).astype(int)  # its own color, lighter
        hi = [tuple(e) + (int(EDGE * fo),) for e, fo in zip(edge.tolist(), fg[:, 0].tolist())]
        shine = [(255, 255, 255, int(GLOSS * fo)) for fo in fg[:, 0].tolist()]
        light = (c * np.minimum(1.0, hh / HEIGHT)[:, None] * fg).astype(int).tolist()
        # soft contact shadows: each block darkens the floor around its foot (drawn small, blurred)
        sh = Image.new("L", (PW // 2, PH // 2), 0)
        sd = ImageDraw.Draw(sh)
        foot_pts, _ = cam.project(np.stack([np.stack(c3, 1) for c3 in (
            (X0 - 0.12, Y0 - 0.05, zero), (X1 + 0.12, Y0 - 0.05, zero), (X1 + 0.15, Y1 + 0.2, zero),
            (X0 - 0.15, Y1 + 0.2, zero))], 1).reshape(-1, 3))
        foot_pts = (foot_pts.reshape(-1, 4, 2) / 2).tolist()
        for q, fo, hs in zip(foot_pts, fg[:, 0].tolist(), (hh / HEIGHT).tolist()):
            sd.polygon([tuple(p) for p in q], fill=int(255 * SHADOW * fo * min(1.0, hs)))
        sh = sh.filter(ImageFilter.GaussianBlur(SHADOW_BLUR)).resize((PW, PH), Image.BILINEAR)
        img = Image.composite(Image.new("RGB", (PW, PH), SHADOW_RGB), img, sh)
        pts = pts.tolist()
        if REFLECT:  # the blocks mirrored in a glossy floor: brighter near it, fading deeper down
            refl = Image.new("RGB", (PW, PH), (0, 0, 0))
            rd = ImageDraw.Draw(refl)
            near_c = (c * fg * 0.8).astype(int).tolist()
            deep_c = (c * fg * 0.35).astype(int).tolist()
            for q, nc, dc in zip(pts, near_c, deep_c):
                P = [tuple(p) for p in q]
                rd.polygon([P[4], P[5], P[28], P[29]], fill=tuple(int(v * 0.75) for v in nc))
                rd.polygon([P[29], P[28], P[31], P[30]], fill=tuple(int(v * 0.75) for v in dc))
                rd.polygon([P[0], P[1], P[25], P[24]], fill=tuple(nc))
                rd.polygon([P[24], P[25], P[26], P[27]], fill=tuple(dc))
            refl = refl.filter(ImageFilter.GaussianBlur(1.5))
            img = Image.fromarray(np.clip(np.asarray(img, np.float32) + np.asarray(refl, np.float32) * REFLECT, 0, 255)
                                  .astype(np.uint8))
        d = ImageDraw.Draw(img, "RGBA")
        for q, a, af, b, bf, r, t, e, g, sn in zip(pts, side, side_foot, front, front_foot, rim, top, hi, light, shine):
            P = [tuple(p) for p in q]
            d.polygon(P[4:8], fill=tuple(a))
            d.polygon([P[4], P[5], P[18], P[19]], fill=tuple(af))  # the side's foot, darker
            d.polygon(P[0:4], fill=tuple(b))
            d.polygon([P[0], P[1], P[16], P[17]], fill=tuple(bf))  # the front's foot, darker
            d.polygon(P[8:12], fill=tuple(r))  # the top's bevel...
            d.polygon(P[12:16], fill=tuple(t))  # ...around its lit face
            d.line([P[8], P[9]], fill=e, width=2)  # a bright far edge: light catching it
            if GLOSS:
                d.polygon(P[20:24], fill=sn)  # a glossy shine
            td.polygon(P[8:12], fill=tuple(g))
        if GLOW:
            small = tops.resize((PW // 2, PH // 2), Image.BILINEAR).filter(ImageFilter.GaussianBlur(GLOW_BLUR / 2))
            glow = np.asarray(small.resize((PW, PH), Image.BILINEAR), np.float32) * GLOW
            img = Image.fromarray(np.clip(np.asarray(img, np.float32) + glow, 0, 255).astype(np.uint8))

    canvas = Image.alpha_composite(img.convert("RGBA"), _horizon())
    if getattr(f, "scrim", 0):
        canvas = Image.alpha_composite(canvas, short.scrim(f.scrim))
    back = _backing(f.texts)
    if back is not None:
        canvas = Image.alpha_composite(canvas, back)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    def to_canvas(bx, by):  # a board-image point (a cell's centre) on a block's top
        x, y = cam.near(bx / short.CELL - 0.5, by / short.CELL - 0.5)
        p, _ = cam.project([(x + 0.5, y + 0.5, HEIGHT)])
        return float(p[0, 0]), float(p[0, 1])

    for s, weight, size, xy, anchor, alpha in f.texts:
        short.ex.text(ld, xy, s, weight, size, alpha, fill=TEXT_FILL, anchor=anchor, outline=TEXT_OUTLINE)
    for extra in f.extras:
        extra(ld, to_canvas)
    return Image.alpha_composite(canvas, layer).convert("RGB").tobytes()
