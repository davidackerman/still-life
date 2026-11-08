"""The universe's life: Game of Life evolution plus the flatline state machine.

Pure and deterministic. Time is measured in generations: generation g happens
at LAUNCH_TIME + g * GEN_SECONDS of wall-clock time, so a 24 h flatline is
720 generations at the default pace. The only clock is the target generation
the caller asks to advance to (see target_generation).

Phases:
  ALIVE       evolving. A repeated state since the last injection -> FLATLINING
              with a deadline flatline_gens ahead.
  FLATLINING  in a cycle with a deadline. A board-changing injection that lands
              outside the recorded cycle -> CONTESTED. Reaching the deadline -> DIED.
  CONTESTED   perturbed, deadline still running, cannot die. Each generation:
                re-enters a recorded cycle state  -> FLATLINING (original deadline)
                repeats a state since injection   -> FLATLINING (new cycle, fresh deadline)
                reaches the deadline otherwise    -> ALIVE (deadline cleared)
  DIED        frozen at the death generation. Nothing changes any more.

History (hashes since the last board-changing injection) is what cycle
detection searches. An injection that adds no new live cells changes nothing:
no history reset, no phase change.
"""

from __future__ import annotations

import math

import numpy as np

import life

ALIVE, FLATLINING, CONTESTED, DIED = "ALIVE", "FLATLINING", "CONTESTED", "DIED"
GEN_SECONDS = 120
FLATLINE_HOURS = 24


def flatline_gens(hours: float = FLATLINE_HOURS, gen_seconds: int = GEN_SECONDS) -> int:
    return math.ceil(hours * 3600 / gen_seconds)


def target_generation(now: float, launch_time: float, gen_seconds: int = GEN_SECONDS) -> int:
    """The generation the universe should have reached at wall-clock `now`."""
    return max(0, math.floor((now - launch_time) / gen_seconds))


