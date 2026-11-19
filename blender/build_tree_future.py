"""The tree of life: the tower to now colored by family (as build_lineage, forest palette), then the future if
nobody comments, dying as it goes (the families' greens -> autumn -> brown -> dead grey), a day of it stuck
(dead grey pillars), then one hypothetical comment (an rpent) whose new growth comes in fresh green. -> OUT.npz

python build_tree_future.py OUT.npz [LEAF_V] [FUT_STEP] [FROZEN_STEP] [REVIVE_STEP] [REVIVE_LAYERS]
"""
import colorsys
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the repo
sys.path.insert(0, ROOT)
import life  # noqa: E402
import short  # noqa: E402
from short import move_input  # noqa: E402

out = sys.argv[1]
LEAF_V = (float(sys.argv[2]) if len(sys.argv) > 2 else 0.45, 0.25)
FUT_STEP = int(sys.argv[3]) if len(sys.argv) > 3 else 48
FROZEN_STEP = int(sys.argv[4]) if len(sys.argv) > 4 else 8
REVIVE_STEP = int(sys.argv[5]) if len(sys.argv) > 5 else 16
REVIVE_LAYERS = int(sys.argv[6]) if len(sys.argv) > 6 else 100
DAY = 86400
h = short.History(os.path.join(ROOT, "state"))
now = h.now_gen
sample = set(range(0, 120)) | set(range(120, now + 1, 16))
_DY, _DX = life._DY, life._DX


def lin_step(before, after, lin):  # life.paint_step, for 16-bit family ids
    o = np.where(after == 1, lin, 0).astype(np.uint16)
    ys, xs = np.nonzero((after == 1) & (before == 0))
    if len(ys):
        H, W = before.shape
        ny, nx = (ys[:, None] + _DY) % H, (xs[:, None] + _DX) % W
        parents = lin[ny, nx][before[ny, nx] == 1].reshape(-1, 3)
        a, b, c = parents.T
        o[ys, xs] = np.where((a == b) | (a == c), a, np.where(b == c, b, a))
    return o


# ---- the history, each cell's family
u = h.fresh()
H, W = u.grid.shape
lin = np.zeros((H, W), np.uint16)
fid = 0
xs, ys, ts, ls, gens, grids = [], [], [], [], [], []
mx, my, mt, mv = [], [], [], []
for gen in range(0, now + 1):
    assert u.generation == gen, (u.generation, gen)
    if gen in h.batches:
        batch = h.batches[gen]
        u.inject([move_input(e) for e in batch])
        for e in batch:
            if not e.get("cells"):
                continue
            fid += 1
            for x, y in e["cells"]:
                lin[y % H, x % W] = 0 if e.get("erase") else fid
            if not e.get("erase"):
                for x, y in e["cells"]:
                    mx.append(x % W); my.append(y % H); mt.append(len(gens)); mv.append(fid)
        lin = np.where(u.grid == 1, lin, 0).astype(np.uint16)
    if gen in sample:
        yy, xx = np.nonzero(u.grid)
        xs.append(xx); ys.append(yy); ts.append(np.full(len(xx), len(gens))); ls.append(lin[yy, xx])
        gens.append(gen); grids.append(u.grid.astype(bool))
    if gen == now:
        break
    before = u.grid.copy()
    u.advance_to(gen + 1)
    lin = lin_step(before, u.grid, lin)
print(f"now {now}; {fid} families; alive now: {len(np.unique(lin[u.grid == 1]))}", flush=True)

# ---- the palette (as build_lineage's forest): mostly greens, some autumn, the odd deep red; the first family bark
rng = np.random.default_rng(7)
pal = []
for k in range(fid + 2):
    r = rng.random()
    if r < 0.72:
        hsv = (0.20 + 0.13 * rng.random(), 0.45 + 0.25 * rng.random(), LEAF_V[0] + LEAF_V[1] * rng.random())
    elif r < 0.94:
        hsv = (0.05 + 0.07 * rng.random(), 0.60 + 0.20 * rng.random(), LEAF_V[0] + 0.12 + LEAF_V[1] * rng.random())
    else:
        hsv = (0.985, 0.6, LEAF_V[0] + 0.1)
    pal.append(colorsys.hsv_to_rgb(*hsv))
pal[0] = (0.40, 0.27, 0.16)
pal = np.array(pal, np.float32)
cols = [pal[L] for L in ls]
FUT = len(gens)  # the first future layer

# ---- the future, nobody commenting: step until a state repeats (stuck); the families carried on
g, gl, gen = u.grid.copy(), lin.copy(), now
seen = {life.state_hash(g)}
stuck = None
traj = {}
while stuck is None and gen < now + 30000:
    g2 = life.step(g)
    gl = lin_step(g, g2, gl)
    g = g2
    gen += 1
    hs = life.state_hash(g)
    if hs in seen:
        stuck = gen
    seen.add(hs)
    if (gen - now) % FUT_STEP == 0:
        traj[gen] = (g.copy(), gl.copy())
