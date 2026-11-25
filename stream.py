#!/usr/bin/env python3
"""Stream the live universe to YouTube, around the clock.

    STREAM_KEY=... python stream.py                  # live, from this checkout
    python stream.py --out test.flv --seconds 60     # a minute to a file
    python stream.py --preview out/stream.png        # one frame (with the latest moves rippling)

A window onto the same universe the main video's comments play. It reads the
public state from this git checkout (pulling every minute), steps the board one
generation every 2 minutes on the wall clock exactly as the tick does, and
when the tick commits new comments it replays them: back to the generation
the first one landed in, each landing with a ripple and a label, then forward
to the live board. It calls no API and needs no secret except the stream key, which comes
from the environment, is redacted from ffmpeg's messages and is never written
anywhere.

When a pull brings new code it exits, so the service restarts on the new
version (systemd Restart=always). If ffmpeg's connection drops it exits too,
for the same restart, and so it does if no frame has gone out for 15 s.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import commands
import explainer as ex
import life
import render
import stream_audio
from engine import FORECAST_GENS
from render import BG, GRID, HIGHLIGHT, PHASE_COLOR
from universe import ALIVE, CONTESTED, DIED, FLATLINING, Universe, target_generation

SW, SH = 1920, 1080
BOARD_X, BOARD_Y, BOARD_W, BOARD_H = 56, 60, 1408, 792  # the board's box (128 x 72 at 11 px)
COL_X, COL_W = 1500, 380  # the column on the right
# The time-left graph: the last RECENT_H hours, every move drawn, take the right
# RECENT_SHARE of it; the whole life before that is drawn smoothed on the left
# (move by move it crowded: hundreds of moves a day in a few hundred pixels).
RECENT_H, RECENT_SHARE = 6, 0.45
# Frames drawn per second; ffmpeg repeats them up to OUT_FPS. Encoding is most
# of the work (about half a fast core at 30 fps, a quarter at 10): STREAM_FPS=15
# if the machine falls behind (YouTube's guidance is 30).
IN_FPS, OUT_FPS = 10, int(os.environ.get("STREAM_FPS", "30"))
PRESET = os.environ.get("X264_PRESET", "veryfast")  # the preset barely matters for this picture
LOOK = os.environ.get("STREAM_LOOK", "3d").strip().lower()  # "3d": the board as lit blocks on a tipped floor (board3d.py); "flat"
RISE_STEPS = 6  # in 3D, a new generation's cells rise (and dying ones sink) in this many steps over FADE_S
# In 3D the screen times its own drawing. The live board changes only every few seconds
# (a generation, a tail fading on, a move): when one takes longer than LOOK_BUDGET_S (the
# median of the last LOOK_SAMPLES) it draws simpler from then on: without the smoothing,
# then without the glow, then flat. While the past or a future plays (a new board every
# frame) the board is drawn plain (no smoothing, no glow) and only as often as it can be
# without the stream falling behind: the frames between show the last one (PLAY_SHARE).
LOOK_BUDGET_S, LOOK_SAMPLES = 0.25, 10
# The creature cam (3D only): the whole board's cells are too small for their faces (11 px, a few on a phone),
# so for the first CAM_HOLD_S of every CAM_EVERY_S, while nothing plays, the board fades to a CAM_W x CAM_H
# window, cells about 3x bigger: on the latest move if it's recent (CAM_MOVE_S), else the liveliest spot.
CAM = os.environ.get("STREAM_CAM", "1") != "0"
CAM_EVERY_S, CAM_HOLD_S, CAM_FADE_S, CAM_MOVE_S = 180, 15, 0.8, 1800
CAM_W, CAM_H = 40, 22
PLAY_SHARE = 0.6  # at most this share of the time goes to drawing the playing board
# A rewind is known ahead (the log up to its slot), so a niced process draws its boards in
# full (smoothing, glow) from PREBUILD_LEAD_S before it, into CACHE; the rewind plays them.
PREBUILD_LEAD_S = 12 * 60
CACHE = os.environ.get("STREAM_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "still-life-stream")
PULL_S = 15  # how often to ask GitHub for a new commit (one small ls-remote; it pulls only then)
STALL_S = 15  # no frame for this long (a bug, or ffmpeg stuck): exit, and the service restarts it
REWIND_EVERY_S, REWIND_S = 30 * 60, 60.0  # at :00 and :30, a minute of the story so far
REWIND_LIT = 30  # generations a comment's cells stay white during the rewind
REWIND_TAGS, REWIND_TAG_S = 10, 3.0  # during the rewind, the moves that changed the most get a label
# Its future if nobody comments, at :15 and :45 (between the rewinds): the board races
# into the loop the forecast says it gets stuck in (to FUTURE_RACE of the way), the
# loop plays (to FUTURE_LOOP), then it goes grey as it dies.
FUTURE_AT_S, FUTURE_S = 15 * 60, 40.0
FUTURE_RACE, FUTURE_LOOP, FUTURE_LOOP_GEN_S = 0.55, 0.85, 0.4
REWIND_NEWS_S = 4.0  # ...and its banner says what was new (docs/changes.json) as it passes when it came
# The instant replay of new moves: the board as it was, each move landing, the generations since.
REPLAY_HOLD_S, REPLAY_LAND_S, REPLAY_GEN_S, REPLAY_MAX_S = 1.2, 1.8, 1.0, 12.0
REWIND_SOUND, REPLAY_SOUND = 0.15, 0.3  # their notes' level: they play many at once (~ one live generation)
FADE_S = 0.6  # crossfade between generations
TRAIL_STEPS = 8  # the newest tails fade on in 8 steps through a live generation (life.Trail)
RIPPLE_S, TAG_S, GLOW_S = 3.0, 90, 600  # a new move: ripple, label, its cells lit
GHOST = (150, 156, 172)  # an eraser's shape ("glider 40,20 black"), outlined where its cells died, fading over GLOW_S
SCREEN = (5, 6, 9)
MUTED, LIGHT, WHITE = (125, 131, 145), (222, 225, 232), (255, 255, 255)
ERASE_INK = (0, 0, 0)  # "black" in the legend: a black chip (CHIP_EDGE around it) with white letters
CHIP_EDGE = (96, 102, 118)
LEGEND = [("glider_se", "glider", "x,y ne|nw|se|sw"), ("block", "block", "x,y"), ("blinker", "blinker", "x,y"),
          ("rpent", "rpent", "x,y"), ("acorn", "acorn", "x,y"), ("random", "random", "x,y"),
          (None, "cells", "x,y x,y …")]  # (pattern, what to type)
DEMO_BOX, DEMO_CELL, DEMO_RATE = 9, 5, 4  # the legend's little films: 9 x 9 cells of 5 px, 4 generations a second
MAIN_VIDEO = "oFWeLMe4bhI"  # universe #1; later universes keep their video in state.json
INGEST = "rtmps://a.rtmps.youtube.com/live2/"
CODE = ("stream.py", "stream_audio.py", "short.py", "render.py", "universe.py", "life.py", "explainer.py", "engine.py",
        "commands.py", "admission.py", "store.py", "board3d.py")  # a change to these restarts the stream


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


def effect_color(effect: str):
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
    deaths: list = field(default_factory=list)  # (gen, the gen it would die at, or None: beyond the forecast)
    paint: np.ndarray | None = None  # the cells' colors (life.paint_step); None while no move has asked for one
    trail: "life.Trail | None" = None  # where cells were lately, for the tails (display only)
    comments_seen: dict = field(default_factory=dict)  # the bot's count: {"total", "day", "today"} (engine.count_comments)
    people: int = 0  # how many people have made a move, in all (len(state.json "scores"): a count, ids hashed)
    scheduled: list = field(default_factory=list)  # [(generation, command)]: moves scheduled for later, soonest first


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
                    video=cfg.get("video_id") or MAIN_VIDEO, seq=s["log"]["seq"],
                    comments_seen=dict(s.get("comments_seen") or {}),
                    people=len(s.get("scores") or {}),
                    scheduled=sorted((q["land_gen"], render.move_text(q)) for q in (s.get("admission") or {}).get("queue", [])
                                     if q.get("scheduled") and "land_gen" in q))
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
    snap.deaths = death_history(state_dir, u.flatline_gens)
    snap.paint = live_paint(state_dir, u)
    snap.trail = life.Trail(u.grid.shape, *life.LIVE_TRAIL)
    snap.trail.see(u.grid, u.generation, snap.paint)
    step(snap, now if now is not None else time.time())
    return snap


def carry_trail(old: "life.Trail | None", snap: Snapshot) -> None:
    """Give a new state the tails so far (the same universe, a board the same size)."""
    if old is not None and snap.trail is not None and old.last.shape == snap.u.grid.shape and old.t <= snap.u.generation:
        snap.trail = old
        snap.trail.see(snap.u.grid, snap.u.generation, snap.paint)


def live_paint(state_dir: str, u: Universe) -> np.ndarray | None:
    """The colors on the live board, replayed from the log (the bot keeps no
    paint: it is only how cells look). None while no move has asked for one."""
    with open(os.path.join(state_dir, "log.jsonl"), encoding="utf-8") as f:
        if '"color"' not in f.read():
            return None
    import short
    t0 = time.monotonic()
    h = short.History(state_dir)
    r = h.fresh()
    h.goto(r, u.generation)
    if not np.array_equal(r.grid, u.grid):  # the log and the state disagree: show no paint rather than wrong paint
        log(f"paint: the replayed board differs from the state at gen {u.generation:,}")
        return None
    took = time.monotonic() - t0
    if took > 1.0:  # it replays every painted generation: if this grows, keep a checkpoint instead
        log(f"paint: replaying the log took {took:.1f} s")
    return r.paint


def death_history(state_dir: str, flatline: int) -> list[tuple[int, int | None]]:
    """When the universe was due to die if nobody commented, and how each move
    changed it: [(0, gen it would die at), (gen of a move, gen it would die at
    after it), ...]. None: not within the forecast. From the moves' effects."""
    out: list[tuple[int, int | None]] = []
    die = lambda stuck: None if stuck is None else stuck + flatline  # noqa: E731
    with open(os.path.join(state_dir, "log.jsonl"), encoding="utf-8") as f:
        for line in f:
            if '"effect"' not in line:
                continue
            e = json.loads(line)
            if e.get("type") != "effect":
                continue
            if not out:
                out.append((0, die(e["before"])))
            out.append((e["gen"], die(e["after"])))
    return out


