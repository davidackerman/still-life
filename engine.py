"""One run of the universe: comments in, simulation forward, display out.

Shared by simulate.py (fake YouTube, fake clock) and, from step 3, tick.py
(real YouTube, state on disk). Pure apart from the `yt` object it is handed.

`yt` must provide:
    fetch_comments() -> list[admission.Comment]   recent top-level comments
    set_title(title: str) -> None
    set_thumbnail(path: str | None) -> None
Display calls may raise; a failure is logged and retried on a later run. It
never touches the simulation, which has already advanced by then.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import admission
import render
from universe import DIED, GEN_SECONDS, Universe, target_generation

TITLE_MIN_INTERVAL_S = 15 * 60
THUMB_INTERVAL_S = 3 * 3600


@dataclass
class World:
    universe: Universe
    launch_time: float
    gen_seconds: int = GEN_SECONDS
    admission: admission.Admission = field(default_factory=admission.Admission)
    runs: int = 0
    last_title: str | None = None
    last_title_at: float | None = None
    last_thumb_at: float | None = None
    last_thumb_phase: str | None = None
    highlight: set = field(default_factory=set)  # cells injected since the last thumbnail


def tick(world: World, now: float, yt, thumb_path: str | None = None) -> list[dict]:
    """Advance the world to wall-clock `now`. Returns the run's log events.

    Order: fetch + admit comments -> apply up to MAX_PER_RUN injections at the
    current generation -> step to the target generation -> update display.
    """
    u = world.universe
    world.runs += 1
    events: list[dict] = [{"type": "tick", "run": world.runs, "now": now, "gen": u.generation}]

    # 1. comments (input). A fetch failure skips input for this run only.
    try:
        comments = yt.fetch_comments()
    except Exception as e:  # noqa: BLE001 - display/input errors must not stop the run
        comments = []
        events.append({"type": "youtube_error", "op": "fetch_comments", "error": repr(e)})
    events += admission.admit(world.admission, comments, dead=(u.phase == DIED))

    # 2. simulation (authoritative)
    if u.phase == DIED:
        for inj in admission.take_batch(world.admission, limit=len(world.admission.queue)):
            events.append({"type": "rejected", "comment_id": inj["comment_id"], "author": inj["author"],
                           "published_at": inj["published_at"], "reason": "universe_dead"})
    else:
        batch = [dict(inj, run=world.runs) for inj in admission.take_batch(world.admission)]
        if batch:
            events += u.inject(batch)
            world.highlight.update(tuple(c) for inj in batch for c in inj["cells"])
        events += u.advance_to(target_generation(now, world.launch_time, world.gen_seconds))

    # 3. display (best effort)
    title = render.title(u, world.gen_seconds)
    title_due = world.last_title_at is None or now - world.last_title_at >= TITLE_MIN_INTERVAL_S
    if title != world.last_title and title_due:
        try:
            yt.set_title(title)
            world.last_title, world.last_title_at = title, now
            events.append({"type": "display", "what": "title", "title": title})
        except Exception as e:  # noqa: BLE001
            events.append({"type": "youtube_error", "op": "set_title", "error": repr(e)})

    # Refresh every 3 h and immediately on a phase change; a dead universe never
    # changes again, so after the DIED thumbnail there is nothing to refresh.
    periodic = world.last_thumb_at is None or now - world.last_thumb_at >= THUMB_INTERVAL_S
    thumb_due = u.phase != world.last_thumb_phase or (periodic and u.phase != DIED)
    if thumb_due:
        try:
            if thumb_path:
                render.thumbnail(u, thumb_path, world.highlight, world.gen_seconds)
            yt.set_thumbnail(thumb_path)
            world.last_thumb_at, world.last_thumb_phase = now, u.phase
            world.highlight = set()
            events.append({"type": "display", "what": "thumbnail", "gen": u.generation, "phase": u.phase})
        except Exception as e:  # noqa: BLE001
            events.append({"type": "youtube_error", "op": "set_thumbnail", "error": repr(e)})
    return events
