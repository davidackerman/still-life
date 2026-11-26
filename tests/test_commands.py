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


def test_random_with_a_spot_puts_the_comments_patch_there():
    text = "purple random 46 56"
    c = commands.parse("c7", text)
    d = hashlib.blake2b(("c7" + text).encode("utf-8"), digest_size=32).digest()
    bits = int.from_bytes(d[4:8], "big")  # the same bits as its patch anywhere else: only the spot is chosen
    assert c.kind == "random" and c.desc == "random 5x5 at 46,56" and c.color == "purple"
    assert set(c.cells) == {(46 + i % 5, 56 + i // 5) for i in range(25) if bits >> i & 1}
    for said in ("random 46,56", "Random (46, 56)", "random at 46,56", "random 46x56", "random 5x5 at 46,56",
                 "random 5x5 46 56", "hi! random 46 56 please"):
        assert commands.parse("c7", said).desc == "random 5x5 at 46,56", said
    assert commands.parse("c7", "random 5 5").desc == "random 5x5 at 5,5"
    for said in ("random", "random 5x5", "random 5x5 please", "randomly 46 56", "random 128,0", "random -1,5"):
        c = commands.parse("c7", said)
        assert c.kind == "random" and c == commands.random_patch("c7", said), said  # no spot: the comment's own
    assert commands.parse("c7", "random 3,4 then glider 5,5").kind == "random"  # the earliest command wins
    assert commands.parse("c7", "glider 5,5 then random 3,4").kind == "glider"
    assert commands.parse("c7", "random 3,4 in 1h").delay_s == 3600
    assert commands.parse("c7", "random 3,4 black").erase  # named in words: black erases it
    wrapped = commands.parse("c7", "random 126,70")  # runs past the corner: wraps, as the patterns do
    assert all(0 <= x < 128 and 0 <= y < 72 for x, y in wrapped.cells)


def test_a_color_word_anywhere_paints_the_move_and_changes_nothing_else():
    plain, blue = commands.parse("c1", "glider 10,20 ne"), commands.parse("c1", "glider 10,20 ne BLUE please")
    assert plain.color is None and blue.color == "blue"
    assert blue.cells == plain.cells and blue.desc == plain.desc  # the same move, only painted
    assert commands.parse("c1", "red then pink: block 3,4").color == "red"  # the first color word
    hi = commands.parse("c9", "hi pink")  # no command: its random patch is painted
    assert hi.kind == "random" and hi.color == "pink"
    assert hi.cells == commands.parse("c9", "hi pink").cells
    for text in ("Fred's redo", "bluebird", "green block 3,4", "black block 3,4"):  # whole words, and only the paints
        assert commands.parse("c1", text).color is None
    assert set(commands.COLORS) == {"red", "orange", "yellow", "blue", "purple", "pink", "cyan"}


def test_a_pattern_named_without_coordinates_is_placed_at_the_comments_spot():
    r = commands.random_patch("c9", "block")  # the spot a random patch from this comment would use
    x, y = (int(v) for v in r.desc.split(" at ")[1].split(","))
    assert commands.parse("c9", "block").desc == f"block {x},{y}"
    assert commands.parse("c9", "block").cells == cells_at("block", x, y)
    assert commands.parse("c1", "glider").desc.endswith(" se") and commands.parse("c1", "Glider NE").desc.endswith(" ne")
    paint = commands.parse("c1", "acorn red")
    assert paint.kind == "acorn" and paint.color == "red"
    assert commands.parse("c1", "i love a block, then glider 5,5").desc == "glider 5,5 se"  # coordinates win
    for text in ("blocks", "myglider", "acorn -1,5", "block ١,٢", "block 128,0", "hello"):
        assert commands.parse("c1", text).kind == "random", text


def cells_at(name, x, y):
    import life
    return tuple(sorted(set(life.place(life.PATTERNS[name], x, y, (life.H, life.W)))))


@pytest.mark.parametrize("text", ["acorn 15x15 orange", "Acorn 15X15", "acorn 15 15", "acorn (15, 15)", "acorn 15,15"])
def test_a_patterns_coordinates_can_be_written_other_ways(text):
    assert commands.parse("c1", text).desc == "acorn 15,15"


def test_cells_still_need_commas():
    assert commands.parse("c1", "cells 1 2 3 4").kind == "random"


@pytest.mark.parametrize("text,delay,kind", [
    ("rpent 1 hr", 3600, "rpent"), ("rpent 40,30 in 1h", 3600, "rpent"), ("hi in 30 min", 1800, "random"),
    ("glider 10,20 ne 90 minutes", 5400, "glider"), ("acorn red in 2 hours", 7200, "acorn"),
    ("block 5,5", 0, "block"), ("acorn 15x15", 0, "acorn"), ("in 99 hours block", 24 * 3600, "block")])
def test_a_time_in_a_message_schedules_its_move(text, delay, kind):
    cmd = commands.parse("c1", text)
    assert cmd.delay_s == delay and cmd.kind == kind
    assert commands.parse("c1", "rpent 40,30 in 1h").desc == "rpent 40,30"  # the time is not a coordinate


def test_black_with_a_named_pattern_erases_instead():
    plain = commands.parse("c1", "glider 40,20")
    for text in ("glider 40,20 black", "BLACK glider 40,20", "glider 40,20 black red"):
        e = commands.parse("c1", text)
        assert e.erase and e.cells == plain.cells and e.desc == plain.desc and e.color is None  # erasing: no paint
    assert commands.parse("c1", "block black").erase  # a bare pattern, at the comment's spot
    swath = commands.parse("c1", "cells 3,4 5,6 black")  # not a pattern: it adds its cells as usual
    assert swath.kind == "cells" and not swath.erase
    later = commands.parse("c1", "rpent 10,10 black in 1h")
    assert later.erase and later.delay_s == 3600
    assert not commands.parse("c1", "glider 40,20 blue").erase


def test_black_erases_a_random_patch_when_it_says_random():
    here = commands.parse("c1", "random 113,8 black")
    assert here.erase and here.desc == "random 5x5 at 113,8" and here.color is None
    painted = commands.parse("c1", "random 113,8 black cyan")
    assert painted.erase and painted.color is None  # erasing: no paint
    spot = commands.parse("c1", "random black")  # no spot: the comment's own
    assert spot.erase and spot.cells == commands.random_patch("c1", "random black").cells
    for text in ("black cats are cool", "randomly black", "cells 3,4 black"):
        assert not commands.parse("c1", text).erase, text  # "random" must be said, as a word of its own


def test_a_period_between_the_numbers_works_too():
    assert commands.parse("c1", "cyan glider 40.53").desc == "glider 40,53 se"
    assert commands.parse("c1", "Pink Blinker 98.33").desc == "blinker 98,33"
    assert commands.parse("c1", "random 46.56").desc == "random 5x5 at 46,56"


def test_black_without_a_pattern_never_erases():
    for text in ("black cats are cool", "hi black", "glider 40,20 blackbird", "blackout block 3,4"):
        assert not commands.parse("c1", text).erase, text
    hi = commands.parse("c9", "black cats are cool")  # the usual random patch: it adds cells
    assert hi.kind == "random" and hi.color is None


@pytest.mark.parametrize("text, desc, color", [
    ("8, 56 red", "random 5x5 at 8,56", "red"),  # from the live chat, Nov 25: a place on its own is where it goes
    ("1,1 red", "random 5x5 at 1,1", "red"),
    ("(8, 56)", "random 5x5 at 8,56", None),
    ("8,56 in 1h", "random 5x5 at 8,56", None),
    ("glider red 8,56", "glider 8,56 se", "red"),  # and a pattern named anywhere goes there
    ("Glider at 8, 56 Red", "glider 8,56 se", "red"),
    ("put a block at 10,20 please", "block 10,20", None),
    ("glider 5,5 then 1,1", "glider 5,5 se", None),  # a whole command still wins
])
def test_a_place_on_its_own_is_where_the_move_goes(text, desc, color):
    c = commands.parse("c", text)
    assert (c.desc, c.color) == (desc, color)
    if c.kind == "random":  # the comment's own patch, only moved there
        assert set(c.cells) == set(commands.random_patch("c", text, at=(int(desc.split()[-1].split(",")[0]),
                                                                       int(desc.split(",")[-1]))).cells)


@pytest.mark.parametrize("text", ["1,000 comments!", "2 '2 white", "12,5,7", "acorn -1,5", "block 128,0", "3.5,2",
                                  "hello"])
def test_numbers_that_are_not_a_place_leave_it_at_the_comments_own_spot(text):
    assert commands.parse("c", text).desc == commands.random_patch("c", text).desc


def test_a_place_never_makes_an_eraser_of_a_plain_message():
    assert commands.parse("c", "glider black 8,56").erase and commands.parse("c", "glider black 8,56").desc == "glider 8,56 se"
    assert not commands.parse("c", "8,56 black").erase  # no pattern named: it adds cells, as before
