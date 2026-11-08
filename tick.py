#!/usr/bin/env python3
"""One scheduled run: load state -> comments -> inject -> step -> display -> save.

Environment:
  VIDEO_ID, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN   YouTube access (secrets)
  STATE_KEY        secret for the keyed hashes of comment/author IDs (any long
                   random string; changing it forgets who already commented)
  DRY_RUN          "true" (default): the death sequence never uploads or changes
                   privacy (step 4). Title and thumbnail updates always happen.
  DESCRIPTION_GRID "true" to also keep a braille rendering in the description
                   (default off)
  First run only (later runs read these from the log's genesis line):
  LAUNCH_TIME      epoch seconds for generation 0 (default: now)
  SEED             starting pattern (default acorn)
  FLATLINE_HOURS   default 24       GEN_SECONDS   default 120
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import engine
import life
import youtube
from store import Store
from universe import DIED, Universe, flatline_gens

THUMB_PATH = os.path.join("out", "thumbnail.png")


def env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def create_world(now: float, key: bytes) -> tuple[engine.World, dict]:
    seed = os.environ.get("SEED", "acorn")
    if seed not in life.PATTERNS:
        raise SystemExit(f"SEED must be one of {sorted(life.PATTERNS)}")
    gen_seconds = int(os.environ.get("GEN_SECONDS", "120"))
    hours = float(os.environ.get("FLATLINE_HOURS", "24"))
    launch = float(os.environ.get("LAUNCH_TIME") or int(now))
    u = Universe(life.mask(life.centered(seed)), flatline_gens(hours, gen_seconds))
    world = engine.World(universe=u, launch_time=launch, seed=seed, gen_seconds=gen_seconds, key=key)
    genesis = {"type": "genesis", "launch_time": launch, "seed": seed, "shape": [life.H, life.W],
               "gen_seconds": gen_seconds, "flatline_gens": u.flatline_gens, "flatline_hours": hours,
               "seed_hash": u.hash.hex(), "rules": "B3/S23 torus"}
    return world, genesis


def run(state_dir: str, now: float, yt_factory, key: bytes, thumb_path: str | None = THUMB_PATH) -> list[dict]:
    """Load (or create), tick once, save. `yt_factory(world)` builds the YouTube client."""
    store = Store(state_dir)
    if store.exists():
        world = store.load(key)
    else:
        world, genesis = create_world(now, key)
        store.create(world, genesis)
        print(f"created a new universe in {state_dir}: {genesis['seed']}, launch {genesis['launch_time']:.0f}")
    events = engine.tick(world, now, yt_factory(world), thumb_path)
    store.save(world, events)
    return events


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Advance the universe once.")
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--now", type=float, help="override the clock (epoch seconds)")
    args = ap.parse_args(argv)

    key = os.environ.get("STATE_KEY", "").strip().encode("utf-8")
    if len(key) < 16:
        print("STATE_KEY must be set to a secret of at least 16 characters", file=sys.stderr)
        return 2
    dry_run = env_flag("DRY_RUN", "true")
    now = args.now if args.now is not None else time.time()
    try:
        video_id = youtube.require_env("VIDEO_ID")["VIDEO_ID"]
        service = youtube.build_service(youtube.credentials_from_env())
    except youtube.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    quota = {}

    def make_yt(world):
        quota["q"] = youtube.Quota(world.quota, now)
        return youtube.YouTube(service, video_id, quota["q"], env_flag("DESCRIPTION_GRID", "false"))

    events = run(args.state_dir, now, make_yt, key)
    for e in events:
        if e["type"] in ("phase", "youtube_error", "display_skipped", "display"):
            print({k: v for k, v in e.items() if k not in ("cells",)})
    tick = next((e for e in events if e["type"] == "tick"), None)
    if tick:
        print(f"gen {tick['gen_start']:,} -> {tick['gen_end']:,} {tick['phase']} | new comments "
              f"{tick['comments_new']}, accepted {tick['accepted']}, rejected {tick['rejected']}, "
              f"queued {tick['queued']} | quota {quota['q'].used}/{youtube.DAILY_BUDGET} | dry_run={dry_run}")
    else:
        print(f"nothing to do | quota {quota['q'].used}/{youtube.DAILY_BUDGET}")
    if tick and tick["phase"] == DIED:
        print("universe is DIED; the death sequence arrives in step 4 (nothing uploaded, nothing made private)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
