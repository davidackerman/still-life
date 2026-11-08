"""Persistence: state/state.json, state/history.bin, state/log.jsonl.

log.jsonl is append-only. Each line is one event (a JSON object) with:
    seq        0, 1, 2, ...
    prev_hash  the previous line's hash ("0" * 64 for the first line)
    hash       SHA-256 of the line's canonical JSON without the "hash" field
Canonical JSON: sorted keys, separators (",", ":"), UTF-8, no ASCII escaping.
Line 0 is the genesis event (seed and pace), which replay.py starts from.

Writes go to temporary files that are then renamed, so a crash leaves the
previous state intact. On GitHub Actions the state is only committed when
tick.py exits successfully.
"""

from __future__ import annotations

import hashlib
import json
import os

from engine import World

STATE, HISTORY, LOG = "state.json", "history.bin", "log.jsonl"
GENESIS_PREV = "0" * 64
FORMAT = 1


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def line_hash(entry: dict) -> str:
    return hashlib.sha256(canonical({k: v for k, v in entry.items() if k != "hash"})).hexdigest()


def read_log(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def verify_chain(entries: list[dict]) -> str:
    """Check seq, prev_hash and hash on every line. Returns the head hash."""
    prev = GENESIS_PREV
    for i, e in enumerate(entries):
        if e.get("seq") != i:
            raise ValueError(f"log line {i}: seq is {e.get('seq')}")
        if e.get("prev_hash") != prev:
            raise ValueError(f"log line {i}: prev_hash does not match line {i - 1}")
        if e.get("hash") != line_hash(e):
            raise ValueError(f"log line {i}: hash does not match its contents")
        prev = e["hash"]
    return prev


def _write_atomic(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Store:
    def __init__(self, directory: str):
        self.dir = directory
        self.seq = 0
        self.head = GENESIS_PREV

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def exists(self) -> bool:
        return os.path.exists(self.path(STATE))

    def create(self, world: World, genesis: dict) -> None:
        if self.exists() or os.path.exists(self.path(LOG)):
            raise FileExistsError(f"{self.dir} already holds a universe")
        os.makedirs(self.dir, exist_ok=True)
        open(self.path(LOG), "w").close()
        self.seq, self.head = 0, GENESIS_PREV
        self.save(world, [genesis])

    def load(self, key: bytes) -> World:
        with open(self.path(STATE), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("format") != FORMAT:
            raise ValueError(f"unknown state format {d.get('format')}")
        with open(self.path(HISTORY), "rb") as f:
            history = f.read()
        self.seq, self.head = d["log"]["seq"], d["log"]["head"]
        last = _last_line(self.path(LOG))
        if last is None or last.get("hash") != self.head or last.get("seq") != self.seq - 1:
            raise ValueError("log.jsonl does not end where state.json says it does")
        return World.from_state(d, history, key)

    def save(self, world: World, events: list[dict]) -> None:
        lines = []
        for ev in events:
            entry = {**ev, "seq": self.seq, "prev_hash": self.head}
            entry["hash"] = line_hash(entry)
            lines.append(json.dumps(entry, sort_keys=True, ensure_ascii=False))
            self.seq, self.head = self.seq + 1, entry["hash"]
        if lines:
            with open(self.path(LOG), "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(f.fileno())
        state, history = world.to_state()
        state = {"format": FORMAT, **state, "log": {"seq": self.seq, "head": self.head}}
        _write_atomic(self.path(HISTORY), history)
        _write_atomic(self.path(STATE), (json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False)
                                         + "\n").encode("utf-8"))


def _last_line(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        pos, chunk = end, b""
        while pos > 0 and chunk.count(b"\n") < 2:
            pos = max(0, pos - 4096)
            f.seek(pos)
            chunk = f.read(end - pos)
    lines = [ln for ln in chunk.split(b"\n") if ln.strip()]
    return json.loads(lines[-1]) if lines else None
