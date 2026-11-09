"""One run of the universe: comments in, simulation forward, display out.

Shared by simulate.py (fake YouTube, fake clock) and tick.py (real YouTube,
state on disk). Pure apart from the `yt` object it is handed.

`yt` must provide:
    fetch_comments(since: float, watch: list[str], now: float) -> list[admission.Comment]
        comments and replies published after `since` on every configured video,
        plus the threads of the comment IDs in `watch` (the status comments)
    set_title(title: str, grid_text: str) -> None   grid_text is used only if the
                                                     client has the description grid on
    set_thumbnail(path: str | None) -> None
Display calls may raise. A failure is logged, backed off, and retried on a later
run; it never touches the simulation, which has already advanced by then. An
exception with `.skip = True` (e.g. over the quota budget) is a skip, not a
failure: no backoff, retried next run.

Logged events hold game data only (see admission.py for the data policy).
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass, field

import admission
import commands
import render
from universe import ALIVE, DIED, GEN_SECONDS, Universe, target_generation

TITLE_MIN_INTERVAL_S = 15 * 60
PINNED_MIN_INTERVAL_S = 30 * 60  # the main video's status comment
PINNED_EXTRA_MIN_INTERVAL_S = 60 * 60  # status comments on extra videos (quota)
# A changed countdown headline refreshes the thumbnail, but YouTube rate-limits
# thumbnail uploads (HTTP 429 uploadRateLimitExceeded after a handful in a day),
# so no more often than the regular 3-hour refresh. Phase changes stay immediate.
THUMB_HEADLINE_MIN_S = 4 * 3600
RECENT_MAX = 8
BURST_LINES = 3  # more comments than this in one run share a single "Latest moves" line
FORECAST_GENS = 30000  # how far ahead the title's "if nobody comments" forecast looks (~42 days)
THUMB_INTERVAL_S = 4 * 3600  # YouTube 429s after ~9 thumbnail uploads/day per channel
BACKOFF_BASE_S = 15 * 60
BACKOFF_MAX_S = 24 * 3600


@dataclass
class World:
    universe: Universe
    launch_time: float
    seed: str = "acorn"
    gen_seconds: int = GEN_SECONDS
    key: bytes = b""  # secret for keyed hashes; never persisted
    admission: admission.Admission = field(default_factory=admission.Admission)
    runs: int = 0  # in-memory counter for simulate.py; not persisted
    last_title: str | None = None
    last_title_at: float | None = None
    last_thumb_at: float | None = None
    last_thumb_phase: str | None = None
    highlight: set = field(default_factory=set)  # cells injected since the last thumbnail
    forecast: dict | None = None  # display only: {"key", "stuck_gen", "horizon"}
    recent: list | None = None  # latest event lines for the pinned comment (game data only)
    # The owner's own status comment on each video, by video index ("0" = main):
    # {"id": comment id, "text": last text set, "at": when}
    pinned: dict = field(default_factory=dict)
    pinned_core: str | None = None  # the comment without the suggestion; a change re-picks the suggestion
    pinned_suggest: str | None = None
    contest_by: dict | None = None  # the comment that broke the current loop: {"gen", "command"}
    saves: int = 0  # times comments saved it from a countdown (shown in the title)
    effects_backfilled: bool = False  # past comments' effects on its lifespan have been logged
    migrations: list = field(default_factory=list)  # one-off fixes already applied, by name
    last_thumb_headline: str | None = None
    backoff: dict = field(default_factory=dict)  # op -> {"until": t, "failures": n}
    last_changer: dict | None = None  # keyed ids of the last board-changing comment
    quota: dict = field(default_factory=dict)  # owned by youtube.Quota
    reply_counts: dict = field(default_factory=dict)  # owned by youtube.YouTube (keyed thread -> [n, t])
    # Succession (death.py): which universe this is, the video hosting it (None =
    # the VIDEO_ID secret, universe #1), videos of dead universes never to be
    # driven again, the death sequence's progress, and videos still to make private.
    universe_no: int = 1
    video_id: str | None = None
    retired: list = field(default_factory=list)
    death: dict | None = None
    retire_pending: list = field(default_factory=list)

    @property
    def pinned_id(self) -> str | None:  # the main video's status comment
        return (self.pinned.get("0") or {}).get("id")

    def to_state(self) -> tuple[dict, bytes]:
        u, history = self.universe.to_state()
        return ({
            "config": {"launch_time": self.launch_time, "seed": self.seed, "gen_seconds": self.gen_seconds,
                       "universe": self.universe_no, "video_id": self.video_id, "retired": self.retired},
            "universe": u,
            "admission": self.admission.to_dict(),
            "display": {"last_title": self.last_title, "last_title_at": self.last_title_at,
                        "last_thumb_at": self.last_thumb_at, "last_thumb_phase": self.last_thumb_phase,
                        "highlight": sorted(list(c) for c in self.highlight), "backoff": self.backoff,
                        "forecast": self.forecast, "recent": self.recent, "pinned": self.pinned,
                        "pinned_core": self.pinned_core, "pinned_suggest": self.pinned_suggest,
                        "contest_by": self.contest_by, "last_thumb_headline": self.last_thumb_headline,
                        "saves": self.saves, "effects_backfilled": self.effects_backfilled,
                        "migrations": self.migrations},
            "last_changer": self.last_changer,
            "quota": self.quota,
            "reply_counts": dict(sorted(self.reply_counts.items())),
            "death": self.death,
            "retire_pending": self.retire_pending,
        }, history)

    @classmethod
    def from_state(cls, d: dict, history: bytes, key: bytes) -> "World":
        disp = d["display"]
        pinned = disp.get("pinned")
        if pinned is None:  # state written before status comments on several videos
            pinned = ({"0": {"id": disp["pinned_id"], "text": disp.get("pinned_text"),
                             "at": disp.get("pinned_at")}} if disp.get("pinned_id") else {})
        return cls(
            universe=Universe.from_state(d["universe"], history),
            launch_time=d["config"]["launch_time"], seed=d["config"]["seed"],
            gen_seconds=d["config"]["gen_seconds"], key=key,
            admission=admission.Admission.from_dict(d["admission"]),
            last_title=disp["last_title"], last_title_at=disp["last_title_at"],
            last_thumb_at=disp["last_thumb_at"], last_thumb_phase=disp["last_thumb_phase"],
            highlight={tuple(c) for c in disp["highlight"]}, backoff=dict(disp["backoff"]),
            last_changer=d.get("last_changer"), quota=dict(d.get("quota", {})),
            forecast=disp.get("forecast"), recent=disp.get("recent"), pinned=pinned,
            reply_counts=dict(d.get("reply_counts", {})),
            universe_no=d["config"].get("universe", 1), video_id=d["config"].get("video_id"),
            retired=list(d["config"].get("retired", [])), death=d.get("death"),
            retire_pending=list(d.get("retire_pending", [])),
            pinned_core=disp.get("pinned_core"), pinned_suggest=disp.get("pinned_suggest"),
            contest_by=disp.get("contest_by"), last_thumb_headline=disp.get("last_thumb_headline"),
            saves=disp.get("saves", 0), effects_backfilled=disp.get("effects_backfilled", False),
            migrations=list(disp.get("migrations", [])),
        )


def refresh_forecast(world: World) -> dict | None:
    """When the universe gets stuck if nobody comments. Without comments the
    future is fixed, so this is recomputed only after an injection changes the
    board, a phase change, or when an empty forecast's horizon runs low."""
    u = world.universe
    if u.phase != ALIVE:
        world.forecast = None
        return None
    key = [u.history_base, u.phase]
    f = world.forecast
    stale = (f is None or f["key"] != key
             or (f["stuck_gen"] is None and u.generation > f["horizon"] - FORECAST_GENS // 2))
    if stale:
        world.forecast = {"key": key, "stuck_gen": u.forecast_stuck(FORECAST_GENS),
                          "horizon": u.generation + FORECAST_GENS}
    return world.forecast


def comment_effects(u: Universe, items: list[dict], before: int | None | bool = False) -> list[dict | None]:
    """What each injection in `items` does to the universe's lifespan, if nobody
    else comments: an "effect" event (when it would get stuck, before -> after)
    per board-changing injection, None otherwise. Only while ALIVE: during a
    countdown, the phase events say what a comment did. Works on a copy.
    `before` is the current forecast if already known (False: compute it)."""
    if u.phase != ALIVE:
        return [None] * len(items)
    if before is False:
        before = u.forecast_stuck(FORECAST_GENS)
    probe, out = copy.deepcopy(u), []
    for it in items:
        ev = probe.inject([it])[0]
        if not ev["changed"] or probe.phase != ALIVE:
            out.append(None)
            continue
        after = probe.forecast_stuck(FORECAST_GENS)
        out.append({"type": "effect", "gen": ev["gen"], "command": it["command"], "before": before,
                    "after": after, "horizon": FORECAST_GENS})
        before = after
    return out


def effects_from_log(entries: list[dict]) -> list[dict]:
    """comment_effects for every comment already in a log (run once, for comments
    that landed before effects were tracked)."""
    import life
    g = entries[0]
    shape = tuple(g["shape"])
    u = Universe(life.mask(life.centered(g["seed"], shape), shape), g["flatline_gens"])
    out, segment = [], []
    for e in entries[1:]:
        if e["type"] != "tick":
            segment.append(e)
            continue
        inj = [{"command": x["command"], "cells": x["cells"], "run_at": x["run_at"]}
               for x in segment if x["type"] == "injection"]
        if inj:
            out += [x for x in comment_effects(u, inj) if x]
            u.inject(inj)
        u.advance_to(e["gen_end"])
        segment = []
    return out


READMIT = "readmit-dropped-2026-11-09"


def readmit_dropped(world: World, comments: list, logged: Counter, shape) -> int:
    """Once: comments dropped under the old one-per-hour rule count after all.
    A comment already handled ("seen") whose move was never applied (not in the
    log, not waiting in the queue) is forgotten, so admit() takes it as new.
    Within one command, the earliest comments are the ones that were applied.
    Random patches are unique to their comment, so those match exactly."""
    left = Counter(logged)
    for q in world.admission.queue:
        left[q["command"]] += 1
    forget = []
    for c in sorted(comments, key=lambda c: (c.epoch, c.id)):
        kid = admission.keyed(world.key, "comment", c.id)
        if kid not in world.admission.seen:
            continue
        desc = commands.parse(c.id, c.text, shape).desc
        if left[desc] > 0:
            left[desc] -= 1
        else:
            forget.append(kid)
    for kid in forget:
        del world.admission.seen[kid]
    return len(forget)


def _display(world: World, op: str, now: float, call, events: list[dict]) -> bool:
    """Run one display call with backoff. Returns True on success."""
    b = world.backoff.get(op)
    if b and now < b["until"]:
        return False
    try:
        call()
    except Exception as e:  # noqa: BLE001 - display errors must not stop the run
        if getattr(e, "skip", False):
            events.append({"type": "display_skipped", "op": op, "reason": str(e)})
            return False
        failures = (b["failures"] if b else 0) + 1
        until = now + min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (failures - 1))
        world.backoff[op] = {"until": until, "failures": failures}
        events.append({"type": "youtube_error", "op": op, "error": repr(e)[:500], "retry_after": until})
        return False
    world.backoff.pop(op, None)
    return True


def tick(world: World, now: float, yt, thumb_path: str | None = None, force_thumbnail: bool = False,
         pinned_link: bool = True, force_pinned: bool = False, logged: Counter | None = None) -> list[dict]:
    """Advance the world to wall-clock `now`. Returns the run's log events.

    Order: fetch + admit comments -> apply up to MAX_PER_RUN injections at the
    current generation -> step to the target generation -> update display.
    A final "tick" event is added only if something happened, so running
    twice at the same moment logs nothing the second time.
    """
    u = world.universe
    world.runs += 1
    run_at = round(now, 3)
    gen_start = u.generation
    events: list[dict] = []

    # 1. comments (input). A fetch failure skips input for this run only.
    fetched = True
    try:
        comments = yt.fetch_comments(now - admission.FETCH_WINDOW_S,
                                     watch=[p["id"] for p in world.pinned.values()], now=now)
    except Exception as e:  # noqa: BLE001
        comments, fetched = [], False
        events.append({"type": "youtube_error", "op": "fetch_comments", "error": repr(e)[:500]})
    # The owner's status comment on each video is not a move in the game: note
    # its ID, skip it. Replies never count as status comments.
    known = {p["id"] for p in world.pinned.values()}

    def is_status(c):
        return c.id in known or (c.by_owner and not c.reply and any(
            m.lower() in c.text.lower() for m in render.PINNED_MARKERS))
    status = [c for c in comments if is_status(c)]
    for v in sorted({c.video for c in status}):
        mine = [c for c in status if c.video == v]
        slot = world.pinned.get(str(v))
        if not slot or slot["id"] not in {c.id for c in mine}:
            world.pinned[str(v)] = {"id": max(mine, key=lambda c: (c.epoch, c.id)).id, "text": None, "at": None}
    comments = [c for c in comments if not is_status(c)]
    if fetched and logged is not None and READMIT not in world.migrations and u.phase != DIED:
        n = readmit_dropped(world, comments, logged, u.grid.shape)
        world.migrations.append(READMIT)
        if n:
            events.append({"type": "readmitted", "count": n, "reason": "every comment counts now"})
    counts = admission.admit(world.admission, comments, now, world.key, dead=(u.phase == DIED),
                             shape=u.grid.shape)
    sim_from = len(events)

    # 2. simulation (authoritative)
    if u.phase == DIED:
        dropped = admission.take_batch(world.admission, limit=len(world.admission.queue))
        if dropped:
            counts["rejected"]["universe_dead"] = counts["rejected"].get("universe_dead", 0) + len(dropped)
    else:
        target = target_generation(now, world.launch_time, world.gen_seconds)
        batch = admission.take_batch(world.admission)
        # Each comment lands at the generation it was posted in (never before
        # where the last run stopped, never after now), in posting order.
        start = u.generation
        landing = sorted(((min(target, max(start, target_generation(q["published_at"], world.launch_time,
                                                                     world.gen_seconds))), n, q)
                          for n, q in enumerate(batch)), key=lambda t: (t[0], t[1]))
        i = 0
        while i < len(landing):
            gen = landing[i][0]
            group = [q for g, _, q in landing[i:] if g == gen]
            i += len(group)
            events += u.advance_to(gen)
            if u.phase == DIED:  # it died before these were posted
                counts["rejected"]["universe_dead"] = counts["rejected"].get("universe_dead", 0) + len(group)
                continue
            items = [{"command": q["command"], "cells": q["cells"], "run_at": run_at} for q in group]
            f = world.forecast
            known = f["stuck_gen"] if f and f["key"] == [u.history_base, u.phase] else False
            effects = comment_effects(u, items, known)
            inj_events = u.inject(items)
            for q, ev in zip(group, [e for e in inj_events if e["type"] == "injection"]):
                if ev["changed"]:
                    world.last_changer = {"comment": q["comment"], "author": q["author"],
                                          "gen": ev["gen"], "command": q["command"]}
            k = 0
            for ev in inj_events:  # each comment's effect follows it in the log
                events.append(ev)
                if ev["type"] == "injection":
                    if effects[k]:
                        events.append(effects[k])
                    k += 1
            done = [e for e in effects if e]
            if done and u.phase == ALIVE:  # the last effect's forecast is the new forecast
                world.forecast = {"key": [u.history_base, u.phase], "stuck_gen": done[-1]["after"],
                                  "horizon": u.generation + FORECAST_GENS}
            world.highlight.update(tuple(c) for q in group for c in q["cells"])
        events += u.advance_to(target)

    if world.recent is None:
        world.recent = []
    last_cmd = None
    sim = events[sim_from:]
    injs = [e for e in sim if e["type"] == "injection"]
    effs = [e for e in sim if e["type"] == "effect"]
    burst = len(injs) > BURST_LINES  # many comments in one run: one summary line, not a wall of them
    for i, ev in enumerate(sim):
        if ev["type"] == "injection" and ev["changed"]:
            last_cmd = (ev["gen"], ev["command"])
        if ev["type"] == "phase" and ev["reason"] == "injection" and last_cmd:
            world.contest_by = {"gen": last_cmd[0], "command": last_cmd[1]}
        if ev["type"] == "phase" and ev["reason"] == "survived_deadline":
            world.saves += 1
        if ev["type"] == "phase" and ev["reason"] == "survived_deadline" and world.contest_by:
            line = (f"🫀 gen {ev['gen']:,}: SAVED, thanks to {world.contest_by['command']} "
                    f"(gen {world.contest_by['gen']:,})")
        elif burst and ev["type"] == "injection":
            line = None
            if ev is injs[0]:
                line = f"gen {ev['gen']:,}: {len(injs)} comments"
                if effs:
                    line += render.effect_suffix({**effs[0], "after": effs[-1]["after"]}, u.flatline_gens,
                                                 world.gen_seconds)
        else:
            line = render.event_line(ev)
        if line and not burst and ev["type"] == "injection" and i + 1 < len(sim) and sim[i + 1]["type"] == "effect":
            line += render.effect_suffix(sim[i + 1], u.flatline_gens, world.gen_seconds)
        if ev["type"] == "phase" and ev["to"] in (ALIVE, DIED):
            world.contest_by = None
        if line:
            world.recent = ([line] + world.recent)[:RECENT_MAX]

    # 3. display (best effort)
    f = refresh_forecast(world)
    title = render.title(u, world.gen_seconds, f and f["stuck_gen"], f and FORECAST_GENS, world.saves)
    title_due = world.last_title_at is None or now - world.last_title_at >= TITLE_MIN_INTERVAL_S
    if title != world.last_title and title_due:
        if _display(world, "set_title", now, lambda: yt.set_title(title, render.braille(u.grid)), events):
            world.last_title, world.last_title_at = title, now
            events.append({"type": "display", "what": "title", "title": title})

    # Refresh every 4 h and immediately on a phase change; a dead universe never
    # changes again, so after the DIED thumbnail there is nothing to refresh.
    periodic = world.last_thumb_at is None or now - world.last_thumb_at >= THUMB_INTERVAL_S
    big, _ = render.headline(u, world.gen_seconds, f and f["stuck_gen"], f and FORECAST_GENS)
    moved = (big != world.last_thumb_headline and world.last_thumb_at is not None
             and now - world.last_thumb_at >= THUMB_HEADLINE_MIN_S)
    if force_thumbnail or u.phase != world.last_thumb_phase or (periodic and u.phase != DIED) or moved:
        def upload():
            if thumb_path:
                render.thumbnail(u, thumb_path, world.highlight, world.gen_seconds,
                                 f and f["stuck_gen"], f and FORECAST_GENS)
            yt.set_thumbnail(thumb_path)
        if _display(world, "set_thumbnail", now, upload, events):
            world.last_thumb_at, world.last_thumb_phase, world.last_thumb_headline = now, u.phase, big
            world.highlight = set()
            events.append({"type": "display", "what": "thumbnail", "gen": u.generation, "phase": u.phase})

    status_line = render.pinned_status(u, world.gen_seconds, f and f["stuck_gen"], f and FORECAST_GENS)
    core = render.pinned_text(status_line, world.recent, None, pinned_link)
    if core != world.pinned_core:  # something real changed: re-pick the suggested move too
        world.pinned_core = core
        world.pinned_suggest = render.suggestion(u) if u.phase != DIED else None
    for v, slot in sorted(world.pinned.items(), key=lambda kv: int(kv[0])):
        main = v == "0"
        text = render.pinned_text(status_line, world.recent, world.pinned_suggest, pinned_link, main=main)
        gap = PINNED_MIN_INTERVAL_S if main else PINNED_EXTRA_MIN_INTERVAL_S
        due = force_pinned or slot.get("at") is None or now - slot["at"] >= gap
        if hasattr(yt, "update_comment") and text != slot.get("text") and due:
            op = "update_pinned" if main else f"update_pinned_{v}"
            if _display(world, op, now, lambda: yt.update_comment(slot["id"], text), events):
                slot["text"], slot["at"] = text, now
                events.append({"type": "display", "what": "pinned_comment", "video": int(v)})

    if events or u.generation != gen_start or counts["new"]:
        events.append({"type": "tick", "now": run_at, "gen_start": gen_start, "gen_end": u.generation,
                       "phase": u.phase, "state_hash": u.hash.hex(), "comments_new": counts["new"],
                       "accepted": counts["accepted"], "rejected": counts["rejected"],
                       "queued": len(world.admission.queue)})
    return events
