"""The daily Short: once a day, a recap of the last 24 hours, posted as a Short
whose comments feed the same universe.

Runs inside the scheduled tick, on the first run after DAILY_HOUR_ET (2 PM in
New York) each New York day; a late or failed run still posts it that day, up
to midnight. DAILY_SHORT (a repository variable) decides what it does:

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
from zoneinfo import ZoneInfo

import render
from universe import ALIVE, DIED

ET = ZoneInfo("America/New_York")
# 2 PM in New York, all year: a Short's views come in its first ~5 hours and
# stalled at midnight when it posted at 6 PM (23:00 UTC until 2026-11-12).
DAILY_HOUR_ET = 14
RECAP_HOURS = 24
OUT = os.path.join("out", "daily.mp4")
LIVE = "davidackerman.github.io/still-life"


def today(now: float) -> str:
    """The New York date (the day a Short is posted for)."""
    return dt.datetime.fromtimestamp(now, ET).date().isoformat()


def due(world_or_state, now: float) -> bool:
    """The first run at or after DAILY_HOUR_ET on a New York day that hasn't had
    one. Takes the World, or the state.json dict (as the workflow reads it)."""
    if isinstance(world_or_state, dict):
        phase, last = world_or_state["universe"]["phase"], (world_or_state.get("daily") or {}).get("date")
    else:
        phase, last = world_or_state.universe.phase, world_or_state.daily.get("date")
    return phase != DIED and dt.datetime.fromtimestamp(now, ET).hour >= DAILY_HOUR_ET and last != today(now)


def day_number(world) -> int:
    return int(world.universe.generation * world.gen_seconds // 86400) + 1


def marker(world, day: int) -> str:
    return f"still-life universe {world.universe_no} day {day}"


def stream_title(world, day: int) -> str:
    return f"This stream dies if nobody chats · Game of Life, Universe {world.universe_no}, Day {day}"


# Which kind of Short posts on a New York date; on every other day, the recap.
PLAN = {"2026-11-14": "flight"}  # Sat Nov 14 (Day 7): the glider Short (short.build_flight)


def snippet(world, day: int, count: int, mark: str, total: int | None = None, main: str | None = None,
            headline: str | None = None) -> dict:
    """The Short's title and description. The forecast in it is dated ("as of day 3"),
    so the description stays true after the forecast moves on."""
    u, G = world.universe, world.gen_seconds
    h, w = u.grid.shape
    f = world.forecast
    if u.phase == ALIVE and f and f.get("stuck_gen") is not None:
        fate = f"it's due to die {render._at(world.launch_time, f['stuck_gen'] + u.flatline_gens, G, '%a %b %d')} if nobody comments."
    elif u.phase == "FLATLINING":
        fate = f"it's stuck in a loop: it dies {render._at(world.launch_time, u.deadline_gen, G, '%a %I:%M %p ET')} unless a comment breaks it."
    elif u.phase == "CONTESTED":
        fate = f"a comment has broken its loop: it lives if it's still changing {render._at(world.launch_time, u.deadline_gen, G, '%a %I:%M %p ET')}."
    else:
        fate = "it won't get stuck for weeks unless a comment changes that."
    countdown = f" The main video's title counts down live: https://youtu.be/{main}" if main else ""
    so_far = f" ({total:,} since it began)" if total is not None and total > count else ""
    intro = f"{headline}: this Short follows it across the universe until it hits something.\n\n" if headline else ""
    desc = (f"{intro}Day {day} of universe {world.universe_no}, a live Conway's Game of Life: {count} comment{'s' * (count != 1)} in the last "
            f"24 hours{so_far}. As of day {day}, {fate}{countdown}\n\n"
            f"This Short is a replay: your comment lands in the live universe within minutes. Watch it happen on "
            f"the 24/7 live stream (type in its chat to play): {render.STREAM_URL}\n\n"
            f"Every comment adds cells (up to 3 moves per person every 2 minutes): glider x,y · block x,y · rpent x,y · acorn x,y · "
            f"or just say anything (x 0–{w - 1}, y 0–{h - 1}, from the top-left). If it gets stuck in a loop "
            f"and nobody saves it within 24 hours, this Short goes private for good.\n\n"
            f"Rewind every move: {LIVE}\nOpen source: github.com/davidackerman/still-life\n\n"
            f"#ConwaysGameOfLife #GameOfLife\n\n({mark})")
    return {"title": f"{headline or 'This Short dies if nobody comments'} · Universe {world.universe_no}, Day {day} #GameOfLife",
            "description": desc,
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
    # every comment read, counted or skipped: the same count as the status comments and the stream
    import engine
    entries = read_log(store.path(LOG))
    count = sum(e.get("comments_new", 0) for e in entries if e["type"] == "tick" and e["gen_end"] > since)
    seen = engine.comment_counts(world, world.universe.generation)
    total = seen[0] if seen else sum(1 for e in entries if e["type"] == "injection")
    headline = None
    if PLAN.get(today(now)) == "flight":
        trip = short.pick_flight(short.History(store.dir))
        if trip is not None:  # else no glider flew far enough: the usual recap
            headline = short.flight_title(trip)
    if headline:
        render_short(store.dir, out, since, kind="flight")
    else:
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
    vid = yt.find_upload(mark) or yt.upload_video(out, snippet(world, day, count, mark, total, getattr(yt, "video_id", None),
                                                               headline))
    if vid not in world.extra_videos:
        world.extra_videos.append(vid)
    world.video_names[vid] = f"the Day {day} Short"  # where its comments' moves came from, on the live page
    world.daily = {"date": today(now), "day": day, "video": vid}
    events = [{"type": "daily_short", "day": day, "video": vid}]
    store.save(world, events)  # recorded before anything else can fail: never posted twice
    try:
        yt.post_comment(vid, "This Short is alive. Latest changes to the universe will appear here in a few minutes.")
        tip = "Its status comment is posted; the bot keeps it up to date."
    except Exception as e:  # noqa: BLE001 - the Short still counts without it
        tip = f"Posting its status comment failed ({e!r}); the bot will pick up one you post by hand."
    if hasattr(yt, "set_stream_title"):  # the 24/7 stream's title counts the days too
        try:
            yt.set_stream_title(stream_title(world, day))
        except Exception as e:  # noqa: BLE001 - only a title
            tip += f" Retitling the live stream failed ({e!r})."
    events = [{"type": "daily_short", "day": day, "video": vid, "posted": True,
               "alert": f"📼 Day {day} Short is live: https://youtube.com/shorts/{vid}",
               "detail": f"Its comments now feed the universe. {tip}\n\nOptional, in Studio (the API can't): "
                         "set its Related video to the main video, pin its status comment, and pick its "
                         "thumbnail frame."}]
    store.save(world, events)
    return events
