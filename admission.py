"""Which comments become injections, and when. Pure logic; no I/O.

Rules:
- Every comment ID is processed at most once, forever (dedupe).
- Comments are processed in (publishedAt, comment ID) order.
- One accepted injection per author per hour, measured by publishedAt. A later
  comment from the same author inside that hour is dropped and logged.
- After the universe has died, comments are dropped and logged.
- Accepted injections enter a FIFO queue; each run applies at most
  MAX_PER_RUN of them and the rest wait for the next run.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import commands
import life

AUTHOR_COOLDOWN_S = 3600
MAX_PER_RUN = 20


@dataclass(frozen=True)
class Comment:
    id: str
    author: str  # authorChannelId.value, or "name:" + display name when absent
    published_at: str  # ISO 8601 as returned by the API, e.g. 2026-11-08T00:20:13Z
    text: str  # textDisplay with textFormat=plainText

    @property
    def epoch(self) -> float:
        return dt.datetime.fromisoformat(self.published_at.replace("Z", "+00:00")).timestamp()


@dataclass
class Admission:
    processed_ids: set[str] = field(default_factory=set)
    last_accepted: dict[str, float] = field(default_factory=dict)  # author -> publishedAt epoch
    queue: list[dict] = field(default_factory=list)  # accepted, not yet applied

    def to_dict(self) -> dict:
        return {
            "processed_ids": sorted(self.processed_ids),
            "last_accepted": dict(sorted(self.last_accepted.items())),
            "queue": list(self.queue),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Admission":
        return cls(set(d.get("processed_ids", [])), dict(d.get("last_accepted", {})), list(d.get("queue", [])))


def admit(state: Admission, new_comments: list[Comment], dead: bool = False,
          shape=(life.H, life.W)) -> list[dict]:
    """Process fetched comments into the queue. Returns log events for rejections.

    Mutates `state`. Comments already processed are skipped silently.
    """
    events = []
    fresh = {c.id: c for c in new_comments if c.id not in state.processed_ids}
    for c in sorted(fresh.values(), key=lambda c: (c.epoch, c.id)):
        state.processed_ids.add(c.id)
        base = {"comment_id": c.id, "author": c.author, "published_at": c.published_at}
        if dead:
            events.append({"type": "rejected", **base, "reason": "universe_dead"})
            continue
        last = state.last_accepted.get(c.author)
        if last is not None and c.epoch < last + AUTHOR_COOLDOWN_S:
            events.append({"type": "rejected", **base, "reason": "author_rate_limit"})
            continue
        cmd = commands.parse(c.id, c.text, shape)
        state.last_accepted[c.author] = c.epoch
        state.queue.append({**base, "text": c.text, "command": cmd.desc,
                            "cells": [list(xy) for xy in cmd.cells]})
    return events


def take_batch(state: Admission, limit: int = MAX_PER_RUN) -> list[dict]:
    """Pop the injections to apply this run (FIFO, at most `limit`)."""
    batch, state.queue = state.queue[:limit], state.queue[limit:]
    return batch
