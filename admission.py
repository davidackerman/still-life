"""Which comments become injections, and when. Pure logic; no I/O.

Rules:
- Only comments published within the last FETCH_WINDOW_S (48 h) are considered.
- Each comment is processed at most once (dedupe).
- Comments are processed in (publishedAt, comment ID) order.
- Comment as often as you like, but each person's moves count at most
  MOVES_PER_GEN times per generation (by the generation each comment was
  posted in); the rest are dropped and counted (since 2026-11-11, after one
  person sent 86 messages in five minutes; before that, from 2026-11-09, every
  comment counted). The older per-author cooldown still exists, off by
  default: with AUTHOR_COOLDOWN_S > 0, a comment inside that window after the
  same author's last accepted one (by publishedAt) is dropped and counted.
- After the universe has died, comments are dropped and counted.
- Accepted injections wait in a queue; each run applies at most MAX_PER_RUN of
  them (a safety net that play doesn't reach, so each lands at the generation
  it was posted in), taking turns between authors (oldest first within each
  author), so one person's flood never holds up everyone else.

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
import math
from dataclasses import dataclass, field

import commands
import life

AUTHOR_COOLDOWN_S = 0  # 0 = off; was 3600 (one per author per hour) until 2026-11-09
MOVES_PER_GEN = 3  # each person's moves that count per generation (2 minutes); 0 = no limit
KEEP_GENS = 60  # how long (in generations: 2 hours) each person's per-generation counts are kept
MAX_PER_RUN = 200  # was 20 until 2026-11-11, when a flood made moves wait and land late
FETCH_WINDOW_S = 48 * 3600  # comments older than this are ignored
RETENTION_S = 72 * 3600  # keyed hashes are forgotten after this (> FETCH_WINDOW_S)


@dataclass(frozen=True)
class Comment:
    id: str
    author: str  # authorChannelId.value, or "name:" + display name when absent
    published_at: str  # ISO 8601 as returned by the API, e.g. 2026-11-08T00:20:13Z
    text: str  # textDisplay with textFormat=plainText
    by_owner: bool = False  # posted by the channel that owns the video
    reply: bool = False  # a reply under another comment (plays exactly like a top-level one)
    video: int = 0  # which of the configured videos: 0 = the main one, 1+ = extras (e.g. a Short)
    name: str = ""  # display name, for the leaderboard in the status comments: in memory only, never stored

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
    per_gen: dict[str, dict] = field(default_factory=dict)  # keyed author -> {generation (str): moves accepted}

    def to_dict(self) -> dict:
        return {"seen": dict(sorted(self.seen.items())),
                "last_accepted": dict(sorted(self.last_accepted.items())),
                "queue": list(self.queue),
                "per_gen": {a: dict(sorted(g.items(), key=lambda kv: int(kv[0]))) for a, g in sorted(self.per_gen.items())}}

    @classmethod
    def from_dict(cls, d: dict) -> "Admission":
        return cls(dict(d.get("seen", {})), dict(d.get("last_accepted", {})), list(d.get("queue", [])),
                   {a: dict(g) for a, g in (d.get("per_gen") or {}).items()})

    def prune(self, now: float) -> None:
        cutoff = now - RETENTION_S
        self.seen = {k: t for k, t in self.seen.items() if t >= cutoff}
        self.last_accepted = {k: t for k, t in self.last_accepted.items() if t >= cutoff}


def admit(state: Admission, new_comments: list[Comment], now: float, key: bytes,
          dead: bool = False, shape=(life.H, life.W), cooldown_s: float | None = None,
          launch_time: float = 0.0, gen_seconds: int = 120, per_gen: int | None = None) -> dict:
    """Process fetched comments into the queue. Mutates `state`.

    A comment's generation is the one it was posted in (from `launch_time`, one
    every `gen_seconds`), the one it lands in. Returns counts:
    {"new": n, "accepted": n, "rejected": {reason: n}}.
    """
    state.prune(now)
    cooldown = AUTHOR_COOLDOWN_S if cooldown_s is None else cooldown_s
    limit = MOVES_PER_GEN if per_gen is None else per_gen

    def gen_of(epoch: float) -> int:
        return max(0, math.floor((epoch - launch_time) / gen_seconds))
    oldest = gen_of(now) - KEEP_GENS
    state.per_gen = {a: kept for a, g in state.per_gen.items()
                     if (kept := {n: c for n, c in g.items() if int(n) >= oldest})}
    counts = {"new": 0, "new_chat": 0, "accepted": 0, "rejected": {}}
    window_start = now - FETCH_WINDOW_S
    fresh = {}
    for c in new_comments:
        kid = keyed(key, "comment", c.id)
        if kid not in state.seen and kid not in fresh and window_start <= c.epoch <= now + 300:
            fresh[kid] = c
    for kid, c in sorted(fresh.items(), key=lambda kc: (kc[1].epoch, kc[1].id)):
        state.seen[kid] = c.epoch
        counts["new"] += 1
        counts["new_chat"] += c.video < 0  # the live chat's (deploy/kick.py runs faster while it talks)
        reason = None
        author = keyed(key, "author", c.author)
        last = state.last_accepted.get(author)
        if dead:
            reason = "universe_dead"
        elif cooldown and last is not None and abs(c.epoch - last) < cooldown:  # also late arrivals
            reason = "author_rate_limit"
        elif limit and state.per_gen.get(author, {}).get(str(gen_of(c.epoch)), 0) >= limit:
            reason = "over_per_generation_limit"
        if reason:
            counts["rejected"][reason] = counts["rejected"].get(reason, 0) + 1
            continue
        cmd = commands.parse(c.id, c.text, shape)
        state.last_accepted[author] = max(c.epoch, last or c.epoch)
        mine = state.per_gen.setdefault(author, {})
        mine[str(gen_of(c.epoch))] = mine.get(str(gen_of(c.epoch)), 0) + 1
        state.queue.append({"comment": kid, "author": author, "published_at": c.epoch,
                            "command": cmd.desc, "cells": [list(xy) for xy in cmd.cells], "chat": c.video < 0,
                            **({"color": cmd.color} if cmd.color else {})})
        counts["accepted"] += 1
    return counts


def take_batch(state: Admission, limit: int = MAX_PER_RUN) -> list[dict]:
    """Pop the injections to apply this run: at most `limit`, taking turns
    between authors (round-robin in order of each author's oldest waiting
    comment; oldest first within an author). Returned in queue order."""
    by_author: dict[str, list[int]] = {}
    for i, q in enumerate(state.queue):
        by_author.setdefault(q["author"], []).append(i)
    picked: set[int] = set()
    lanes = list(by_author.values())
    rnd = 0
    while len(picked) < limit and any(rnd < len(lane) for lane in lanes):
        for lane in lanes:
            if rnd < len(lane) and len(picked) < limit:
                picked.add(lane[rnd])
        rnd += 1
    batch = [q for i, q in enumerate(state.queue) if i in picked]
    state.queue = [q for i, q in enumerate(state.queue) if i not in picked]
    return batch