class Universe:
    def __init__(self, grid: np.ndarray, flatline_gens: int, generation: int = 0):
        self.grid = grid.astype(np.uint8)
        self.generation = generation
        self.flatline_gens = flatline_gens
        self.phase = ALIVE
        self.deadline_gen: int | None = None
        self.cycle: frozenset[bytes] = frozenset()  # hashes of the recorded flatline cycle
        self.died_gen: int | None = None
        h = life.state_hash(self.grid)
        self.history: list[bytes] = [h]  # history[i] is the state at history_base + i
        self.history_base = generation
        self._index: dict[bytes, int] = {h: generation}

    # ------------------------------------------------------------------ helpers
    @property
    def hash(self) -> bytes:
        return life.state_hash(self.grid)

    def _reset_history(self, h: bytes) -> None:
        self.history, self.history_base, self._index = [h], self.generation, {h: self.generation}

    def _record(self, h: bytes) -> None:
        self._index[h] = self.generation
        self.history.append(h)

    def _event(self, new_phase: str, reason: str, **extra) -> dict:
        ev = {"type": "phase", "gen": self.generation, "from": self.phase, "to": new_phase,
              "reason": reason, "state_hash": self.hash.hex(), **extra}
        self.phase = new_phase
        return ev

    def _start_flatline(self, h: bytes, reason: str) -> dict:
        first = self._index[h]  # generation where the cycle began
        self.cycle = frozenset(self.history[first - self.history_base:])
        self.deadline_gen = self.generation + self.flatline_gens
        return self._event(FLATLINING, reason, cycle_start=first, period=len(self.cycle),
                           deadline_gen=self.deadline_gen)

    # ------------------------------------------------------------------ inputs
    def inject(self, injections: list[dict]) -> list[dict]:
        """Apply a batch of injections at the current generation (before stepping).

        Each injection is a dict with at least "cells": [[x, y], ...]. Returns
        events: one "injection" event per input (with changed/hash_after),
        plus at most one phase event for the batch as a whole.
        """
        if self.phase == DIED:
            raise RuntimeError("cannot inject into a dead universe")
        events = []
        before = self.grid.copy()
        h, w = self.grid.shape
        for inj in injections:
            prev = self.grid.copy()
            for x, y in inj["cells"]:
                self.grid[y % h, x % w] = 1
            changed = bool((self.grid != prev).any())
            events.append({"type": "injection", "gen": self.generation, **inj,
                           "changed": changed, "hash_after": self.hash.hex()})
        if np.array_equal(self.grid, before):
            return events  # nothing new on the board: nothing else happens
        h_now = self.hash
        self._reset_history(h_now)
        if self.phase in (FLATLINING, CONTESTED):
            if h_now in self.cycle:  # landed on a state of the cycle: still flatlining
                if self.phase == CONTESTED:
                    events.append(self._event(FLATLINING, "reentered_cycle"))
            elif self.phase == FLATLINING:
                events.append(self._event(CONTESTED, "injection"))
        return events

    def advance_to(self, target_gen: int) -> list[dict]:
        """Step until generation `target_gen` (or death). Returns phase events."""
        events = []
        while self.generation < target_gen and self.phase != DIED:
            self.grid = life.step(self.grid)
            self.generation += 1
            h = self.hash
            if self.phase == ALIVE:
                if h in self._index:
                    events.append(self._start_flatline(h, "cycle_detected"))
                else:
                    self._record(h)
            elif self.phase == CONTESTED:
                if h in self.cycle:
                    events.append(self._event(FLATLINING, "reentered_cycle"))
                elif h in self._index:
                    events.append(self._start_flatline(h, "new_cycle"))
                elif self.generation >= self.deadline_gen:
                    self._record(h)
                    events.append(self._event(ALIVE, "survived_deadline", old_deadline_gen=self.deadline_gen))
                    self.deadline_gen, self.cycle = None, frozenset()
                else:
                    self._record(h)
            if self.phase == FLATLINING and self.generation >= self.deadline_gen:
                self.died_gen = self.generation
                events.append(self._event(DIED, "deadline"))
        return events

    # ------------------------------------------------------------------ status
    def forecast_stuck(self, max_gens: int) -> int | None:
        """If nobody comments: the generation at which this ALIVE universe would be
        found stuck in a loop (the same check advance_to makes), or None if not
        within max_gens. Read-only; display use only."""
        if self.phase != ALIVE:
            return None
        seen = set(self._index)  # states since the last board-changing injection
        g, gen = self.grid, self.generation
        for _ in range(max_gens):
            g = life.step(g)
            gen += 1
            h = life.state_hash(g)
            if h in seen:
                return gen
            seen.add(h)
        return None

    def gens_left(self) -> int | None:
        if self.deadline_gen is None or self.phase not in (FLATLINING, CONTESTED):
            return None
        return max(0, self.deadline_gen - self.generation)

    # ------------------------------------------------------------------ persistence
    def to_state(self) -> tuple[dict, bytes]:
        """(JSON-able dict, history bytes) for state.json and history.bin."""
        return ({
            "generation": self.generation,
            "shape": list(self.grid.shape),
            "grid": life.encode(self.grid),
            "flatline_gens": self.flatline_gens,
            "phase": self.phase,
            "deadline_gen": self.deadline_gen,
            "cycle": sorted(h.hex() for h in self.cycle),
            "died_gen": self.died_gen,
            "history_base": self.history_base,
            "hash": self.hash.hex(),
        }, b"".join(self.history))

    @classmethod
    def from_state(cls, d: dict, history: bytes) -> "Universe":
        shape = tuple(d["shape"])
        u = cls(life.decode(d["grid"], shape), d["flatline_gens"], d["generation"])
        if u.hash.hex() != d["hash"]:
            raise ValueError("state grid does not match its recorded hash")
        if len(history) % 32 or not history:
            raise ValueError("history.bin is not a non-empty sequence of 32-byte hashes")
        u.phase, u.deadline_gen, u.died_gen = d["phase"], d["deadline_gen"], d["died_gen"]
        u.cycle = frozenset(bytes.fromhex(h) for h in d["cycle"])
        u.history = [history[i:i + 32] for i in range(0, len(history), 32)]
        u.history_base = d["history_base"]
        u._index = {h: u.history_base + i for i, h in enumerate(u.history)}
        if u.history_base + len(u.history) - 1 > u.generation:
            raise ValueError("history.bin runs past the current generation")
        return u

    def summary(self) -> dict:
        return {"gen": self.generation, "phase": self.phase, "deadline_gen": self.deadline_gen,
                "died_gen": self.died_gen, "hash": self.hash.hex(), "live_cells": int(self.grid.sum())}


# Fields inject() adds to an injection event; stripped to recover its input.
RESULT_FIELDS = ("type", "gen", "changed", "hash_after")


def replay(seed_grid: np.ndarray, flatline_gens: int, batches, final_gen: int):
    """Rebuild a life from its seed and its injection batches.

    `batches` is [(gen, [injection_event, ...]), ...] in the order they were
    applied, one entry per inject() call (one per run). Returns (universe,
    events); the events equal the original run's phase and injection events.
    """
    u = Universe(seed_grid, flatline_gens)
    events: list[dict] = []
    for gen, batch in batches:
        events += u.advance_to(gen)
        if u.generation != gen or u.phase == DIED:
            raise ValueError(f"injections logged at gen {gen}, but replay is at gen "
                             f"{u.generation} ({u.phase})")
        events += u.inject([{k: v for k, v in e.items() if k not in RESULT_FIELDS} for e in batch])
    events += u.advance_to(final_gen)
    return u, events
