import statistics

import numpy as np
import pytest

import life

SMALL = (16, 16)


def evolve(g, n):
    for _ in range(n):
        g = life.step(g)
    return g


def test_blinker_has_period_2():
    g = life.mask(life.place(life.PATTERNS["blinker"], 5, 5, SMALL), SMALL)
    assert not np.array_equal(life.step(g), g)
    assert np.array_equal(evolve(g, 2), g)


def test_block_is_still():
    g = life.mask(life.place(life.PATTERNS["block"], 5, 5, SMALL), SMALL)
    assert np.array_equal(life.step(g), g)


def test_empty_stays_empty():
    assert not life.step(life.empty(SMALL)).any()


@pytest.mark.parametrize("direction,dx,dy", [("se", 1, 1), ("sw", -1, 1), ("ne", 1, -1), ("nw", -1, -1)])
def test_glider_moves_one_cell_diagonally_every_4_generations(direction, dx, dy):
    shape = (20, 20)
    g = life.mask(life.place(life.PATTERNS[f"glider_{direction}"], 8, 8, shape), shape)
    assert np.array_equal(evolve(g, 4), np.roll(np.roll(g, dy, 0), dx, 1))


def test_glider_wraps_around_the_torus():
    shape = (8, 12)  # glider returns after 4 * lcm(8, 12) generations
    g = life.mask(life.place(life.PATTERNS["glider_se"], 0, 0, shape), shape)
    assert np.array_equal(evolve(g, 4 * 24), g)
    assert not np.array_equal(evolve(g, 4 * 12), g)


def test_hash_is_stable_and_sensitive():
    g = life.mask(life.centered("acorn"))
    # Golden values: replay depends on this exact scheme (BLAKE2b-256 of packbits).
    assert life.state_hash(g).hex() == "adf3a35e42750ceedfc680147ed3b58a26d952a96f93cce320c929199fe643cd"
    assert life.state_hash(life.empty()).hex() == "36a80a05f2f5d5be0a38028fa0e72820816174a00703ed4e8dd6452936304b7e"
    g2 = g.copy()
    g2[0, 0] = 1
    assert life.state_hash(g2) != life.state_hash(g)


def test_encode_decode_roundtrip():
    rng = np.random.default_rng(0)
    g = (rng.random((life.H, life.W)) < 0.3).astype(np.uint8)
    assert np.array_equal(life.decode(life.encode(g)), g)


# Reference numbers from the prototype, used as sanity ranges rather than hard
# asserts. Measured here: acorn cycle starts at gen 1853 (period 2), R-pentomino
# at 792 (period 2), exactly as in the prototype.
def test_acorn_reference():
    mu, period = life.find_cycle(life.mask(life.centered("acorn")))
    assert 1800 <= mu <= 1900 and period == 2


def test_rpent_reference():
    mu, period = life.find_cycle(life.mask(life.centered("rpent")))
    assert 750 <= mu <= 850 and period == 2


def test_random_soup_median_transient():
    rng = np.random.default_rng(12345)
    mus = [life.find_cycle((rng.random((life.H, life.W)) < 0.3).astype(np.uint8))[0] for _ in range(25)]
    assert 1000 <= statistics.median(mus) <= 3000  # prototype: ~1900
