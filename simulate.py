#!/usr/bin/env python3
"""Fully local run: fake YouTube, fake clock, synthetic commenters.

Fast-forwards the real tick logic (engine.tick) in 5-minute steps and prints
the phase timeline. Use it to tune the pace (--gen-seconds) and the flatline
window (--flatline-hours).

    python simulate.py --rate 0.5 --days 30                one lifetime, with timeline
    python simulate.py --rate 2 --thumbs out/sim           also write every thumbnail "uploaded"
    python simulate.py --sweep 0,0.1,0.5,2,10 --seeds 5    compare comment rates
"""

from __future__ import annotations

import argparse
import bisect
import os
import shutil
import statistics

import numpy as np

import engine
import life
from admission import Comment
from universe import CONTESTED, DIED, FLATLINING, GEN_SECONDS, Universe, flatline_gens

LAUNCH = 1_800_000_000.0  # arbitrary fixed epoch for the fake clock (2027-01-15)
CHATTER = ["first!", "save it", "let it die", "what am I looking at", "hello universe", "RIP",
           "this is oddly calming", "is it still alive?", "🔥🔥🔥", "come on little gliders",
           "I was here", "who else is watching at 3am", "don't repeat yourself", "beautiful"]
PATTERN_WEIGHTS = [("glider", 0.40), ("block", 0.15), ("blinker", 0.15), ("rpent", 0.10),
                   ("acorn", 0.10), ("cells", 0.10)]


def synthetic_comments(rng, rate_per_hour, days, authors, command_frac, shape):
    """Poisson comment arrivals over `days`, as (sorted times, Comments)."""
    times, comments = [], []
    if rate_per_hour <= 0:
        return times, comments
    t = LAUNCH
    end = LAUNCH + days * 86400
    n = 0
    while True:
        t += rng.exponential(3600 / rate_per_hour)
        if t >= end:
            break
        n += 1
        h, w = shape
        if rng.random() < command_frac:
            kind = rng.choice([k for k, _ in PATTERN_WEIGHTS], p=[p for _, p in PATTERN_WEIGHTS])
            if kind == "cells":
                k = int(rng.integers(3, 9))
                text = "cells " + " ".join(f"{rng.integers(w)},{rng.integers(h)}" for _ in range(k))
            elif kind == "glider":
                text = f"glider {rng.integers(w)},{rng.integers(h)} {rng.choice(['ne', 'nw', 'se', 'sw'])}"
            else:
                text = f"{kind} {rng.integers(w)},{rng.integers(h)}"
        else:
            text = str(rng.choice(CHATTER))
        ts = np.datetime64(int(t), "s").astype(str) + "Z"
        comments.append(Comment(id=f"Ugsim{n:06d}", author=f"UCsim{int(rng.integers(authors)):04d}",
                                published_at=ts, text=text))
        times.append(float(int(t)))
    return times, comments


class FakeYouTube:
    """Stands in for youtube.py. Returns comments posted in the last 2 hours."""

    def __init__(self, times, comments, thumbs_dir=None):
        self.times, self.comments = times, comments
        self.now = LAUNCH
        self.titles: list[tuple[float, str]] = []
        self.thumbnails = 0
        self.thumbs_dir = thumbs_dir

    def fetch_comments(self):
        lo = bisect.bisect_left(self.times, self.now - 7200)
        hi = bisect.bisect_right(self.times, self.now)
        return self.comments[lo:hi]

    def set_title(self, title):
        self.titles.append((self.now, title))

    def set_thumbnail(self, path):
        self.thumbnails += 1
        if path and self.thumbs_dir:
            os.makedirs(self.thumbs_dir, exist_ok=True)
            shutil.copy(path, os.path.join(self.thumbs_dir, f"{self.thumbnails:04d}.png"))


def when(gen: int, gen_seconds: int) -> str:
    s = gen * gen_seconds
    return f"day {s // 86400:>2} {s % 86400 // 3600:02d}:{s % 3600 // 60:02d}"


def describe(ev: dict, gen_seconds: int) -> str:
    r = ev["reason"]
    if r in ("cycle_detected", "new_cycle"):
        hrs = (ev["deadline_gen"] - ev["gen"]) * gen_seconds / 3600
        what = "cycle detected" if r == "cycle_detected" else "injection led into a NEW cycle"
        return (f"{what} (period {ev['period']}, began gen {ev['cycle_start']:,}); "
                f"dies at gen {ev['deadline_gen']:,} unless saved ({hrs:g}h)")
    return {"injection": "a comment broke the cycle; deadline still running",
            "reentered_cycle": "fell back into the same cycle; original deadline stands",
            "survived_deadline": "SAVED: still changing when the deadline passed",
            "deadline": "deadline reached"}.get(r, r)


