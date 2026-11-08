"""One run of the universe: comments in, simulation forward, display out.

Shared by simulate.py (fake YouTube, fake clock) and tick.py (real YouTube,
state on disk). Pure apart from the `yt` object it is handed.

`yt` must provide:
    fetch_comments(since: float) -> list[admission.Comment]   top-level comments
                                                                published after `since`
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

from dataclasses import dataclass, field

import admission
import render
from universe import ALIVE, DIED, GEN_SECONDS, Universe, target_generation

TITLE_MIN_INTERVAL_S = 15 * 60
FORECAST_GENS = 30000  # how far ahead the title's "if nobody comments" forecast looks (~42 days)
THUMB_INTERVAL_S = 3 * 3600
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
    backoff: dict = field(default_factory=dict)  # op -> {"until": t, "failures": n}
    last_changer: dict | None = None  # keyed ids of the last board-changing comment
    quota: dict = field(default_factory=dict)  # owned by youtube.Quota

    def to_state(self) -> tuple[dict, bytes]:
        u, history = self.universe.to_state()
        return ({
            "config": {"launch_time": self.launch_time, "seed": self.seed, "gen_seconds": self.gen_seconds},
            "universe": u,
            "admission": self.admission.to_dict(),
            "display": {"last_title": self.last_title, "last_title_at": self.last_title_at,
                        "last_thumb_at": self.last_thumb_at, "last_thumb_phase": self.last_thumb_phase,
                        "highlight": sorted(list(c) for c in self.highlight), "backoff": self.backoff,
                        "forecast": self.forecast},
            "last_changer": self.last_changer,
            "quota": self.quota,
        }, history)

    @classmethod
    def from_state(cls, d: dict, history: bytes, key: bytes) -> "World":
        disp = d["display"]
        return cls(
            universe=Universe.from_state(d["universe"], history),
            launch_time=d["config"]["launch_time"], seed=d["config"]["seed"],
            gen_seconds=d["config"]["gen_seconds"], key=key,
            admission=admission.Admission.from_dict(d["admission"]),
            last_title=disp["last_title"], last_title_at=disp["last_title_at"],
            last_thumb_at=disp["last_thumb_at"], last_thumb_phase=disp["last_thumb_phase"],
            highlight={tuple(c) for c in disp["highlight"]}, backoff=dict(disp["backoff"]),
            last_changer=d.get("last_changer"), quota=dict(d.get("quota", {})),
            forecast=disp.get("forecast"),
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


def tick(world: World, now: float, yt, thumb_path: str | None = None, force_thumbnail: bool = False) -> list[dict]:
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
    try:
        comments = yt.fetch_comments(now - admission.FETCH_WINDOW_S)
    except Exception as e:  # noqa: BLE001
        comments = []
        events.append({"type": "youtube_error", "op": "fetch_comments", "error": repr(e)[:500]})
    counts = admission.admit(world.admission, comments, now, world.key, dead=(u.phase == DIED),
                             shape=u.grid.shape)

    # 2. simulation (authoritative)
    if u.phase == DIED:
        dropped = admission.take_batch(world.admission, limit=len(world.admission.queue))
        if dropped:
            counts["rejected"]["universe_dead"] = counts["rejected"].get("universe_dead", 0) + len(dropped)
    else:
        batch = admission.take_batch(world.admission)
        if batch:
            inj_events = u.inject([{"command": q["command"], "cells": q["cells"], "run_at": run_at}
                                   for q in batch])
            for q, ev in zip(batch, [e for e in inj_events if e["type"] == "injection"]):
                if ev["changed"]:
                    world.last_changer = {"comment": q["comment"], "author": q["author"],
                                          "gen": ev["gen"], "command": q["command"]}
            events += inj_events
            world.highlight.update(tuple(c) for q in batch for c in q["cells"])
        events += u.advance_to(target_generation(now, world.launch_time, world.gen_seconds))

    # 3. display (best effort)
    f = refresh_forecast(world)
    title = render.title(u, world.gen_seconds, f and f["stuck_gen"], f and FORECAST_GENS)
    title_due = world.last_title_at is None or now - world.last_title_at >= TITLE_MIN_INTERVAL_S
    if title != world.last_title and title_due:
        if _display(world, "set_title", now, lambda: yt.set_title(title, render.braille(u.grid)), events):
            world.last_title, world.last_title_at = title, now
            events.append({"type": "display", "what": "title", "title": title})

    # Refresh every 3 h and immediately on a phase change; a dead universe never
    # changes again, so after the DIED thumbnail there is nothing to refresh.
    periodic = world.last_thumb_at is None or now - world.last_thumb_at >= THUMB_INTERVAL_S
    if force_thumbnail or u.phase != world.last_thumb_phase or (periodic and u.phase != DIED):
        def upload():
            if thumb_path:
                render.thumbnail(u, thumb_path, world.highlight, world.gen_seconds)
            yt.set_thumbnail(thumb_path)
        if _display(world, "set_thumbnail", now, upload, events):
            world.last_thumb_at, world.last_thumb_phase = now, u.phase
            world.highlight = set()
            events.append({"type": "display", "what": "thumbnail", "gen": u.generation, "phase": u.phase})

    if events or u.generation != gen_start or counts["new"]:
        events.append({"type": "tick", "now": run_at, "gen_start": gen_start, "gen_end": u.generation,
                       "phase": u.phase, "state_hash": u.hash.hex(), "comments_new": counts["new"],
                       "accepted": counts["accepted"], "rejected": counts["rejected"],
                       "queued": len(world.admission.queue)})
    return events
