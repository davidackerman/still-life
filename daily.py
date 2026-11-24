"""The daily Short: once a day, a recap (or a planned kind of Short), posted as a Short
whose comments feed the same universe.

Runs inside the scheduled tick, on the first run after each of SLOTS_ET (now
only 8 PM in New York); a late run still posts that slot's Short, until the next
slot or midnight, and a failed one waits for the next slot. DAILY_SHORT (a repository variable) decides what it does:

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
# One Short a day, at 8 PM in New York, from 2026-11-19. Each Short is shown to about the
# same number of people (unique reach 1,017-1,118 for the first six, whether 40% or 97%
# of it was watched), so a second a day was a second audience, but the Blender-made
# Shorts (posted by hand into the slot, the best yet) take most of a day to make. With
# more than one slot, the later ones are the evening Shorts. (One a day at 6 PM until
# 2026-11-12, then 2 PM until 2026-11-15, then noon and 8 PM until 2026-11-18.)
SLOTS_ET = (20,)
# These slots' Shorts go out to subscribers (their subscriptions feed and a notification).
# Noon was left out on 2026-11-16 and got 1 view in its first 36 minutes, none from the
# Shorts feed (earlier Shorts: 9-88 in 30), so both do: the API's notifySubscribers=False
# seems to keep a Short out of the subscriptions feed too, the early viewers every Short had.
NOTIFY_SLOTS = SLOTS_ET
# A date's extra slots (New York), as well as SLOTS_ET; they notify subscribers too. Sun Nov 22
# (Day 15): the countdown at noon, then the week's summary (the rewind) at 8 PM, as on Sun Nov 15
# (the day's two Shorts: 23 and 22 comments; the 3D tree Shorts at 8 PM on Nov 20-21: 5 and 2).
# Tue Nov 24 (Day 17): a noon one as well, the 9-second Blender creature countdown (posted by hand into the slot).
EXTRA_SLOTS = {"2026-11-22": (12,), "2026-11-24": (12,)}
RECAP_HOURS = 24  # the first slot's recap; a later slot's covers the hours since the slot before it
OUT = os.path.join("out", "daily.mp4")
LIVE = "davidackerman.github.io/still-life"


def today(now: float) -> str:
    """The New York date (the day a Short is posted for)."""
    return dt.datetime.fromtimestamp(now, ET).date().isoformat()


def slots(now: float) -> tuple[int, ...]:
    """The slots on `now`'s New York date: SLOTS_ET and that date's EXTRA_SLOTS."""
    return tuple(sorted(set(SLOTS_ET) | set(EXTRA_SLOTS.get(today(now), ()))))


def slot(now: float) -> int | None:
    """The slot (its New York hour) that `now` falls in: the latest one begun today."""
    hour = dt.datetime.fromtimestamp(now, ET).hour
    begun = [s for s in slots(now) if s <= hour]
    return begun[-1] if begun else None


def due(world_or_state, now: float) -> bool:
    """The first run in a slot that hasn't had its Short (posted, or tried and
    failed). Takes the World, or the state.json dict (as the workflow reads it).
    A record from before the slots (no "slot") counts as the first slot."""
    if isinstance(world_or_state, dict):
        phase, last = world_or_state["universe"]["phase"], world_or_state.get("daily") or {}
    else:
        phase, last = world_or_state.universe.phase, world_or_state.daily or {}
    s = slot(now)
    if phase == DIED or s is None:
        return False
    return (last.get("date") or "", last.get("slot", SLOTS_ET[0])) < (today(now), s)


def day_number(world) -> int:
    return int(world.universe.generation * world.gen_seconds // 86400) + 1


def marker(world, day: int, at: int | None = None) -> str:
    """Found in the description again if a run dies half way. One per slot: neither
    slot's marker is part of the other's."""
    return f"still-life universe {world.universe_no} day {day}" + (f" slot {at}" if at is not None else "")


def stream_title(world, day: int) -> str:
    return f"This stream dies if nobody chats · Game of Life, Universe {world.universe_no}, Day {day}"


# Each slot's kind of Short, unless PLAN says otherwise: noon, the stranger (what one real
# comment did, for people who've never seen this universe); 8 PM, the countdown (how
# long it has, and the live chat). Either falls back to the recap when it has nothing
# true to say (nothing moved its fate much; no loop in sight). From 2026-11-16.
DEFAULT_KINDS = {12: "stranger", 20: "countdown"}
# Which kind of Short posts on a New York date: a kind (the first slot's) or {slot: kind};
# every other slot, its DEFAULT_KINDS kind.
PLAN = {"2026-11-14": "flight",  # Sat Nov 14 (Day 7): the glider Short (short.build_flight)
        "2026-11-15": {20: "rewind"},  # Sun Nov 15 (Day 8), 8 PM: the week backward (short.build_rewind)
        "2026-11-17": {12: "made"},  # Tue Nov 17 (Day 10), noon: "1,000 comments built this" (short.build_made)
        "2026-11-22": {12: "countdown", 20: "rewind"},  # Sun Nov 22 (Day 15): the countdown, then the two weeks backward
        "2026-11-24": {12: "countdown"}}  # Tue Nov 24 (Day 17), noon: the countdown (the creature donut, by hand)


def kind(now: float) -> str:
    plan, s = PLAN.get(today(now)), slot(now)
    if isinstance(plan, dict) and s in plan:
        return plan[s]
    if isinstance(plan, str) and s == slots(now)[0]:
        return plan
    return DEFAULT_KINDS.get(s, "recap")


def recap_hours(at: int | None, now: float | None = None) -> int:
    ss = SLOTS_ET if now is None else slots(now)
    k = ss.index(at) if at in ss else 0
    return RECAP_HOURS if k == 0 else ss[k] - ss[k - 1]


INTROS = {"stranger": "every comment on this Short adds cells to a live Game of Life universe, and some change "
                      "how long it lives.",
          "countdown": "if nobody comments, it gets stuck in a loop, and 24 hours later it dies. Every comment "
                       "adds cells and can change that.",
          "flight": "this Short follows it across the universe until it hits something.",
          "made": "it started as 7 cells, and every comment since has added more.",
          "rewind": "Conway's Game of Life is a zero-player game: it plays itself out from wherever it starts. "
                    "This one has players: every comment adds cells. This Short rewinds all of it to the 7 cells "
                    "it started as, then shows the universe it would have been with zero players."}


def satisfying(kind: str) -> bool:
    """A Short drawn in 3D (lit blocks rising and sinking): tagged #satisfying, for the much
    bigger satisfying-simulation audience, not only people who know the Game of Life."""
    import short
    return {"countdown": short.COUNTDOWN_LOOK, "made": short.MADE_LOOK, "rewind": short.REWIND_LOOK}.get(kind) == "3d"


def snippet(world, day: int, count: int, mark: str, total: int | None = None, main: str | None = None,
            headline: str | None = None, kind: str = "flight", hours: int = RECAP_HOURS, evening: bool = False) -> dict:
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
    sep = " " if headline and headline.endswith("?") else ": "  # "Can a zero-player game have players? Conway's ..."
    intro = f"{headline}{sep}{INTROS.get(kind, INTROS['flight'])}\n\n" if headline else ""
    desc = (f"{intro}Day {day} of universe {world.universe_no}, a live Conway's Game of Life: {count} comment{'s' * (count != 1)} in the last "
            f"{hours} hours{so_far}. As of day {day}, {fate}{countdown}\n\n"
            f"This Short is a replay: your comment lands in the live universe within minutes. Watch it happen on "
            f"the 24/7 live stream (type in its chat to play): {render.STREAM_URL}\n\n"
            f"Every comment adds cells (up to 3 moves per person every 2 minutes): glider x,y · block x,y · rpent x,y · acorn x,y · "
            f"or just say anything (x 0–{w - 1}, y 0–{h - 1}, from the top-left). Add black to a pattern to erase "
            f"instead: glider x,y black. If it gets stuck in a loop "
            f"and nobody saves it within 24 hours, this Short goes private for good.\n\n"
            f"Rewind every move: {LIVE}\nOpen source: github.com/davidackerman/still-life\n\n"
            f"{'#satisfying #simulation ' if satisfying(kind) else ''}#ConwaysGameOfLife #GameOfLife\n\n({mark})")
    when = f"Day {day}, evening" if evening else f"Day {day}"
    title = f"{headline or 'This Short dies if nobody comments'} · Universe {world.universe_no}, {when} #GameOfLife"
    if headline and kind in ("stranger", "countdown", "made", "rewind"):  # for strangers: what happened, not the channel's day count
        title = f"{headline} {'#satisfying ' if satisfying(kind) else ''}#GameOfLife"
    return {"title": title,
            "description": desc,
            "tags": ["game of life", "conway's game of life", "cellular automata", "glider", "simulation",
                     "interactive", "live", "emergence"] + (["satisfying", "satisfying simulation", "3d"] if satisfying(kind) else []),
            "categoryId": "28", "defaultLanguage": "en"}


def maybe_post(world, store, yt, now: float, mode: str, render_short=None, out: str | None = None) -> list[dict]:
    """Post today's Short if it's due. Returns events (already saved)."""
    if mode not in ("dry", "live") or not due(world, now):
        return []
    import short
    out = out or OUT
    render_short = render_short or short.render_video
    day = day_number(world)
    at = slot(now)
    evening = at != slots(now)[0]
    hours = recap_hours(at, now)
    since = max(0, world.universe.generation - hours * 3600 // world.gen_seconds)
    from store import LOG, read_log
    # every comment read, counted or skipped: the same count as the status comments and the stream
    import engine
    entries = read_log(store.path(LOG))
    count = sum(e.get("comments_new", 0) for e in entries if e["type"] == "tick" and e["gen_end"] > since)
    seen = engine.comment_counts(world, world.universe.generation)
    total = seen[0] if seen else sum(1 for e in entries if e["type"] == "injection")
    headline, what = None, kind(now)
    if what == "flight":
        trip = short.pick_flight(short.History(store.dir))
        if trip is not None:  # else no glider flew far enough: the usual recap
            headline = short.flight_title(trip)
    elif what == "rewind":
        headline = short.REWIND_TITLE
    elif what == "stranger":  # the period's biggest real save or kill; none moved it much: the recap
        move = short.opening_move(short.History(store.dir), since)
        headline = short.stranger_title(move, world.gen_seconds) if move else None
    elif what == "made":  # "1,000 comments built this simulation": always true (the count is rounded down)
        headline = short.made_title(short.History(store.dir))
    elif what == "countdown":  # how long it has; no loop in sight: the recap (no urgency to claim)
        hook = short.countdown(short.History(store.dir))
        headline = hook[1] if hook else None
    if headline:
        render_short(store.dir, out, since, kind=what)
    else:
        what = "recap"
        render_short(store.dir, out, since)
    mark = marker(world, day, at)
    name = f"Day {day} evening" if evening else f"Day {day}"
    record = {"date": today(now), "slot": at, "day": day}
    if mode == "dry":
        world.daily = {**record, "video": None, "dry": True}
        events = [{"type": "daily_short", "day": day, "slot": at, "dry_run": True,
                   "alert": f"📼 {name} Short rendered (dry run, not uploaded)",
                   "detail": "Watch it in this run's artifacts (Actions → the tick run → Artifacts → daily-short). "
                             "To post them, set the repository variable DAILY_SHORT to live."}]
        store.save(world, events)
        return events
    vid = yt.find_upload(mark) or yt.upload_video(out, snippet(world, day, count, mark, total, getattr(yt, "video_id", None),
                                                               headline, what, hours, evening),
                                                       notify=at in NOTIFY_SLOTS + EXTRA_SLOTS.get(today(now), ()))
    if vid not in world.extra_videos:
        world.extra_videos.append(vid)
    world.video_names[vid] = f"the {name} Short"  # where its comments' moves came from, on the live page
    world.daily = {**record, "video": vid}
    events = [{"type": "daily_short", "day": day, "slot": at, "kind": what, "video": vid}]
    store.save(world, events)  # recorded before anything else can fail: never posted twice
    try:
        yt.post_comment(vid, "This Short is alive. Latest changes to the universe will appear here in a few minutes.")
        tip = "Its status comment is posted; the bot keeps it up to date."
    except Exception as e:  # noqa: BLE001 - the Short still counts without it
        tip = f"Posting its status comment failed ({e!r}); the bot will pick up one you post by hand."
    if not evening and hasattr(yt, "set_stream_title"):  # the 24/7 stream's title counts the days too
        try:
            yt.set_stream_title(stream_title(world, day))
        except Exception as e:  # noqa: BLE001 - only a title
            tip += f" Retitling the live stream failed ({e!r})."
    events = [{"type": "daily_short", "day": day, "slot": at, "kind": what, "video": vid, "posted": True,
               "alert": f"📼 {name} Short is live: https://youtube.com/shorts/{vid}",
               "detail": f"Its comments now feed the universe. {tip}\n\nOptional, in Studio (the API can't): "
                         "set its Related video to the live stream (more of its viewers play there than "
                         "anywhere else), pin its status comment, and pick its thumbnail frame."}]
    store.save(world, events)
    return events
