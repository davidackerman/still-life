import hashlib

import pytest

import commands
import life


def cells(text, cid="c1"):
    return set(commands.parse(cid, text).cells)


def at(name, x, y):
    return set(life.place(life.PATTERNS[name], x, y))


def test_glider_defaults_to_se():
    c = commands.parse("c", "glider 10,20")
    assert c.kind == "glider" and c.desc == "glider 10,20 se"
    assert set(c.cells) == at("glider_se", 10, 20)


@pytest.mark.parametrize("d", ["ne", "nw", "se", "sw"])
def test_glider_directions(d):
    assert cells(f"glider 3,4 {d}") == at(f"glider_{d}", 3, 4)


@pytest.mark.parametrize("name", ["block", "blinker", "rpent", "acorn"])
def test_named_patterns(name):
    assert cells(f"{name} 50,30") == at(name, 50, 30)


def test_case_insensitive_and_spacing():
    assert cells("GLIDER 1 , 2 NW") == at("glider_nw", 1, 2)
    assert cells("Block 7,8") == at("block", 7, 8)


def test_direction_needs_a_word_boundary():
    assert commands.parse("c", "glider 3,4 neat").desc == "glider 3,4 se"


def test_command_can_appear_mid_sentence():
    assert cells("love this! glider 5,6 sw please") == at("glider_sw", 5, 6)


def test_first_valid_command_wins():
    assert commands.parse("c", "block 1,1 then glider 5,5").kind == "block"
    assert commands.parse("c", "glider 5,5 then block 1,1").kind == "glider"


def test_invalid_command_is_skipped_for_a_later_valid_one():
    assert commands.parse("c", "glider 200,5 block 3,3").desc == "block 3,3"


@pytest.mark.parametrize("text", ["block 128,0", "block 0,72", "glider 999,1", "acorn -1,5"])
def test_out_of_range_falls_back_to_random(text):
    assert commands.parse("c", text).kind == "random"


def test_edge_coordinates_wrap():
    assert cells("block 127,71") == {(127, 71), (0, 71), (127, 0), (0, 0)}


def test_cells_command():
    c = commands.parse("c", "cells 1,2 3,4  5 , 6")
    assert c.kind == "cells" and set(c.cells) == {(1, 2), (3, 4), (5, 6)}


def test_cells_max_25():
    ok = "cells " + " ".join(f"{i},{i}" for i in range(25))
    too_many = "cells " + " ".join(f"{i},{i}" for i in range(26))
    assert commands.parse("c", ok).kind == "cells"
    assert commands.parse("c", too_many).kind == "random"


def test_cells_with_one_bad_coordinate_is_invalid():
    assert commands.parse("c", "cells 1,1 200,1").kind == "random"


def test_non_ascii_digits_are_not_coordinates():
    assert commands.parse("c", "block ١,٢").kind == "random"  # Arabic-Indic digits


def test_word_must_stand_alone():
    assert commands.parse("c", "myglider 1,2").kind == "random"


def test_random_patch_is_deterministic():
    a, b = commands.parse("Ugx1", "hello universe"), commands.parse("Ugx1", "hello universe")
    assert a == b
    assert commands.parse("Ugx2", "hello universe") != a
    assert commands.parse("Ugx1", "hello universe!") != a


def test_random_patch_matches_the_published_formula():
    cid, text = "UgxTEST123", "hello universe"
    d = hashlib.blake2b((cid + text).encode("utf-8"), digest_size=32).digest()
    x, y = int.from_bytes(d[0:2], "big") % 128, int.from_bytes(d[2:4], "big") % 72
    bits = int.from_bytes(d[4:8], "big")
    expected = {((x + i % 5) % 128, (y + i // 5) % 72) for i in range(25) if bits >> i & 1}
    c = commands.parse(cid, text)
    assert c.kind == "random" and set(c.cells) == expected and c.desc == f"random 5x5 at {x},{y}"
    # Golden value so the formula cannot drift silently.
    assert c.desc == "random 5x5 at 45,46" and len(c.cells) == 14


def test_random_patch_fits_in_5x5_window():
    for i in range(200):
        c = commands.parse(f"id{i}", "x")
        x0, y0 = map(int, c.desc.rsplit(" ", 1)[1].split(","))
        for x, y in c.cells:
            assert (x - x0) % 128 < 5 and (y - y0) % 72 < 5


def test_a_colour_word_anywhere_paints_the_move_and_changes_nothing_else():
    plain, blue = commands.parse("c1", "glider 10,20 ne"), commands.parse("c1", "glider 10,20 ne BLUE please")
    assert plain.color is None and blue.color == "blue"
    assert blue.cells == plain.cells and blue.desc == plain.desc  # the same move, only painted
    assert commands.parse("c1", "red then pink: block 3,4").color == "red"  # the first colour word
    hi = commands.parse("c9", "hi pink")  # no command: its random patch is painted
    assert hi.kind == "random" and hi.color == "pink"
    assert hi.cells == commands.parse("c9", "hi pink").cells
    for text in ("Fred's redo", "bluebird", "green block 3,4", "black block 3,4"):  # whole words, and only the paints
        assert commands.parse("c1", text).color is None
    assert set(commands.COLORS) == {"red", "orange", "yellow", "blue", "purple", "pink", "cyan"}
