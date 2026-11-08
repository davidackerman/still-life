#!/usr/bin/env python3
"""Rebuild the universe's whole life from the log and check it.

    python replay.py [--state-dir state]

Checks, in order:
  1. the log's hash chain (seq, prev_hash, hash on every line)
  2. the genesis line: seed pattern and its hash
  3. run by run: re-apply each run's logged injections at the logged
     generation, step to the run's end, and require the same injection and
     phase events and the same end-of-run state hash
  4. the final state equals state/state.json

Exit status 0 if everything matches, 1 with a description of the first
mismatch otherwise. Needs no secrets and no network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import life
from store import HISTORY, LOG, STATE, read_log, verify_chain
from universe import RESULT_FIELDS, Universe

LOG_FIELDS = ("seq", "prev_hash", "hash")
SIM_EVENTS = ("injection", "phase")


class ReplayError(Exception):
    pass


def _sim(e: dict) -> dict:
    return {k: v for k, v in e.items() if k not in LOG_FIELDS}


def replay_log(entries: list[dict]) -> Universe:
    """Re-run the life described by a verified log. Returns the final universe."""
    g = entries[0]
    if g.get("type") != "genesis":
        raise ReplayError("log line 0 is not a genesis event")
    shape = tuple(g["shape"])
    seed = life.mask(life.centered(g["seed"], shape), shape)
    u = Universe(seed, g["flatline_gens"])
    if u.hash.hex() != g["seed_hash"]:
        raise ReplayError(f"seed {g['seed']!r} does not hash to the genesis seed_hash")

    segment: list[dict] = []
    for e in entries[1:]:
        if e["type"] != "tick":
            segment.append(e)
            continue
        where = f"run ending at log line {e['seq']} (gen {e['gen_start']}->{e['gen_end']})"
        if u.generation != e["gen_start"]:
            raise ReplayError(f"{where}: replay is at gen {u.generation}")
        logged = [_sim(x) for x in segment if x["type"] in SIM_EVENTS]
        injections = [x for x in logged if x["type"] == "injection"]
        produced: list[dict] = []
        if injections:
            if {x["gen"] for x in injections} != {u.generation}:
                raise ReplayError(f"{where}: injections not at the run's starting generation")
            produced += u.inject([{k: v for k, v in x.items() if k not in RESULT_FIELDS}
                                  for x in injections])
        produced += u.advance_to(e["gen_end"])
        if produced != logged:
            for i, (a, b) in enumerate(zip(produced, logged)):
                if a != b:
                    raise ReplayError(f"{where}: event {i} differs\n  replayed: {a}\n  logged:   {b}")
            raise ReplayError(f"{where}: replay produced {len(produced)} events, log has {len(logged)}")
        if u.generation != e["gen_end"] or u.phase != e["phase"] or u.hash.hex() != e["state_hash"]:
            raise ReplayError(f"{where}: end state differs (replay gen {u.generation} {u.phase} "
                              f"{u.hash.hex()[:12]}, log {e["phase"]} {e["state_hash"][:12]})")
        segment = []
    if any(x["type"] in SIM_EVENTS for x in segment):
        raise ReplayError("log ends with injection/phase events that no run completed")
    return u


def verify(state_dir: str) -> Universe:
    entries = read_log(os.path.join(state_dir, LOG))
    if not entries:
        raise ReplayError("log is empty")
    try:
        head = verify_chain(entries)
    except ValueError as e:
        raise ReplayError(f"hash chain broken: {e}") from e
    u = replay_log(entries)
    with open(os.path.join(state_dir, STATE), encoding="utf-8") as f:
        state = json.load(f)
    with open(os.path.join(state_dir, HISTORY), "rb") as f:
        history = f.read()
    if state["log"] != {"seq": len(entries), "head": head}:
        raise ReplayError("state.json does not point at the end of the log")
    saved = Universe.from_state(state["universe"], history)
    for attr in ("generation", "phase", "deadline_gen", "died_gen", "cycle"):
        if getattr(saved, attr) != getattr(u, attr):
            raise ReplayError(f"state.json {attr} = {getattr(saved, attr)!r}, replay = {getattr(u, attr)!r}")
    if saved.hash != u.hash:
        raise ReplayError("state.json grid differs from the replayed grid")
    if saved.history != u.history or saved.history_base != u.history_base:
        raise ReplayError("history.bin differs from the replayed history")
    return u


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verify the universe's log by replaying it from the seed.")
    ap.add_argument("--state-dir", default="state")
    args = ap.parse_args(argv)
    try:
        u = verify(args.state_dir)
    except ReplayError as e:
        print(f"REPLAY FAILED: {e}", file=sys.stderr)
        return 1
    print(f"replay OK: gen {u.generation:,}, {u.phase}, state {u.hash.hex()[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
