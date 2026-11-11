#!/usr/bin/env python3
"""Stream the live universe to YouTube, around the clock.

    STREAM_KEY=... python stream.py                  # live, from this checkout
    python stream.py --out test.flv --seconds 60     # a minute to a file
    python stream.py --preview out/stream.png        # one frame (with the latest moves rippling)

A window onto the same universe the main video's comments play. It reads the
public state from this git checkout (pulling every minute), steps the board one
generation every 2 minutes on the wall clock exactly as the tick does, and
when the tick commits new comments it shows their cells with a ripple and a
label. It calls no API and needs no secret except the stream key, which comes
from the environment, is redacted from ffmpeg's messages and is never written
anywhere.

When a pull brings new code it exits, so the service restarts on the new
version (systemd Restart=always). If ffmpeg's connection drops it exits too,
for the same restart, and so it does if no frame has gone out for 15 s.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import explainer as ex
import render
import stream_audio
from engine import FORECAST_GENS
from render import BG, GRID, HIGHLIGHT, PHASE_COLOR
from universe import ALIVE, DIED, Universe, target_generation

SW, SH = 1920, 1080
BOARD_X, BOARD_Y, BOARD_W, BOARD_H = 56, 60, 1408, 792  # the board's box (128 x 72 at 11 px)
COL_X, COL_W = 1500, 380  # the column on the right
# Frames drawn per second; ffmpeg repeats them up to OUT_FPS. Encoding is most
# of the work (about half a fast core at 30 fps, a quarter at 10): STREAM_FPS=15
# if the machine falls behind (YouTube's guidance is 30).
IN_FPS, OUT_FPS = 10, int(os.environ.get("STREAM_FPS", "30"))
PRESET = os.environ.get("X264_PRESET", "veryfast")  # the preset barely matters for this picture
PULL_S = 15  # how often to ask GitHub for a new commit (one small ls-remote; it pulls only then)
STALL_S = 15  # no frame for this long (a bug, or ffmpeg stuck): exit, and the service restarts it
REWIND_EVERY_S, REWIND_S = 30 * 60, 60.0  # at :00 and :30, a minute of the story so far
REWIND_LIT = 30  # generations a comment's cells stay white during the rewind
FADE_S = 0.6  # crossfade between generations
RIPPLE_S, TAG_S, GLOW_S = 3.0, 90, 600  # a new move: ripple, label, its cells lit
SCREEN = (5, 6, 9)
MUTED, LIGHT, WHITE = (125, 131, 145), (222, 225, 232), (255, 255, 255)
MAIN_VIDEO = "oFWeLMe4bhI"  # universe #1; later universes keep their video in state.json
INGEST = "rtmps://a.rtmps.youtube.com/live2/"
CODE = ("stream.py", "stream_audio.py", "short.py", "render.py", "universe.py", "life.py", "explainer.py", "engine.py",
        "commands.py", "admission.py", "store.py")  # a change to these restarts the stream


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- fonts
_fonts: dict = {}
DEJAVU = os.path.exists(ex.LINUX_FONTS["regular"])  # the server; Avenir on a Mac has no arrow


def font(weight: str, size: int):
    key = (weight, size)
    if key not in _fonts:
        try:
            _fonts[key] = (ImageFont.truetype(ex.LINUX_FONTS[weight], size) if DEJAVU
                           else ImageFont.truetype(ex.FONT_FILE, size, index=ex.FACE[weight]))
        except OSError:
            _fonts[key] = ImageFont.load_default(size=size)
    return _fonts[key]


def tidy(s: str) -> str:
    return s if DEJAVU else s.replace("→", "to")


def wrap(d, s: str, weight: str, size: int, width: int) -> list[str]:
    out, line = [], ""
    for w in s.split():
        trial = f"{line} {w}".strip()
        if line and d.textlength(trial, font=font(weight, size)) > width:
            out.append(line)
            line = w
        else:
            line = trial
    return out + [line] if line else out


def span_s(s: str) -> float | None:
    """'8d 21h' -> seconds; '42d+' counts as forever."""
    if s.strip().endswith("+"):
        return float("inf")
    parts = re.findall(r"(\d+)([dhm])", s)
    return sum(int(n) * {"d": 86400, "h": 3600, "m": 60}[u] for n, u in parts) if parts else None


def effect_colour(effect: str):
    """Green if the move gave the universe more time, red if less (as on the live page)."""
    before, arrow, after = effect.partition("→")
    a, b = span_s(after), span_s(before)
    if not arrow or a is None or b is None or a == b:
        return MUTED
    return PHASE_COLOR[ALIVE] if a > b else (240, 90, 90)


def fit(d, s: str, weight: str, size: int, width: int, smallest: int) -> int:
    while size > smallest and d.textlength(s, font=font(weight, size)) > width:
        size -= 1
    return size


# ---------------------------------------------------------------- state
@dataclass
class Snapshot:
    u: Universe
    launch: float
    gen_seconds: int
    forecast: dict | None
    recent: list[str]
    universe_no: int
    video: str
    seq: int  # log lines so far
    new: list[dict] = field(default_factory=list)  # injections since the previous snapshot
    effects: dict = field(default_factory=dict)  # (gen, command) -> effect event
    checked: float | None = None  # when the tick last read the comments


def load(state_dir: str, after_seq: int | None = None, now: float | None = None) -> Snapshot:
    """The committed state, stepped on to the wall clock. With `after_seq`, also
    the injections (and their effects) logged since then."""
    with open(os.path.join(state_dir, "state.json"), encoding="utf-8") as f:
        s = json.load(f)
    with open(os.path.join(state_dir, "history.bin"), "rb") as f:
        u = Universe.from_state(s["universe"], f.read())
    cfg, disp = s["config"], s.get("display") or {}
    snap = Snapshot(u=u, launch=cfg["launch_time"], gen_seconds=cfg["gen_seconds"], forecast=disp.get("forecast"),
                    recent=list(disp.get("recent") or []), universe_no=cfg.get("universe", 1),
                    video=cfg.get("video_id") or MAIN_VIDEO, seq=s["log"]["seq"])
    if after_seq is not None and snap.seq > after_seq:
        with open(os.path.join(state_dir, "log.jsonl"), encoding="utf-8") as f:
            for line in f:
                e = json.loads(line)
                if e.get("seq", -1) < after_seq:
                    continue
                if e["type"] == "injection":
                    snap.new.append(e)
                elif e["type"] == "effect":
                    snap.effects.setdefault((e["gen"], e["command"]), e)
    snap.checked = last_check(state_dir)
    step(snap, now if now is not None else time.time())
    return snap


def last_check(state_dir: str) -> float | None:
    """When the tick last read the comments: the time on the log's last tick line."""
    with open(os.path.join(state_dir, "log.jsonl"), "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 65536))
        tail = f.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(tail):
        try:
            e = json.loads(line)
        except ValueError:  # the first, cut-off line of the tail
            continue
        if e.get("type") == "tick" and "now" in e:
            return float(e["now"])
    return None


def checked_text(checked: float | None, now: float) -> str:
    """'chat checked 4 min ago' ('' if unknown). The tick reads the chat and the comments together."""
    if checked is None:
        return ""
    mins = int(max(0.0, now - checked) // 60)
    return "chat checked just now" if mins < 1 else f"chat checked {mins} min ago"


def step(snap: Snapshot, now: float) -> bool:
    """Advance to the generation the wall clock says (as the tick will). True if it moved."""
    target = target_generation(now, snap.launch, snap.gen_seconds)
    if snap.u.phase == DIED or target <= snap.u.generation:
        return False
    snap.u.advance_to(target)
    return True


# ---------------------------------------------------------------- drawing
@dataclass
class Tag:
    text: str
    cells: list  # (x, y)
    t0: float


_tiles: dict = {}


def geometry(shape) -> tuple[int, int, int]:
    """(cell px, left, top) for a board of `shape` (h, w), centred in the board box."""
    h, w = shape
    cell = max(2, min(BOARD_W // w, BOARD_H // h))
    return cell, BOARD_X + (BOARD_W - w * cell) // 2, BOARD_Y + (BOARD_H - h * cell) // 2


def board_rgb(grid: np.ndarray, phase: str, lit: np.ndarray | None) -> np.ndarray:
    h, w = grid.shape
    cell = geometry(grid.shape)[0]
    if (h, w) not in _tiles:  # the empty board, and where each cell's inside is
        inner = np.zeros((cell, cell), bool)
        inner[1:-1, 1:-1] = True
        tile = np.tile(inner, (h, w))
        empty = np.empty(tile.shape + (3,), np.uint8)
        empty[:] = GRID
        empty[tile] = BG
        _tiles[(h, w)] = (tile, empty)
    tile, empty = _tiles[(h, w)]
    img = empty.copy()

    def big(m):  # a cell mask, scaled up to pixels (insides only)
        return m.astype(bool).repeat(cell, 0).repeat(cell, 1) & tile
    img[big(grid)] = PHASE_COLOR[phase]
    if lit is not None and lit.any():
        img[big(grid & lit)] = HIGHLIGHT
    return img


def centre_px(cells, shape) -> tuple[float, float]:
    """Screen centre of a pattern's cells, taking the torus wrap into account."""
    h, w = shape
    cell, left, top = geometry(shape)
    x0, y0 = cells[0]
    xs = [x0 + (x - x0 + w // 2) % w - w // 2 for x, _ in cells]
    ys = [y0 + (y - y0 + h // 2) % h - h // 2 for _, y in cells]
    cx, cy = (sum(xs) / len(xs)) % w, (sum(ys) / len(ys)) % h
    return left + (cx + 0.5) * cell, top + (cy + 0.5) * cell


def free_row(y0: float, x0: float, x1: float, placed: list[tuple], lo: float, hi: float, step: int = 34) -> float:
    """Where a label spanning x0..x1 goes: the first height, in steps from y0 (down to
    hi, then up to lo), clear of the labels already `placed`; y0 itself if none is.
    A bounded search: moves bunched near an edge once sent the old loop back and
    forth between two blocked rows for ever, and froze the stream."""
    ys = [y0] + [y0 + k * step for k in range(1, int((hi - y0) // step) + 1)]
    ys += [y0 - k * step for k in range(1, int((y0 - lo) // step) + 1)]
    return next((y for y in ys if not any(abs(y - py) < 32 and x0 < px1 and x1 > px0 for px0, px1, py in placed)), y0)


def rewind_slot(now: float) -> float | None:
    """When the rewind running at `now` began (at :00 and :30), or None."""
    slot = now // REWIND_EVERY_S * REWIND_EVERY_S
    return slot if now - slot < REWIND_S else None


class Rewind:
    """The story so far: the universe from its first cells to now in REWIND_S,
    replayed from the log with the bot's own rules (it ends on the live board)."""

    def __init__(self, state_dir: str, start: float, end_gen: int):
        import short
        self.h = short.History(state_dir)
        self.u = self.h.fresh()
        self.start, self.end = start, max(1, end_gen)
        self.lit: list[tuple[int, list]] = []  # (gen, cells) of recent comments, drawn white
        self.pool, self.pool_at = self.u.grid.copy(), start  # sound: births and deaths pooled per 0.3 s

    def done(self, now: float) -> bool:
        return now >= self.start + REWIND_S

    def advance(self, now: float, sound=None) -> None:
        p = min(1.0, max(0.0, (now - self.start) / REWIND_S))
        target = int(round(self.end * p))
        if target > self.u.generation:
            cells = self.h.goto(self.u, target)
            if cells:
                self.lit.append((self.u.generation, cells))
                if sound:
                    sound.landed(cells[0][0], self.u.grid.shape[1])
        self.lit = [(g, c) for g, c in self.lit if self.u.generation - g < REWIND_LIT]
        if sound and now - self.pool_at >= 0.3:
            sound.generation(self.pool, self.u.grid)
            self.pool, self.pool_at = self.u.grid.copy(), now

    def lit_mask(self) -> np.ndarray | None:
        if not self.lit:
            return None
        m = np.zeros(self.u.grid.shape, np.uint8)
        h, w = m.shape
        for _, cells in self.lit:
            for x, y in cells:
                m[y % h, x % w] = 1
        return m


class Screen:
    def __init__(self, snap: Snapshot):
        self.snap = snap
        self.tags: list[Tag] = []
        self.prev: np.ndarray | None = None  # the board before the last change, for the crossfade
        self.changed_at = 0.0
        self.cur: np.ndarray | None = None
        self.cur_key = None
        self.sound = None  # a stream_audio.Soundtrack, when the stream has sound
        self.rewind: Rewind | None = None
        self.rewound_at: float | None = None  # the slot of the last rewind (one per slot)
        self._chrome = None  # (key, the frame without the board), see compose

    # A new snapshot (the tick committed) or a new generation (the clock).
    def swap(self, snap: Snapshot, now: float) -> None:
        if snap.universe_no != self.snap.universe_no:
            self.tags = []
        for e in snap.new:
            eff = snap.effects.get((e["gen"], e["command"]))
            suffix = render.effect_suffix(eff, snap.u.flatline_gens, snap.gen_seconds) if eff else ""
            self.tags.append(Tag(e["command"] + suffix, [tuple(c) for c in e["cells"]], now))
            if self.sound and e["cells"]:
                self.sound.landed(e["cells"][0][0], snap.u.grid.shape[1])
        if self.sound and snap.universe_no == self.snap.universe_no:  # the comments' cells appearing
            self.sound.generation(self.snap.u.grid, snap.u.grid, abs(snap.u.generation - self.snap.u.generation) + 1)
        self._changed(now)
        self.snap = snap

    def tick(self, now: float, state_dir: str | None = None) -> None:
        before, gen = (self.snap.u.grid.copy(), self.snap.u.generation) if self.sound else (None, 0)
        if step(self.snap, now):
            self._changed(now)
            if self.sound and not self.rewind:
                self.sound.generation(before, self.snap.u.grid, self.snap.u.generation - gen)
        slot = rewind_slot(now)
        if (state_dir and slot is not None and self.rewind is None and slot != self.rewound_at
                and self.snap.u.phase != DIED):
            self.rewound_at = slot
            try:
                self.rewind = Rewind(state_dir, slot, self.snap.u.generation)
                log(f"rewind: the story so far, gen 0 to {self.snap.u.generation:,}")
            except Exception as e:  # noqa: BLE001 - no rewind this time; the live board goes on
                log(f"rewind failed: {e!r}")
        if self.rewind:
            self.rewind.advance(now, self.sound)
            if self.rewind.done(now):
                self.rewind = None
                self._changed(now)  # crossfade back to the live board
        if self.sound:
            self.sound.set_phase(self.rewind.u.phase if self.rewind else self.snap.u.phase)

    def _changed(self, now: float) -> None:
        self.prev = self.cur
        self.changed_at = now

    def animating(self, now: float) -> bool:
        return (self.rewind is not None or now - self.changed_at < FADE_S
                or any(now - t.t0 < RIPPLE_S for t in self.tags))

    def key(self, now: float):
        """Changes whenever the picture would: the board, the countdown's second, the labels."""
        u = self.snap.u
        live = tuple(id(t) for t in self.tags if now - t.t0 < TAG_S)
        return (id(self.snap), u.generation, u.phase, int(now), live)

    def board(self, now: float) -> np.ndarray:
        if self.rewind:
            r = self.rewind.u
            self.cur, self.cur_key = board_rgb(r.grid, r.phase, self.rewind.lit_mask()), None
            return self.cur
        u = self.snap.u
        self.tags = [t for t in self.tags if now - t.t0 < max(TAG_S, GLOW_S)]
        glowing = [c for t in self.tags if now - t.t0 < GLOW_S for c in t.cells]
        lit = None
        if glowing and u.phase != DIED:
            lit = np.zeros(u.grid.shape, np.uint8)
            for x, y in glowing:
                lit[y % u.grid.shape[0], x % u.grid.shape[1]] = 1
        k = (id(self.snap), u.generation, u.phase, len(glowing))
        if k != self.cur_key or self.cur is None:
            self.cur, self.cur_key = board_rgb(u.grid, u.phase, lit), k
        a = (now - self.changed_at) / FADE_S
        if self.prev is not None and self.prev.shape == self.cur.shape and 0 <= a < 1:
            return (self.prev * (1 - a) + self.cur * a).astype(np.uint8)
        self.prev = None
        return self.cur

    def compose(self, now: float) -> Image.Image:
        u = self.snap.u
        shape = u.grid.shape
        cell, left, top = geometry(shape)
        # Everything but the board changes at most once a second: drawn then, reused
        # for the other frames (the rewind and the ripples draw every frame).
        k = (int(now), id(self.snap), u.generation, u.phase)
        if self._chrome is None or self._chrome[0] != k:
            img = np.empty((SH, SW, 3), np.uint8)
            img[:] = SCREEN
            im = Image.fromarray(img)
            d = ImageDraw.Draw(im, "RGBA")
            d.rectangle((left - 1, top - 1, left + shape[1] * cell, top + shape[0] * cell), outline=(42, 46, 58))
            self._axes(d, shape)
            self._column(d, now)
            self._bottom(d)
            self._chrome = (k, np.asarray(im))
        img = self._chrome[1].copy()
        b = self.board(now)
        img[top:top + b.shape[0], left:left + b.shape[1]] = b
        im = Image.fromarray(img)
        d = ImageDraw.Draw(im, "RGBA")
        if self.rewind:
            self._rewind_banner(d, now, shape)
        else:
            self._moves(d, now, shape)
        return im

    def _rewind_banner(self, d, now: float, shape) -> None:
        """What the board is showing: the past, when, and how long until it's live again."""
        r, snap = self.rewind, self.snap
        cell, left, top = geometry(shape)
        cx = left + shape[1] * cell / 2
        when = render._at(snap.launch, r.u.generation, snap.gen_seconds, "%a %b %d, %I:%M %p")
        line1 = "REWIND · the story so far"
        line2 = f"gen {r.u.generation:,} · {when} · live again in {max(0, int(r.start + REWIND_S - now))} s"
        f1, f2 = font("bold", 30), font("regular", 22)
        w = int(max(d.textlength(line1, font=f1), d.textlength(line2, font=f2))) + 48
        x0, y0 = int(cx - w / 2), top + 18  # whole pixels: older Pillow rejects some fractional rounded boxes
        d.rounded_rectangle((x0, y0, x0 + w, y0 + 96), radius=12, fill=(5, 6, 9, 225), outline=(255, 255, 255, 90))
        d.text((cx, y0 + 30), line1, font=f1, fill=WHITE, anchor="mm")
        d.text((cx, y0 + 64), line2, font=f2, fill=LIGHT, anchor="mm")
        frac = min(1.0, (now - r.start) / REWIND_S)
        d.rounded_rectangle((x0 + 24, y0 + 82, x0 + w - 24, y0 + 86), radius=2, fill=(40, 44, 54))
        d.rounded_rectangle((x0 + 24, y0 + 82, x0 + 24 + max(6, int((w - 48) * frac)), y0 + 86), radius=2, fill=WHITE)

    def _axes(self, d, shape) -> None:
        """Coordinates along the top and left, so a viewer can aim 'glider x,y'."""
        h, w = shape
        cell, left, top = geometry(shape)
        every = 16 if cell >= 8 else 32
        f = font("regular", 15)
        for x in range(0, w, every):
            d.text((left + (x + 0.5) * cell, top - 8), str(x), font=f, fill=MUTED, anchor="ms")
        for y in range(0, h, every // 2):
            d.text((left - 8, top + (y + 0.5) * cell), str(y), font=f, fill=MUTED, anchor="rm")
        d.text((left + w * cell, top - 8), "x →" if DEJAVU else "x", font=f, fill=MUTED, anchor="rs")

    def _moves(self, d, now: float, shape) -> None:
        """New moves: a ripple from where they landed, then a label for a while."""
        placed: list[tuple] = []
        cell, left, top = geometry(shape)
        bottom = top + shape[0] * cell
        for t in self.tags:
            age = now - t.t0
            if age >= TAG_S:
                continue
            cx, cy = centre_px(t.cells, shape)
            for k in range(3):
                p = (age - k * 0.4) / (RIPPLE_S - 0.8)
                if 0 < p < 1:
                    r = 8 + p * 130
                    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, int(230 * (1 - p))), width=3)
            f = font("bold", 19)
            label = tidy(t.text)
            tw = d.textlength(label, font=f)
            x0 = min(max(cx - tw / 2 - 9, left + 4), left + shape[1] * cell - tw - 22)
            y0 = cy - 46 if cy - 46 > top + 4 else cy + 18
            y0 = free_row(y0, x0, x0 + tw + 18, placed, top + 4, bottom - 30)
            placed.append((x0, x0 + tw + 18, y0))
            alpha = int(255 * min(1.0, (TAG_S - age) / 5))
            d.rounded_rectangle((x0, y0, x0 + tw + 18, y0 + 28), radius=6, fill=(255, 255, 255, alpha))
            d.text((x0 + 9, y0 + 14), label, font=f, fill=(10, 12, 16, alpha), anchor="lm")

    def _column(self, d, now: float) -> None:
        snap, u = self.snap, self.snap.u
        x, y = COL_X, BOARD_Y
        d.ellipse((x, y + 6, x + 16, y + 22), fill=(240, 60, 60))
        d.text((x + 26, y + 14), "LIVE", font=font("bold", 24), fill=WHITE, anchor="lm")
        d.text((x + 100, y + 14), f"universe #{snap.universe_no}", font=font("regular", 22), fill=MUTED, anchor="lm")
        y += 64
        stuck = snap.forecast["stuck_gen"] if snap.forecast and u.phase == ALIVE else None
        big, small = render.headline(u, snap.gen_seconds, stuck, FORECAST_GENS if snap.forecast else None, snap.launch)
        small = re.sub(r" · gen [\d,]+$", "", small)
        size = fit(d, big, "bold", 54, COL_W, 34)
        for line in wrap(d, big, "bold", size, COL_W):
            d.text((x, y), line, font=font("bold", size), fill=PHASE_COLOR[u.phase], anchor="lt")
            y += int(size * 1.15)
        for line in wrap(d, small, "regular", 24, COL_W):
            d.text((x, y + 4), line, font=font("regular", 24), fill=LIGHT, anchor="lt")
            y += 31
        y += 34
        d.text((x, y), "generation", font=font("regular", 22), fill=MUTED, anchor="lt")
        d.text((x, y + 26), f"{u.generation:,}", font=font("bold", 52), fill=WHITE, anchor="lt")
        y += 92
        if u.phase != DIED:
            nxt = snap.launch + (u.generation + 1) * snap.gen_seconds
            left = max(0, int(nxt - now))
            d.text((x, y), f"next in {left // 60}:{left % 60:02d}", font=font("regular", 22), fill=MUTED, anchor="lt")
            frac = 1 - min(1.0, max(0.0, (nxt - now) / snap.gen_seconds))
            d.rounded_rectangle((x, y + 32, x + COL_W, y + 38), radius=3, fill=(40, 44, 54))
            d.rounded_rectangle((x, y + 32, x + max(6, int(COL_W * frac)), y + 38), radius=3, fill=PHASE_COLOR[u.phase])
        y += 58
        checked = checked_text(snap.checked, now)
        if checked:  # so a viewer knows when their comment will show up
            d.text((x, y), checked, font=font("regular", 22), fill=LIGHT, anchor="lt")
            d.text((x, y + 28), "(about every 5 min)", font=font("regular", 19), fill=MUTED, anchor="lt")
            y += 64
        y += 18
        d.text((x, y), "Latest moves", font=font("bold", 24), fill=WHITE, anchor="lt")
        y += 38
        for r in snap.recent:
            what, _, effect = r.partition(" · ")  # "gen 716: random 5x5 at 47,17" · "8d 21h → 2d 23h"
            lines = [(ln, LIGHT) for ln in wrap(d, what, "regular", 20, COL_W)]
            lines += [(ln, effect_colour(effect)) for ln in wrap(d, tidy(effect), "regular", 20, COL_W)]
            if y + 26 * len(lines) > BOARD_Y + BOARD_H:
                break
            for line, colour in lines:
                d.text((x, y), line, font=font("regular", 20), fill=colour, anchor="lt")
                y += 26
            y += 10
        if not snap.recent:
            d.text((x, y), "none yet: be the first!", font=font("regular", 19), fill=LIGHT, anchor="lt")

    def _bottom(self, d) -> None:
        snap, u = self.snap, self.snap.u
        x, y = BOARD_X, BOARD_Y + BOARD_H + 34
        width = SW - 2 * BOARD_X
        head = "Every chat message adds cells. Some save it. Some kill it."
        d.text((x, y), head, font=font("bold", 36), fill=WHITE, anchor="lt")
        try_one = f"try {render.suggestion(u)} (a quiet corner)   ·   " if u.phase != DIED else ""
        line = f"Type it in the chat (or comment on youtu.be/{snap.video})   ·   {try_one}or just say anything"
        size = fit(d, line, "regular", 27, width, 18)
        d.text((x, y + 54), line, font=font("regular", size), fill=LIGHT, anchor="lt")
        cmds = (f"glider x,y [ne|nw|se|sw] · block x,y · blinker x,y · rpent x,y · acorn x,y · cells x,y x,y …"
                f"   ·   every move so far: {render.LIVE_ADDRESS}")
        size = fit(d, cmds, "regular", 22, width, 15)
        d.text((x, y + 98), cmds, font=font("regular", size), fill=MUTED, anchor="lt")


# ---------------------------------------------------------------- the loop
class Puller(threading.Thread):
    """Every PULL_S: if GitHub has a new commit, git pull, then load the state if
    the log grew. Notices new code."""

    def __init__(self, repo: str, state_dir: str, seq: int, pull: bool):
        super().__init__(daemon=True)
        self.repo, self.state_dir, self.seq, self.pull = repo, state_dir, seq, pull
        self.pending: Snapshot | None = None
        self.restart = False
        self.lock = threading.Lock()
        self.code = self._code()

    def _code(self) -> str | None:
        if not self.pull:
            return None
        r = subprocess.run(["git", "-C", self.repo, "rev-parse", *[f"HEAD:{p}" for p in CODE]],
                           capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None

    def _unchanged(self) -> bool:
        """True if GitHub's branch is at our commit (when it can't tell: pull anyway)."""
        try:
            remote = subprocess.run(["git", "-C", self.repo, "ls-remote", "origin", "HEAD"], capture_output=True,
                                    text=True, timeout=30).stdout.split()
            local = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
                                   text=True, timeout=30).stdout.strip()
        except Exception:  # noqa: BLE001
            return False
        return bool(remote) and remote[0] == local

    def run(self) -> None:
        while not self.restart:
            time.sleep(PULL_S)
            if self.pull:
                if self._unchanged():
                    continue
                r = subprocess.run(["git", "-C", self.repo, "pull", "--ff-only", "-q"], capture_output=True,
                                   text=True, timeout=120)
                if r.returncode:
                    log(f"git pull failed: {r.stderr.strip()[:300]}")
                    continue
                if self._code() != self.code:
                    log("new code: restarting")
                    self.restart = True
                    return
            try:
                with open(os.path.join(self.state_dir, "state.json"), encoding="utf-8") as f:
                    seq = json.load(f)["log"]["seq"]
                if seq != self.seq:
                    snap = load(self.state_dir, self.seq)
                    with self.lock:
                        self.pending, self.seq = snap, seq
                    log(f"state: gen {snap.u.generation} {snap.u.phase}, {len(snap.new)} new move(s)")
            except Exception as e:  # noqa: BLE001 - a half-pulled file: try again next minute
                log(f"load failed: {e!r}")

    def take(self) -> Snapshot | None:
        with self.lock:
            snap, self.pending = self.pending, None
        return snap


def ffmpeg_cmd(out: str, bitrate_k: int, audio_fd: int | None = None) -> list[str]:
    """Video from stdin; audio from the pipe `audio_fd` (16-bit stereo PCM), or silence."""
    b = f"{bitrate_k}k"
    audio = (["-thread_queue_size", "64", "-probesize", "32", "-analyzeduration", "0",  # don't wait for seconds of it
              "-f", "s16le", "-ar", str(stream_audio.SR), "-ac", "2", "-i", f"pipe:{audio_fd}"]
             if audio_fd is not None else ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={stream_audio.SR}"])
    return ["ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "warning",
            "-thread_queue_size", "16", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{SW}x{SH}",
            "-framerate", str(IN_FPS), "-i", "-", *audio,
            "-map", "0:v", "-map", "1:a", "-shortest",
            # YouTube: H.264, CBR, a keyframe every 2 s, AAC audio
            "-c:v", "libx264", "-preset", PRESET, "-pix_fmt", "yuv420p", "-r", str(OUT_FPS),
            "-g", str(2 * OUT_FPS), "-keyint_min", str(2 * OUT_FPS), "-sc_threshold", "0",
            "-b:v", b, "-minrate", b, "-maxrate", b, "-bufsize", f"{2 * bitrate_k}k", "-x264-params", "nal-hrd=cbr",
            "-c:a", "aac", "-b:a", "128k", "-ar", str(stream_audio.SR),
            *(["-f", "flv"] if out.startswith("rtmp") else []), out]


def redact(line: str, secret: str | None) -> str:
    return line.replace(secret, "[stream key]") if secret else line


def stream(repo: str, out: str, secret: str | None, seconds: float | None, pull: bool, bitrate_k: int,
           sound: bool = True, rewinds: bool = True) -> int:
    state_dir = os.path.join(repo, "state")
    snap = load(state_dir)
    screen = Screen(snap)
    audio_r = audio = None
    if sound:
        screen.sound = stream_audio.Soundtrack()
        screen.sound.warm()
        audio_r, audio_w = os.pipe()
        try:  # Linux: room for a few seconds of sound while ffmpeg starts
            import fcntl
            fcntl.fcntl(audio_w, 1031, 1 << 20)  # F_SETPIPE_SZ
        except (ImportError, OSError):
            pass
        audio = os.fdopen(audio_w, "wb", buffering=0)
    puller = Puller(repo, state_dir, snap.seq, pull)
    puller.start()
    proc = subprocess.Popen(ffmpeg_cmd(out, bitrate_k, audio_r), stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                            pass_fds=(audio_r,) if sound else ())
    if audio_r is not None:
        os.close(audio_r)  # ffmpeg has its own copy
    per_frame = stream_audio.SR // IN_FPS
    # Sound goes through its own thread: a write to one of ffmpeg's pipes may wait
    # while ffmpeg reads the other, and must not hold up the other one.
    sound_q: queue.Queue = queue.Queue(maxsize=10 * IN_FPS)

    def feed():
        while (chunk := sound_q.get()) is not None:
            try:
                audio.write(chunk)
            except OSError:
                return
    feeder = threading.Thread(target=feed, daemon=True)
    if audio:
        feeder.start()

    def relay():  # ffmpeg's messages, without the key
        for raw in proc.stderr:
            log("ffmpeg: " + redact(raw.decode(errors="replace").rstrip(), secret))
    threading.Thread(target=relay, daemon=True).start()

    # If frames stop going out, YouTube soon ends the broadcast for good, and nothing
    # here would notice (a stuck loop once froze it for hours with ffmpeg waiting
    # quietly). So: no frame for STALL_S, and the whole process exits. Restarts that
    # took ~12 s in all never ended a broadcast; this one takes ~15 + RestartSec 10.
    last_frame, finished = [time.monotonic()], threading.Event()

    def watchdog():
        while not finished.wait(min(5.0, STALL_S / 4)):
            if time.monotonic() - last_frame[0] > STALL_S:
                log(f"no frame for {STALL_S} s: exiting so the service starts again")
                os._exit(1)
    threading.Thread(target=watchdog, daemon=True).start()

    log(f"streaming universe #{snap.universe_no} from gen {snap.u.generation} ({snap.u.phase})")
    t0, n, data, last_key, behind = time.monotonic(), 0, b"", None, 0.0
    try:
        while not puller.restart and (seconds is None or n < seconds * IN_FPS):
            now = time.time()
            new = puller.take()
            if new is not None:
                screen.swap(new, now)
            rewinding = screen.rewind is not None
            screen.tick(now, state_dir if rewinds else None)
            if rewinding and screen.rewind is None:
                log(f"rewind done; the frames ran at most {max(0.0, behind):.2f} s behind")
                behind = 0.0
            k = screen.key(now)
            if not data or k != last_key or screen.animating(now):
                data, last_key = screen.compose(now).tobytes(), k
            if audio:
                try:
                    sound_q.put(screen.sound.chunk(per_frame), timeout=10)
                except queue.Full:
                    raise BrokenPipeError("ffmpeg stopped reading the sound") from None
            proc.stdin.write(data)
            last_frame[0] = time.monotonic()
            n += 1
            delay = t0 + n / IN_FPS - time.monotonic()
            if screen.rewind is not None:
                behind = max(behind, -delay)
            if delay > 0:  # behind (a slow frame): write the next ones straight away to catch up
                time.sleep(delay)
    except BrokenPipeError:
        log("ffmpeg stopped (connection lost?)")
        return 1
    finally:
        finished.set()
        if audio:
            try:
                sound_q.put(None, timeout=1)
            except queue.Full:
                pass
            feeder.join(timeout=5)
        for f in (audio, proc.stdin):
            try:
                if f:
                    f.close()
            except BrokenPipeError:
                pass
        proc.wait(timeout=30)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Stream the live universe (see the module docstring).")
    ap.add_argument("--repo", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--out", help="a file or rtmp(s) URL (default: YouTube, with STREAM_KEY)")
    ap.add_argument("--seconds", type=float, help="stop after this long (testing)")
    ap.add_argument("--preview", help="write one frame to this PNG and stop")
    ap.add_argument("--no-pull", action="store_true", help="use the checkout as it is")
    ap.add_argument("--bitrate", type=int, default=5500, help="video kbit/s (YouTube's 1080p minimum is 5000)")
    ap.add_argument("--no-sound", action="store_true", help="a silent track (also: STREAM_SOUND=off)")
    args = ap.parse_args(argv)

    if args.preview:
        now = time.time()
        snap = load(os.path.join(args.repo, "state"))
        screen = Screen(snap)
        entries = [json.loads(ln) for ln in open(os.path.join(args.repo, "state", "log.jsonl"), encoding="utf-8")]
        effects = {(e["gen"], e["command"]): e for e in entries if e["type"] == "effect"}
        for i, e in enumerate([e for e in entries if e["type"] == "injection"][-3:]):  # as if just committed
            eff = effects.get((e["gen"], e["command"]))
            suffix = render.effect_suffix(eff, snap.u.flatline_gens, snap.gen_seconds) if eff else ""
            screen.tags.append(Tag(e["command"] + suffix, [tuple(c) for c in e["cells"]], now - 0.5 - 0.5 * i))
        os.makedirs(os.path.dirname(os.path.abspath(args.preview)), exist_ok=True)
        screen.compose(now).save(args.preview)
        print(args.preview)
        return 0

    secret = os.environ.get("STREAM_KEY", "").strip() or None
    out = args.out or (INGEST + secret if secret else None)
    if not out:
        print("set STREAM_KEY (YouTube Studio → Go live → Stream key) or pass --out", file=sys.stderr)
        return 2
    sound = not args.no_sound and os.environ.get("STREAM_SOUND", "on").strip().lower() not in ("0", "off", "false", "no")
    return stream(args.repo, out, secret, args.seconds, not args.no_pull, args.bitrate, sound)


if __name__ == "__main__":
    sys.exit(main())
