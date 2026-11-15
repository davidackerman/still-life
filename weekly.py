#!/usr/bin/env python3
"""Render the week-one video: the universe's first seven days in about a minute
(16:9, 1920x1080, on-screen text, procedural sound).

    python weekly.py                                  # out/week1.mp4 from state/
    python weekly.py --preview out/week1_frames       # also save a PNG every 2 s
    python weekly.py --frames-only                    # just the timing and the facts

The shape: the board today with its premise; the seed left alone, dying on its
Wednesday; the rules in two screens; three real moments from the week (the command, its
cells landing, what it did to the forecast); the forecast today and what to do.

Everything on screen is replayed from the log: the board, every move at its
generation, what each move did to the forecast (the log's "effect" entries),
and the seed's own future with no moves at all. Moves are shown by their
commands only: the log holds no names, channels or message text. Every number
on screen is computed here from the log or state.json, and each pick below is
checked against the log, so a caption can't drift from what happened. Needs ffmpeg.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import subprocess
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageDraw

import commands
import explainer as ex
import life
import render as rd
import short
from short import History
from universe import DIED

W, H = short.BW, short.BH  # the board at 15 px a cell: 1920 x 1080, the frame
FPS = ex.FPS
ET = ZoneInfo("America/New_York")
CPS, LOOK_S, MIN_S, FADE = 17, 0.8, 1.8, 8  # the Shorts' reading pace (short.rf): 14 cps felt slow here
FORECAST_GENS = 30000
WHITE, GREEN, RED = (255, 255, 255), (120, 230, 160), (255, 110, 110)
ZOOM = 2.6  # a moment's close-up
TAIL = 6  # frames more of time-lapse after a moment's words are read
LIGHT = (222, 225, 232)
# The last screen: how to play, as on the stream (stream.LEGEND): each move as a little film
# beside what to type. (pattern, what to type, a second line)
HOWTO = [("glider_se", "glider x,y", "+ ne, nw, se or sw"), ("acorn", "acorn x,y", ""), ("rpent", "rpent x,y", ""),
         ("block", "block x,y", ""), ("blinker", "blinker x,y", "")]
HOWTO_BOX, HOWTO_CELL, HOWTO_RATE = 9, 16, 4  # films of 9 x 9 cells, 16 px a cell, 4 generations a second
HOWTO_LINES = ["Comment anything.", "Even “hi” adds cells. Or aim a pattern:",
               "x 0–127 across, y 0–71 down, from the top-left",
               "Add a color:", "Or type in the live stream's chat on this channel."]


def rf(*lines: str) -> int:
    """Frames a screen needs for all its words (reading + a look + its fades)."""
    return int(round(max(MIN_S, sum(map(len, lines)) / CPS + LOOK_S) * FPS)) + 2 * FADE


def fade(i: int, n: int) -> float:
    return ex.fade(i, n, FADE)


def ease(p: float) -> float:
    return short.ease(p)


# ---------------------------------------------------------------- words
def effect_gens(eff: dict) -> int:
    """Generations one move gave (+) or took (-): engine.effect_gens."""
    end = lambda st: st if st is not None else eff["gen"] + eff.get("horizon", FORECAST_GENS)  # noqa: E731
    return end(eff["after"]) - end(eff["before"])


def result(eff: dict, gen_seconds: int) -> tuple[str, tuple]:
    """'+8 days' (green) or '−4 days' (red): hours under two days, else whole days."""
    d = effect_gens(eff)
    s = abs(d) * gen_seconds
    if s < 48 * 3600:
        n = max(1, round(s / 3600))
        unit = "hour" + "s" * (n != 1)
    else:
        n = round(s / 86400)
        unit = "day" + "s" * (n != 1)
    return (f"+{n} {unit}", GREEN) if d > 0 else (f"−{n} {unit}", RED)


def quoted(e: dict) -> str:
    """A move as what someone typed: “acorn 16,18 red”."""
    return "“" + e["command"] + (f" {e['color']}" if e.get("color") else "") + "”"


# ---------------------------------------------------------------- drawing
_scrim = None


def scrim() -> np.ndarray:
    """A dark gradient behind the lower-third text."""
    global _scrim
    if _scrim is None:
        a = np.clip((np.arange(H) - 680) / 400, 0, 1) * 0.5  # light: the words have their own outline
        _scrim = (a * 255).astype(np.uint8)
    return _scrim


def crop(img: Image.Image, f):
    """The part of the board on screen at f.zoom around f.focus, wrapping (it is a torus)."""
    if f.zoom <= 1:
        return img
    w, h = W / f.zoom, H / f.zoom
    x0, y0 = f.focus[0] - w / 2, f.focus[1] - h / 2
    xi, yi, wi, hi = int(np.floor(x0)), int(np.floor(y0)), int(np.ceil(w)) + 1, int(np.ceil(h)) + 1
    arr = np.asarray(img)[np.ix_(np.arange(yi, yi + hi) % H, np.arange(xi, xi + wi) % W)]
    return Image.fromarray(arr).resize((W, H), Image.NEAREST, box=(x0 - xi, y0 - yi, x0 - xi + w, y0 - yi + h))


def compose(f) -> bytes:
    img = short.board(f)
    flash = getattr(f, "flash", None)
    if flash is not None:  # cells landing: white, fading back to their own color
        mask, a = flash
        on = (f.grid.astype(bool) & mask.astype(bool)).repeat(short.CELL, 0).repeat(short.CELL, 1) & short._tile
        arr = np.asarray(img).copy()
        arr[on] = (arr[on] * (1 - a) + 255 * a).astype(np.uint8)
        img = Image.fromarray(arr)
    base = crop(img, f).convert("RGBA")
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if f.dim:
        d.rectangle((0, 0, W, H), fill=(5, 6, 9, int(255 * f.dim)))
    if getattr(f, "low", 0):  # lower-third text: a gradient behind it
        g = np.zeros((H, W, 4), np.uint8)
        g[..., 3] = (scrim() * f.low)[:, None]
        layer = Image.alpha_composite(layer, Image.fromarray(g, "RGBA"))
        d = ImageDraw.Draw(layer)
    words = Image.new("RGBA", (W, H), (0, 0, 0, 0))  # on their own layer: a fading word blends in
    d = ImageDraw.Draw(words)
    for s, weight, size, xy, anchor, alpha, *fill in f.texts:
        if alpha > 0.01:
            ex.text(d, xy, s, weight, size, alpha, anchor=anchor, fill=fill[0] if fill else WHITE)
    if getattr(f, "stamp", None):  # the day, on a dark tab in the corner
        tw = d.textlength(f.stamp, font=ex.font("demi", 40))
        d.rounded_rectangle((32, 30, 32 + tw + 48, 92), radius=14, fill=(8, 10, 14, 200))
        ex.text(d, (56, 61), f.stamp, "demi", 40, 1.0, anchor="lm", outline=False)
    if getattr(f, "howto", None) is not None:
        howto(d, *f.howto)
    return Image.alpha_composite(Image.alpha_composite(base, layer), words).convert("RGB").tobytes()


def howto(d, i: int, a: float) -> None:
    """The how-to-play card over the whole board (zoom 1, so the ruler's numbers are the
    board's own columns and rows): what to type, each move's little film, the colors."""
    c = short.CELL
    for x in range(0, 128, 16):  # the ruler, as on the stream: aim by these
        ex.text(d, (x * c + c / 2 + (6 if x == 0 else 0), 24), str(x), "demi", 30, a, fill=LIGHT)
    ex.text(d, (W - 22, 24), "x", "demi", 30, a, fill=LIGHT)
    for y in range(8, 72, 8):
        ex.text(d, (26, y * c + c / 2), str(y), "demi", 30, a, fill=LIGHT)
    ex.text(d, (26, H - 24), "y", "demi", 30, a, fill=LIGHT)
    ex.text(d, (W // 2, 190), HOWTO_LINES[0], "bold", 104, a)
    ex.text(d, (W // 2, 312), HOWTO_LINES[1], "bold", 62, a)
    k = i * HOWTO_RATE // FPS
    iw, col = HOWTO_BOX * HOWTO_CELL, 340
    x0 = W // 2 - col * (len(HOWTO) - 1) / 2
    for n, (pattern, name, sub) in enumerate(HOWTO):
        cx, top = x0 + n * col, 380
        left = cx - iw / 2
        d.rounded_rectangle((left - 6, top - 6, left + iw + 5, top + iw + 5), radius=10,
                            fill=(14, 16, 22, int(235 * a)), outline=(52, 56, 70, int(255 * a)), width=2)
        film = rd.demo(pattern, HOWTO_BOX)
        for cy, cx_ in np.argwhere(film[k % len(film)]):
            px, py = left + cx_ * HOWTO_CELL, top + cy * HOWTO_CELL
            d.rectangle((px, py, px + HOWTO_CELL - 2, py + HOWTO_CELL - 2), fill=GREEN + (int(255 * a),))
        ex.text(d, (cx, top + iw + 50), name, "bold", 50, a)
        if sub:
            ex.text(d, (cx, top + iw + 100), sub, "demi", 34, a, fill=LIGHT)
    ex.text(d, (W // 2, 750), HOWTO_LINES[2], "demi", 42, a, fill=LIGHT)
    f = ex.font("bold", 50)  # "Add a color:" and each color in its own paint
    parts = [(HOWTO_LINES[3] + "  ", WHITE)] + [(name + "  ", rd.PAINT[name]) for name in commands.COLORS]
    x = W // 2 - sum(d.textlength(t, font=f) for t, _ in parts) / 2
    for t, fill in parts:
        ex.text(d, (x, 840), t.rstrip(), "bold", 50, a, fill=fill, anchor="lm")
        x += d.textlength(t, font=f)
    ex.text(d, (W // 2, 950), HOWTO_LINES[4], "demi", 52, a)


def centre_text(lines, alpha: float, yc: int = H // 2):
    """[(text, weight, size)] centred as a block on yc."""
    steps = [int(size * 1.35) for _, _, size in lines]
    y = yc - (sum(steps) - steps[-1]) / 2
    out = []
    for (s, weight, size), step in zip(lines, steps):
        out.append((s, weight, size, (W // 2, round(y)), "mm", alpha))
        y += step
    return out


# ---------------------------------------------------------------- the week
# Three real moments from the week (seven was too many), picked for being easy to see (named patterns, colors, a
# glider's flight) and checked against the log: each move's cells must still be on
# the board several generations after landing (Week.visible), else the render stops.
#   ("one", gen, command)                         one move
#   ("pair", gen, command, gen2, command2)        a move and the one that answered it
PICKS = [
    ("one", 220, "glider 16,61 sw"),          # Sun: the week's longest glider flight, +8 days
    ("pair", 2829, "acorn 16,18", 2833, "random 5x5 at 118,11"),  # Wed: +9 days, then -9 days 8 minutes later
    ("one", 4977, "acorn 11,11"),             # Sat: from the live chat, +9 days
]


RULES_PICK = (4686, "glider 99,11 se")  # "Every comment adds cells.": an orange glider landing in open space, Sat 12:16 PM


class Week:
    def __init__(self, state_dir: str):
        self.dir = state_dir
        self.h = History(state_dir)
        self.G = self.h.gen_seconds
        self.launch = self.h.genesis["launch_time"]
        with open(os.path.join(state_dir, "state.json")) as fh:
            self.state = json.load(fh)
        self.frames: list = []
        self.moves = [e for g in sorted(self.h.batches) for e in self.h.batches[g]]
        self.shown: list = []  # what the montage said, for the claims list
        self.landings: list = []  # frame numbers just after each move lands (previews check them)

    # -- facts
    def when(self, gen: int, fmt: str = "%-I:%M %p") -> str:
        return dt.datetime.fromtimestamp(self.launch + gen * self.G, ET).strftime(fmt)

    def stamp(self, gen: int) -> str:
        return f"Day {gen * self.G // 86400 + 1} · " + self.when(gen, "%A · %-I:%M %p")

    def move(self, gen: int, command: str) -> dict:
        for e in self.h.batches.get(gen, []):
            if e["command"] == command:
                return e
        raise SystemExit(f"weekly.py: no move {command!r} at generation {gen} in the log; update PICKS")

    def effect(self, e: dict) -> dict:
        eff = self.h.effects.get((e["gen"], e["command"]))
        if eff is None:
            raise SystemExit(f"weekly.py: no effect logged for {e['command']!r} at {e['gen']}")
        return eff

    def record(self, e: dict) -> str | None:
        """Where a move ranks among the week's (the first 7 days'): "the week's biggest save",
        "the week's 2nd biggest kill", or "Day 1's biggest save"; else None. From the log's
        effect entries, the measure the leaderboard uses."""
        d = effect_gens(self.effect(e))
        if d == 0:
            return None
        week = 7 * 86400 // self.G
        kind = "save" if d > 0 else "kill"
        rivals = [x for x in self.h.effects.values() if x["gen"] < week and effect_gens(x) * d > 0]
        rank = 1 + sum(1 for x in rivals if abs(effect_gens(x)) > abs(d))
        if rank <= 3:
            return f"the week's {('', '2nd ', '3rd ')[rank - 1]}biggest {kind}"
        day = e["gen"] * self.G // 86400
        lo, hi = day * 86400 // self.G, (day + 1) * 86400 // self.G
        if not any(lo <= x["gen"] < hi and abs(effect_gens(x)) > abs(d) for x in rivals):
            return f"Day {day + 1}'s biggest {kind}"
        return None

    def visible(self, e: dict, after: int = 4) -> int:
        """Live cells around where a move landed, `after` generations later (its own replay)."""
        h = History(self.dir)
        u = h.fresh()
        h.goto(u, e["gen"] + after)
        piece = short.main_piece([tuple(c) for c in e["cells"]])
        xs, ys = [x for x, _ in piece], [y for _, y in piece]
        rows = np.arange(min(ys) - 1, max(ys) + 2) % u.grid.shape[0]
        cols = np.arange(min(xs) - 1, max(xs) + 2) % u.grid.shape[1]
        return int(u.grid[np.ix_(rows, cols)].sum())

    def people(self) -> tuple[int, int, int]:
        """(people credited with a move, how many of them gave it time overall, took time overall):
        HMAC'd keys in state.json's scores. A floor: live-chat moves from before scores were
        kept (Nov 10) couldn't be matched to anyone."""
        sc = self.state.get("scores") or {}
        return len(sc), sum(1 for v in sc.values() if v[0] > 0), sum(1 for v in sc.values() if v[0] < 0)

    # -- frames
    def add(self, u, texts=(), zoom=1.0, focus=(W / 2, H / 2), dim=0.0, stamp=None, flash=None, low=0.0,
            grey=0.0, cue=None):
        f = ex.Frame(u, texts=texts, grey=grey, hud=False, zoom=zoom, dim=dim, cue=cue)
        f.focus, f.stamp, f.flash, f.low = focus, stamp, flash, low
        paint = getattr(u, "paint", None)
        f.paint = paint.copy() if paint is not None and paint.any() else None
        self.frames.append(f)

    def screen(self, u, lines, step: int = 2, dim: float = 0.4, first: bool = False, last: bool = False,
               n: int | None = None, advance=None):
        """Centred words over a moving board, held for all of them. `first`: at full
        strength from frame 0 (the first thing anyone sees); `last`: no fade out."""
        n = n or rf(*[t for t, _, _ in lines])
        for i in range(n):
            if i % step == 0:
                (advance or (lambda: History.advance(u, u.generation + 1)))()
            a = min(1.0, 1.0 if first else (i + 1) / FADE, 1.0 if last else (n - i) / FADE)
            self.add(u, centre_text(lines, a), dim=dim * (1.0 if first else a))

    def moment(self, u, pick):
        """One day's moment: punch in, the command, its cells landing white, the result big."""
        h, G = self.h, self.G
        e1 = self.move(pick[1], pick[2])
        pair = pick[0] == "pair"
        e2 = self.move(pick[3], pick[4]) if pair else None
        for e in (e1, e2) if pair else (e1,):
            if self.visible(e, 2) < 3:
                raise SystemExit(f"weekly.py: {e['command']!r} at {e['gen']} is gone before it can be seen; repick")
        r1, c1 = result(self.effect(e1), G)
        g1 = e1["gen"]
        p1 = short.main_piece([tuple(c) for c in e1["cells"]])
        fx, fy = short.centre(p1)
        if pair:  # both in one window: halfway between them, the short way around
            p2 = short.main_piece([tuple(c) for c in e2["cells"]])
            x2, y2 = short.centre(p2)
            dx = (x2 - fx + W / 2) % W - W / 2
            dy = (y2 - fy + H / 2) % H - H / 2
            fx, fy = fx + dx / 2, fy + dy / 2
            zoom = min(ZOOM, 0.75 * W / max(abs(dx) + 240, 1), 0.55 * H / max(abs(dy) + 160, 1))
        else:
            zoom = ZOOM
        command = e1["command"]
        flies = command.startswith("glider") and self._flown(e1) >= 40
        follow = np.array(short.GLIDER_DIRS[command.split()[-1]]) / 4 if flies else None
        # the words. One move: its command, then (once read) what it did, big.
        # A pair: the first move and its result, then the answer and its result as it lands.
        top = quoted(e1)
        tag1 = self.record(e1)
        lead = 8  # frames punching in before it lands
        if pair:
            r2, c2 = result(self.effect(e2), G)
            tag2 = self.record(e2)
            r1 = f"{r1} · {tag1}" if tag1 else r1  # one line each: "+9 days · the week's biggest save"
            r2 = f"{r2} · {tag2}" if tag2 else r2
            gap = (e2["gen"] - g1) * G // 60
            what2 = "a message (any words)" if e2["command"].startswith("random") else quoted(e2)
            second = f"{gap} minutes later, {what2}:"
            t2_at = lead + 2 * (e2["gen"] - g1)  # when it lands (a generation every 2 frames till then)
            n_cmd = 0
            n = t2_at + rf(top, r1, second, r2) + TAIL
        else:
            n_cmd = rf(top) - FADE  # the command alone, read before the result shows
            n = lead + n_cmd + rf(top, r1, tag1 or "") + TAIL
        start = g1 - 3
        if u.generation < start:
            h.goto(u, start)
        # after the landing: a generation every 2 frames while the command is read, then every frame.
        # The corner keeps the moment's own day and time while the board runs on.
        every = 2 if pair else 1  # (the user found 3 and 2 slow, the gliders above all)
        tag = self.stamp(g1)
        g0 = None
        landed1 = landed2 = None
        t1 = t2 = None
        for i in range(n):
            # the board: 3 generations into the landing, slow through it (and the answer), then a time-lapse
            if i < lead:
                target = start + round(3 * (i + 1) / lead)
            elif i < lead + 8 or (pair and u.generation < e2["gen"]):
                target = g1 + (i - lead) // 2
            elif i < lead + n_cmd:
                target = u.generation + (i % 2 == 0)
            else:
                target = u.generation + (i % every == 0)
            h.goto(u, max(u.generation, target))
            if u.generation >= g1 and landed1 is None:
                landed1, t1, g0 = life.mask([tuple(c) for c in e1["cells"]], u.grid.shape), i, u.generation
            if pair and u.generation >= e2["gen"] and landed2 is None:
                landed2, t2 = life.mask([tuple(c) for c in e2["cells"]], u.grid.shape), i
            flash = None
            for m, t in ((landed1, t1), (landed2, t2)):
                if m is not None and i - t < 24:
                    a = 1.0 if i - t < 10 else 1 - (i - t - 10) / 14
                    flash = (m, a) if flash is None else (flash[0] | m, max(flash[1], a))
            # the view: punch in; a long flight is followed; it eases out a little while the result shows
            z = 1 + (zoom - 1) * ease((i + 1) / lead)
            res0 = lead + n_cmd if not pair else t2_at
            if i > res0:
                z -= (zoom - 1) * 0.3 * ease((i - res0) / max(1, n - res0))
            ox = oy = 0.0
            if follow is not None and g0 is not None:
                ox, oy = follow * (u.generation - g0) * short.CELL
            focus = (fx + ox, fy + oy + 0.16 * H / z)  # the piece above the words
            a_out = min(1.0, (n - i) / FADE)
            a_in = min(1.0, (i + 1) / 6, a_out)
            if pair:
                a2 = min(1.0, max(0.0, (i - t2) / 6), a_out) if t2 is not None else 0.0
                texts = [(top, "bold", 64, (W // 2, 690), "mm", a_in), (r1, "bold", 72, (W // 2, 775), "mm", a_in, c1),
                         (second, "bold", 52, (W // 2, 880), "mm", a2), (r2, "bold", 72, (W // 2, 962), "mm", a2, c2)]
            else:
                a_res = min(1.0, max(0.0, (i - lead - n_cmd) / 6), a_out)
                texts = [(top, "bold", 76, (W // 2, 770), "mm", a_in), (r1, "bold", 132, (W // 2, 900), "mm", a_res, c1)]
                if tag1:
                    texts.append((tag1, "bold", 56, (W // 2, 1012), "mm", a_res, c1))
            cue = ("inject", float(np.mean([c[0] for c in e1["cells"]]))) if i in (t1, t2) else None
            if i in (t1, t2):
                self.landings.append(len(self.frames) + 4)
            self.add(u, texts, zoom=z, focus=focus, stamp=tag, flash=flash, low=a_in, cue=cue)
        shown = {"day": self.when(g1, "%a %b %-d"), "time": self.when(g1), "gen": g1, "command": quoted(e1),
                 "result": r1 if pair or not tag1 else f"{r1} · {tag1}", "effect": self.effect(e1), "via": e1.get("via")}
        if pair:
            shown["then"] = {"gen": e2["gen"], "words": second, "result": r2, "effect": self.effect(e2),
                             "command": e2["command"]}
        if follow is not None:
            shown["flew"] = self._flown(e1)
        self.shown.append(shown)

    def _flown(self, e: dict) -> int:
        if not hasattr(self, "_flights"):
            self._flights = {(g, x["command"]): n for n, g, x, _ in short.flights(History(self.dir))}
        return self._flights.get((e["gen"], e["command"]), 0)

    # -- the story
    def build(self) -> list:
        h, G = self.h, self.G
        now = h.fresh()
        h.goto(now, h.now_gen)
        if h.now_hash and now.hash.hex() != h.now_hash:
            raise SystemExit("replaying the log did not reproduce the current board; run replay.py")
        people, gave, took = self.people()
        week = min(h.now_gen, 7 * 86400 // self.G)  # the first 7 days exactly, however late it renders

        # 1. cold open. Frame 0: the board today, moving, its premise at full strength.
        u = copy.deepcopy(now)
        self.screen(u, [("This universe lives", "bold", 116), ("on YouTube comments.", "bold", 116)], first=True,
                    dim=0.35)
        seed = History(self.dir).fresh()
        stuck = copy.deepcopy(seed).forecast_stuck(FORECAST_GENS)
        if stuck is None:
            raise SystemExit("the seed alone doesn't get stuck within the forecast; rewrite the cold open")
        died_at = stuck + seed.flatline_gens
        acorn = short.centre(life.centered(h.genesis["seed"]))
        lines = [("With no comments,", "bold", 96), (f"it would have died on {self.when(died_at, '%A')}.", "bold", 96)]
        n = rf(*[t for t, _, _ in lines])
        for i in range(n):  # the seed alone, fast: alive, stuck (red), dead (grey)
            p = (i + 1) / n
            g = round(stuck * ease(min(1.0, p / 0.45))) if p < 0.45 else \
                stuck + round((died_at - stuck) * min(1.0, (p - 0.45) / 0.33))
            History.advance(seed, max(seed.generation, g))
            a = fade(i, n)
            self.add(seed, centre_text(lines, a), zoom=1 + 3 * (1 - ease(min(1.0, i / (0.3 * n)))), focus=acorn,
                     dim=0.3 * a, stamp=self.stamp(seed.generation), grey=1.0 if seed.phase == DIED else 0.0)
        u = copy.deepcopy(now)
        over = people // 100 * 100
        self.screen(u, [(f"Over {over} people have played." if people > over else f"{people} people have played.",
                         "bold", 84), ("It's still alive.", "bold", 84)])

        # 2. the rules, in two screens: a real move landing (the latest named pattern that stays
        # visible), then what moves do
        ex1 = self.move(*RULES_PICK)
        if self.visible(ex1, 2) < 3:
            raise SystemExit(f"weekly.py: {ex1['command']!r} is gone before it can be seen; repick RULES_PICK")
        last = ex1["gen"]
        if ex1 is not None:
            lu = History(self.dir)
            v = lu.fresh()
            lu.goto(v, last - 4)
            lines = [("Every comment adds cells.", "bold", 92), (quoted(ex1), "bold", 64)]
            n = rf(*[t for t, _, _ in lines])
            mask, t = life.mask([tuple(c) for c in ex1["cells"]], v.grid.shape), None
            px, py = short.centre(short.main_piece([tuple(c) for c in ex1["cells"]]))
            cmd = ex1["command"]
            follow = np.array(short.GLIDER_DIRS[cmd.split()[-1]]) / 4 if cmd.startswith("glider") else np.zeros(2)
            for i in range(n):
                if i % 2 == 0:
                    lu.goto(v, v.generation + 1)
                    if v.generation >= last and t is None:
                        t = i
                fl = (mask, 1.0 if i - t < 12 else max(0.0, 1 - (i - t - 12) / 14)) if t is not None else None
                z = 1 + 1.2 * ease(min(1.0, (i + 1) / 12))
                ox, oy = follow * max(0, v.generation - last) * short.CELL
                self.add(v, centre_text(lines, fade(i, n), 250), dim=0.25 * fade(i, n), zoom=z,
                         focus=(px + ox, py + oy - 0.1 * H / z), flash=fl, stamp=self.stamp(last))
        u = copy.deepcopy(now)
        History.advance(u, u.generation + 40)
        self.screen(u, [("Some moves give it more time to live.", "bold", 80), ("Some take time away.", "bold", 80),
                        ("A few of the week's biggest:", "demi", 56)])

        # 3. three moments from the week, on one replay from the seed
        u = h.fresh()
        for pick in PICKS:
            self.moment(u, pick)

        # 4. the forecast today, and what to do
        u = copy.deepcopy(now)
        st = copy.deepcopy(now).forecast_stuck(FORECAST_GENS)
        as_of = self.when(h.now_gen, "%A, %b %-d")
        if st is not None:
            days_off = (st - h.now_gen) * G / 86400
            when = self.when(st, "%A") if days_off < 6 else self.when(st, "%A, %b %-d")
            fate = [("With no more comments,", "bold", 80), (f"it gets stuck {when}.", "bold", 80),
                    (f"(forecast as of {as_of})", "demi", 44)]
        else:
            fate = [("With no more comments,", "bold", 80), ("it won't get stuck for weeks.", "bold", 80),
                    (f"(forecast as of {as_of})", "demi", 44)]
        self.screen(u, fate, dim=0.5)
        # the last screen: how to play, held for all its words, no fade out
        n = rf(*HOWTO_LINES, *[t + " " + sub for _, t, sub in HOWTO], *commands.COLORS)
        for i in range(n):
            if i % 2 == 0:
                History.advance(u, u.generation + 1)
            a = min(1.0, (i + 1) / FADE)
            self.add(u, dim=0.62 * a)
            self.frames[-1].howto = (i, a)
        moves = sum(1 for e in self.moves if e["gen"] < week)
        self.facts = {"people": people, "gave": gave, "took": took, "seed_stuck": stuck, "seed_died": died_at,
                      "seed_stuck_at": self.when(stuck, "%a %b %-d %-I:%M %p ET"),
                      "seed_died_at": self.when(died_at, "%a %b %-d %-I:%M %p ET"), "week_moves": moves,
                      "now_gen": h.now_gen, "forecast_stuck": st,
                      "forecast_at": self.when(st, "%a %b %-d %-I:%M %p ET") if st else None, "as_of": as_of,
                      "rules_move_gen": last, "rules_move_at": self.when(last, "%a %b %-d %-I:%M %p") if last else None}
        short.add_trails(self.frames)
        return self.frames


# ---------------------------------------------------------------- render
def render(state_dir: str, out: str, preview: str | None = None) -> tuple[str, Week]:
    ex.SCALE = 1  # explainer's text helpers draw in pixels here
    wk = Week(state_dir)
    frames = wk.build()
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if preview:
        os.makedirs(preview, exist_ok=True)
    wav = os.path.splitext(out)[0] + ".wav"
    ex.write_wav(wav, ex.soundtrack(frames))
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-i", wav, "-shortest",
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", out],
        stdin=subprocess.PIPE)
    for i, f in enumerate(frames):
        data = compose(f)
        ff.stdin.write(data)
        if preview and (i == 0 or i % (2 * FPS) == FPS):
            Image.frombytes("RGB", (W, H), data).save(os.path.join(preview, f"{i / FPS:05.1f}s.png"))
        if preview and i in wk.landings:
            Image.frombytes("RGB", (W, H), data).save(os.path.join(preview, f"land_{i / FPS:05.1f}s.png"))
    ff.stdin.close()
    if ff.wait() != 0:
        raise RuntimeError("ffmpeg failed")
    return out, wk


def report(wk: Week) -> str:
    out = [json.dumps(wk.facts, indent=1)]
    for s in wk.shown:
        line = f"{s['day']} {s['time']} gen {s['gen']}: {s['command']} {s['result']} " \
               f"(stuck gen {s['effect']['before']} -> {s['effect']['after']}; via {s['via']})"
        if "flew" in s:
            line += f"; flew {s['flew']} generations"
        if "then" in s:
            t = s["then"]
            line += f" | then gen {t['gen']}: {t['words']} {t['result']} ({t['command']}; " \
                    f"stuck gen {t['effect']['before']} -> {t['effect']['after']})"
        out.append(line)
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--out", default=os.path.join("out", "week1.mp4"))
    ap.add_argument("--preview", help="also save the first frame and one every 2 s as PNG into this folder")
    ap.add_argument("--frames-only", action="store_true", help="build the frames and print the timing; no video")
    a = ap.parse_args()
    if a.frames_only:
        ex.SCALE = 1
        wk = Week(a.state_dir)
        fr = wk.build()
        print(f"{len(fr)} frames, {len(fr) / FPS:.1f} s")
        print(report(wk))
        return
    out, wk = render(a.state_dir, a.out, a.preview)
    print(f"wrote {out} ({os.path.getsize(out) / 1e6:.1f} MB, {len(wk.frames) / FPS:.1f} s)")
    print(report(wk))


if __name__ == "__main__":
    main()