def death_gen(snap: Snapshot) -> int | None:
    """The generation it dies at if nobody comments, as of now (None: not within the forecast)."""
    u = snap.u
    if u.phase == DIED:
        return u.died_gen
    if u.phase in (FLATLINING, CONTESTED):
        return u.deadline_gen
    if snap.forecast and snap.forecast.get("stuck_gen") is not None:
        return snap.forecast["stuck_gen"] + u.flatline_gens
    return None


def clock(seconds: int) -> str:
    """'2d 14:03:27', '14:03:27'."""
    days, rest = divmod(max(0, seconds), 86400)
    h, rest = divmod(rest, 3600)
    t = f"{h:02d}:{rest // 60:02d}:{rest % 60:02d}"
    return f"{days}d {t}" if days else t


def countdown_text(snap: Snapshot, now: float) -> str | None:
    """The live countdown under the headline: 'in 2d 14:03:27' (None when there's no date to count to)."""
    g = death_gen(snap)
    if g is None or snap.u.phase == DIED:
        return None
    t = clock(int(snap.launch + g * snap.gen_seconds - now))
    return {ALIVE: f"in {t}", FLATLINING: f"{t} left", CONTESTED: f"{t} to go"}[snap.u.phase]


def time_left_curve(snap: Snapshot) -> tuple[list[tuple[int, int]], list[tuple[int, int, int]]]:
    """The time it had left, in generations, from launch to now: (points (gen, gens
    left), jumps (gen, gens left before, after)). It falls one generation per
    generation, and jumps where a move changed when it would die."""
    cap = FORECAST_GENS + snap.u.flatline_gens  # 'not within the forecast' is drawn at the top
    left = lambda die, g: cap if die is None else min(cap, max(0, die - g))  # noqa: E731
    now_gen, current = snap.u.generation, death_gen(snap)
    history = snap.deaths or [(0, current)]
    die = history[0][1]
    pts, jumps = [(0, left(die, 0))], []
    for g, after in history[1:] + [(now_gen, current)]:
        g = min(g, now_gen)
        a, b = left(die, g), left(after, g)
        pts.append((g, a))
        if a != b:
            pts.append((g, b))
            jumps.append((g, a, b))
        die = after
    return pts, jumps


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


