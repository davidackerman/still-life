"""Which comments become injections, and when. Pure logic; no I/O.

Rules:
- Only comments published within the last FETCH_WINDOW_S (48 h) are considered.
- Each comment is processed at most once (dedupe).
- Comments are processed in (publishedAt, comment ID) order.
- One accepted injection per author per hour, measured by publishedAt. A later
  comment from the same author inside that hour is dropped and counted.
- After the universe has died, comments are dropped and counted.
- Accepted injections enter a FIFO queue; each run applies at most
  MAX_PER_RUN of them and the rest wait for the next run.

Data policy: YouTube's developer policies limit how long other users' data may
be stored, and this state is committed to a public repo. So nothing here keeps
raw comment IDs, channel IDs or text. IDs are stored only as HMAC-SHA256 under a
secret key, and are pruned after RETENTION_S. Text is used in memory to parse
the command and is never stored.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from dataclasses import dataclass, field

import commands
import life

AUTHOR_COOLDOWN_S = 3600
MAX_PER_RUN = 20
FETCH_WINDOW_S = 48 * 3600  # comments older than this are ignored
RETENTION_S = 72 * 3600  # keyed hashes are forgotten after this (> FETCH_WINDOW_S)


@dataclass(frozen=True)
class Comment:
    id: str
    author: str  # authorChannelId.value, or "name:" + display name when absent
    published_at: str  # ISO 8601 as returned by the API, e.g. 2026-11-08T00:20:13Z
    text: str  # textDisplay with textFormat=plainText
    by_owner: bool = False  # posted by the channel that owns the video

    @property
    def epoch(self) -> float:
        return parse_time(self.published_at)


def parse_time(iso: str) -> float:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def keyed(key: bytes, kind: str, value: str) -> str:
    """HMAC-SHA256(key, kind:value), truncated to 128 bits, as hex."""
    return hmac.new(key, f"{kind}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


@dataclass
class Admission:
    seen: dict[str, float] = field(default_factory=dict)  # keyed comment id -> publishedAt epoch
    last_accepted: dict[str, float] = field(default_factory=dict)  # keyed author -> publishedAt epoch
    queue: list[dict] = field(default_factory=list)  # accepted, not yet applied

    def to_dict(self) -> dict:
        return {"seen": dict(sorted(self.seen.items())),
                "last_accepted": dict(sorted(self.last_accepted.items())),
                "queue": list(self.queue)}

    @classmethod
    def from_dict(cls, d: dict) -> "Admission":
        return cls(dict(d.get("seen", {})), dict(d.get("last_accepted", {})), list(d.get("queue", [])))

    def prune(self, now: float) -> None:
        cutoff = now - RETENTION_S
        self.seen = {k: t for k, t in self.seen.items() if t >= cutoff}
        self.last_accepted = {k: t for k, t in self.last_accepted.items() if t >= cutoff}


def admit(state: Admission, new_comments: list[Comment], now: float, key: bytes,
          dead: bool = False, shape=(life.H, life.W)) -> dict:
    """Process fetched comments into the queue. Mutates `state`.

    Returns counts: {"new": n, "accepted": n, "rejected": {reason: n}}.
    """
    state.prune(now)
    counts = {"new": 0, "accepted": 0, "rejected": {}}
    window_start = now - FETCH_WINDOW_S
    fresh = {}
    for c in new_comments:
        kid = keyed(key, "comment", c.id)
        if kid not in state.seen and kid not in fresh and window_start <= c.epoch <= now + 300:
            fresh[kid] = c
    for kid, c in sorted(fresh.items(), key=lambda kc: (kc[1].epoch, kc[1].id)):
        state.seen[kid] = c.epoch
        counts["new"] += 1
        reason = None
        author = keyed(key, "author", c.author)
        last = state.last_accepted.get(author)
        if dead:
            reason = "universe_dead"
        elif last is not None and abs(c.epoch - last) < AUTHOR_COOLDOWN_S:  # also late arrivals
            reason = "author_rate_limit"
        if reason:
            counts["rejected"][reason] = counts["rejected"].get(reason, 0) + 1
            continue
        cmd = commands.parse(c.id, c.text, shape)
        state.last_accepted[author] = max(c.epoch, last or c.epoch)
        state.queue.append({"comment": kid, "author": author, "published_at": c.epoch,
                            "command": cmd.desc, "cells": [list(xy) for xy in cmd.cells]})
        counts["accepted"] += 1
    return counts


def take_batch(state: Admission, limit: int = MAX_PER_RUN) -> list[dict]:
    """Pop the injections to apply this run (FIFO, at most `limit`)."""
    batch, state.queue = state.queue[:limit], state.queue[limit:]
    return batch
