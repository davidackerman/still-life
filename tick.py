#!/usr/bin/env python3
"""One scheduled run: load state -> comments -> inject -> step -> display -> save.

Environment:
  VIDEO_ID, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN   YouTube access (secrets)
  EXTRA_VIDEO_IDS  optional, comma-separated: more of the channel's videos
                   (e.g. a Short) whose comments also play. Their owner status
                   comments are kept up to date; title and thumbnail stay on
                   VIDEO_ID only.
  STATE_KEY        secret for the keyed hashes of comment/author IDs (any long
                   random string; changing it forgets who already commented)
  DRY_RUN          "true" (default): the death sequence never uploads or changes
                   privacy (step 4). Title and thumbnail updates always happen.
  DESCRIPTION_GRID "true" to also keep a braille rendering in the description
                   (default off)
  FORCE_THUMBNAIL  "true" to upload a fresh thumbnail this run (e.g. after a
                   design change); normally every 4 h and on phase changes
  PINNED_LINK      "false" to leave the live-page address out of the pinned
                   status comment (if YouTube holds comments with links)
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
import render
import youtube
from store import Store
from universe import DIED, Universe, flatline_gens

THUMB_PATH = os.path.join("out", "thumbnail.png")
ALERTS_PATH = os.path.join("out", "alerts.json")  # posted as GitHub issues by the workflow


def write_alerts(events: list[dict], world, video_id: str | None, path: str = ALERTS_PATH) -> list[dict]:
    import json

    import alerts
    found = alerts.alerts_for(events, world.launch_time, world.gen_seconds,
                              f"https://www.youtube.com/watch?v={video_id}" if video_id else None)
    if found:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(found, f, ensure_ascii=False, indent=1)
    return found


def env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def create_world(now: float, key: bytes, shape: tuple[int, int] | None = None,
                 extra: dict | None = None) -> tuple[engine.World, dict]:
    """A new universe at generation 0. `shape` is (H, W), default 128 x 72;
    `extra` adds fields to the genesis line (e.g. which universe this is)."""
    seed = os.environ.get("SEED", "acorn")
    if seed not in life.PATTERNS:
        raise SystemExit(f"SEED must be one of {sorted(life.PATTERNS)}")
    shape = tuple(shape or (life.H, life.W))
    gen_seconds = int(os.environ.get("GEN_SECONDS", "120"))
    hours = float(os.environ.get("FLATLINE_HOURS", "24"))
    launch = float(os.environ.get("LAUNCH_TIME") or int(now))
    u = Universe(life.mask(life.centered(seed, shape), shape), flatline_gens(hours, gen_seconds))
    world = engine.World(universe=u, launch_time=launch, seed=seed, gen_seconds=gen_seconds, key=key)
    genesis = {"type": "genesis", "launch_time": launch, "seed": seed, "shape": list(shape),
               "gen_seconds": gen_seconds, "flatline_gens": u.flatline_gens, "flatline_hours": hours,
               "seed_hash": u.hash.hex(), "rules": "B3/S23 torus", **(extra or {})}
    return world, genesis


def run(state_dir: str, now: float, yt_factory, key: bytes, thumb_path: str | None = THUMB_PATH,
        force_thumbnail: bool = False) -> list[dict]:
    """Load (or create), tick once, save. `yt_factory(world)` builds the YouTube client."""
    store = Store(state_dir)
    if store.exists():
        world = store.load(key)
    else:
        world, genesis = create_world(now, key)
        store.create(world, genesis)
        print(f"created a new universe in {state_dir}: {genesis['seed']}, launch {genesis['launch_time']:.0f}")
    if world.recent is None:  # first run with the pinned comment: backfill from the log
        from store import LOG, read_log
        lines = [render.event_line(e) for e in read_log(store.path(LOG))]
        world.recent = [ln for ln in reversed(lines) if ln][: engine.RECENT_MAX]
    events = engine.tick(world, now, yt_factory(world), thumb_path, force_thumbnail,
                         pinned_link=env_flag("PINNED_LINK", "true"))
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
        extra_ids = [v.strip() for v in os.environ.get("EXTRA_VIDEO_IDS", "").split(",") if v.strip()]
        service = youtube.build_service(youtube.credentials_from_env())
    except youtube.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    quota, seen = {}, {}

    def make_yt(world):
        seen["world"] = world
        quota["q"] = youtube.Quota(world.quota, now)
        return youtube.YouTube(service, video_id, quota["q"], env_flag("DESCRIPTION_GRID", "false"),
                               extra_video_ids=extra_ids, reply_counts=world.reply_counts, key=key)

    events = run(args.state_dir, now, make_yt, key, force_thumbnail=env_flag("FORCE_THUMBNAIL", "false"))
    for a in write_alerts(events, seen["world"], video_id):
        print(f"alert: {a['title']}")
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
