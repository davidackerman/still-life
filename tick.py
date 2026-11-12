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
  FORCE_PINNED     "true" to rewrite the status comments this run if their text
                   changed (normally at most every 30 min, hourly on extras)
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


SLOW_EXTRA_S = 30 * 60  # Shorts older than the newest three: comments read every 30 minutes
COMMENTS_EVERY_S = 270  # the videos' comments: about every 5 minutes (runs come every 1-5 for the chat: deploy/kick.py)


def choose_extras(world, env_extras: list[str], now: float, mark: bool = True) -> tuple[list[str], set[str]]:
    """The extra videos (EXTRA_VIDEO_IDS, then the bot's own daily Shorts), and
    which of the older ones to skip reading this run. Order never changes, so
    status-comment slots stay put. Marks the ones read now (if `mark`: comments are read this run)."""
    extras = []
    for v in [*env_extras, *world.extra_videos]:
        if v and v not in extras and v not in world.retired:
            extras.append(v)
    skip = {v for v in extras[:-3] if now - world.extra_read.get(v, 0) < SLOW_EXTRA_S}
    for v in extras:
        if v not in skip and mark:
            world.extra_read[v] = now
    return extras, skip


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


def create_world(now: float, key: bytes, shape: tuple[int, int] | None = None, extra: dict | None = None,
                 seed: str | None = None, gen_seconds: int | None = None, hours: float | None = None,
                 launch: float | None = None) -> tuple[engine.World, dict]:
    """A new universe at generation 0. `shape` is (H, W), default 128 x 72;
    `extra` adds fields to the genesis line (e.g. which universe this is).
    Settings not given come from the environment (first universe only)."""
    seed = seed or os.environ.get("SEED", "acorn")
    if seed not in life.PATTERNS:
        raise SystemExit(f"SEED must be one of {sorted(life.PATTERNS)}")
    shape = tuple(shape or (life.H, life.W))
    gen_seconds = gen_seconds or int(os.environ.get("GEN_SECONDS", "120"))
    hours = hours or float(os.environ.get("FLATLINE_HOURS", "24"))
    launch = launch if launch is not None else float(os.environ.get("LAUNCH_TIME") or int(now))
    u = Universe(life.mask(life.centered(seed, shape), shape), flatline_gens(hours, gen_seconds))
    world = engine.World(universe=u, launch_time=launch, seed=seed, gen_seconds=gen_seconds, key=key,
                         effects_backfilled=True)
    genesis = {"type": "genesis", "launch_time": launch, "seed": seed, "shape": list(shape),
               "gen_seconds": gen_seconds, "flatline_gens": u.flatline_gens, "flatline_hours": hours,
               "seed_hash": u.hash.hex(), "rules": "B3/S23 torus", **(extra or {})}
    return world, genesis