def run(rate, days=30, seed=1, authors=30, command_frac=0.5, pattern="acorn",
        hours=24.0, gen_seconds=GEN_SECONDS, tick_minutes=5, thumbs_dir=None, quiet=False):
    rng = np.random.default_rng(seed)
    shape = (life.H, life.W)
    times, comments = synthetic_comments(rng, rate, days, authors, command_frac, shape)
    yt = FakeYouTube(times, comments, thumbs_dir)
    u = Universe(life.mask(life.centered(pattern)), flatline_gens(hours, gen_seconds))
    world = engine.World(universe=u, launch_time=LAUNCH, gen_seconds=gen_seconds)
    thumb_path = os.path.join("out", "sim_thumbnail.png") if thumbs_dir else None

    stats = {"applied": 0, "changed": 0, "rate_limited": 0, "dead_rejected": 0, "flatlines": 0,
             "saves": 0, "failed_saves": 0, "last_changer": None}
    if not quiet:
        print(f"rate {rate}/h · {len(comments)} comments over {days} days · seed {seed} · "
              f"{pattern} · flatline {hours:g}h · 1 gen / {gen_seconds}s")
        print(f"  {when(0, gen_seconds)}  gen      0  ALIVE        launched with {pattern}")
    now, end = LAUNCH, LAUNCH + days * 86400
    while now <= end:
        yt.now = now
        for ev in engine.tick(world, now, yt, thumb_path):
            t = ev["type"]
            if t == "injection":
                stats["applied"] += 1
                if ev["changed"]:
                    stats["changed"] += 1
                    stats["last_changer"] = (ev["author"], ev["command"], ev["gen"])
            elif t == "rejected":
                stats["rate_limited" if ev["reason"] == "author_rate_limit" else "dead_rejected"] += 1
            elif t == "phase":
                if ev["to"] == FLATLINING and ev["reason"] != "reentered_cycle":
                    stats["flatlines"] += 1
                stats["saves"] += ev["reason"] == "survived_deadline"
                stats["failed_saves"] += ev["reason"] == "reentered_cycle"
                if not quiet:
                    print(f"  {when(ev['gen'], gen_seconds)}  gen {ev['gen']:>6,}  {ev['to']:<11}  "
                          f"{describe(ev, gen_seconds)}")
        if u.phase == DIED:
            break
        now += tick_minutes * 60

    sim_days = (now - LAUNCH) / 86400
    stats.update(outcome=u.phase, gen=u.generation, died_day=(u.died_gen * gen_seconds / 86400) if u.died_gen else None,
                 titles=len(yt.titles), thumbnails=yt.thumbnails, runs=world.runs,
                 quota_per_day=(world.runs * 1 + len(yt.titles) * 51 + yt.thumbnails * 50) / max(sim_days, 1e-9))
    if not quiet:
        if u.phase == DIED:
            print(f"\n  DIED at gen {u.died_gen:,}, {stats['died_day']:.1f} days after launch.")
        else:
            print(f"\n  Still {u.phase} at gen {u.generation:,} after {days} days.")
        lc = stats["last_changer"]
        print(f"  injections applied {stats['applied']} (changed the board: {stats['changed']}), "
              f"rate-limited {stats['rate_limited']}, after death {stats['dead_rejected']}")
        print(f"  flatlines {stats['flatlines']}, saves {stats['saves']}, failed saves {stats['failed_saves']}")
        if lc:
            print(f"  last injection that changed the universe: {lc[1]!r} by {lc[0]} at gen {lc[2]:,}")
        print(f"  display: {stats['titles']} title updates, {stats['thumbnails']} thumbnails, "
              f"~{stats['quota_per_day']:,.0f} quota units/day (estimate)")
    return stats


def sweep(rates, seeds, **kw):
    days = kw.get("days", 30)
    over = f">{days:g}"
    print(f"{'rate/h':>7}  {'comments/day':>12}  {'died on day (one per seed)':<34} {'median':>7}  "
          f"{'flatlines':>9} {'saves':>5}")
    for rate in rates:
        results = [run(rate, seed=s, quiet=True, **kw) for s in range(1, seeds + 1)]
        died = [r["died_day"] for r in results]
        cells = " ".join(f"{d:>5.1f}" if d is not None else f"{over:>5}" for d in died)
        med = statistics.median([d if d is not None else float("inf") for d in died])
        med_s = f"{med:.1f}" if med != float("inf") else over
        print(f"{rate:>7g}  {rate * 24:>12g}  {cells:<34} {med_s:>7}  "
              f"{statistics.mean(r['flatlines'] for r in results):>9.1f} "
              f"{statistics.mean(r['saves'] for r in results):>5.1f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rate", type=float, default=0.5, help="comments per hour (Poisson)")
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--authors", type=int, default=30, help="distinct commenters")
    ap.add_argument("--command-frac", type=float, default=0.5, help="share of comments that are commands")
    ap.add_argument("--pattern", default="acorn", choices=sorted(life.PATTERNS))
    ap.add_argument("--flatline-hours", type=float, default=24.0)
    ap.add_argument("--gen-seconds", type=int, default=GEN_SECONDS)
    ap.add_argument("--tick-minutes", type=float, default=5)
    ap.add_argument("--thumbs", metavar="DIR", help="write every thumbnail 'uploaded' to DIR")
    ap.add_argument("--sweep", metavar="RATES", help="comma-separated comment rates to compare")
    ap.add_argument("--seeds", type=int, default=5, help="seeds per rate in --sweep")
    a = ap.parse_args()
    kw = dict(days=a.days, authors=a.authors, command_frac=a.command_frac, pattern=a.pattern,
              hours=a.flatline_hours, gen_seconds=a.gen_seconds, tick_minutes=a.tick_minutes)
    if a.sweep:
        sweep([float(r) for r in a.sweep.split(",")], a.seeds, **kw)
    else:
        run(a.rate, seed=a.seed, thumbs_dir=a.thumbs, **kw)


if __name__ == "__main__":
    main()
