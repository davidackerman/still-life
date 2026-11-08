#!/usr/bin/env python3
"""Rehearse a death without touching YouTube: fast-forward a scratch universe
(acorn, a few made-up comments) until it dies, then run the real death sequence
in dry-run mode, which renders the real obituary to out/obituary.mp4.

    python rehearse.py [--dir /tmp/still-life-rehearsal]

Used by .github/workflows/rehearsal.yml to check fonts, ffmpeg and timing on
GitHub's runners. Needs no secrets.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import time

import tick
from admission import Comment

LAUNCH = 1_794_114_283.0


class FakeYT:
    videos = ["MAIN-VIDEO", "A-SHORT"]

    def __init__(self):
        iso = lambda t: dt.datetime.fromtimestamp(LAUNCH + t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        self.comments = [Comment("c1", "u1", iso(20000), "glider 64,71 nw"),
                         Comment("c2", "u2", iso(26000), "glider 16,61 sw"),
                         Comment("c3", "u3", iso(90000), "hello universe")]

    def fetch_comments(self, since, watch=(), now=None):
        return [c for c in self.comments if c.epoch <= (now or 0)]

    def set_title(self, t, grid_text=""):
        pass

    def set_thumbnail(self, path):
        pass

    def display_name_of(self, keyed_comment):
        return "@a-commenter"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", default=os.path.join("/tmp", "still-life-rehearsal"))
    a = ap.parse_args()
    shutil.rmtree(a.dir, ignore_errors=True)
    os.environ["LAUNCH_TIME"] = str(int(LAUNCH))
    yt, key, t = FakeYT(), b"rehearsal-key-0123456789", 0
    while True:
        ev = tick.run(a.dir, LAUNCH + t, lambda w: yt, key, thumb_path=None)
        if any(e["type"] == "phase" and e["to"] == "DIED" for e in ev):
            break
        t += 1800
    print(f"died after {t / 86400:.1f} simulated days; running the death sequence (dry run)")
    start = time.time()
    ev = tick.run(a.dir, LAUNCH + t + 300, lambda w: yt, key, thumb_path=None, dry_run=True)
    for e in ev:
        if e["type"] == "death":
            print({k: v for k, v in e.items() if k != "detail"})
    print(f"death sequence took {time.time() - start:.0f} s")


if __name__ == "__main__":
    main()