def run(state_dir: str, now: float, yt_factory, key: bytes, thumb_path: str | None = THUMB_PATH,
        force_thumbnail: bool = False, dry_run: bool = True, render_obituary=None,
        force_pinned: bool = False, daily_mode: str | None = None, render_short=None) -> list[dict]:
    """Load (or create), tick once, save; then, if the universe was already dead
    when the run began, move the death sequence on (death.py), and make any
    retired videos private. `yt_factory(world)` builds the YouTube client."""
    store = Store(state_dir)
    if store.exists():
        world = store.load(key)
    else:
        world, genesis = create_world(now, key)
        store.create(world, genesis)
        print(f"created a new universe in {state_dir}: {genesis['seed']}, launch {genesis['launch_time']:.0f}")
    from store import LOG, read_log
    if not world.effects_backfilled:  # once: what the comments before this feature did
        effects = engine.effects_from_log(read_log(store.path(LOG)))
        world.effects_backfilled = True
        world.recent = None  # rebuilt below, with the effects
        store.save(world, effects)
    if "recent-in-hours-2026-11-09" not in world.migrations:  # once: rewrite old lines as "3d 5h"
        world.migrations.append("recent-in-hours-2026-11-09")
        world.recent = None
    if world.recent is None:  # first run with the pinned comment: backfill from the log
        lines = render.lines_from_log(read_log(store.path(LOG)), world.universe.flatline_gens, world.gen_seconds)
        world.recent = list(reversed(lines))[: engine.RECENT_MAX]
    if "total" not in world.comments_seen:  # once: count every comment read so far, from the log
        entries = read_log(store.path(LOG))
        day = engine.game_day(world.universe.generation, world.gen_seconds)
        ticks = [e for e in entries if e["type"] == "tick"]
        again = sum(e["count"] for e in entries if e["type"] == "readmitted")  # those were read twice
        world.comments_seen = {
            "total": sum(e.get("comments_new", 0) for e in ticks) - again, "day": day,
            "today": sum(e.get("comments_new", 0) for e in ticks if engine.game_day(e["gen_end"], world.gen_seconds) == day)}
    was_dead = world.universe.phase == DIED
    yt = yt_factory(world)
    logged = None
    if engine.READMIT not in world.migrations:  # see engine.readmit_dropped
        from collections import Counter
        logged = Counter(e["command"] for e in read_log(store.path(LOG)) if e["type"] == "injection")
    log_entries = read_log(store.path(LOG)) if engine.SCORES not in world.migrations else None  # engine.backfill_scores
    events = engine.tick(world, now, yt, thumb_path, force_thumbnail, pinned_link=env_flag("PINNED_LINK", "true"),
                         force_pinned=force_pinned, logged=logged, log_entries=log_entries)
    store.save(world, events)
    if was_dead:
        import death
        try:
            world, dev = death.advance(world, store, yt, now, dry_run, list(getattr(yt, "videos", [])), key,
                                       render=render_obituary)
        except Exception as e:  # noqa: BLE001 - recorded, alerted once, retried next run
            d = world.death or {}
            dev = [{"type": "death", "stage": d.get("stage"), "error": repr(e)[:500]}]
            if not d.get("alerted"):
                d["alerted"] = True
                dev[0]["alert"] = f"⚠️ Death sequence error at stage {d.get('stage')!r} (will retry)"
                dev[0]["detail"] = f"{e!r}"[:1500]
            world.death = d
            store.save(world, dev)
        events += dev
        if world.death is None:  # a new universe launched: continue on its files
            store = Store(state_dir)
            world = store.load(key)
    if world.retire_pending and not dry_run:
        import death
        rev = death.retire(world, yt, store)
        store.save(world, rev)
        events += rev
    mode = (daily_mode if daily_mode is not None else os.environ.get("DAILY_SHORT", "off")).strip().lower()
    if mode in ("dry", "live"):
        import daily
        try:
            events += daily.maybe_post(world, store, yt, now, mode, render_short=render_short)
        except Exception as e:  # noqa: BLE001 - no retry loop: tomorrow is another day
            world.daily = {"date": daily.today(now), "error": repr(e)[:300]}
            dev = [{"type": "daily_short", "error": repr(e)[:500],
                    "alert": "⚠️ Today's Short failed (no retry until tomorrow)", "detail": f"{e!r}"[:1500]}]
            store.save(world, dev)
            events += dev
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
        main = world.video_id or video_id  # universe #2 on lives on the obituary video
        names = world.video_names  # what the live page calls each video a move came from
        names.setdefault(main, "the main video")
        for v in extra_ids:
            names.setdefault(v, "the teaser Short")
        for i, v in enumerate(world.extra_videos):  # (the daily Shorts so far, one a day from Day 2)
            names.setdefault(v, f"the Day {i + 2} Short")
        due = now - world.comments_read_at >= COMMENTS_EVERY_S  # the chat is read every run
        if due:
            world.comments_read_at = now
        extras, skip = choose_extras(world, extra_ids, now, mark=due)
        return youtube.YouTube(service, main, quota["q"], env_flag("DESCRIPTION_GRID", "false"),
                               extra_video_ids=extras, reply_counts=world.reply_counts, key=key,
                               skip_video_ids=skip, chat=world.chat, comments_due=due)

    events = run(args.state_dir, now, make_yt, key, force_thumbnail=env_flag("FORCE_THUMBNAIL", "false"),
                 dry_run=dry_run, force_pinned=env_flag("FORCE_PINNED", "false"))
    for a in write_alerts(events, seen["world"], seen["world"].video_id or video_id):
        print(f"alert: {a['title']}")
    for e in events:
        if e["type"] in ("phase", "youtube_error", "display_skipped", "display"):
            print({k: v for k, v in e.items() if k not in ("cells",)})
    tick = next((e for e in events if e["type"] == "tick"), None)
    if tick:
        print(f"gen {tick['gen_start']:,} -> {tick['gen_end']:,} {tick['phase']} | new comments "
              f"{tick['comments_new']}, accepted {tick['accepted']}, rejected {tick['rejected']}, "
              f"queued {tick['queued']} | quota {quota['q'].used}/{quota['q'].budget} | dry_run={dry_run}")
    else:
        print(f"nothing to do | quota {quota['q'].used}/{quota['q'].budget}")
    for e in events:
        if e["type"] == "death":
            print({k: v for k, v in e.items() if k != "detail"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
