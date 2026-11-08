"""Every phase transition, on a small torus with a short flatline (F = 10 gens).

Generations are the clock: advance_to(g) is "wall-clock time reached gen g".
"""
import numpy as np
import pytest

import life
from universe import (ALIVE, CONTESTED, DIED, FLATLINING, Universe, flatline_gens, replay,
                      target_generation)

SHAPE = (32, 32)
F = 10


def board(*pieces):
    cells = []
    for name, x, y in pieces:
        cells += life.place(life.PATTERNS[name], x, y, SHAPE)
    return life.mask(cells, SHAPE)


def blinker_universe():
    return Universe(board(("blinker", 2, 2)), F)


def inj(cells, cid="c"):
    return {"comment_id": cid, "cells": [list(c) for c in cells]}


def far_block():
    return inj(life.place(life.PATTERNS["block"], 20, 20, SHAPE), "block")


def lone_cell():
    return inj([(20, 20)], "lone")  # dies after one generation


def phases(events):
    return [(e["gen"], e["to"], e["reason"]) for e in events if e["type"] == "phase"]


def flatlining():
    u = blinker_universe()
    ev = u.advance_to(2)
    assert phases(ev) == [(2, FLATLINING, "cycle_detected")]
    return u


# --- clock ------------------------------------------------------------------
def test_target_generation_is_floor_of_elapsed_over_pace():
    assert target_generation(1000, 1000) == 0
    assert target_generation(1000 + 119.9, 1000) == 0
    assert target_generation(1000 + 120, 1000) == 1
    assert target_generation(1000 + 3600, 1000) == 30
    assert target_generation(500, 1000) == 0  # before launch


def test_24h_flatline_is_720_generations():
    assert flatline_gens(24, 120) == 720


# --- ALIVE ------------------------------------------------------------------
def test_alive_to_flatlining_when_a_state_repeats():
    u = blinker_universe()
    ev = u.advance_to(5)
    p = [e for e in ev if e["type"] == "phase"][0]
    assert (p["gen"], p["to"], p["period"], p["cycle_start"], p["deadline_gen"]) == (2, FLATLINING, 2, 0, 12)
    assert u.phase == FLATLINING and len(u.cycle) == 2


def test_injection_while_alive_resets_history():
    u = blinker_universe()
    u.advance_to(1)
    u.inject([far_block()])
    assert u.phase == ALIVE
    ev = u.advance_to(10)
    # Without the reset the repeat would be found at gen 2; now it is gen 3.
    p = [e for e in ev if e["type"] == "phase"][0]
    assert (p["gen"], p["cycle_start"]) == (3, 1)


# --- FLATLINING ---------------------------------------------------------------
def test_flatlining_dies_at_the_deadline():
    u = flatlining()
    ev = u.advance_to(100)
    assert phases(ev) == [(12, DIED, "deadline")]
    assert u.phase == DIED and u.died_gen == 12 and u.generation == 12


def test_dead_universe_is_frozen():
    u = flatlining()
    u.advance_to(100)
    h = u.hash
    assert u.advance_to(200) == [] and u.generation == 12 and u.hash == h
    with pytest.raises(RuntimeError):
        u.inject([far_block()])


def test_injection_that_adds_nothing_changes_nothing():
    u = flatlining()
    alive_now = [(int(x), int(y)) for y, x in zip(*np.nonzero(u.grid))]
    ev = u.inject([inj(alive_now)])
    assert [e["changed"] for e in ev if e["type"] == "injection"] == [False]
    assert phases(ev) == [] and u.phase == FLATLINING and u.deadline_gen == 12


def test_injection_that_lands_on_a_cycle_state_stays_flatlining():
    # Beacon: period 2, and its 6-cell phase is a subset of its 8-cell phase.
    beacon8 = [(0, 0), (1, 0), (0, 1), (1, 1), (2, 2), (3, 2), (2, 3), (3, 3)]
    u = Universe(life.mask(life.place(beacon8, 5, 5, SHAPE), SHAPE), F)
    u.advance_to(2)
    assert u.phase == FLATLINING
    u.advance_to(3)  # now in the 6-cell phase
    assert u.grid.sum() == 6
    ev = u.inject([inj([(6, 6), (7, 7)])])  # restore the 8-cell phase
    assert [e["changed"] for e in ev if e["type"] == "injection"] == [True]
    assert phases(ev) == [] and u.phase == FLATLINING and u.deadline_gen == 12


def test_flatlining_to_contested_on_injection():
    u = flatlining()
    ev = u.inject([far_block()])
    assert phases(ev) == [(2, CONTESTED, "injection")] and u.deadline_gen == 12


# --- CONTESTED ----------------------------------------------------------------
def test_contested_reentering_the_cycle_keeps_the_original_deadline():
    u = flatlining()
    u.advance_to(5)
    u.inject([lone_cell()])
    assert u.phase == CONTESTED
    ev = u.advance_to(6)
    assert phases(ev) == [(6, FLATLINING, "reentered_cycle")] and u.deadline_gen == 12
    assert phases(u.advance_to(100)) == [(12, DIED, "deadline")]


def test_contested_new_cycle_gets_a_fresh_deadline():
    u = flatlining()
    old_cycle = u.cycle
    u.advance_to(5)
    u.inject([far_block()])
    ev = u.advance_to(20)
    p = [e for e in ev if e["type"] == "phase"][0]
    assert (p["gen"], p["to"], p["reason"], p["deadline_gen"]) == (7, FLATLINING, "new_cycle", 17)
    assert u.cycle.isdisjoint(old_cycle)


