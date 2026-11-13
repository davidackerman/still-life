"""short.py builds its frames from a real (fake-clock) log and reproduces the board."""
import datetime as dt

import pytest

import explainer
import short
import tick
from admission import Comment

KEY = b"test-state-key-0123456789"
LAUNCH = 1_800_000_000.0


class FakeYT:
    def __init__(self, comments):
        self.comments = comments

    def fetch_comments(self, since, watch=(), now=None):
        return list(self.comments)

    def set_title(self, t, grid_text=""):
        pass

    def set_thumbnail(self, path):
        pass


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    for k in ("SEED", "GEN_SECONDS", "FLATLINE_HOURS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LAUNCH_TIME", str(int(LAUNCH)))
    yt = FakeYT([Comment("c1", "u1", iso(LAUNCH + 900), "glider 64,71 nw"),  # straddles the edge
                 Comment("c2", "u2", iso(LAUNCH + 1500), "hello")])
    for t in range(0, 3 * 3600, 600):
        tick.run(str(tmp_path), LAUNCH + t, lambda w: yt, KEY, thumb_path=None)
    return str(tmp_path)


def test_short_replays_the_log_and_tells_the_story(state_dir, monkeypatch):
    monkeypatch.setattr(explainer, "SCALE", 1)
    frames = short.build(state_dir)
    assert 25 * short.FPS < len(frames) < 50 * short.FPS
    assert sum(1 for f in frames if f.cue and f.cue[0] == "inject") == 2  # one chime per comment
    assert any(f.phase == "FLATLINING" for f in frames) and any(f.grey > 0 for f in frames)
    data = short.compose(frames[len(frames) // 2])
    assert len(data) == short.PW * short.PH * 3


def test_a_tampered_log_is_refused(state_dir, monkeypatch):
    import json
    import os
    monkeypatch.setattr(explainer, "SCALE", 1)
    path = os.path.join(state_dir, "log.jsonl")
    lines = open(path).read().splitlines()
    for i, ln in enumerate(lines):
        e = json.loads(ln)
        if e["type"] == "injection":
            e["cells"] = [[1, 1], [2, 1], [3, 1]]
            lines[i] = json.dumps(e)
            break
    open(path, "w").write("\n".join(lines) + "\n")
    with pytest.raises(SystemExit):
        short.build(state_dir)


def test_the_daily_recap_is_short_and_opens_on_a_full_screen_of_board(state_dir, monkeypatch):
    monkeypatch.setattr(explainer, "SCALE", 1)
    frames = short.build(state_dir, since_gen=0)
    assert 12 * short.FPS < len(frames) < 40 * short.FPS  # short enough to be watched to the end, every word readable
    # the board's whole height filling the screen (Day 5, a band across the middle, had 57% swipe away)
    assert frames[0].band == (0, short.PH) and abs(short.view(frames[0])[3] - short.BH) < 1
    assert 1 <= sum(1 for f in frames if f.cue and f.cue[0] == "inject") <= 2  # the day's biggest moves
    assert any(f.grey > 0 for f in frames)  # its future: stuck, then dead
    assert len(short.compose(frames[0])) == short.PW * short.PH * 3
    end = frames[-1]  # it ends on what you can comment, one under another, each with its film, over the dimmed board
    assert end.extras and end.dim > 0.5 and any("one of these" in t[0] for t in end.texts)
    assert len(short.compose(end)) == short.PW * short.PH * 3
    assert len([f for f in frames if f.extras and f.dim]) >= 6 * short.FPS  # long enough to watch them


def test_a_close_up_opening_can_come_back(state_dir, monkeypatch):
    monkeypatch.setattr(explainer, "SCALE", 1)
    monkeypatch.setattr(short, "HOOK_ZOOM", 4.0)
    monkeypatch.setattr(short, "opening_move", lambda h, since: None)  # (the hook alone)
    frames = short.build(state_dir, since_gen=0)
    n = short.rf("THIS SHORT", "DIES", "if nobody comments")  # held long enough to read
    assert frames[0].zoom == 4.0 and frames[n - 1].zoom < 1.1  # close up, then the whole board


def test_busiest_finds_the_active_patch():
    import numpy as np
    w = np.zeros((72, 128), int)
    w[60:64, 100:104] = 5
    x, y = short.busiest(w, 4.0)  # a 32 x 18 cell window
    assert abs(x / short.CELL - 102) <= 16 and abs(y / short.CELL - 62) <= 9


def test_effect_words_are_plain():
    g = 120  # seconds per generation
    assert short.effect_words(1000, 1000 + 6 * 720, g) == "gave it 6 more days to live"
    assert short.effect_words(5000, 5000 - 7 * 720, g) == "took 7 days off its life"
    assert short.effect_words(5000, 5000 - 270, g) == "took 9 hours off its life"
    assert short.effect_words(5000, None, g) == "gave it weeks more to live"
    assert short.effect_words(None, 5000, g) == "took weeks off its life"
    assert short.effect_words(5000, 5005, g) == "barely changed its fate"


def test_where_the_daily_short_says_comment_a_bubble_shows_one_and_what_it_adds(state_dir, monkeypatch):
    monkeypatch.setattr(explainer, "SCALE", 1)
    frames = short.build(state_dir, since_gen=0)
    ending = next(i for i, f in enumerate(frames) if f.dim)  # the ending lists them all over the dimmed board
    assert all(f.extras for f in frames[:ending])  # from the first frame, through the stakes, never a gap
    assert ending > short.chip_turn()  # long enough for more than one example, each film played through
    assert any(f.grey > 0 for f in frames[:ending])  # still there as it says how it dies
    assert len(short.compose(frames[0])) == short.PW * short.PH * 3
    full = short.build(state_dir)  # the original Short is unchanged: no bubbles
    assert not any(f.extras for f in full[:int(2.7 * short.FPS)])


def test_each_bubble_is_held_until_its_film_has_played_through():
    from PIL import Image, ImageDraw
    seen = []
    real = short._bubble
    try:
        short._bubble = lambda d, x0, yc, w, say, adds, cells, *a, **k: seen.append((adds, cells.sum()))
        per = 100.5  # what end_chips picks: the bubbles' time split into whole films
        d = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        for i in range(int(2 * per)):
            short.comment_chip(i, 1.0, "5,5", per)(d, None)
    finally:
        short._bubble = real
    film = short.render.demo(short.CYCLE[0][1], short.CHIP_BOX)
    first = [n for adds, n in seen if adds == short.CYCLE[0][3]]
    assert len(first) == int(per) + 1 and first[0] == film[0].sum() and first[-1] == film[-1].sum()
    assert seen[len(first)][0] != seen[0][0]  # then the next one, from the start of its own film


def test_the_short_shows_paint(tmp_path, monkeypatch):
    import numpy as np
    for k in ("SEED", "GEN_SECONDS", "FLATLINE_HOURS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LAUNCH_TIME", str(int(LAUNCH)))
    monkeypatch.setattr(explainer, "SCALE", 1)
    yt = FakeYT([Comment("c1", "u1", iso(LAUNCH + 900), "glider 20,20 se blue")])
    for t in range(0, 3600, 600):
        tick.run(str(tmp_path), LAUNCH + t, lambda w: yt, b"test-state-key-0123456789", thumb_path=None)
    frames = short.build(str(tmp_path), since_gen=0)
    painted = [f for f in frames if f.paint is not None and (f.paint == 4).any() and f.grey == 0]
    assert painted
    img = np.asarray(short.board(painted[-1]))
    assert (img.reshape(-1, 3) == np.array(short.render.PAINT["blue"])).all(axis=1).any()


def test_the_shorts_tails_start_afresh_on_a_cut_back_in_time():
    import numpy as np
    import life

    class F:
        def __init__(self, grid, gen):
            self.grid, self.gen, self.paint = grid, gen, None
    a = life.mask([(1, 1), (2, 1), (3, 1)], (10, 10))
    b = life.step(a)
    frames = [F(a, 5), F(b, 6), F(a, 1)]  # the last one jumps back to an earlier day
    short.add_trails(frames)
    assert frames[1].trail is not None and frames[1].trail[0].max() > 0  # a tail where the blinker was
    assert frames[2].trail is None  # no ghosts from the later board
    first = [F(b, 11)]
    short.add_trails(first, primer=[(a, None, 10)])
    assert first[0].trail is not None  # the primer gives even the first frame its tails



def test_the_daily_short_opens_on_what_one_real_comment_did(state_dir, monkeypatch):
    monkeypatch.setattr(explainer, "SCALE", 1)
    h = short.History(state_dir)
    move = short.opening_move(h, 0)
    assert move is not None
    g, e, f = move
    frames = short.build(state_dir, since_gen=0)
    top = " ".join(t[0] for t in frames[0].texts)
    words = short.effect_words(f["before"], f["after"], h.gen_seconds)
    amount = words.split()[1 if words.startswith("took") else 2]  # "took 2 days ...", "gave it 9 more days ..."
    assert top.startswith("One comment ") and "this Short" in top and f" {amount} " in f" {top} "
    assert frames[0].band == (0, short.PH) and frames[0].extras  # a full screen of board, its words in a bubble where it lands
    landed = [i for i, fr in enumerate(frames) if fr.cue and fr.cue[0] == "inject"]
    assert landed and landed[0] == int(short.OPEN_LAND_S * short.FPS) and frames[landed[0]].gen == g
    assert frames[0].gen == short.open_first(g) and frames[landed[0] - 1].gen == max(0, g - 1)  # moving from frame one
    n = next(i for i, fr in enumerate(frames) if any("DIES" in t[0] for t in fr.texts))  # the opening's end
    said = [t[0] for t in frames[n - 20].texts]  # every word of it, held long enough to read
    assert n >= max(int(short.OPEN_S * short.FPS), short.rf(*said))
    assert frames[n - 1].band == (short.BAND_Y, short.BAND_H)  # then back into the band, for the hook
    for fr in (frames[0], frames[n // 2], frames[n - 4]):
        assert len(short.compose(fr)) == short.PW * short.PH * 3
    assert any(fr.zoom > 1.5 for fr in frames[:n])  # closer as it lands
    assert all(any("DIES" in t[0] for t in fr.texts) for fr in frames[n:n + short.rf("THIS SHORT", "DIES", "if nobody comments")])



def test_the_opening_alternates_between_the_days_biggest_kill_and_save(state_dir):
    h = short.History(state_dir)
    kill, save = short.opening_move(h, 0, "kill"), short.opening_move(h, 0, "save")
    for move, sign in ((kill, -1), (save, 1)):
        if move:
            f = move[2]
            b, a = f["before"], f["after"]
            d = (a if a is not None else b + short.FORECAST_GENS) - (b if b is not None else a + short.FORECAST_GENS)
            assert d * sign > 0 or move == (save if sign < 0 else kill)  # its kind, or the other when the day had none
    day = int(h.now_gen * h.gen_seconds // 86400) + 1
    assert short.opening_move(h, 0, "alternate") == (kill if day % 2 == 0 else save)


def test_the_glider_short_follows_one_glider_until_it_crashes(tmp_path, monkeypatch):
    for k in ("SEED", "GEN_SECONDS", "FLATLINE_HOURS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LAUNCH_TIME", str(int(LAUNCH)))
    monkeypatch.setattr(explainer, "SCALE", 1)
    yt = FakeYT([Comment("c1", "u1", iso(LAUNCH + 900), "glider 64,71 nw")])  # straddles the edge
    for t in range(0, 8 * 3600, 1200):
        tick.run(str(tmp_path), LAUNCH + t, lambda w: yt, KEY, thumb_path=None)
    h = short.History(str(tmp_path))
    flown, g, e, _ = short.flights(h)[0]
    assert e["command"] == "glider 64,71 nw" and flown >= short.FLIGHT_MIN_GENS
    spotted, quiet = [], short.render.quiet_spot
    monkeypatch.setattr(short.render, "quiet_spot", lambda *a: spotted.append(1) or quiet(*a))
    frames = short.build_flight(str(tmp_path))
    assert len(spotted) == 1  # the ending's example keeps its spot
    assert any("became this glider" in t[0] for t in frames[0].texts)  # it opens on the glider as it lands
    assert frames[0].gen == g and frames[0].band == (0, short.PH)
    counted = [int(t[0].replace(",", "")) for f in frames for t in f.texts if t[0].replace(",", "").isdigit()]
    assert counted[0] == 0 and max(counted) == flown  # the counter climbs to the crash
    assert any(t[0] == "Crash." for f in frames for t in f.texts)
    assert any(t[0] == "Can yours fly farther?" for t in frames[-1].texts)  # it ends on the challenge
    assert all(f.extras for f in frames[-10:])  # and a comment that takes it up,
    assert frames[-1].band == (0, short.PH) and frames[-1].gen > h.now_gen  # over the universe now, moving on
    assert not frames[-1].dim  # and the board isn't darkened
    for fr in (frames[0], frames[len(frames) // 2], frames[-1]):
        assert len(short.compose(fr)) == short.PW * short.PH * 3


def test_the_glider_short_picks_a_colored_glider_before_a_longer_plain_one(monkeypatch):
    h = type("H", (), {"now_gen": 5000, "gen_seconds": 120})()
    plain, purple, blue = ({"command": "glider 1,1 sw"}, {"command": "glider 2,2 se", "color": "purple"},
                           {"command": "glider 3,3 ne", "color": "blue"})
    trips = [(316, 100, plain, None), (60, 2000, purple, None), (44, 3000, blue, None), (20, 4000, {**blue}, None)]
    monkeypatch.setattr(short, "flights", lambda h: trips)
    assert short.pick_flight(h)[2] is purple  # the longest colored one that flew far enough
    assert short.pick_flight(h, pick="glider 1,1 sw")[2] is plain
    monkeypatch.setattr(short, "flights", lambda h: [trips[0], trips[3]])
    assert short.pick_flight(h)[2] is plain  # none colored flew far enough: the longest plain one
