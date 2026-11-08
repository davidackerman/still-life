from PIL import Image

import life
import render
from universe import Universe, flatline_gens


def acorn():
    return Universe(life.mask(life.centered("acorn")), flatline_gens())


def test_titles_count_down_in_every_phase():
    u = acorn()
    stuck = u.forecast_stuck(30000)  # 1855: 2575 gens to death = 3 days, 13 hours, 50 minutes
    assert render.title(u, stuck_gen=stuck) == "This video dies in 3 days, 13 hours if nobody comments · Game of Life"
    assert render.title(u, stuck_gen=None, horizon_gens=30000) == "This video dies in 42+ days if nobody comments · Game of Life"
    u.advance_to(1855)
    assert render.title(u) == "This video dies in 1 day: its universe is stuck in a loop"
    u.advance_to(2575 - 25)
    assert render.title(u) == "This video dies in 50 minutes: its universe is stuck in a loop"
    u.inject([{"cells": [[0, 0], [1, 0], [0, 1], [1, 1]]}])
    assert render.title(u) == "This video might die in 50 minutes: a comment broke the loop"
    u2 = acorn()
    u2.advance_to(5000)
    assert render.title(u2) == "This video died: its universe got stuck in a loop"


def test_duration_wording():
    assert render.duration(2 * 86400 + 14 * 3600 + 59 * 60) == "2 days, 14 hours"
    assert render.duration(86400) == "1 day"
    assert render.duration(86400 + 3600) == "1 day, 1 hour"
    assert render.duration(14 * 3600 + 50 * 60) == "14 hours"
    assert render.duration(3599) == "60 minutes"
    assert render.duration(61) == "2 minutes"
    assert render.duration(0) == "1 minute"


def test_titles_fit_youtube_limits():
    u = acorn()
    worst = render.title(u, stuck_gen=u.generation + 30000)
    assert len(worst) <= 100 and "<" not in worst and ">" not in worst
    u.phase, u.deadline_gen = "FLATLINING", u.generation + 720
    assert len(render.title(u)) <= 100


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
