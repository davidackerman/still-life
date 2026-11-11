#!/usr/bin/env python3
"""Render the obituary: a dead universe's whole life, its death, and the next
universe starting (the same video then hosts it).

    python obituary.py --state-dir state --next 176x99 [--credit "@name"] [--scale 1]

Everything shown is replayed from the log: every comment at its generation,
the phases, the loop it died in. The end card credits the last comment that
changed the board: its command always, and the commenter's display name when
the caller looked it up (never stored anywhere). Needs ffmpeg.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageDraw

import explainer as ex
import life
from render import BG, GRID, HIGHLIGHT, PHASE_COLOR
from short import History
from universe import ALIVE, DIED, FLATLINING, Universe

DW, DH = ex.DW, ex.DH  # design units; pixels = design units x ex.SCALE
FPS = ex.FPS
LIVE_URL = ex.LIVE_URL
ET = ZoneInfo("America/New_York")
CPS, LOOK_S, MIN_S, FADE = 14, 1.2, 2.2, 10


def rf(*lines: str) -> int:
    return int(round(max(MIN_S, sum(map(len, lines)) / CPS + LOOK_S) * FPS)) + 2 * FADE


def fade(i: int, n: int) -> float:
    return ex.fade(i, n, FADE)


def block(lines, y0: int, alpha: float):
    out, y = [], y0
    for text, weight, size in lines:
        out.append((text, weight, size, (DW // 2, y), "mm", alpha))
        y += int(size * 1.4)
    return out


# ---------------------------------------------------------------- drawing
_tiles: dict = {}


def board(f) -> Image.Image:
    """The board filling the frame, the right way up, at any 16:9 size."""
    h, w = f.grid.shape
    W, H = DW * ex.SCALE, DH * ex.SCALE
    c = max(2, round(W / w))
    if (h, w, c) not in _tiles:
        inner = np.zeros((c, c), bool)
        inner[1:-1, 1:-1] = True
        _tiles[(h, w, c)] = np.tile(inner, (h, w))
    tile = _tiles[(h, w, c)]
    color = np.array(PHASE_COLOR[f.phase], float)
    if f.grey:
        color = color * (1 - f.grey) + np.array(PHASE_COLOR[DIED], float) * f.grey
    img = np.empty(tile.shape + (3,), np.uint8)
    img[:] = GRID
    img[tile] = BG
    img[f.grid.repeat(c, 0).repeat(c, 1).astype(bool) & tile] = color.astype(np.uint8)
    if f.highlight is not None:
        img[(f.grid & f.highlight).repeat(c, 0).repeat(c, 1).astype(bool) & tile] = HIGHLIGHT
    im = Image.fromarray(img)
    bw, bh = im.size
    if f.zoom > 1:
        fx, fy = getattr(f, "focus", (bw / 2, bh / 2))
        zw, zh = bw / f.zoom, bh / f.zoom
        x0, y0 = min(max(fx * bw - zw / 2, 0), bw - zw), min(max(fy * bh - zh / 2, 0), bh - zh)
        return im.resize((W, H), Image.NEAREST, box=(x0, y0, x0 + zw, y0 + zh))
    return im if im.size == (W, H) else im.resize((W, H), Image.LANCZOS)


def compose(f) -> bytes:
    base = board(f).convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if f.texts and f.dim:
        a = max(t[5] for t in f.texts)
        d.rectangle(ex.S(0, 0, DW, DH - (120 if f.hud else 0)), fill=(5, 6, 9, int(255 * f.dim * a)))
    if f.hud:
        ex.hud(d, f)
    for s, weight, size, xy, anchor, alpha in f.texts:
        ex.text(d, xy, s, weight, size, alpha, anchor=anchor)
    return Image.alpha_composite(base, layer).convert("RGB").tobytes()


# ---------------------------------------------------------------- the story
def day(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, ET).strftime("%b %-d")


def lifespan(secs: float) -> str:
    d, h = int(secs // 86400), int(secs % 86400 // 3600)
    return f"{d} day{'s' * (d != 1)}, {h} hour{'s' * (h != 1)}" if d else f"{h} hour{'s' * (h != 1)}"


def facts(h: History) -> dict:
    """What the obituary says, from the log alone."""
    from store import read_log
    log = read_log(h.path)
    phases = [e for e in log if e["type"] == "phase"]
    died = next((e for e in phases if e["to"] == DIED), None)
    if died is None:
        raise SystemExit("this universe has not died")
    stuck = [e for e in phases if e["to"] == FLATLINING and e["gen"] <= died["gen"]]
    changed = [e for e in log if e["type"] == "injection" and e["changed"]]
    g = h.genesis
    return {"universe": g.get("universe", 1), "launch": g["launch_time"], "died_gen": died["gen"],
            "died_at": g["launch_time"] + died["gen"] * g["gen_seconds"], "last_stuck": stuck[-1]["gen"],
            "comments": len([e for e in log if e["type"] == "injection"]), "changed": len(changed),
            "saves": len([e for e in phases if e["reason"] == "survived_deadline"]),
            "last_change": changed[-1] if changed else None}


def build(state_dir: str, next_shape: tuple[int, int], credit: str | None = None) -> list:
    h = History(state_dir)
    h.path = os.path.join(state_dir, "log.jsonl")
    fx = facts(h)
    n = fx["universe"]
    frames = []

    def add(u, texts=(), focus=(0.5, 0.5), **kw):
        f = ex.Frame(u, texts=texts, **{"hud": False, "dim": 0.55, **kw})
        f.focus = focus
        frames.append(f)

    # replay to the end once, so the opening can sit on the dead board
    end = h.fresh()
    h.goto(end, fx["died_gen"])
    span = f"{day(fx['launch'])} – {day(fx['died_at'])}, {dt.datetime.fromtimestamp(fx['died_at'], ET).year}"

    # 1. it is dead
    lines = [(f"Universe #{n} has died.", "bold", 100),
             (f"{span} · it lived {lifespan(fx['died_at'] - fx['launch'])}", "medium", 50),
             (f"{fx['died_gen']:,} generations · {fx['comments']:,} comments", "medium", 50)]
    k = rf(*[t for t, _, _ in lines])
    for i in range(k):
        add(end, block(lines, 420, fade(i, k)), grey=1.0, dim=0.6)

    # 2. its whole life, fast (about 25 s), slowing into the last loop
    u = h.fresh()
    k = 25 * FPS
    target = fx["last_stuck"]
    lit, lit_until, comments = None, -1, 0
    for i in range(k):
        cells = h.goto(u, max(u.generation, round(target * ((i + 1) / k) ** 1.15)))
        if cells:
            lit, lit_until = life.mask(cells, u.grid.shape), i + 20
            comments += 1
        cue = ("inject", float(np.mean([c[0] for c in cells]))) if cells and i % 6 == 0 else None
        add(u, [(f"{day(fx['launch'] + u.generation * h.gen_seconds)}", "demi", 44, (60, 70), "lm", 1.0)],
            hud=True, dim=0.0, highlight=lit if i <= lit_until else None, cue=cue)

    # 3. stuck, the countdown, death
    lines = [(f"Generation {fx['last_stuck']:,}: stuck in a loop.", "bold", 76)]
    k = rf(lines[0][0])
    for i in range(k):
        if i % 8 == 0:
            u.advance_to(u.generation + 1)
        add(u, block(lines, 470, fade(i, k)), hud=True, dim=0.35)
    lines = [("24 hours. Nobody broke it.", "bold", 76)]
    k = rf(lines[0][0]) + FPS
    start = u.generation
    for i in range(k):
        u.advance_to(min(fx["died_gen"], start + round((fx["died_gen"] - start) * (i + 1) / (k - FPS))))
        add(u, block(lines, 470, fade(i, k)), hud=True, dim=0.35, grey=max(0.0, (i - (k - FPS)) / FPS))

    # 4. the end card
    lc = fx["last_change"]
    lines = [(f"Universe #{n}", "bold", 84), (span, "medium", 48),
             (f"{fx['changed']:,} comment{'s' * (fx['changed'] != 1)} changed it.", "demi", 56)]
    if fx["saves"]:
        lines.append((f"Comments saved it from a countdown {fx['saves']}×.", "demi", 50))
    if lc:
        who = f", by {credit}" if credit else ""
        lines.append((f"The last change: {lc['command']}{who} (gen {lc['gen']:,})", "medium", 44))
    lines.append(("Thank you.", "bold", 64))
    k = rf(*[t for t, _, _ in lines]) + FPS
    for i in range(k):
        add(end, block(lines, 300, fade(i, k)), grey=1.0, dim=0.7)

    # 5. the next universe, bigger
    H2, W2 = next_shape
    seed = h.genesis["seed"]
    nxt = Universe(life.mask(life.centered(seed, next_shape), next_shape), h.genesis["flatline_gens"])
    lines = [(f"Universe #{n + 1} starts now.", "bold", 96), (f"Bigger: {W2} × {H2} cells.", "medium", 56)]
    k = rf(*[t for t, _, _ in lines])
    for i in range(k):
        add(nxt, [(lines[0][0], "bold", 96, (DW // 2, 150), "mm", fade(i, k)),
                  (lines[1][0], "medium", 56, (DW // 2, 940), "mm", fade(i, k))], zoom=8.0, dim=0.2)
    rules = [("This video dies when its universe gets stuck in a loop.", "bold", 62),
             ("Every comment adds cells:", "demi", 50),
             ("glider x,y · block x,y · blinker x,y · rpent x,y · acorn x,y · or anything", "medium", 40),
             (f"x is 0–{W2 - 1} and y is 0–{H2 - 1}, from the top-left · as many as you like", "medium", 40)]
    k = rf(*[t for t, _, _ in rules])
    for i in range(k):
        nxt.advance_to(nxt.generation + 1)
        add(nxt, block(rules, 330, fade(i, k)), zoom=1 + 7.0 * (1 - min(1.0, i / 75)) ** 2, dim=0.55)
    end_lines = [("Comment below to change it.", "bold", 80), ("Watch it live, generation by generation:", "medium", 44),
                 (LIVE_URL, "demi", 54)]
    k = max(rf(*[t for t, _, _ in end_lines]), 6 * FPS)
    for i in range(k):
        nxt.advance_to(nxt.generation + 1)
        add(nxt, block(end_lines, 400, min(1.0, (i + 1) / FADE)), dim=0.5)
    return frames


def render(state_dir: str, next_shape: tuple[int, int], out: str, credit: str | None = None,
           scale: int = 1, preview: str | None = None) -> str:
    ex.SCALE = scale
    frames = build(state_dir, next_shape, credit)
    W, H = DW * scale, DH * scale
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
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--next", required=True, help="next universe's size, WxH, e.g. 176x99")
    ap.add_argument("--credit", help="display name for the last change (looked up by the bot)")
    ap.add_argument("--out", default=os.path.join("out", "obituary.mp4"))
    ap.add_argument("--scale", type=int, default=1, help="1 = 1080p (default), 2 = 4K")
    ap.add_argument("--preview", help="also save one frame every 2 s as PNG into this folder")
    a = ap.parse_args()
    w, h = map(int, a.next.lower().split("x"))
    print(f"wrote {render(a.state_dir, (h, w), a.out, a.credit, a.scale, a.preview)}")


if __name__ == "__main__":
    main()
