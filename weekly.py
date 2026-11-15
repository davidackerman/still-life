#!/usr/bin/env python3
"""Render the week-one video: the universe's first seven days, day by day
(16:9, 1920x1080, about four and a half minutes, on-screen text, procedural sound).

    python weekly.py                                  # out/week1.mp4 from state/
    python weekly.py --preview out/week1_frames       # also save a PNG every 2 s

Everything on screen is replayed from the log: the board, every move at its
generation, what each move did to the forecast (the log's "effect" entries),
and the seed's own future with no moves at all. Moves are shown by their
commands only: the log holds no names, channels or message text. Every number
in a caption is computed here from the log or state.json, and the picks below
are checked against the log, so a caption can't drift from what happened.
Needs ffmpeg.
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

import explainer as ex
import life
import short
from short import History
from universe import DIED

W, H = short.BW, short.BH  # the board at 15 px a cell: 1920 x 1080, the frame
FPS = ex.FPS
ET = ZoneInfo("America/New_York")
CPS, LOOK_S, MIN_S, FADE = 14, 1.2, 2.2, 10  # obituary.py's reading pace: a long video, read slower
FORECAST_GENS = 30000
GREEN, RED, MUTED = (120, 230, 160), (255, 120, 120), (200, 205, 215)


def rf(*lines: str) -> int:
    return int(round(max(MIN_S, sum(map(len, lines)) / CPS + LOOK_S) * FPS)) + 2 * FADE


def fade(i: int, n: int) -> float:
    return ex.fade(i, n, FADE)


def ease(p: float) -> float:
    return short.ease(p)


# ---------------------------------------------------------------- words
def span(gens: int, gen_seconds: int) -> tuple[int, str]:
    """(n, unit): hours under two days, else days."""
    s = abs(gens) * gen_seconds
    if s < 48 * 3600:
        n = max(1, round(s / 3600))
        return n, "hour" + "s" * (n != 1)
    n = round(s / 86400)
    return n, "day" + "s" * (n != 1)


def effect_gens(eff: dict) -> int | None:
    """Generations one move gave (+) or took (-) (engine.effect_gens); None if unknown."""
    if eff is None:
        return None
    end = lambda st: st if st is not None else eff["gen"] + eff.get("horizon", FORECAST_GENS)  # noqa: E731
    return end(eff["after"]) - end(eff["before"])


def effect_line(eff: dict, gen_seconds: int) -> tuple[str, tuple]:
    d = effect_gens(eff)
    if eff["after"] is None:
        return "Gave it weeks more to live.", GREEN
    if eff["before"] is None:
        return "Took weeks off its life.", RED
    n, unit = span(d, gen_seconds)
    if d * gen_seconds >= 1800:
        return f"Gave it {n} more {unit} to live.", GREEN
    if d * gen_seconds <= -1800:
        return f"Took {n} {unit} off its life.", RED
    return "Barely changed its fate.", MUTED


def effect_short(eff: dict, gen_seconds: int) -> tuple[str, tuple]:
    d = effect_gens(eff)
    n, unit = span(d, gen_seconds)
    return (f"+{n} {unit}", GREEN) if d > 0 else (f"−{n} {unit}", RED)


def what(e: dict, said: bool = False) -> str:
    """A move as its command: 'glider 64,71 nw', 'acorn 16,18 red', 'random cells at 64,38'
    (with `said`, 'a message: random cells at 64,38': any words land as a random patch)."""
    c = e["command"]
    if c.startswith("random"):
        c = ("a message: " if said else "") + "random cells at " + c.rsplit(" ", 1)[1]
    return c + (f" {e['color']}" if e.get("color") else "")


# ---------------------------------------------------------------- drawing
def crop(img: Image.Image, f):
    """The part of the board on screen at f.zoom around f.focus, wrapping (it is a torus).
    Returns the frame and a board-px -> screen-px mapping."""
    if f.zoom <= 1:
        return img, (lambda bx, by: (bx, by))
    w, h = W / f.zoom, H / f.zoom
    x0, y0 = f.focus[0] - w / 2, f.focus[1] - h / 2
    if not getattr(f, "wrap", False):
        x0, y0 = min(max(x0, 0), W - w), min(max(y0, 0), H - h)
    xi, yi, wi, hi = int(np.floor(x0)), int(np.floor(y0)), int(np.ceil(w)) + 1, int(np.ceil(h)) + 1
    arr = np.asarray(img)[np.ix_(np.arange(yi, yi + hi) % H, np.arange(xi, xi + wi) % W)]
    out = Image.fromarray(arr).resize((W, H), Image.NEAREST, box=(x0 - xi, y0 - yi, x0 - xi + w, y0 - yi + h))

    def to_screen(bx, by):  # measured from the window's centre, around the torus the short way
        if getattr(f, "wrap", False):
            dx = (bx - (x0 + w / 2) + W / 2) % W - W / 2 + w / 2
            dy = (by - (y0 + h / 2) + H / 2) % H - H / 2 + h / 2
        else:
            dx, dy = bx - x0, by - y0
        return dx * f.zoom, dy * f.zoom
    return out, to_screen


def compose(f) -> bytes:
    img, to_screen = crop(short.board(f), f)
    base = img.convert("RGBA")
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if f.dim:
        d.rectangle((0, 0, W, H), fill=(5, 6, 9, int(255 * f.dim)))
    for extra in f.extras:
        extra(d, to_screen)
    for s, weight, size, xy, anchor, alpha in f.texts:
        ex.text(d, xy, s, weight, size, alpha, anchor=anchor)
    if getattr(f, "stamp", None):  # the date, on a dark tab so cells behind it don't break it up
        tw = d.textlength(f.stamp, font=ex.font("demi", 40))
        d.rounded_rectangle((32, 30, 32 + tw + 48, 92), radius=14, fill=(8, 10, 14, 200))
        ex.text(d, (56, 61), f.stamp, "demi", 40, 1.0, anchor="lm", outline=False)
    return Image.alpha_composite(base, layer).convert("RGB").tobytes()


def centre_text(lines, alpha: float, yc: int = H // 2):
    """[(text, weight, size)] centred as a block on yc."""
    steps = [int(size * 1.35) for _, _, size in lines]
    y = yc - (sum(steps) - steps[-1]) / 2
    out = []
    for (s, weight, size), step in zip(lines, steps):
        out.append((s, weight, size, (W // 2, round(y)), "mm", alpha))
        y += step
    return out


def caption(lines, alpha: float, yc: int = 890):
    """[(text, weight, size, fill)] in a dark pill across the lower third."""
    def draw(d, to_screen):
        if alpha <= 0:
            return
        widths = [d.textlength(t, font=ex.font(w, s)) for t, w, s, _ in lines]
        steps = [int(s * 1.32) for _, _, s, _ in lines]
        tw, th = max(widths), sum(steps)
        x0, x1 = W / 2 - tw / 2 - 40, W / 2 + tw / 2 + 40
        y0 = yc - th / 2 - 22
        d.rounded_rectangle((x0, y0, x1, yc + th / 2 + 22), radius=24, fill=(8, 10, 14, int(220 * alpha)))
        y = y0 + 22
        for (t, w, s, fill), step in zip(lines, steps):
            ex.text(d, (W / 2, y + step / 2), t, w, s, alpha, fill=fill, outline=False)
            y += step
    return draw


def ring(cells, alpha: float, shift=(0.0, 0.0)):
    """A ring around cells (moved by `shift` board px: a glider's ring follows it)."""
    def draw(d, to_screen):
        if alpha <= 0:
            return
        pts = np.array([to_screen(*(np.add(short.cell_px(x, y), shift))) for x, y in cells])
        cx, cy = pts.mean(axis=0)
        r = max(44.0, float(np.hypot(*(pts - (cx, cy)).T).max()) + 30)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, int(255 * alpha)), width=6)
    return draw


