"""Conway's Game of Life (B3/S23) on a toroidal grid.

The grid is a NumPy uint8 array of shape (H, W) holding 0/1, indexed grid[y, x]
with the origin at the top-left. Every function here is pure.
"""

from __future__ import annotations

import base64
import hashlib

import numpy as np

W, H = 128, 72  # production grid: 128 wide x 72 tall

# Patterns as (x, y) offsets from the top-left of their bounding box.
PATTERNS: dict[str, tuple[tuple[int, int], ...]] = {
    "block": ((0, 0), (1, 0), (0, 1), (1, 1)),
    "blinker": ((0, 0), (1, 0), (2, 0)),
    "rpent": ((1, 0), (2, 0), (0, 1), (1, 1), (1, 2)),
    "acorn": ((1, 0), (3, 1), (0, 2), (1, 2), (4, 2), (5, 2), (6, 2)),
    # Gliders, named by the direction they travel (y grows downward).
    "glider_se": ((1, 0), (2, 1), (0, 2), (1, 2), (2, 2)),
    "glider_sw": ((1, 0), (0, 1), (0, 2), (1, 2), (2, 2)),
    "glider_ne": ((0, 0), (1, 0), (2, 0), (2, 1), (1, 2)),
    "glider_nw": ((0, 0), (1, 0), (2, 0), (0, 1), (1, 2)),
}


def empty(shape: tuple[int, int] = (H, W)) -> np.ndarray:
    return np.zeros(shape, dtype=np.uint8)


def place(offsets, x: int, y: int, shape: tuple[int, int] = (H, W)) -> list[tuple[int, int]]:
    """Absolute (x, y) cells for a pattern whose top-left is at (x, y), wrapped."""
    h, w = shape
    return [((x + dx) % w, (y + dy) % h) for dx, dy in offsets]


def centered(name: str, shape: tuple[int, int] = (H, W)) -> list[tuple[int, int]]:
    """A named pattern with its bounding box centered on the grid."""
    offsets = PATTERNS[name]
    pw = max(dx for dx, _ in offsets) + 1
    ph = max(dy for _, dy in offsets) + 1
    h, w = shape
    return place(offsets, (w - pw) // 2, (h - ph) // 2, shape)


def mask(cells, shape: tuple[int, int] = (H, W)) -> np.ndarray:
    m = empty(shape)
    for x, y in cells:
        m[y % shape[0], x % shape[1]] = 1
    return m


def step(grid: np.ndarray) -> np.ndarray:
    """One B3/S23 generation on a torus."""
    v = grid + np.roll(grid, 1, 0) + np.roll(grid, -1, 0)
    s = v + np.roll(v, 1, 1) + np.roll(v, -1, 1)  # 3x3 sum including the cell itself
    return ((s == 3) | ((s == 4) & (grid == 1))).astype(np.uint8)


_DY = np.array([-1, -1, -1, 0, 0, 1, 1, 1])  # the 8 neighbours, read from the top left
_DX = np.array([-1, 0, 1, -1, 1, -1, 0, 1])


def paint_step(before: np.ndarray, after: np.ndarray, paint: np.ndarray) -> np.ndarray:
    """The paint on the cells one generation on, from `before` to `after` =
    step(before). Paint (0: none, 1.. a color) is only how cells look: it
    never changes which live. A cell that lives on keeps its paint; a cell
    born takes the paint of its three parents: the majority's, or if all three
    differ, the first one's (reading its neighbours from the top left). Dead
    cells have none."""
    out = np.where(after == 1, paint, 0).astype(np.uint8)
    if not out.any() and not paint.any():
        return out
    ys, xs = np.nonzero((after == 1) & (before == 0))
    if len(ys) == 0:
        return out
    h, w = before.shape
    ny, nx = (ys[:, None] + _DY) % h, (xs[:, None] + _DX) % w
    parents = paint[ny, nx][before[ny, nx] == 1].reshape(-1, 3)  # a birth has exactly 3 live neighbours
    a, b, c = parents.T
    out[ys, xs] = np.where((a == b) | (a == c), a, np.where(b == c, b, a))
    return out


TRAIL_GENS, TRAIL_MAX = 10, 0.42  # the tails: a cell that died fades out over 10 steps, from 42% bright


class Trail:
    """Where the cells were lately, for the tails: each cell's last step alive and
    its color then (a paint index; 0 the phase color). A step is a generation on
    the live board and in a replay, a drawn frame in the rewind. Only how the
    board looks: the game never sees it."""

    def __init__(self, shape):
        self.last = np.full(shape, -(10 ** 9), np.int64)
        self.col = np.zeros(shape, np.uint8)
        self.t = 0

    def see(self, grid: np.ndarray, t: int, paint: np.ndarray | None = None) -> None:
        alive = grid == 1
        self.last[alive] = t
        self.col[alive] = paint[alive] if paint is not None else 0
        self.t = max(self.t, t)

    def layer(self, grid: np.ndarray, frac: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """(brightness 0..TRAIL_MAX, color index) per cell; `frac` of the way to the
        next step, so the newest tails fade on between generations."""
        d = (self.t - self.last).astype(np.float32) + frac  # steps since it was last alive
        a = TRAIL_MAX * np.clip(1 - (d - 1) / TRAIL_GENS, 0, 1) ** 1.6
        a[(grid == 1) | (d < 1)] = 0
        return a, self.col


def state_hash(grid: np.ndarray) -> bytes:
    """BLAKE2b-256 of np.packbits(grid) (row-major, 1 bit per cell)."""
    return hashlib.blake2b(np.packbits(grid).tobytes(), digest_size=32).digest()


def encode(grid: np.ndarray) -> str:
    """Grid -> base64 of packbits, for state.json."""
    return base64.b64encode(np.packbits(grid).tobytes()).decode("ascii")


def decode(data: str, shape: tuple[int, int] = (H, W)) -> np.ndarray:
    bits = np.unpackbits(np.frombuffer(base64.b64decode(data), dtype=np.uint8))
    return bits[: shape[0] * shape[1]].reshape(shape).astype(np.uint8)


def find_cycle(grid: np.ndarray, max_gens: int = 1_000_000) -> tuple[int, int]:
    """Evolve undisturbed until a state repeats.

    Returns (mu, period): the first generation that is part of the cycle, and
    the cycle length. The repeat is *detected* at generation mu + period.
    """
    seen: dict[bytes, int] = {state_hash(grid): 0}
    for gen in range(1, max_gens + 1):
        grid = step(grid)
        h = state_hash(grid)
        if h in seen:
            return seen[h], gen - seen[h]
        seen[h] = gen
    raise RuntimeError(f"no cycle within {max_gens} generations")