def rewind_text(now: float) -> str:
    """'next rewind in 12:34': when the story so far plays next."""
    if rewind_slot(now) is not None:
        return "rewinding: the story so far"
    left = int((now // REWIND_EVERY_S + 1) * REWIND_EVERY_S - now)
    return f"next rewind in {left // 60}:{left % 60:02d}"


def curve_until(pts: list, g: float) -> list:
    """The time-left curve up to generation `g`, ending exactly there (on the
    line between the two points either side of it)."""
    out = [p for p in pts if p[0] <= g]
    nxt = next((p for p in pts if p[0] > g), None)
    if out and nxt and out[-1][0] < g:
        (g0, v0), (g1, v1) = out[-1], nxt
        out.append((g, v0 + (v1 - v0) * (g - g0) / (g1 - g0)))
    return out


def curve_from(pts: list, g: float) -> list:
    """The time-left curve from generation `g` on, starting exactly there."""
    return [(g, curve_until(pts, g)[-1][1])] + [p for p in pts if p[0] > g]


def typical(pts: list, g0: float, g1: float, n: int) -> list[tuple[float, float, float]]:
    """The time-left curve from generation g0 to g1 in n equal slices (one per
    pixel): each slice's (lowest, highest, average)."""
    gs = np.array([p[0] for p in pts], float)
    vs = np.array([p[1] for p in pts], float)
    edges = np.linspace(g0, g1, n + 1)
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        both = np.concatenate([vs[(gs >= a) & (gs < b)], np.interp([a, b], gs, vs)])
        out.append((float(both.min()), float(both.max()), float(np.interp(np.linspace(a, b, 24), gs, vs).mean())))
    return out


def ago(secs: float) -> str:
    """'just now', '12 min ago', '2h 05m ago', '1d 3h ago': how long ago a move came in."""
    secs = max(0.0, secs)
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    return f"{render.short_span(secs)} ago"


def step(snap: Snapshot, now: float) -> bool:
    """Advance to the generation the wall clock says (as the tick will). True if it moved."""
    target = target_generation(now, snap.launch, snap.gen_seconds)
    if snap.u.phase == DIED or target <= snap.u.generation:
        return False
    painted = snap.paint is not None and snap.paint.any()
    if painted or target - snap.u.generation <= life.TRAIL_GENS:  # a generation at a time: the paint and tails follow
        while snap.u.generation < target and snap.u.phase != DIED:
            before = snap.u.grid
            snap.u.advance_to(snap.u.generation + 1)
            if painted:
                snap.paint = life.paint_step(before, snap.u.grid, snap.paint)
            if snap.trail is not None:
                snap.trail.see(snap.u.grid, snap.u.generation, snap.paint)
    else:
        snap.u.advance_to(target)
        if snap.trail is not None:  # a long jump (a restart): no tails across it
            snap.trail.see(snap.u.grid, snap.u.generation, snap.paint)
    return True


# ---------------------------------------------------------------- drawing
@dataclass
class Tag:
    text: str
    cells: list  # (x, y)
    t0: float
    erase: bool = False  # an eraser's: its cells are outlined (GHOST), not lit


def geometry(shape) -> tuple[int, int, int]:
    """(cell px, left, top) for a board of `shape` (h, w), centred in the board box."""
    h, w = shape
    cell = max(2, min(BOARD_W // w, BOARD_H // h))
    return cell, BOARD_X + (BOARD_W - w * cell) // 2, BOARD_Y + (BOARD_H - h * cell) // 2


def board_rgb(grid: np.ndarray, phase: str, lit: np.ndarray | None, paint: np.ndarray | None = None,
              trail: tuple | None = None, ghost: np.ndarray | None = None, ghost_a: float = 1.0) -> np.ndarray:
    """The board as pixels: each cell's color worked out on the small grid, then
    scaled up once, with the grid lines painted over (strided writes; no
    full-size masks, so a frame stays cheap with the tails on). `ghost`: an
    eraser's cells, outlined where they are dead, at strength `ghost_a`."""
    h, w = grid.shape
    cell = geometry(grid.shape)[0]
    cells = np.empty((h, w, 3), np.uint8)
    cells[:] = BG
    if trail is not None and phase != DIED:  # the tails: cells that just died, fading into the background
        a, col = trail
        on = a > 0.004
        if on.any():
            lut = np.array([PHASE_COLOR[phase]] + [render.PAINT[c] for c in commands.COLORS], np.float32)
            cells[on] = (np.array(BG, np.float32) * (1 - a[on, None]) + lut[col[on]] * a[on, None]).astype(np.uint8)
    alive = grid == 1
    if paint is None or phase == DIED or not paint.any():  # a dead universe is grey, paint and all
        cells[alive] = PHASE_COLOR[phase]
    else:  # 0: unpainted (the phase color); 1..: the paint
        lut = np.array([PHASE_COLOR[phase]] + [render.PAINT[c] for c in commands.COLORS], np.uint8)
        cells[alive] = lut[paint[alive]]
    if lit is not None and lit.any():
        m = alive & (lit == 1)
        if paint is not None and phase != DIED:
            m &= paint == 0  # a color shows from the moment it lands
        cells[m] = HIGHLIGHT
    img = cells.repeat(cell, 0).repeat(cell, 1)
    for k in (0, cell - 1):  # each cell's edge: the grid lines
        img[k::cell, :] = GRID
        img[:, k::cell] = GRID
    if ghost is not None and ghost_a > 0:  # where an eraser hit: the outline of its shape (a few cells)
        ink = (np.array(BG, float) + (np.array(GHOST, float) - BG) * min(1.0, ghost_a)).astype(np.uint8)
        for y, x in zip(*np.nonzero((ghost == 1) & ~alive)):
            y0, x0, y1, x1 = y * cell + 1, x * cell + 1, (y + 1) * cell - 2, (x + 1) * cell - 2
            img[y0, x0:x1 + 1] = img[y1, x0:x1 + 1] = ink
            img[y0:y1 + 1, x0] = img[y0:y1 + 1, x1] = ink
    return img


def centre_cells(cells, shape) -> tuple[float, float]:
    """A pattern's centre on the board, in cells (a cell's centre is +0.5), taking the torus wrap into account."""
    h, w = shape
    x0, y0 = cells[0]
    xs = [x0 + (x - x0 + w // 2) % w - w // 2 for x, _ in cells]
    ys = [y0 + (y - y0 + h // 2) % h - h // 2 for _, y in cells]
    return (sum(xs) / len(xs)) % w + 0.5, (sum(ys) / len(ys)) % h + 0.5


def centre_px(cells, shape) -> tuple[float, float]:
    """Screen centre of a pattern's cells on the flat board, taking the torus wrap into account."""
    cell, left, top = geometry(shape)
    cx, cy = centre_cells(cells, shape)
    return left + cx * cell, top + cy * cell


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


def future_slot(now: float) -> float | None:
    """When the look at the future running at `now` began (at :15 and :45), or None."""
    slot = (now - FUTURE_AT_S) // REWIND_EVERY_S * REWIND_EVERY_S + FUTURE_AT_S
    return slot if now - slot < FUTURE_S else None


class Future:
    """Its future if nobody comments: a copy of the live board raced ahead into the
    loop the bot's forecast says it gets stuck in, the loop playing, then grey: it
    dies FLATLINE hours into the loop. (Already stuck: the loop, then grey at its
    deadline.) No moves land in it: it's the future with nobody commenting. A move
    arriving ends it (Screen.swap)."""

    def __init__(self, snap: Snapshot, start: float):
        u = snap.u
        self.start, self.launch, self.G = start, snap.launch, snap.gen_seconds
        self.stuck_now = u.phase == FLATLINING
        self.from_gen, self.dies = u.generation, death_gen(snap)
        if self.stuck_now:
            self.stuck = u.generation
        else:
            self.stuck = (snap.forecast or {}).get("stuck_gen")
            if self.stuck is not None:
                self.stuck = max(self.stuck, u.generation)
        # no loop within the forecast: race three days on, and say so
        self.to = self.stuck if self.stuck is not None else u.generation + 3 * 86400 // self.G
        self.race = 1.0 if self.stuck is None else 0.0 if self.stuck_now else FUTURE_RACE
        self.u = copy.deepcopy(u)
        self.u.paint = snap.paint.copy() if snap.paint is not None and snap.paint.any() else None
        self.trail, self.frame = life.Trail(u.grid.shape), 0
        self.tags: list[Tag] = []

    def done(self, now: float) -> bool:
        return now >= self.start + FUTURE_S

    def part(self, now: float) -> str:
        """'race' (into the loop), 'loop', or 'dies' (grey)."""
        p = (now - self.start) / FUTURE_S
        if p < self.race:
            return "race"
        return "loop" if p < FUTURE_LOOP else "dies"

    def advance(self, now: float, sound=None) -> None:
        t = max(0.0, now - self.start)
        q = t / (self.race * FUTURE_S) if self.race else 1.0
        if q < 1:  # fast at first, easing into the loop
            target = self.from_gen + round((self.to - self.from_gen) * (1 - (1 - q) ** 2))
        else:  # the loop, a generation at a time
            target = self.to + int((t - self.race * FUTURE_S) / FUTURE_LOOP_GEN_S)
        if self.part(now) == "dies":
            self.u.phase = DIED  # grey (and still): how it ends, a day into the loop, not simulated that far
        moved = False
        while self.u.generation < target and self.u.phase != DIED:
            before = self.u.grid
            self.u.advance_to(self.u.generation + 1)
            if self.u.paint is not None:
                self.u.paint = life.paint_step(before, self.u.grid, self.u.paint)
            moved = True
        if moved:
            self.frame += 1
            self.trail.see(self.u.grid, self.frame, self.u.paint)

    def lit_mask(self) -> np.ndarray | None:
        return None


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
        # the moves that changed its lifespan the most: labelled as they land
        stuck = lambda e, k: e["gen"] + FORECAST_GENS if e[k] is None else e[k]  # noqa: E731
        swing = lambda e: abs(stuck(e, "after") - stuck(e, "before"))  # noqa: E731
        big = sorted((e for e in self.h.effects.values() if swing(e)), key=lambda e: (-swing(e), e["gen"]))
        self.big = {(e["gen"], e["command"]): e for e in big[:REWIND_TAGS]}
        self.seen: set[int] = set()  # generations whose moves have landed
        self.tags: list[Tag] = []
        self.trail, self.frame = life.Trail(self.u.grid.shape), 0  # tails by drawn frame: streaks as it races
        self.news = changes(state_dir, self.h.gen_seconds)  # [(gen, text)]: what was new, and when
        self.news_shown: tuple[str, float] | None = None

    def done(self, now: float) -> bool:
        return now >= self.start + REWIND_S

    def advance(self, now: float, sound=None) -> None:
        p = min(1.0, max(0.0, (now - self.start) / REWIND_S))
        target = int(round(self.end * p))
        if target > self.u.generation:
            cells = self.h.goto(self.u, target)
            self.frame += 1
            self.trail.see(self.u.grid, self.frame, getattr(self.u, "paint", None))
            if cells:
                self.lit.append((self.u.generation, cells))
                if sound:
                    sound.landed(cells[0][0], self.u.grid.shape[1], REWIND_SOUND)
        self.lit = [(g, c) for g, c in self.lit if self.u.generation - g < REWIND_LIT]
        for g in sorted(self.h.applied - self.seen):
            self.seen.add(g)
            for e in self.h.batches[g]:
                eff = self.big.get((g, e["command"]))
                if eff and e["cells"]:
                    text = render.move_label(e) + render.effect_suffix(eff, self.u.flatline_gens, self.h.gen_seconds)
                    self.tags.append(Tag(text, [tuple(c) for c in e["cells"]], now, bool(e.get("erase"))))
        self.tags = [t for t in self.tags if now - t.t0 < REWIND_TAG_S]
        while self.news and self.news[0][0] <= self.u.generation:
            self.news_shown = (self.news.pop(0)[1], now)
        if sound and now - self.pool_at >= 0.3:
            sound.generation(self.pool, self.u.grid, gain=REWIND_SOUND)
            self.pool, self.pool_at = self.u.grid.copy(), now

    def lit_mask(self) -> np.ndarray | None:
        return mask_of([c for _, cells in self.lit for c in cells], self.u.grid.shape)


def changes(state_dir: str, gen_seconds: int) -> list[tuple[int, str]]:
    """What changed in the game and when (docs/changes.json next to the state), as
    (generation, text), oldest first; [] if there's no list."""
    path = os.path.join(os.path.dirname(os.path.abspath(state_dir)), "docs", "changes.json")
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
        with open(os.path.join(state_dir, "state.json"), encoding="utf-8") as f:
            launch = json.load(f)["config"]["launch_time"]
    except (OSError, ValueError, KeyError):
        return []
    return sorted((max(0, int((c["at"] - launch) // gen_seconds)), str(c["text"])) for c in items if "at" in c)


def mask_of(cells, shape) -> np.ndarray | None:
    """A board-sized 0/1 mask of these cells (None if there are none)."""
    if not cells:
        return None
    m = np.zeros(shape, np.uint8)
    h, w = shape
    for x, y in cells:
        m[y % h, x % w] = 1
    return m


class Replay:
    """New moves, played back from the log: the board as it was just before the
    first one landed, each landing (lit, with its ripple, chime and label), and
    the generations since, up to the live board. The moves land in the past
    (each at the generation it was sent), so without this they'd only show up
    already a few generations old."""

    def __init__(self, state_dir: str, moves: list[dict], start: float, live_gen: int, label):
        import short
        self.h = short.History(state_dir)
        self.u = self.h.fresh()
        self.first = min(e["gen"] for e in moves)
        self.live = max(live_gen, self.first)
        self.h.goto(self.u, self.first - 1)
        self.u.advance_to(self.first)  # the moment before the first one lands
        self.new: dict[int, list[dict]] = {}
        for e in moves:
            self.new.setdefault(e["gen"], []).append(e)
        gens = range(self.first, self.live + 1)
        pause = lambda g: REPLAY_LAND_S if g in self.new else REPLAY_GEN_S  # noqa: E731
        squeeze = min(1.0, (REPLAY_MAX_S - REPLAY_HOLD_S) / sum(pause(g) for g in gens))  # long ones go faster
        t, self.steps = start + REPLAY_HOLD_S, []
        for g in gens:
            self.steps.append((t, g))
            t += pause(g) * squeeze
        self.start, self.end, self.label = start, t, label
        self.lit: list = []  # the new moves' cells, drawn white
        self.ghost: list = []  # the new erasers' cells, outlined
        self.shown = self.first - 1  # the last generation played (before the first: none yet)
        self.trail = life.Trail(self.u.grid.shape, *life.LIVE_TRAIL)  # the live board's past: its tails
        self.trail.see(self.u.grid, self.u.generation, getattr(self.u, "paint", None))

    def landed(self) -> bool:
        """All its moves have landed (the panel can show what they did)."""
        return not any(g in self.new for _, g in self.steps)

    def done(self, now: float) -> bool:
        return now >= self.end

    def advance(self, now: float, sound=None) -> list[Tag]:
        """Play the generations that are due. The labels of the moves that landed."""
        tags = []
        while self.steps and self.steps[0][0] <= now:
            t, g = self.steps.pop(0)
            before = self.u.grid.copy()
            self.h.goto(self.u, g)
            self.shown = g
            self.trail.see(self.u.grid, g, getattr(self.u, "paint", None))
            for e in self.new.get(g, []):
                tags.append(self.label(e, t))
                (self.ghost if e.get("erase") else self.lit).extend(tuple(c) for c in e["cells"])
                if sound and e["cells"]:
                    sound.landed(e["cells"][0][0], self.u.grid.shape[1], REPLAY_SOUND)
            if sound:
                sound.generation(before, self.u.grid, gain=REPLAY_SOUND)
        return tags

    def lit_mask(self) -> np.ndarray | None:
        return mask_of(self.lit, self.u.grid.shape)

    def ghost_mask(self) -> np.ndarray | None:
        return mask_of(self.ghost, self.u.grid.shape)


def rewind_dir(slot: float) -> str:
    return os.path.join(CACHE, f"rewind-{int(slot)}")


def prebuild_rewind(repo: str, slot: float) -> str:
    """Draw the rewind that starts at `slot` ahead of time: each frame's board as it will
    play at IN_FPS, in full (smoothing and glow), into rewind_dir(slot) as k.png. meta.json
    comes first ({end, gens: each frame's generation, moves: the generations with moves},
    so the frames can be used as they appear) and "done" last. Frames already there from
    the same log are kept (a restart picks up where it was)."""
    import board3d
    state_dir = os.path.join(repo, "state")
    snap = load(state_dir, now=slot)
    r = Rewind(state_dir, slot, snap.u.generation)
    n = int(REWIND_S * IN_FPS)
    times = [slot + k / IN_FPS for k in range(n)]
    gens = [int(round(r.end * min(1.0, (t - slot) / REWIND_S))) for t in times]  # as Rewind.advance steps
    look = [board3d.TILT, board3d.HEIGHT, board3d.MARGIN, board3d.GLOW, board3d.GLOW_BLUR, board3d.SS,
            board3d.EYES]  # a new look: drawn again
    meta = {"end": r.end, "gens": gens, "moves": sorted(int(g) for g in r.h.batches), "look": look}
    out = rewind_dir(slot)
    os.makedirs(out, exist_ok=True)
    try:
        with open(os.path.join(out, "meta.json"), encoding="utf-8") as fh:
            keep = json.load(fh) == meta
    except (OSError, ValueError):
        keep = False
    if not keep:
        for name in os.listdir(out):
            os.remove(os.path.join(out, name))
        with open(os.path.join(out, "meta.json.tmp"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        os.replace(os.path.join(out, "meta.json.tmp"), os.path.join(out, "meta.json"))
    v = board3d.View(r.u.grid.shape)
    for k, t in enumerate(times):
        r.advance(t)
        path = os.path.join(out, f"{k}.png")
        if keep and os.path.exists(path):
            continue
        img = v.render(r.u.grid, r.u.phase, r.lit_mask(), getattr(r.u, "paint", None), r.trail.layer(r.u.grid),
                       font=font)
        Image.fromarray(img).save(path + ".tmp", format="PNG", compress_level=1)
        os.replace(path + ".tmp", path)
    open(os.path.join(out, "done"), "w").close()
    return out


class Prebuilder:
    """Starts prebuild_rewind for the next rewind, PREBUILD_LEAD_S ahead, as a niced
    process (one at a time); clears out the old ones."""

    def __init__(self, repo: str):
        self.repo, self.proc, self.slot = repo, None, None

    def poll(self, now: float) -> None:
        nxt = (now // REWIND_EVERY_S + 1) * REWIND_EVERY_S
        if nxt - now > PREBUILD_LEAD_S or nxt == self.slot or (self.proc and self.proc.poll() is None):
            return
        self.slot = nxt
        try:
            if os.path.isdir(CACHE):
                for name in os.listdir(CACHE):  # rewinds that have played
                    if name.startswith("rewind-") and name[7:].isdigit() and int(name[7:]) < now - REWIND_S:
                        shutil.rmtree(os.path.join(CACHE, name), ignore_errors=True)
            if os.path.exists(os.path.join(rewind_dir(nxt), "done")):
                return
            cmd = [sys.executable, os.path.join(self.repo, "stream.py"), "--repo", self.repo,
                   "--prebuild-rewind", str(int(nxt))]
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, preexec_fn=lambda: os.nice(19))
            log(f"drawing the {time.strftime('%H:%M', time.localtime(nxt))} rewind ahead")
        except Exception as e:  # noqa: BLE001 - it plays without, drawn as it goes
            log(f"prebuild failed to start: {e!r}")


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
        self.replay: Replay | None = None
        self.after_replay: Snapshot | None = None  # the new snapshot, shown once its moves have played
        self.rewound_at: float | None = None  # the slot of the last rewind (one per slot)
        self.future: Future | None = None
        self.futured_at: float | None = None  # the slot of the last look at the future
        self._chrome = None  # (key, the frame without the board), see compose
        self._demos: list[tuple] = []  # (x, y, film) of the legend's little films, placed by _legend
        self.look3d = LOOK == "3d"  # the board (and the legend's films) in blocks: board3d.py
        self.demo_cell = DEMO_CELL + 1 if self.look3d else DEMO_CELL
        self._view = None  # board3d.View, for the board's shape
        self._grid_shown = None  # in 3D: the last board drawn, and the one it changed from (cells rise from it)
        self._grid_before = None
        self.simpler = 0  # 3D drawn simpler because it was slow: 1 no smoothing, 2 no glow either (3: flat)
        self._costs: list[float] = []
        self._play_cost = 0.0  # what a playing board took to draw (smoothed), and when the last one was drawn
        self._play_at = -1e9
        self._pre = None  # the prebuilt rewind being played: (dir, meta, first new move's gen, checked at)
        self._pre_frame = (None, None)  # (path, board) last shown from it
        self._cam = None  # the creature cam's visit: (its start, (x0, y0) its window's top left, its View)
        self._cam_img, self._cam_key = None, None
        self._cam_last = None  # the window of the visit before, so the next one looks somewhere else
        self._last_move = None  # (when it landed, its cells): where the creature cam looks first

    # A new snapshot (the tick committed) or a new generation (the clock).
    def swap(self, snap: Snapshot, now: float, state_dir: str | None = None) -> None:
        """With `state_dir`, new moves are replayed first (see Replay); the panel
        (countdown, graph, latest moves) changes when the replay reaches now."""
        if self.replay:  # another update before the last replay finished: finish it now
            for t in self.replay.advance(float("inf")):
                self.tags.append(t)
            self._end_replay(now)
        same = snap.universe_no == self.snap.universe_no
        if not same:
            self.tags = []
        moves = [e for e in snap.new if e.get("cells")]
        if moves:
            self._last_move = (now, [tuple(c) for c in moves[-1]["cells"]])
        if self.future and moves:  # someone commented: that future is gone
            self.future = None
            self._changed(now)
        if state_dir and same and moves and self.rewind is None and snap.u.phase != DIED:
            try:
                self.replay = Replay(state_dir, moves, now, snap.u.generation, lambda e, t: self._tag(snap, e, t))
                self.after_replay = snap
                log(f"replay: {len(moves)} new move(s), gen {self.replay.first:,} to {self.replay.live:,}")
                return
            except Exception as e:  # noqa: BLE001 - no replay this time; show them as before
                log(f"replay failed: {e!r}")
                self.replay = self.after_replay = None
        for e in snap.new:
            self.tags.append(self._tag(snap, e, now))
            if self.sound and e["cells"]:
                self.sound.landed(e["cells"][0][0], snap.u.grid.shape[1])
        if self.sound and snap.universe_no == self.snap.universe_no:  # the comments' cells appearing
            self.sound.generation(self.snap.u.grid, snap.u.grid, abs(snap.u.generation - self.snap.u.generation) + 1)
        self._changed(now)
        carry_trail(self.snap.trail if same else None, snap)
        self.snap = snap

    @staticmethod
    def _tag(snap: Snapshot, e: dict, t0: float) -> Tag:
        eff = snap.effects.get((e["gen"], e["command"]))
        suffix = render.effect_suffix(eff, snap.u.flatline_gens, snap.gen_seconds) if eff else ""
        return Tag(render.move_label(e) + suffix, [tuple(c) for c in e["cells"]], t0, bool(e.get("erase")))

    def _end_replay(self, now: float) -> None:
        r, self.replay = self.replay, None
        if self.after_replay is not None:
            self.snap, self.after_replay = self.after_replay, None
            carry_trail(r.trail if r is not None else None, self.snap)  # its tails carry on from the replay
            step(self.snap, now)
        self._changed(now)  # crossfade from the replay to the live board

    def tick(self, now: float, state_dir: str | None = None) -> None:
        before, gen = (self.snap.u.grid.copy(), self.snap.u.generation) if self.sound else (None, 0)
        if step(self.snap, now):
            self._changed(now)
            if self.sound and not self.rewind and not self.replay and not self.future:
                self.sound.generation(before, self.snap.u.grid, self.snap.u.generation - gen)
        if self.replay:
            if self.after_replay is not None:
                step(self.after_replay, now)  # its clock runs on too (its generation, 'next in')
            self.tags += self.replay.advance(now, self.sound)
            if self.replay.done(now):
                self._end_replay(now)
        slot = rewind_slot(now)
        if (state_dir and slot is not None and self.rewind is None and self.replay is None
                and slot != self.rewound_at and self.snap.u.phase != DIED):
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
        fslot = future_slot(now)
        if (state_dir and fslot is not None and self.future is None and self.rewind is None and self.replay is None
                and fslot != self.futured_at and self.snap.u.phase in (ALIVE, FLATLINING)):
            self.futured_at = fslot
            try:
                self.future = Future(self.snap, fslot)
                log(f"future: gen {self.future.from_gen:,} to {self.future.to:,} if nobody comments")
            except Exception as e:  # noqa: BLE001 - not this time; the live board goes on
                log(f"future failed: {e!r}")
        if self.future:
            self.future.advance(now)
            if self.future.done(now):
                self.future = None
                self._changed(now)
        if self.sound:
            playing = self.rewind or self.replay or self.future
            self.sound.set_phase(playing.u.phase if playing else self.snap.u.phase)

    def panel(self) -> Snapshot:
        """What the column and the graph show: during a replay, the new snapshot
        once its moves have landed (so the countdown jumps as they land)."""
        r = self.replay
        return self.after_replay if r and self.after_replay is not None and r.landed() else self.snap

    def _changed(self, now: float) -> None:
        self.prev = self.cur
        self.changed_at = now
        self._grid_before = self._grid_shown

    def animating(self, now: float) -> bool:
        return (self.rewind is not None or self.replay is not None or self.future is not None
                or now - self.changed_at < FADE_S
                or any(now - t.t0 < RIPPLE_S for t in self.tags))

    def key(self, now: float):
        """Changes whenever the picture would: the board, the countdown's second, the labels."""
        u = self.snap.u
        live = tuple(id(t) for t in self.tags if now - t.t0 < TAG_S)
        return (id(self.snap), u.generation, u.phase, int(now), live, int(now * DEMO_RATE))

    def board(self, now: float) -> np.ndarray:
        playing = self.rewind or self.replay or self.future
        if playing:
            r = playing.u
            ghost = playing.ghost_mask() if hasattr(playing, "ghost_mask") else None
            self.cur, self.cur_key = board_rgb(r.grid, r.phase, playing.lit_mask(), getattr(r, "paint", None),
                                               playing.trail.layer(r.grid), ghost), None
            return self.cur
        u = self.snap.u
        lit, ghost, ghost_a, frac, k = self._live(now)
        if k != self.cur_key or self.cur is None:
            tails = self.snap.trail.layer(u.grid, frac) if self.snap.trail is not None else None
            self.cur, self.cur_key = board_rgb(u.grid, u.phase, lit, self.snap.paint, tails, ghost, ghost_a), k
        a = (now - self.changed_at) / FADE_S
        if self.prev is not None and self.prev.shape == self.cur.shape and 0 <= a < 1:
            return (self.prev * (1 - a) + self.cur * a).astype(np.uint8)
        self.prev = None
        return self.cur

    def _live(self, now: float):
        """The live board's layers: (lit cells, an eraser's ghost, its strength, how far the
        newest tails have faded on, a key that changes whenever any of it does)."""
        u = self.snap.u
        self.tags = [t for t in self.tags if now - t.t0 < max(TAG_S, GLOW_S)]
        glowing = [c for t in self.tags if now - t.t0 < GLOW_S and not t.erase for c in t.cells]
        erasers = [t for t in self.tags if now - t.t0 < GLOW_S and t.erase]
        ghost = mask_of([c for t in erasers for c in t.cells], u.grid.shape) if u.phase != DIED else None
        ghost_a = round(1 - (now - max(t.t0 for t in erasers)) / GLOW_S, 1) if erasers else 0.0  # fades in 10 steps
        lit = None
        if glowing and u.phase != DIED:
            lit = np.zeros(u.grid.shape, np.uint8)
            for x, y in glowing:
                lit[y % u.grid.shape[0], x % u.grid.shape[1]] = 1
        # the newest tails fade on through the generation, in TRAIL_STEPS steps
        since = (now - self.snap.launch) / self.snap.gen_seconds - u.generation
        frac = min(TRAIL_STEPS - 1, max(0, int(since * TRAIL_STEPS))) / TRAIL_STEPS
        return lit, ghost, ghost_a, frac, (id(self.snap), u.generation, u.phase, len(glowing), frac, len(erasers), ghost_a)

    def view(self, shape):
        if self._view is None or self._view.shape != tuple(shape):
            import board3d
            self._view = board3d.View(tuple(shape))
        return self._view

    def cam(self, now: float):
        """The creature cam now: ((x0, y0) its window's top left, its View, how far faded in, 0..1), or None:
        the whole board. On for the first CAM_HOLD_S of every CAM_EVERY_S, while nothing plays."""
        if not (CAM and self.look3d) or self.rewind or self.replay or self.future:
            return None
        t = now % CAM_EVERY_S
        if t >= CAM_HOLD_S:
            return None
        start = now - t
        if self._cam is None or self._cam[0] != start:  # a new visit: where it looks is chosen once
            import board3d
            spot = self._cam_spot(now)
            self._cam_last = spot
            self._cam = (start, spot, board3d.View((CAM_H, CAM_W), origin=spot, full=self.snap.u.grid.shape,
                                                   steps=(4, 4)))
            self._cam_key = None
        return self._cam[1], self._cam[2], max(0.0, min(1.0, t / CAM_FADE_S, (CAM_HOLD_S - t) / CAM_FADE_S))

    def _cam_spot(self, now: float) -> tuple[int, int]:
        """Where the creature cam looks (its window's top left, wrapping): the latest move, once, if it landed
        in the last CAM_MOVE_S; else the window with the most creatures and lately the most change, away from
        the last visit's."""
        u = self.snap.u
        h, w = u.grid.shape
        if self._last_move and now - self._last_move[0] < CAM_MOVE_S and getattr(self, "_cam_moved", None) != self._last_move[0]:
            self._cam_moved = self._last_move[0]
            cx, cy = centre_cells(self._last_move[1], u.grid.shape)
            return int(round(cx - CAM_W / 2)) % w, int(round(cy - CAM_H / 2)) % h
        m = u.grid.astype(np.float64)
        if self.snap.trail is not None:
            m = m + 3 * (self.snap.trail.layer(u.grid)[0] > 0.004)  # where cells died lately: things happening
        p = np.pad(np.pad(m, ((0, CAM_H), (0, CAM_W)), mode="wrap").cumsum(0).cumsum(1), ((1, 0), (1, 0)))
        s = p[CAM_H:CAM_H + h, CAM_W:CAM_W + w] - p[:h, CAM_W:CAM_W + w] - p[CAM_H:CAM_H + h, :w] + p[:h, :w]
        if self._cam_last is not None:  # somewhere else than last time
            lx, ly = self._cam_last
            dx = np.abs((np.arange(w) - lx + w // 2) % w - w // 2)
            dy = np.abs((np.arange(h) - ly + h // 2) % h - h // 2)
            s[(dy[:, None] < CAM_H // 2) & (dx[None, :] < CAM_W // 2)] = -1
        y0, x0 = np.unravel_index(int(np.argmax(s)), s.shape)
        return int(x0), int(y0)

    def _cam_label(self, d, cam) -> None:
        """Where the creature cam is, in board coordinates, so anyone can still aim a move at what they see."""
        (x0, y0), cv, a = cam
        h, w = self.snap.u.grid.shape
        text = f"Up close · x {x0}–{(x0 + CAM_W - 1) % w}, y {y0}–{(y0 + CAM_H - 1) % h}"
        f = font("bold", 26)
        x, y = cv.area[0] + 12, cv.area[3] - 50
        al = int(255 * a)
        d.rounded_rectangle((x, y, x + d.textlength(text, font=f) + 28, y + 42), radius=12,
                            fill=(8, 12, 16, int(220 * a)), outline=(120, 220, 160, al), width=2)
        d.text((x + 14, y + 21), text, font=f, fill=(230, 240, 235, al), anchor="lm")

    def board3d(self, now: float) -> np.ndarray:
        """The board in blocks (board3d.View's area). A change of board (a generation, a
        move landing, the end of a rewind) raises the new cells and sinks the dying ones
        over FADE_S, in RISE_STEPS steps, instead of the flat board's crossfade."""
        v = self.view(self.snap.u.grid.shape)
        playing = self.rewind or self.replay or self.future
        if playing and self.rewind is not None:
            pre = self._prebuilt(self.rewind)
            if pre is not None and pre.shape[:2] == (v.size[1], v.size[0]):
                self._grid_shown = self.rewind.u.grid.copy()
                self.cur3d, self.cur3d_key = pre, None
                return pre
        if playing:  # a new board every frame: plain, no rising, as often as there's time for
            if getattr(self, "cur3d_key", None) is None and getattr(self, "cur3d", None) is not None \
                    and now - self._play_at < self._play_cost / PLAY_SHARE:
                return self.cur3d  # still drawing the last one's share: show it a frame longer
            r = playing.u
            ghost = playing.ghost_mask() if hasattr(playing, "ghost_mask") else None
            self._grid_shown = r.grid.copy()
            t0 = time.perf_counter()
            self.cur3d = v.render(r.grid, r.phase, playing.lit_mask(), getattr(r, "paint", None),
                                  playing.trail.layer(r.grid), ghost, font=font, glow=False, ss=1)
            cost = time.perf_counter() - t0
            self._play_cost = cost if self._play_cost == 0 else 0.7 * self._play_cost + 0.3 * cost
            self._play_at, self.cur3d_key = now, None
            return self.cur3d
        u = self.snap.u
        lit, ghost, ghost_a, frac, k = self._live(now)
        a = (now - self.changed_at) / FADE_S
        before = self._grid_before
        rising = before is not None and before.shape == u.grid.shape and 0 <= a < 1 and not np.array_equal(before, u.grid)
        rise = min(RISE_STEPS, int(a * RISE_STEPS) + 1) / RISE_STEPS if rising else 1.0
        k = (k, rise)
        if k != getattr(self, "cur3d_key", None):
            tails = self.snap.trail.layer(u.grid, frac) if self.snap.trail is not None else None
            self.cur3d = self._timed(lambda: v.render(
                u.grid, u.phase, lit, self.snap.paint, tails, ghost, ghost_a, before=before if rising else None,
                rise=rise, font=font, glow=self.simpler < 2, ss=1 if self.simpler else None))
            self.cur3d_key = k
        if not rising:
            self._grid_shown = u.grid.copy()
        cam = self.cam(now)
        if cam is None:
            return self.cur3d
        (x0, y0), cv, fa = cam  # the creature cam: the same board, its window drawn big, faded in over the whole
        if (k, x0, y0) != self._cam_key:
            h, w = u.grid.shape
            ys, xs = (np.arange(CAM_H) + y0) % h, (np.arange(CAM_W) + x0) % w

            def win(m):
                return None if m is None else m[np.ix_(ys, xs)]

            tails = self.snap.trail.layer(u.grid, frac) if self.snap.trail is not None else None
            self._cam_img = cv.render(win(u.grid), u.phase, win(lit), win(self.snap.paint),
                                      None if tails is None else (win(tails[0]), win(tails[1])), win(ghost), ghost_a,
                                      before=win(before) if rising else None, rise=rise, font=font,
                                      glow=self.simpler < 2, ss=1 if self.simpler else None)
            self._cam_key = (k, x0, y0)
        if fa >= 1:
            return self._cam_img
        return (self.cur3d * (1 - fa) + self._cam_img * fa).astype(np.uint8)

    def _prebuilt(self, r) -> np.ndarray | None:
        """This rewind's board drawn ahead (prebuild_rewind), or None: not there (yet), or
        drawn before a move that has landed since (from that move's generation on)."""
        import bisect
        d, wall = rewind_dir(r.start), time.monotonic()
        if self._pre is None or self._pre[0] != d or (self._pre[1] is None and wall - self._pre[3] > 1.0):
            try:
                with open(os.path.join(d, "meta.json"), encoding="utf-8") as fh:
                    meta = json.load(fh)
                new = set(r.h.batches) - set(meta["moves"])
                self._pre = (d, meta, min(new) if new else float("inf"), wall)
            except (OSError, ValueError, KeyError):
                self._pre = (d, None, 0, wall)
        _, meta, first_new, _ = self._pre
        if meta is None or abs(meta["end"] - r.end) > 2:
            return None
        k = bisect.bisect_right(meta["gens"], r.u.generation) - 1
        if k < 0 or meta["gens"][k] >= first_new:
            return None
        path = os.path.join(d, f"{k}.png")
        if self._pre_frame[0] == path:
            return self._pre_frame[1]
        try:
            with Image.open(path) as im:
                board = np.asarray(im.convert("RGB"))
        except OSError:
            return None
        self._pre_frame = (path, board)
        return board

    def _timed(self, draw):
        """draw(), timed: past LOOK_BUDGET_S (the median of LOOK_SAMPLES), draw simpler from now on."""
        t0 = time.perf_counter()
        out = draw()
        self._costs.append(time.perf_counter() - t0)
        if len(self._costs) >= LOOK_SAMPLES:
            cost = sorted(self._costs)[len(self._costs) // 2]
            self._costs = []
            if cost > LOOK_BUDGET_S:
                self.simpler += 1
                log(f"3D board took {cost * 1000:.0f} ms: drawing it "
                    + ("without smoothing", "without glow", "flat")[min(self.simpler, 3) - 1])
                if self.simpler >= 3:  # flat again, frame and numbers and all
                    self.look3d, self._chrome, self.prev = False, None, None
        return out

    def _box(self, shape) -> tuple[float, float, float]:
        """The board's centre x, top and bottom on the frame (the 3D board's far and near edges)."""
        if self.look3d:
            v = self.view(shape)
            return v.cx, v.top, v.bottom
        cell, left, top = geometry(shape)
        return left + shape[1] * cell / 2, top, top + shape[0] * cell

    def _centre(self, cells, shape, z: float = 0.0) -> tuple[float, float]:
        """Where a move's cells are on the frame (in 3D, `z` cells up off the floor)."""
        if self.look3d:
            bx, by = centre_cells(cells, shape)
            p = self.view(shape).at([(bx, by, z)])[0]
            return float(p[0]), float(p[1])
        return centre_px(cells, shape)

    def compose(self, now: float) -> Image.Image:
        u = self.snap.u
        shape = u.grid.shape
        cell, left, top = geometry(shape)
        # Everything but the board changes at most once a second: drawn then, reused
        # for the other frames (the rewind and the ripples draw every frame).
        playing = self.rewind is not None or self.replay is not None
        panel = self.panel()
        k = (int(now), id(self.snap), u.generation, u.phase, playing, id(panel), panel.u.generation)
        if self._chrome is None or self._chrome[0] != k:
            img = np.empty((SH, SW, 3), np.uint8)
            img[:] = SCREEN
            im = Image.fromarray(img)
            d = ImageDraw.Draw(im, "RGBA")
            alarm = u.phase in (FLATLINING, CONTESTED)  # the edge says it's in trouble, whatever the paint
            if not self.look3d:  # (in 3D the floor has its edge and numbers; the alarm goes over it, below)
                d.rectangle((left - 3 if alarm else left - 1, top - 3 if alarm else top - 1,
                             left + shape[1] * cell + (2 if alarm else 0), top + shape[0] * cell + (2 if alarm else 0)),
                            outline=PHASE_COLOR[u.phase] if alarm else (42, 46, 58), width=3 if alarm else 1)
                self._axes(d, shape)
            self._column(d, now)
            self._bottom(d)
            if not playing:  # while the past plays, the graph follows it every frame
                self._graph(d)
            self._chrome = (k, np.asarray(im))
        img = self._chrome[1].copy()
        if self.look3d:
            v = self.view(shape)
            b = self.board3d(now)
            ax, ay = v.area[:2]
            img[ay:ay + b.shape[0], ax:ax + b.shape[1]] = b
        else:
            b = self.board(now)
            img[top:top + b.shape[0], left:left + b.shape[1]] = b
        im = Image.fromarray(img)
        d = ImageDraw.Draw(im, "RGBA")
        cam = self.cam(now) if self.look3d else None
        if self.look3d and u.phase in (FLATLINING, CONTESTED):  # the edge says it's in trouble
            v = cam[1] if cam is not None and cam[2] > 0.5 else self.view(shape)
            d.line(v.edge + v.edge[:1], fill=PHASE_COLOR[u.phase], width=3, joint="curve")
        if cam is not None:
            self._cam_label(d, cam)
        self._films(d, now)
        if playing:
            self._graph(d)
        # the banner goes last, over the move labels (which also steer clear of it)
        if self.rewind:
            self._moves(d, now, shape, self.rewind.tags, REWIND_TAG_S, avoid=self._banner_rows(shape, self._box(shape)[1] + 18))
            self._rewind_banner(d, now, shape)
        elif self.replay:
            y0 = self._replay_y0(shape)
            self._moves(d, now, shape, avoid=self._banner_rows(shape, y0))
            self._replay_banner(d, now, shape, y0)
        elif self.future:  # no move labels: nobody has commented in the future it shows
            self._future_banner(d, now, shape)
        elif cam is None:  # (up close, the labels would point at the whole board's places: none)
            self._moves(d, now, shape)
        return im

    def _replay_y0(self, shape) -> int:
        """The replay banner's top: at the top of the board, or the bottom if a move landed up there."""
        _, top, bottom = self._box(shape)
        high = any(self._centre(e["cells"], shape)[1] < top + 150 for v in self.replay.new.values() for e in v)
        return bottom - 114 if high else top + 18

    def _replay_banner(self, d, now: float, shape, y0: int) -> None:
        """'REPLAY · 3 new moves', and when, and how long until it's live again."""
        r, snap = self.replay, self.snap
        n = sum(len(v) for v in r.new.values())
        line1 = f"REPLAY · {'a new move' if n == 1 else f'{n} new moves'}"
        when = render._at(snap.launch, r.u.generation, snap.gen_seconds, "%I:%M %p").lstrip("0")
        line2 = f"gen {r.u.generation:,} · {when} · live again in {max(0, math.ceil(r.end - now))} s"
        self._banner(d, shape, y0, line1, line2, (now - r.start) / max(0.1, r.end - r.start))

    def _rewind_banner(self, d, now: float, shape) -> None:
        """What the board is showing: the past, when, and how long until it's live again."""
        r, snap = self.rewind, self.snap
        _, top, _ = self._box(shape)
        when = render._at(snap.launch, r.u.generation, snap.gen_seconds, "%a %b %d, %I:%M %p")
        line1 = "REWIND · the story so far"
        if r.news_shown and now - r.news_shown[1] < REWIND_NEWS_S:  # it just passed something new
            line1 = f"NEW · {r.news_shown[0]}"
        line2 = f"gen {r.u.generation:,} · {when} · live again in {max(0, int(r.start + REWIND_S - now))} s"
        self._banner(d, shape, top + 18, line1, line2, (now - r.start) / REWIND_S)

    def _future_banner(self, d, now: float, shape) -> None:
        """What the future shows: when it gets there, the loop, the day it dies."""
        f = self.future
        top = self._box(shape)[1]
        at = lambda g: render._at(f.launch, g, f.G, "%a %I:%M %p").replace(" 0", " ")  # noqa: E731
        live = f"live again in {max(0, int(f.start + FUTURE_S - now))} s"
        part = f.part(now)
        if f.stuck is None:
            line1 = "THE FUTURE · if nobody comments"
            line2 = f"{at(f.u.generation)} · no loop in sight for {FORECAST_GENS * f.G // 86400} days · {live}"
        elif part == "race":
            line1, line2 = "THE FUTURE · if nobody comments", f"{at(f.u.generation)} · {live}"
        elif part == "loop":
            hours = f.u.flatline_gens * f.G // 3600
            if f.stuck_now:
                line1, line2 = "IT'S STUCK IN A LOOP", f"a comment can break it · {live}"
            else:
                line1, line2 = f"STUCK IN A LOOP · {at(f.stuck)}", f"then {hours} hours to break it · {live}"
        else:
            line1 = f"IT DIES · {at(f.dies)}" if f.dies is not None else "IT DIES"
            line2 = f"unless someone comments or chats · {live}"
        self._banner(d, shape, top + 18, line1, line2, (now - f.start) / FUTURE_S)

    def _banner(self, d, shape, y0: int, line1: str, line2: str, frac: float) -> None:
        cx = self._box(shape)[0]
        f1, f2 = font("bold", 30), font("regular", 22)
        w = int(max(d.textlength(line1, font=f1), d.textlength(line2, font=f2))) + 48
        x0, y0 = int(cx - w / 2), int(y0)  # whole pixels: older Pillow rejects some fractional rounded boxes
        d.rounded_rectangle((x0, y0, x0 + w, y0 + 96), radius=12, fill=(5, 6, 9, 225), outline=(255, 255, 255, 90))
        d.text((cx, y0 + 30), line1, font=f1, fill=WHITE, anchor="mm")
        d.text((cx, y0 + 64), line2, font=f2, fill=LIGHT, anchor="mm")
        frac = min(1.0, max(0.0, frac))
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

    def _banner_rows(self, shape, y0: int) -> list[tuple]:
        """The banner's box as label rows, so labels steer clear of it (see free_row)."""
        cx = self._box(shape)[0]
        return [(cx - 330, cx + 330, y0 + k * 30) for k in range(4)]

    def _moves(self, d, now: float, shape, tags: list | None = None, life: float = TAG_S,
               avoid: list | None = None) -> None:
        """New moves: a ripple from where they landed, then a label for `life` seconds."""
        placed: list[tuple] = list(avoid or [])
        cell, left, top = geometry(shape)
        lo_x, hi_x = left, left + shape[1] * cell
        if self.look3d:  # the labels keep inside the 3D board's area; the ripples spread on its floor
            import board3d
            v = self.view(shape)
            _, top, bottom = self._box(shape)
            lo_x, hi_x = v.area[0] + 8, v.area[2] - 8
            ring = np.linspace(0, 2 * math.pi, 49)
        else:
            bottom = top + shape[0] * cell
        fade = min(5.0, life / 3)
        for t in self.tags if tags is None else tags:
            age = now - t.t0
            if age >= life:
                continue
            if self.look3d:
                bx, by = centre_cells(t.cells, shape)
                cx, cy = self._centre(t.cells, shape, board3d.HEIGHT)
            else:
                cx, cy = centre_px(t.cells, shape)
            for k in range(3):
                p = (age - k * 0.4) / (RIPPLE_S - 0.8)
                if 0 < p < 1:
                    r = 8 + p * 130
                    ink = (255, 255, 255, int(230 * (1 - p)))
                    if self.look3d:  # a ring on the floor, as wide (in cells) as the flat board's
                        rc = r / cell
                        pts = v.at(np.stack([bx + rc * np.cos(ring), by + rc * np.sin(ring)], 1))
                        d.line([tuple(q) for q in pts], fill=ink, width=3, joint="curve")
                    else:
                        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=ink, width=3)
            f = font("bold", 19)
            label = tidy(t.text)
            tw = d.textlength(label, font=f)
            x0 = min(max(cx - tw / 2 - 9, lo_x + 4), hi_x - tw - 22)
            y0 = cy - 46 if cy - 46 > top + 4 else cy + 18
            y0 = free_row(y0, x0, x0 + tw + 18, placed, top + 4, bottom - 30)
            placed.append((x0, x0 + tw + 18, y0))
            alpha = int(255 * min(1.0, (life - age) / fade))
            d.rounded_rectangle((x0, y0, x0 + tw + 18, y0 + 28), radius=6, fill=(255, 255, 255, alpha))
            d.text((x0 + 9, y0 + 14), label, font=f, fill=(10, 12, 16, alpha), anchor="lm")

    def _column(self, d, now: float) -> None:
        snap = self.panel()
        u = snap.u
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
        count = countdown_text(snap, now)
        if count:  # ticks every second; jumps when a move lands
            d.text((x, y + 2), count, font=font("bold", 42), fill=WHITE, anchor="lt")
            y += 54
        for line in wrap(d, small, "regular", 24, COL_W):
            d.text((x, y + 4), line, font=font("regular", 24), fill=LIGHT, anchor="lt")
            y += 31
        y += 26
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
            d.text((x, y + 28), "(every minute while people play, else 3)", font=font("regular", 19), fill=MUTED, anchor="lt")
            y += 64
        if u.phase != DIED:  # the story so far, every half hour
            text, extra = rewind_text(now), "  (every 30 min)"
            d.text((x, y), text, font=font("regular", 22), fill=LIGHT, anchor="lt")
            tw = d.textlength(text, font=font("regular", 22))
            if tw + d.textlength(extra, font=font("regular", 19)) <= COL_W and not self.rewind:
                d.text((x + tw, y + 3), extra, font=font("regular", 19), fill=MUTED, anchor="lt")
            y += 34
        y += 18
        d.text((x, y), "Latest moves", font=font("bold", 24), fill=WHITE, anchor="lt")
        m = snap.comments_seen
        if m.get("total"):  # every comment read so far (skipped ones too), and today (Day N, as the Shorts count)
            today = m.get("today", 0) if m.get("day") == int(u.generation * snap.gen_seconds // 86400) + 1 else 0
            who = f" · {snap.people:,} people" if snap.people else ""
            tally = f"{m['total']:,} comments{who} · {today:,} today"
            if d.textlength(tally, font=font("regular", 19)) > COL_W:  # (the server's font runs wider)
                tally = f"{m['total']:,} comments{who}"
            d.text((x, y + 32), tally, font=font("regular", 19), fill=MUTED, anchor="lt")
            y += 26
        soon = [(g, c) for g, c in snap.scheduled if g > u.generation]
        if soon:  # moves people scheduled for later ("rpent 1 hr")
            g, what = soon[0]
            left = (g - u.generation) * snap.gen_seconds
            line = f"coming up: {what} " + ("in under a minute" if left < 60 else f"in {ago(left).removesuffix(' ago')}")
            more = f"  (+{len(soon) - 1} more)" if len(soon) > 1 else ""
            line = line if d.textlength(line + more, font=font("regular", 19)) > COL_W else line + more
            d.text((x, y + 32), line, font=font("regular", 19), fill=LIGHT, anchor="lt")
            y += 26
        y += 38
        f20 = font("regular", 20)
        for r in snap.recent:
            what, _, effect = r.partition(" · ")  # "gen 716: random 5x5 at 47,17" · "8d 21h → 2d 23h"
            lines = [(ln, LIGHT) for ln in wrap(d, what, "regular", 20, COL_W)]
            lines += [(ln, effect_color(effect)) for ln in wrap(d, tidy(effect), "regular", 20, COL_W)]
            g = render.line_gen(r)
            age = f"   {ago(now - snap.launch - g * snap.gen_seconds)}" if g is not None else ""
            inline = bool(age) and d.textlength(lines[-1][0] + age, font=f20) <= COL_W
            if age and not inline:
                lines.append((age.strip(), MUTED))
            if y + 26 * len(lines) > BOARD_Y + BOARD_H:
                break
            for line, color in lines:
                d.text((x, y), line, font=f20, fill=color, anchor="lt")
                y += 26
            if inline:  # when it came in, after how it changed things
                d.text((x + d.textlength(lines[-1][0], font=f20), y - 26), age, font=f20, fill=MUTED, anchor="lt")
            y += 10
        if not snap.recent:
            d.text((x, y), "none yet: be the first!", font=font("regular", 19), fill=LIGHT, anchor="lt")

    def _bottom(self, d) -> None:
        snap, u = self.snap, self.snap.u
        x, y = BOARD_X, BOARD_Y + BOARD_H + 34
        width = COL_X - BOARD_X - 30  # the time-left graph has the corner
        head = "Every chat message adds cells. Some save it. Some kill it."
        d.text((x, y), head, font=font("bold", 36), fill=WHITE, anchor="lt")
        try_one = f"try {render.suggestion(u)} (a quiet corner)   ·   " if u.phase != DIED else ""
        line = f"Type it in the chat (or comment on youtu.be/{snap.video})   ·   {try_one}or just say anything"
        size = fit(d, line, "regular", 27, width, 18)
        d.text((x, y + 54), line, font=font("regular", size), fill=LIGHT, anchor="lt")
        self._legend(d, x, y + 98, width)

    def _legend(self, d, x, y, width) -> None:
        """What each move does: a little film of it (see _films) beside what to type."""
        iw = DEMO_BOX * self.demo_cell
        for size in (22, 21, 19, 17, 15):  # the largest that fits
            fb, fr = font("bold", size), font("regular", size)
            total = sum(iw + 9 + d.textlength(name + " ", font=fb) + d.textlength(args, font=fr) + 30
                        for _, name, args in LEGEND)
            if total <= width:
                break
        mid, x0 = y + 14, x
        self._demos = []
        for pattern, name, args in LEGEND:
            top = int(mid - iw / 2)
            d.rounded_rectangle((x - 2, top - 2, x + iw + 1, top + iw + 1), radius=4, fill=(16, 18, 24),
                                outline=(42, 46, 58))
            self._demos.append((x, top, render.demo(pattern or "cells", DEMO_BOX)))
            x += iw + 9
            d.text((x, mid), name + " ", font=fb, fill=LIGHT, anchor="lm")
            x += d.textlength(name + " ", font=fb)
            d.text((x, mid), args, font=fr, fill=MUTED, anchor="lm")
            x += d.textlength(args, font=fr) + 30
        f18 = font("regular", 18)
        # the fullest wording that fits the board's width (the server's font is wider than a Mac's):
        # the long rule, then the short one, then the live page's address alone, then without it
        for first, tail in (("one move per message, up to 3 per person every 2 min", "every move so far: "),
                            ("up to 3 moves per person every 2 min", "every move so far: "),
                            ("up to 3 moves per person every 2 min", ""),
                            ("up to 3 moves per person every 2 min", None)):
            parts = [(first + "   ·   add a color: ", MUTED)] + [(c + " ", render.PAINT[c]) for c in commands.COLORS]
            parts += [("  ·   ", MUTED), (commands.ERASE, ERASE_INK), (" erases", MUTED)]  # "glider 40,20 black"
            chip = 12  # the black chip's padding, beside its word
            if tail is not None:
                parts += [(f"  ·   {tail}{render.LIVE_ADDRESS}", MUTED)]
            if sum(d.textlength(s, font=f18) for s, _ in parts) + chip <= width:
                break
        for s, color in parts:
            if color == ERASE_INK:  # "black" as a black swatch: light letters on a black chip, like a color's own ink
                tw = d.textlength(s, font=f18)
                d.rounded_rectangle((x0, mid + 40 - 12, x0 + tw + 12, mid + 40 + 12), radius=5,
                                    fill=ERASE_INK, outline=CHIP_EDGE)
                d.text((x0 + 6, mid + 40), s, font=f18, fill=WHITE, anchor="lm")
                x0 += tw + 12
                continue
            d.text((x0, mid + 40), s, font=f18, fill=color, anchor="lm")
            x0 += d.textlength(s, font=f18)

    def _films(self, d, now: float) -> None:
        """The legend's films, a frame each 1/DEMO_RATE s: so something always moves."""
        k = int(now * DEMO_RATE)
        color = PHASE_COLOR[ALIVE]
        c = self.demo_cell
        if self.look3d:  # little blocks: a lit top standing up off a darker front and side
            base = np.array(color, float)
            top_c = tuple(int(min(255, v * 1.06 + 10)) for v in base)
            front_c, side_c = tuple(int(v * 0.6) for v in base), tuple(int(v * 0.45) for v in base)
            lift, w = max(2, round(c * 0.4)), c - 2
            for x, top, film in self._demos:
                on = film[k % len(film)]
                for cy, cx in sorted(map(tuple, np.argwhere(on))):  # far rows first: nearer blocks cover them
                    px, py = x + cx * c, top + cy * c + lift
                    d.rectangle((px, py - lift + w, px + w, py + w), fill=front_c)
                    if not (cx + 1 < on.shape[1] and on[cy, cx + 1]):
                        d.polygon([(px + w, py - lift), (px + w + 1, py - lift + 1), (px + w + 1, py + w),
                                   (px + w, py + w)], fill=side_c)
                    d.rectangle((px, py - lift, px + w, py - lift + w), fill=top_c)
            return
        for x, top, film in self._demos:
            for cy, cx in np.argwhere(film[k % len(film)]):
                px, py = x + cx * c, top + cy * c
                d.rectangle((px, py, px + c - 2, py + c - 2), fill=color)

    def _graph(self, d) -> None:
        """Time left, from launch to now: falling steadily, jumping at each move
        (green: it bought time; red: it took time away). The last RECENT_H hours
        show every move, on the right; the life before that is smoothed on the
        left (see RECENT_H). While a rewind plays, the whole life is drawn move by
        move instead, up to where it has got to, the rest faintly, each jump
        flashing as its move lands; while a replay plays, the same on the right."""
        snap = self.panel() if not self.replay else (self.after_replay or self.snap)
        pts, jumps = time_left_curve(snap)
        playing = self.rewind or self.replay
        upto = None
        if self.rewind:
            upto = self.rewind.u.generation
        elif self.replay:
            upto = self.replay.shown
        x0, x1, y0, y1 = COL_X, COL_X + COL_W, BOARD_Y + BOARD_H + 28, SH - 30
        d.text((x0, y0), "Time left", font=font("bold", 20), fill=WHITE, anchor="lt")
        gap = d.textlength("Time left", font=font("bold", 20)) + 10  # the server's font is wider than a Mac's
        d.text((x0 + gap, y0 + 2), "each jump is a move", font=font("regular", 17), fill=MUTED, anchor="lt")
        px0, px1, py0, py1 = x0 + 34, x1 - 6, y0 + 34, y1 - 22
        day = 86400 / snap.gen_seconds  # generations in a day
        top_days = max(1.0, max(p[1] for p in pts) / day)
        every = next(s for s in (1, 2, 5, 10, 20, 50) if top_days / s <= 4)
        top = math.ceil(top_days / every) * every * day
        now_gen = pts[-1][0]
        # Where the detailed part starts: launch while a rewind plays (or the life is young)
        g_cut = 0 if self.rewind else max(0, now_gen - RECENT_H * 3600 / snap.gen_seconds)
        x_cut = px0 if g_cut <= 0 else round(px0 + (px1 - px0) * (1 - RECENT_SHARE))
        X = lambda g: x_cut + (px1 - x_cut) * (g - g_cut) / max(1, now_gen - g_cut)  # noqa: E731
        Y = lambda v: py1 - (py1 - py0) * v / top  # noqa: E731
        f = font("regular", 15)
        for k in range(0, int(top / day / every) + 1):
            yy = Y(k * every * day)
            d.line((px0, yy, px1, yy), fill=(34, 38, 48), width=1)
            d.text((px0 - 6, yy), f"{k * every}d", font=f, fill=MUTED, anchor="rm")
        color = PHASE_COLOR[playing.u.phase if playing else snap.u.phase]
        up = lambda b, a: PHASE_COLOR[ALIVE] if b > a else (240, 90, 90)  # noqa: E731
        if g_cut > 0:  # the life before: one pixel each, its range faint, its average the line
            k = (id(snap), round(g_cut), x_cut - px0)
            if getattr(self, "_typical", (None,))[0] != k:
                self._typical = (k, typical(pts, 0, g_cut, x_cut - px0))
            line = []
            for i, (lo, hi, mean) in enumerate(self._typical[1]):
                d.line((px0 + i, Y(hi), px0 + i, py1), fill=color + (22,))
                d.line((px0 + i, Y(hi), px0 + i, Y(lo)), fill=color + (70,))
                line.append((px0 + i, Y(mean)))
            d.line(line, fill=color + (230,), width=2)
            pts, jumps = curve_from(pts, g_cut), [j for j in jumps if j[0] >= g_cut]
            d.line(line[-1:] + [(X(g_cut), Y(pts[0][1]))], fill=color + (120,), width=1)
            d.line((x_cut, py0 - 4, x_cut, py1), fill=(90, 96, 110), width=1)
            d.text((x_cut, py1 + 6), f"{RECENT_H}h ago", font=f, fill=MUTED, anchor="mt")
            if upto is not None:
                upto = max(upto, g_cut)
        if upto is not None:  # the whole story, faintly, under the part played so far
            xy = [(X(g), Y(v)) for g, v in pts]
            d.polygon(xy + [(xy[-1][0], py1), (xy[0][0], py1)], fill=color + (12,))
            d.line(xy, fill=color + (60,), width=1)
            for g, a, b in jumps:
                if g > upto:
                    d.line((X(g), Y(a), X(g), Y(b)), fill=up(b, a) + (70,), width=1)
        past = pts if upto is None else curve_until(pts, max(0, upto))
        xy = [(X(g), Y(v)) for g, v in past]
        if len(xy) > 1:
            d.polygon(xy + [(xy[-1][0], py1), (xy[0][0], py1)], fill=color + (38,))
            d.line(xy, fill=color + (200,), width=2)
        flash = 40 if self.rewind else 3  # generations a jump flashes for, once it lands (the rewind is fast)
        for g, a, b in jumps:  # over the line, so each move shows
            if upto is None or g <= upto:
                fresh = upto is not None and upto - g <= flash
                d.line((X(g), Y(a), X(g), Y(b)), fill=up(b, a), width=4 if fresh else 2)
                if fresh:
                    d.ellipse((X(g) - 7, Y(b) - 7, X(g) + 7, Y(b) + 7), outline=WHITE, width=2)
        cx, cy = xy[-1]
        d.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=WHITE)
        if upto is not None:  # how long it had left, at the moment being played
            label = f"{render.short_span(past[-1][1] * snap.gen_seconds)} left"
            fb = font("bold", 16)
            tw = d.textlength(label, font=fb)
            lx = min(max(cx - tw / 2, px0 + 2), px1 - tw - 2)
            ly = cy - 30 if cy - 30 > py0 - 8 else cy + 10
            d.rounded_rectangle((int(lx - 6), int(ly - 2), int(lx + tw + 6), int(ly + 20)), radius=5, fill=(5, 6, 9, 220))
            d.text((lx, ly), label, font=fb, fill=WHITE, anchor="lt")
        launched = render._at(snap.launch, 0, snap.gen_seconds, "%a %b %d")  # 'Sun Nov 8'
        label = f"launched {launched}"
        cut_label = d.textlength(f"{RECENT_H}h ago", font=f)
        if g_cut > 0 and px0 + d.textlength(label, font=f) + 12 > x_cut - cut_label / 2:
            label = launched  # the server's font is wider: clear of 'Nh ago'
        d.text((px0, py1 + 6), label, font=f, fill=MUTED, anchor="lt")
        d.text((px1, py1 + 6), "now", font=f, fill=MUTED, anchor="rt")


# ---------------------------------------------------------------- the loop
class Puller(threading.Thread):
    """Every PULL_S: if GitHub has a new commit, git pull, then load the state if
    the log grew. Notices new code."""

    def __init__(self, repo: str, state_dir: str, seq: int, pull: bool, code: tuple = CODE):
        super().__init__(daemon=True)
        self.repo, self.state_dir, self.seq, self.pull = repo, state_dir, seq, pull
        self.files = code  # a change to these is new code: restart
        self.pending: Snapshot | None = None
        self.restart = False
        self.lock = threading.Lock()
        self.code = self._code()

    def _code(self) -> str | None:
        if not self.pull:
            return None
        r = subprocess.run(["git", "-C", self.repo, "rev-parse", *[f"HEAD:{p}" for p in self.files]],
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


def ffmpeg_cmd(out: str, bitrate_k: int, audio_fd: int | None = None, size: tuple[int, int] = (SW, SH)) -> list[str]:
    """Video from stdin (frames of `size`); audio from the pipe `audio_fd` (16-bit stereo PCM), or silence."""
    b = f"{bitrate_k}k"
    audio = (["-thread_queue_size", "64", "-probesize", "32", "-analyzeduration", "0",  # don't wait for seconds of it
              "-f", "s16le", "-ar", str(stream_audio.SR), "-ac", "2", "-i", f"pipe:{audio_fd}"]
             if audio_fd is not None else ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={stream_audio.SR}"])
    return ["ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "warning",
            "-thread_queue_size", "16", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}",
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
           sound: bool = True, rewinds: bool = True, tall: bool = False) -> int:
    """`tall`: the vertical version (vertical.py), for YouTube's Dual stream, as its own process."""
    state_dir = os.path.join(repo, "state")
    snap = load(state_dir)
    screen = Screen(snap)
    draw, size, code = screen.compose, (SW, SH), CODE
    if tall:
        import vertical
        draw, size, code = (lambda now: vertical.compose(screen, now)), (vertical.TW, vertical.TH), vertical.CODE
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
    puller = Puller(repo, state_dir, snap.seq, pull, code)
    puller.start()
    prebuilder = Prebuilder(repo) if rewinds and screen.look3d and not tall else None
    proc = subprocess.Popen(ffmpeg_cmd(out, bitrate_k, audio_r, size), stdin=subprocess.PIPE, stderr=subprocess.PIPE,
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

    log(f"streaming universe #{snap.universe_no} from gen {snap.u.generation} ({snap.u.phase})"
        + (f", tall ({size[0]}x{size[1]}, {OUT_FPS} fps)" if tall else ""))
    t0, n, data, last_key, behind = time.monotonic(), 0, b"", None, 0.0
    try:
        while not puller.restart and (seconds is None or n < seconds * IN_FPS):
            now = time.time()
            new = puller.take()
            if new is not None:
                screen.swap(new, now, state_dir if rewinds else None)
            rewinding = screen.rewind is not None
            screen.tick(now, state_dir if rewinds else None)
            if prebuilder is not None and screen.look3d:
                prebuilder.poll(now)
            if rewinding and screen.rewind is None:
                log(f"rewind done; the frames ran at most {max(0.0, behind):.2f} s behind")
                behind = 0.0
            k = screen.key(now)
            if not data or k != last_key or screen.animating(now):
                data, last_key = draw(now).tobytes(), k
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
    ap.add_argument("--tall", action="store_true", help="the vertical version (vertical.py), with the vertical key")
    ap.add_argument("--prebuild-rewind", type=float, metavar="SLOT", help="draw the rewind at SLOT ahead (3D) and stop")
    args = ap.parse_args(argv)

    if args.prebuild_rewind is not None:
        t0 = time.time()
        out = prebuild_rewind(args.repo, args.prebuild_rewind)
        log(f"rewind drawn ahead into {out} in {time.time() - t0:.0f} s")
        return 0

    if args.preview:
        now = time.time()
        snap = load(os.path.join(args.repo, "state"))
        screen = Screen(snap)
        entries = [json.loads(ln) for ln in open(os.path.join(args.repo, "state", "log.jsonl"), encoding="utf-8")]
        effects = {(e["gen"], e["command"]): e for e in entries if e["type"] == "effect"}
        for i, e in enumerate([e for e in entries if e["type"] == "injection"][-3:]):  # as if just committed
            eff = effects.get((e["gen"], e["command"]))
            suffix = render.effect_suffix(eff, snap.u.flatline_gens, snap.gen_seconds) if eff else ""
            screen.tags.append(Tag(render.move_label(e) + suffix, [tuple(c) for c in e["cells"]], now - 0.5 - 0.5 * i,
                                   bool(e.get("erase"))))
        os.makedirs(os.path.dirname(os.path.abspath(args.preview)), exist_ok=True)
        if args.tall:
            import vertical
            vertical.compose(screen, now).save(args.preview)
        else:
            screen.compose(now).save(args.preview)
        print(args.preview)
        return 0

    secret = os.environ.get("STREAM_KEY", "").strip() or None
    out = args.out or (INGEST + secret if secret else None)
    if not out:
        print("set STREAM_KEY (YouTube Studio → Go live → Stream key) or pass --out", file=sys.stderr)
        return 2
    sound = not args.no_sound and os.environ.get("STREAM_SOUND", "on").strip().lower() not in ("0", "off", "false", "no")
    return stream(args.repo, out, secret, args.seconds, not args.no_pull, args.bitrate, sound, tall=args.tall)


if __name__ == "__main__":
    sys.exit(main())