def test_contested_survives_the_deadline_back_to_alive():
    u = flatlining()
    u.advance_to(5)
    u.inject([inj(life.place(life.PATTERNS["glider_se"], 14, 20, SHAPE), "glider")])
    ev = u.advance_to(12)
    assert phases(ev) == [(12, ALIVE, "survived_deadline")]
    assert u.phase == ALIVE and u.deadline_gen is None and not u.cycle


def test_death_cannot_happen_while_contested():
    # Contested one generation before the deadline: the deadline then passes
    # while CONTESTED, and the universe returns to ALIVE instead of dying.
    u = flatlining()
    u.advance_to(11)
    u.inject([inj(life.place(life.PATTERNS["glider_se"], 14, 20, SHAPE), "glider")])
    ev = u.advance_to(12)
    assert phases(ev) == [(12, ALIVE, "survived_deadline")] and u.died_gen is None


def test_reentering_the_cycle_at_the_deadline_dies_that_generation():
    u = flatlining()
    u.advance_to(11)
    u.inject([lone_cell()])
    assert u.phase == CONTESTED
    assert phases(u.advance_to(30)) == [(12, FLATLINING, "reentered_cycle"), (12, DIED, "deadline")]


def test_another_injection_while_contested_stays_contested_and_resets_history():
    u = flatlining()
    u.inject([far_block()])  # gen 2: CONTESTED
    u.advance_to(3)
    ev = u.inject([inj([(10, 25)], "second")])  # lone cell, but the block keeps it CONTESTED
    assert phases(ev) == [] and u.phase == CONTESTED and u.history_base == 3


def test_injection_while_contested_that_lands_on_a_cycle_state_is_a_failed_save():
    # OR-ing cells can only grow the board, so set the state up by hand: CONTESTED
    # with the beacon's 6-cell phase on the board and its 8-cell phase in the cycle.
    beacon8 = life.mask(life.place(((0, 0), (1, 0), (0, 1), (1, 1), (2, 2), (3, 2), (2, 3), (3, 3)), 5, 5, SHAPE), SHAPE)
    u = Universe(life.step(beacon8), F, generation=4)
    u.phase, u.cycle, u.deadline_gen = CONTESTED, frozenset({life.state_hash(beacon8)}), 9
    ev = u.inject([inj([(6, 6), (7, 7)])])
    assert phases(ev) == [(4, FLATLINING, "reentered_cycle")] and u.deadline_gen == 9


# --- determinism ----------------------------------------------------------------
def test_advance_is_resumable():
    a = Universe(life.mask(life.centered("acorn")), 720)
    b = Universe(life.mask(life.centered("acorn")), 720)
    ev_a = a.advance_to(3000)
    ev_b = b.advance_to(1000) + b.advance_to(1854) + b.advance_to(1855) + b.advance_to(3000)
    assert ev_a == ev_b and a.hash == b.hash
    assert phases(ev_a) == [(1855, FLATLINING, "cycle_detected"), (2575, DIED, "deadline")]


def test_replay_roundtrip():
    seed = life.mask(life.centered("acorn"))
    u = Universe(seed, 720)
    log, batches = [], []
    plan = {300: ["glider 10,10 se"], 1250: ["block 5,5", "rpent 90,40"], 1500: ["hello"]}
    import commands
    for gen in sorted(plan):
        log += u.advance_to(gen)
        batch = []
        for i, text in enumerate(plan[gen]):
            c = commands.parse(f"id{gen}-{i}", text)
            batch.append({"comment_id": f"id{gen}-{i}", "text": text, "command": c.desc,
                          "cells": [list(xy) for xy in c.cells], "run": gen})
        ev = u.inject(batch)
        log += ev
        batches.append((gen, [e for e in ev if e["type"] == "injection"]))
    log += u.advance_to(6000)

    r, replayed = replay(seed, 720, batches, 6000)
    assert replayed == log
    assert r.hash == u.hash and r.phase == u.phase and r.generation == u.generation


def test_replay_rejects_injections_after_death():
    seed = board(("blinker", 2, 2))
    with pytest.raises(ValueError):
        replay(seed, F, [(50, [{"cells": [[1, 1]]}])], 60)


# --- forecast (display only) ----------------------------------------------------
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_forecast_matches_what_actually_happens(seed):
    rng = np.random.default_rng(seed)
    u = Universe((rng.random(SHAPE) < 0.3).astype(np.uint8), F)
    u.advance_to(7)
    predicted = u.forecast_stuck(5000)
    ev = u.advance_to(6000)
    assert predicted == [e["gen"] for e in ev if e["type"] == "phase"][0]


def test_forecast_accounts_for_states_since_the_last_injection():
    u = blinker_universe()
    u.advance_to(1)  # history already holds gen 0 and gen 1
    assert u.forecast_stuck(100) == 2


def test_forecast_is_read_only_and_only_for_alive():
    u = flatlining()
    assert u.forecast_stuck(100) is None
    a = blinker_universe()
    h, gen = a.hash, a.generation
    a.forecast_stuck(100)
    assert (a.hash, a.generation, a.phase) == (h, gen, ALIVE)
