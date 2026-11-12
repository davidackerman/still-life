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


def test_paint_only_colors_cells_and_follows_them():
    import numpy as np
    g = life.mask(life.place(life.PATTERNS["glider_se"], 5, 5, (20, 20)), (20, 20))
    paint = g * 4  # a blue glider
    for _ in range(12):
        nxt = life.step(g)
        paint = life.paint_step(g, nxt, paint)
        g = nxt
        assert np.array_equal(paint > 0, g == 1)  # every live cell painted, no dead one
    assert set(np.unique(paint[g == 1])) == {4}  # still blue as it flies
    # a birth takes the majority of its three parents...
    b = np.zeros((5, 5), np.uint8)
    b[1, 1] = b[1, 2] = b[1, 3] = 1  # a blinker: births above and below the middle
    p = np.zeros((5, 5), np.uint8)
    p[1, 1], p[1, 2], p[1, 3] = 1, 2, 2
    out = life.paint_step(b, life.step(b), p)
    assert out[0, 2] == 2 and out[2, 2] == 2 and out[1, 2] == 2  # majority, and the survivor keeps its own
    assert out[1, 1] == 0 and out[1, 3] == 0  # the dead have none
    # ...or, if all three differ, the first one's (read from the top left)
    p[1, 1], p[1, 2], p[1, 3] = 3, 1, 2
    out = life.paint_step(b, life.step(b), p)
    assert out[0, 2] == 3 and out[2, 2] == 3
    # unpainted boards stay unpainted
    assert not life.paint_step(b, life.step(b), np.zeros((5, 5), np.uint8)).any()


def test_tails_fade_out_in_their_color_and_never_touch_the_game():
    import numpy as np
    g = life.mask([(5, 5), (6, 5), (7, 5)], (20, 20))  # a blinker
    paint = np.zeros(g.shape, np.uint8)
    paint[5, 5:8] = 4  # blue
    t = life.Trail(g.shape)
    t.see(g, 0, paint)
    g1 = life.step(g)
    t.see(g1, 1, paint)
    a, col = t.layer(g1)
    assert a[5, 5] == life.TRAIL_MAX and col[5, 5] == 4  # just died: brightest, in its color
    assert a[5, 6] == 0 and (a[g1 == 1] == 0).all()  # alive: no tail
    later = t.layer(g1, frac=0.5)[0][5, 5]
    assert 0 < later < life.TRAIL_MAX  # fading on through the generation
    t.see(g1, 1 + life.TRAIL_GENS + 1, paint)
    assert t.layer(g1)[0][5, 5] == 0  # gone after TRAIL_GENS
    assert np.array_equal(life.step(g), g1)  # (the game itself is untouched)
