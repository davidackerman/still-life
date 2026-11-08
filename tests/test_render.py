from PIL import Image

import life
import render
from universe import Universe, flatline_gens


def acorn():
    return Universe(life.mask(life.centered("acorn")), flatline_gens())


def test_titles_for_every_phase():
    u = acorn()
    u.advance_to(400)
    assert render.title(u) == "This video dies when its universe repeats · Gen 400 · ALIVE"
    u.advance_to(1855)
    assert render.title(u) == "This video dies when its universe repeats · Gen 1,855 · FLATLINING · 24h left"
    u.advance_to(2575 - 25)
    assert render.title(u).endswith("· FLATLINING · 50m left")
    u.inject([{"cells": [[0, 0], [1, 0], [0, 1], [1, 1]]}])
    assert " · CONTESTED · 50m left" in render.title(u)
    u2 = acorn()
    u2.advance_to(5000)
    assert render.title(u2) == "This video dies when its universe repeats · DIED at gen 2,575"


def test_titles_fit_youtube_limits():
    u = acorn()
    u.generation = 9_999_999
    u.phase, u.deadline_gen = "FLATLINING", u.generation + 720
    t = render.title(u)
    assert len(t) <= 100 and "<" not in t and ">" not in t


def test_time_left_rounds_up():
    assert render.time_left(720) == "24h left"
    assert render.time_left(691) == "24h left"  # 23.03 h
    assert render.time_left(30) == "1h left"
    assert render.time_left(29) == "58m left"
    assert render.time_left(0) == "1m left"


def test_thumbnail_is_1280x720_png_under_2mb(tmp_path):
    u = acorn()
    u.advance_to(500)
    path = tmp_path / "t.png"
    size = render.thumbnail(u, str(path), highlight={(0, 0)})
    im = Image.open(path)
    assert im.size == (1280, 720) and im.format == "PNG" and size < 2 * 1024 * 1024