assert stuck, "never stuck within 30000 gens"
die = stuck + DAY // h.gen_seconds
print(f"stuck at {stuck} (+{(stuck - now) * h.gen_seconds / DAY:.1f} d); dies at {die}", flush=True)
AUTUMN, BROWN, DEAD = (np.array(c, np.float32) for c in ((0.80, 0.52, 0.14), (0.40, 0.25, 0.12), (0.47, 0.45, 0.42)))


def dying(base, f):  # f 0 -> 1: the living color -> autumn -> brown -> dead grey
    stops = [base, np.broadcast_to(AUTUMN, base.shape), np.broadcast_to(BROWN, base.shape), np.broadcast_to(DEAD, base.shape)]
    p = min(max(f, 0.0), 1.0) * 3
    i = min(int(p), 2)
    return stops[i] * (1 - (p - i)) + stops[i + 1] * (p - i)


def add(grid, c):
    yy, xx = np.nonzero(grid)
    xs.append(xx); ys.append(yy); ts.append(np.full(len(xx), len(gens))); cols.append(c(yy, xx))
    grids.append(grid.astype(bool))


fut_gens = [k for k in sorted(traj) if k < stuck]
for j, gg in enumerate(fut_gens):
    gr, lr = traj[gg]
    f = (j + 1) / max(1, len(fut_gens))
    add(gr, lambda yy, xx: dying(pal[lr[yy, xx]], f))
    gens.append(gg)
FROZEN = len(gens)
# a day stuck, sampled finer so the pillars show; the comment lands most of the way through it
g = traj[fut_gens[-1]][0].copy() if fut_gens else u.grid.copy()
gen = fut_gens[-1] if fut_gens else now
save_at = stuck + int(0.8 * (die - stuck))
while gen < save_at:
    g = life.step(g)
    gen += 1
    if gen >= stuck and (gen - stuck) % FROZEN_STEP == 0:
        add(g, lambda yy, xx: np.broadcast_to(DEAD, (len(yy), 3)).copy())
        gens.append(gen)
# ---- the hypothetical comment: an rpent where it brings the most back (long chaos before it repeats)
cx0, cy0 = float(xs[0].mean()), float(ys[0].mean())  # where it began: the middle of the picture
best = None
for dx in range(-20, 21, 5):
    for dy in range(-12, 13, 4):
        px, py = int(cx0 + dx) % W, int(cy0 + dy) % H
        cells = life.place(life.PATTERNS["rpent"], px, py, (H, W))
        trial = g.copy()
        for x, y in cells:
            trial[y, x] = 1
        s2, t2 = {life.state_hash(trial)}, trial
        for k in range(1, REVIVE_LAYERS * REVIVE_STEP + 1):
            t2 = life.step(t2)
            hs = life.state_hash(t2)
            if hs in s2:
                break
            s2.add(hs)
        score = k - 0.5 * (abs(dx) + abs(dy))
        if best is None or score > best[0]:
            best = (score, px, py, cells, k)
_, px, py, cells, lasted = best
print(f"comment: rpent {px},{py} lasts {lasted} gens unstuck", flush=True)
frozen_board = g.copy()
for x, y in cells:
    g[y, x] = 1
INJECT = len(gens)
for x, y in cells:
    mx.append(x); my.append(y); mt.append(INJECT); mv.append(fid + 1)
SPRING = np.array([[0.36, 0.70, 0.20], [0.46, 0.78, 0.26], [0.30, 0.62, 0.18], [0.95, 0.62, 0.68], [0.98, 0.88, 0.80]], np.float32)


def revived(yy, xx):  # the comment's new growth fresh green (some blossom); what it hasn't touched stays dead
    c = SPRING[np.where((xx * 31 + yy * 17) % 9 == 0, 3 + (xx + yy) % 2, (xx * 7 + yy * 3) % 3)]
    c[frozen_board[yy, xx] > 0] = DEAD
    return c


add(g, revived)
gens.append(gen)
for _ in range(REVIVE_LAYERS - 1):
    for _ in range(REVIVE_STEP):
        g = life.step(g)
        gen += 1
    add(g, revived)
    gens.append(gen)

x, y, t = map(np.concatenate, (xs, ys, ts))
c = (np.clip(np.concatenate(cols), 0, 1) * 255).astype(np.uint8)
mc = (pal[np.minimum(np.array(mv, int), fid)] * 255).astype(np.uint8)
np.savez_compressed(out, x=x.astype(np.int16), y=y.astype(np.int16), t=t.astype(np.int16), c=c,
                    shape=np.array((H, W)), mx=np.array(mx, np.int16), my=np.array(my, np.int16),
                    mt=np.array(mt, np.int16), mc=mc, mv=np.array(mv, np.int32),
                    gens=np.array(gens, np.int32), grids=np.packbits(np.array(grids), axis=-1),
                    fut=FUT, frozen=FROZEN, inject=INJECT, now=now, stuck=stuck, die=die, comment=f"rpent {px},{py}")
print(f"{len(gens)} layers: history {FUT}, future {FROZEN - FUT}, stuck {INJECT - FROZEN}, revived {len(gens) - INJECT};",
      len(x), "voxels;", len(set(mv)), "moves", flush=True)