# ---------------------------------------------------------------- the week
# One to three real moments a day, picked from the log (each is checked there).
#   ("move", gen, command, lead)          one move: what it was, what it did
#   ("flight", gen, command, lead)        a glider's move, followed as it flies
#   ("pair", gen, cmd, gen2, cmd2, lead2) two moves close together, the second answering the first
#   ("batch", gen, lead)                  every move in one generation, with what each did
#   ("seed", "stuck" | "died")            when the universe would have got stuck / died with no moves
PICKS = {
    1: [("move", 166, "glider 64,71 nw", "the first move"),
        ("flight", 220, "glider 16,61 sw", None)],
    2: [("move", 1208, "rpent 17,23", "5 cells of chaos")],
    3: [("seed", "stuck"),
        ("move", 1950, "random 5x5 at 64,38", None)],
    4: [("seed", "died"),
        ("move", 2684, "glider 66,8 se", "the first colored move"),
        ("pair", 2829, "acorn 16,18", 2833, "random 5x5 at 118,11", "{gap} later")],
    5: [("pair", 3422, "random 5x5 at 102,61", 3448, "glider 18,55 se", "{gap} later, from the same Short")],
    6: [("batch", 4077, "{n} live-chat messages, one generation")],
    7: [("batch", 4977, "{n} live-chat messages, one generation")],
}
CHANGES = {3: "live-chat messages became moves", 4: "colors arrived"}  # docs/changes.json, on that day


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

    # -- facts
    def when(self, gen: int, fmt: str = "%-I:%M %p") -> str:
        return dt.datetime.fromtimestamp(self.launch + gen * self.G, ET).strftime(fmt)

    def stamp(self, gen: int) -> str:
        return f"Day {gen * self.G // 86400 + 1} · " + self.when(gen, "%a %b %-d · %-I:%M %p")

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

    def day_moves(self, d: int) -> list:
        lo, hi = (d - 1) * 86400 // self.G, d * 86400 // self.G
        return [e for e in self.moves if lo <= e["gen"] < hi]

    def people(self) -> int:
        """Distinct people credited with a move (HMAC'd keys in state.json's scores). A floor:
        live-chat moves from before scores were kept (Nov 10) couldn't be matched to anyone."""
        return len(self.state.get("scores") or {})

    # -- frames
    def add(self, u, texts=(), zoom=1.0, focus=(W / 2, H / 2), extras=(), dim=0.0, stamp=None, wrap=False,
            highlight=None, grey=0.0, cue=None):
        f = ex.Frame(u, highlight=highlight, texts=texts, grey=grey, hud=False, extras=extras, zoom=zoom,
                     dim=dim, cue=cue)
        f.focus, f.stamp, f.wrap = focus, stamp, wrap
        paint = getattr(u, "paint", None)
        f.paint = paint.copy() if paint is not None and paint.any() else None
        self.frames.append(f)

    def run(self, u, target: int, n: int, **kw):
        """n frames carrying u to `target`, easing, its moves lighting up as they land."""
        start, lit, until = u.generation, None, -1
        for i in range(n):
            g = start + round((target - start) * (i + 1) / n)
            cells = self.h.goto(u, max(u.generation, g))
            cue = None
            if cells:
                lit, until = life.mask(cells, u.grid.shape), i + 12
                if i % 5 == 0:
                    cue = ("inject", float(np.mean([c[0] for c in cells])))
            self.add(u, highlight=lit if i <= until else None, cue=cue, stamp=self.stamp(u.generation), **kw)

    def hold(self, u, lines, n: int | None = None, step: int = 4, focus_cells=None, zoom: float = 2.4,
             follow=None, highlight=None, rings=(), cue=None, stop: int | None = None):
        """A caption over the board, stepping a generation every `step` frames,
        zoomed in on focus_cells (and following them at `follow` cells/generation)."""
        n = n or rf(*[t for t, *_ in lines])
        g0 = u.generation
        if focus_cells is not None:
            fx, fy = short.centre(short.main_piece(focus_cells))
        for i in range(n):
            stop_ = min(stop, self.h.now_gen) if stop is not None else self.h.now_gen  # never past the log
            if i and i % step == 0 and u.generation < stop_:
                self.h.goto(u, u.generation + 1)
            a = fade(i, n)
            kw = {}
            if focus_cells is not None:
                k = ease(min(i / 14, (n - 1 - i) / 14, 1.0))
                z = 1 + (zoom - 1) * k
            dx, dy = (np.array(follow) * (u.generation - g0) * short.CELL) if follow else (0.0, 0.0)
            if focus_cells is not None:
                # the piece sits above the caption: the window's centre a little below it
                kw = dict(zoom=z, focus=(fx + dx, fy + dy + 0.12 * H / z), wrap=True)
            ring_x = [ring(r, a, (dx, dy)) for r in rings]
            self.add(u, extras=ring_x + [caption(lines, a)], stamp=self.stamp(u.generation),
                     highlight=highlight if i < 40 else None, cue=cue if i == 0 else None, **kw)

    def card(self, u, lines, step: int = 2, zoom: float = 1.0, focus=(W / 2, H / 2), dim: float = 0.5,
             stamp: bool = True, extra_frames: int = 0, grey=None):
        n = rf(*[t for t, _, _ in lines]) + extra_frames
        for i in range(n):
            if i and i % step == 0:
                self.h.goto(u, u.generation + 1)
            a = fade(i, n)
            z = zoom(i, n) if callable(zoom) else zoom
            self.add(u, centre_text(lines, a), zoom=z, focus=focus, dim=dim * a,
                     stamp=self.stamp(u.generation) if stamp else None, grey=grey(i, n) if grey else 0.0)

    # -- the story
    def build(self) -> list:
        h, G = self.h, self.G
        now = h.fresh()
        h.goto(now, h.now_gen)
        if h.now_hash and now.hash.hex() != h.now_hash:
            raise SystemExit("replaying the log did not reproduce the current board; run replay.py")
        days_old = h.now_gen * G // 86400
        people = self.people()

        # 1. cold open: the board today, already moving; then the seed left alone; then back
        u = copy.deepcopy(now)
        lines = [(f"This universe is {'one week' if days_old == 7 else f'{days_old} days'} old.", "bold", 96)]
        n = rf(lines[0][0])
        for i in range(n):
            if i % 2 == 0:
                History.advance(u, u.generation + 1)
            a = min(1.0, (i + 1) / FADE) if i < n - FADE else fade(i, n)
            self.add(u, centre_text(lines, a), dim=0.4 * a)
        seed = History(self.dir).fresh()
        stuck = copy.deepcopy(seed).forecast_stuck(FORECAST_GENS)
        if stuck is None:
            raise SystemExit("the seed alone doesn't get stuck within the forecast; rewrite the cold open")
        F = seed.flatline_gens
        died_at = stuck + F
        acorn = short.centre(life.centered(h.genesis["seed"]))
        lines = [("What if nobody had commented?", "bold", 92)]
        n = rf(lines[0][0])
        for i in range(n):
            p = (i + 1) / n
            History.advance(seed, round((stuck - 40) * p ** 2.2))
            a = fade(i, n)
            z = 1 + 5 * (1 - ease(min(1.0, i / (0.6 * n))))
            self.add(seed, centre_text(lines, a), zoom=z, focus=acorn, dim=0.35 * a, stamp=self.stamp(seed.generation))
        died_words = self.when(died_at, "%A at %-I:%M %p")
        lines = [(f"It would have died {died_words}.", "bold", 84),
                 ("Stuck in a loop, then 24 hours, then nothing.", "demi", 52)]
        n = rf(*[t for t, _, _ in lines]) + FPS
        start = seed.generation
        for i in range(n):
            p = min(1.0, (i + 1) / (0.75 * n))
            History.advance(seed, start + round((died_at - start) * ease(p)))
            a = fade(i, n)
            grey = 1.0 if seed.phase == DIED else 0.0
            self.add(seed, centre_text(lines, a), dim=0.35 * a, stamp=self.stamp(seed.generation), grey=grey)
        u = copy.deepcopy(now)
        lines = [(f"Instead, more than {people // 100 * 100} people played." if people >= 100
                  else f"Instead, {people} people played.", "bold", 84),
                 ("Together, they kept it alive.", "demi", 64)]
        n = rf(*[t for t, _, _ in lines])
        for i in range(n):
            if i % 2 == 0:
                History.advance(u, u.generation + 1)
            self.add(u, centre_text(lines, fade(i, n)), dim=0.45 * fade(i, n))
        self.facts = {"days_old": days_old, "people": people, "seed_stuck": stuck, "seed_died": died_at,
                      "seed_stuck_at": self.when(stuck, "%a %b %-d %-I:%M %p ET"),
                      "seed_died_at": self.when(died_at, "%a %b %-d %-I:%M %p ET")}

        # 2. day by day, on one replay from the seed
        flights = {(g, e["command"]): n for n, g, e, _ in short.flights(History(self.dir))}
        longest = max(flights.values()) if flights else 0
        u = h.fresh()
        last_day = min(7, days_old + 1)
        for d in range(1, last_day + 1):
            day0 = (d - 1) * 86400 // G
            if u.generation < day0:
                self.run(u, day0, int(1.6 * FPS))
            ms = self.day_moves(d)
            from_chat = sum(1 for e in ms if e.get("via") == "chat")
            busiest = max(range(1, last_day + 1), key=lambda k: len(self.day_moves(k)))
            stat = f"{len(ms)} moves" + (", the busiest day" if d == busiest else "")
            if from_chat > len(ms) / 2:
                stat += f", {from_chat} from the live chat"
            if d in CHANGES:
                stat += f" · {CHANGES[d]}"
            lines = [(f"Day {d}", "bold", 130), (self.when(day0, "%A, %b %-d"), "demi", 64), (stat, "medium", 44)]
            if d == 1:  # 7 cells at first: up close, pulling back as it grows
                self.card(u, lines, step=1, zoom=lambda i, n: 1 + 5 * (1 - ease(i / n)), focus=acorn, dim=0.4)
            else:
                self.card(u, lines, dim=0.55)
            for pick in PICKS.get(d, []):
                self.moment(u, pick, flights, longest)
        end_gen = min(h.now_gen, last_day * 86400 // G)
        if u.generation < end_gen:
            self.run(u, end_gen, int(1.6 * FPS))

        # 3. how it works, over the board today
        u = copy.deepcopy(now)
        rules = [
            [("Conway's Game of Life:", "bold", 80), ("one generation every 2 minutes, around the clock.", "demi", 56)],
            [("Every comment adds cells:", "bold", 80),
             ("on this video, on its Shorts, or in the live stream's chat.", "demi", 56)],
            [("Left alone, it always gets stuck in a loop.", "bold", 76),
             ("Then a 24-hour countdown starts.", "demi", 56)],
            [("If nobody breaks the loop in time, it dies.", "bold", 76)],
            [("Every move is in a public log.", "bold", 80), ("Anyone can replay its whole life.", "demi", 56)],
        ]
        for lines in rules:
            n = rf(*[t for t, _, _ in lines])
            for i in range(n):
                if i % 2 == 0:
                    History.advance(u, u.generation + 1)
                self.add(u, centre_text(lines, fade(i, n)), dim=0.5 * fade(i, n))

        # 4. the week in numbers, the forecast today, and what to do
        week = min(h.now_gen, 7 * 86400 // self.G)  # the first 7 days exactly, however late it renders
        moves = sum(1 for e in self.moves if e["gen"] < week)
        lines = [(f"Week one: {moves:,} moves,", "bold", 80),
                 (f"{week:,} generations, not stuck once.", "bold", 80)]
        phases = [e for e in read_log_entries(self.dir) if e["type"] == "phase"]
        if any(e.get("gen", 0) < week for e in phases):
            lines[1] = (f"{week:,} generations.", "bold", 80)
        n = rf(*[t for t, _, _ in lines])
        for i in range(n):
            if i % 2 == 0:
                History.advance(u, u.generation + 1)
            self.add(u, centre_text(lines, fade(i, n)), dim=0.5 * fade(i, n))
        st = copy.deepcopy(now).forecast_stuck(FORECAST_GENS)
        as_of = self.when(h.now_gen, "%A, %b %-d")
        if st is not None:
            fate = [(f"As of {as_of}, with no more comments,", "demi", 56),
                    (f"it gets stuck {self.when(st, '%A, %b %-d')}.", "bold", 80)]
        else:
            fate = [(f"As of {as_of}, it won't get stuck for weeks.", "bold", 72)]
        self.facts.update(moves=moves, now_gen=h.now_gen, forecast_stuck=st,
                          forecast_at=self.when(st, "%a %b %-d %-I:%M %p ET") if st else None, as_of=as_of,
                          longest_flight=longest, phases=len(phases))
        n = rf(*[t for t, _, _ in fate])
        for i in range(n):
            if i % 2 == 0:
                History.advance(u, u.generation + 1)
            self.add(u, centre_text(fate, fade(i, n)), dim=0.5 * fade(i, n))
        end = [("Comment anything.", "bold", 110), ("Even “hi” adds cells.", "demi", 72),
               ("Or type in the live stream's chat. It's on this channel.", "medium", 48)]
        n = max(rf(*[t for t, _, _ in end]), 9 * FPS)
        for i in range(n):
            if i % 2 == 0:
                History.advance(u, u.generation + 1)
            a = min(1.0, (i + 1) / FADE)
            self.add(u, centre_text(end, a), dim=0.5 * a)
        short.add_trails(self.frames)
        return self.frames

    def moment(self, u, pick, flights, longest):
        h, G = self.h, self.G
        kind = pick[0]
        if kind == "seed":
            seed = History(self.dir).fresh()
            st = seed.forecast_stuck(FORECAST_GENS)
            g = st if pick[1] == "stuck" else st + seed.flatline_gens
            self.run(u, g, int(1.8 * FPS))
            what_now = "be stuck in a loop now" if pick[1] == "stuck" else "have died now"
            lines = [(f"{self.when(g)} · With no comments, it would {what_now}.", "bold", 52, (255, 255, 255)),
                     ("Instead, it's still changing.", "demi", 46, GREEN)]
            self.hold(u, lines, step=2)
            return
        if kind in ("move", "flight"):
            _, g, command, lead = pick
            e = self.move(g, command)
            self.run(u, g - 1, int(1.8 * FPS))
            cells = [tuple(c) for c in e["cells"]]
            eff_text, eff_fill = effect_line(self.effect(e), G)
            head = f"{self.when(g)} · " + (f"{lead}: " if lead else "") + what(e, said=not lead)
            lines = [(head, "bold", 56, (255, 255, 255)), (eff_text, "demi", 50, eff_fill)]
            follow, step = None, 4
            if command.startswith("glider"):  # it flies: the view and its ring go with it
                dx, dy = short.GLIDER_DIRS[command.split()[-1]]
                follow = (dx / 4, dy / 4)
            if kind == "flight":
                flown = flights.get((g, command), 0)
                hours = flown * G // 3600
                tail = "the longest flight of the week" if flown == longest else "before it hit something"
                lines.append((f"Then it flew for over {hours} hours: {tail}.", "demi", 46, (255, 255, 255)))
                step = 2
            landed = h.goto(u, g)
            self.hold(u, lines, step=step, focus_cells=cells, follow=follow, rings=[short.main_piece(cells)],
                      highlight=life.mask(landed, u.grid.shape), cue=("inject", float(np.mean([c[0] for c in cells]))))
            return
        if kind == "pair":
            _, g1, c1, g2, c2, lead2 = pick
            e1, e2 = self.move(g1, c1), self.move(g2, c2)
            gap = _gap((g2 - g1) * G)
            src = self.state.get("video_names", {}).get(e1.get("via"))
            for k, (g, e) in enumerate(((g1, e1), (g2, e2))):
                if u.generation < g - 1:
                    self.run(u, g - 1, int((1.8 if k == 0 else 0.8) * FPS))
                cells = [tuple(c) for c in e["cells"]]
                eff_text, eff_fill = effect_line(self.effect(e), G)
                if k == 0:
                    head = f"{self.when(g)} · " + (f"a comment on {src}: " if src and e.get("via") != "chat" else "") + what(e)
                else:
                    same = e2.get("via") == e1.get("via")
                    lead = lead2.format(gap=gap)
                    if not same:
                        lead = lead.replace(", from the same Short", "")
                    head = f"{lead[0].upper()}{lead[1:]}: {what(e)}"
                lines = [(head, "bold", 54, (255, 255, 255)), (eff_text, "demi", 50, eff_fill)]
                landed = h.goto(u, g)
                n = rf(*[t for t, *_ in lines])
                # the first caption mustn't run past the second move: slow the board to fit
                step = max(4, -(-n // max(1, g2 - g1 - 1))) if k == 0 else 4
                self.hold(u, lines, n=n, step=step, focus_cells=cells, rings=[short.main_piece(cells)], stop=g2 - 1 if k == 0 else None,
                          highlight=life.mask(landed, u.grid.shape),
                          cue=("inject", float(np.mean([c[0] for c in cells]))))
            return
        if kind == "batch":
            _, g, lead = pick
            batch = [e for e in h.batches.get(g, []) if (g, e["command"]) in h.effects]
            if len(batch) < 2:
                raise SystemExit(f"weekly.py: generation {g} doesn't hold several moves with effects")
            self.run(u, g - 1, int(1.8 * FPS))
            chat = all(e.get("via") == "chat" for e in batch)
            head = f"{self.when(g)} · " + lead.format(n=_count(len(batch))) if chat else \
                f"{self.when(g)} · {_count(len(batch))} moves, one generation"
            lines = [(head, "bold", 54, (255, 255, 255))]
            for e in batch:
                t, fill = effect_short(self.effect(e), G)
                lines.append((f"{what(e)}: {t}", "demi", 46, fill))
            landed = h.goto(u, g)
            rings = [short.main_piece([tuple(c) for c in e["cells"]]) for e in batch]
            self.hold(u, lines, step=6, rings=rings, highlight=life.mask(landed, u.grid.shape),
                      cue=("inject", float(np.mean([c[0] for c in landed]))))


def _gap(seconds: int) -> str:
    m = round(seconds / 60)
    return f"{m} minutes" if m < 120 else f"{round(m / 60)} hours"


def _count(n: int) -> str:
    return {2: "Two", 3: "Three", 4: "Four", 5: "Five"}.get(n, str(n))


def read_log_entries(state_dir: str) -> list:
    from store import read_log
    return read_log(os.path.join(state_dir, "log.jsonl"))


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
        if preview and i % (2 * FPS) == FPS:
            Image.frombytes("RGB", (W, H), data).save(os.path.join(preview, f"{i // FPS:03d}s.png"))
    ff.stdin.close()
    if ff.wait() != 0:
        raise RuntimeError("ffmpeg failed")
    return out, wk


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--out", default=os.path.join("out", "week1.mp4"))
    ap.add_argument("--preview", help="also save one frame every 2 s as PNG into this folder")
    ap.add_argument("--frames-only", action="store_true", help="build the frames and print the timing; no video")
    a = ap.parse_args()
    if a.frames_only:
        ex.SCALE = 1
        wk = Week(a.state_dir)
        fr = wk.build()
        print(f"{len(fr)} frames, {len(fr) / FPS:.1f} s")
        print(json.dumps(wk.facts, indent=1))
        return
    out, wk = render(a.state_dir, a.out, a.preview)
    print(f"wrote {out} ({os.path.getsize(out) / 1e6:.1f} MB, {len(wk.frames) / FPS:.1f} s)")
    print(json.dumps(wk.facts, indent=1))


if __name__ == "__main__":
    main()
