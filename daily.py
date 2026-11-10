"""The daily Short: once a day, a recap of the last 24 hours, posted as a Short
whose comments feed the same universe.

Runs inside the scheduled tick, on the first run after DAILY_HOUR_UTC each
(UTC) day. DAILY_SHORT (a repository variable) decides what it does:

    off   nothing (default)
    dry   render it, attach it to the workflow run, open an issue; no upload
    live  also upload it (public), register it so its comments play, and post
    its status comment (the owner may pin it and set its Related video in
    Studio later: the API can't do either)

A marker in the description finds an upload again if a run dies half way, so
a day is never posted twice.
"""

from __future__ import annotations

import datetime as dt
import os

import render
from universe import ALIVE, DIED

DAILY_HOUR_UTC = 23  # 6 PM in New York (7 PM in summer)
RECAP_HOURS = 24
OUT = os.path.join("out", "daily.mp4")
LIVE = "davidackerman.github.io/still-life"


def today(now: float) -> str:
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()


def due(world, now: float) -> bool:
    """The first run at or after DAILY_HOUR_UTC on a day that hasn't had one."""
    t = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    return (world.universe.phase != DIED and t.hour >= DAILY_HOUR_UTC
            and world.daily.get("date") != today(now))


def day_number(world) -> int:
    return int(world.universe.generation * world.gen_seconds // 86400) + 1


def marker(world, day: int) -> str:
    return f"still-life universe {world.universe_no} day {day}"


def snippet(world, day: int, count: int, mark: str) -> dict:
    u, G = world.universe, world.gen_seconds
    h, w = u.grid.shape
    f = world.forecast
    if u.phase == ALIVE and f and f.get("stuck_gen") is not None:
        fate = f"It's due to die {render._at(world.launch_time, f['stuck_gen'] + u.flatline_gens, G, '%a %b %d')} if nobody comments."
    elif u.phase == "FLATLINING":
        fate = f"It's stuck in a loop: it dies {render._at(world.launch_time, u.deadline_gen, G, '%a %I:%M %p ET')} unless a comment breaks it."
    elif u.phase == "CONTESTED":
        fate = f"A comment broke its loop: it lives if it's still changing {render._at(world.launch_time, u.deadline_gen, G, '%a %I:%M %p ET')}."
    else:
        fate = "It won't get stuck for weeks unless a comment changes that."
    desc = (f"Day {day} of a live Conway's Game of Life universe: {count} comment{'s' * (count != 1)} in the last "
            f"24 hours. {fate}\n\n"
            f"This Short is a replay: your comment lands in the live universe within minutes. Watch it on the "
            f"main video's thumbnail, or live at {LIVE}\n\n"
            f"Every comment adds cells, as many as you like: glider x,y · block x,y · rpent x,y · acorn x,y · "
            f"or just say anything (x 0–{w - 1}, y 0–{h - 1}, from the top-left). If it gets stuck in a loop "
            f"and nobody saves it within 24 hours, this Short goes private for good.\n\n"
            f"Rewind every move: {LIVE}\nOpen source: github.com/davidackerman/still-life\n\n"
            f"#ConwaysGameOfLife #GameOfLife\n\n({mark})")
    return {"title": f"Day {day}: This Short dies if nobody comments #GameOfLife", "description": desc,
            "tags": ["game of life", "conway's game of life", "cellular automata", "glider", "simulation",
                     "interactive", "live", "emergence"],
            "categoryId": "28", "defaultLanguage": "en"}


def maybe_post(world, store, yt, now: float, mode: str, render_short=None, out: str | None = None) -> list[dict]:
    """Post today's Short if it's due. Returns events (already saved)."""
    if mode not in ("dry", "live") or not due(world, now):
        return []
    import short
    out = out or OUT
    render_short = render_short or short.render_video
    day = day_number(world)
    since = max(0, world.universe.generation - RECAP_HOURS * 3600 // world.gen_seconds)
    from store import LOG, read_log
    count = sum(1 for e in read_log(store.path(LOG)) if e["type"] == "injection" and e["gen"] > since)
    render_short(store.dir, out, since)
    mark = marker(world, day)
    if mode == "dry":
        world.daily = {"date": today(now), "day": day, "video": None, "dry": True}
        events = [{"type": "daily_short", "day": day, "dry_run": True,
                   "alert": f"📼 Day {day} Short rendered (dry run, not uploaded)",
                   "detail": "Watch it in this run's artifacts (Actions → the tick run → Artifacts → daily-short). "
                             "To post one every day, set the repository variable DAILY_SHORT to live."}]
        store.save(world, events)
        return events
    vid = yt.find_upload(mark) or yt.upload_video(out, snippet(world, day, count, mark))
    if vid not in world.extra_videos:
        world.extra_videos.append(vid)
    world.daily = {"date": today(now), "day": day, "video": vid}
    events = [{"type": "daily_short", "day": day, "video": vid}]
    store.save(world, events)  # recorded before anything else can fail: never posted twice
    try:
        yt.post_comment(vid, "This Short is alive. Latest changes to the universe will appear here in a few minutes.")
        tip = "Its status comment is posted; the bot keeps it up to date."
    except Exception as e:  # noqa: BLE001 - the Short still counts without it
        tip = f"Posting its status comment failed ({e!r}); the bot will pick up one you post by hand."
    events = [{"type": "daily_short", "day": day, "video": vid, "posted": True,
               "alert": f"📼 Day {day} Short is live: https://youtube.com/shorts/{vid}",
               "detail": f"Its comments now feed the universe. {tip}\n\nOptional, in Studio (the API can't): "
                         "set its Related video to the main video, pin its status comment, and pick its "
                         "thumbnail frame."}]
    store.save(world, events)
    return events
