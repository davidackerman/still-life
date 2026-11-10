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


def test_the_daily_recap_is_short_and_opens_on_the_whole_board(state_dir, monkeypatch):
    monkeypatch.setattr(explainer, "SCALE", 1)
    frames = short.build(state_dir, since_gen=0)
    assert 12 * short.FPS < len(frames) < 25 * short.FPS  # short enough to be watched to the end
    assert frames[0].zoom == 1  # the whole board, as the teaser opened (it kept more viewers)
    assert 1 <= sum(1 for f in frames if f.cue and f.cue[0] == "inject") <= 2  # the day's biggest moves
    assert any(f.grey > 0 for f in frames)  # its future: stuck, then dead
    assert len(short.compose(frames[0])) == short.PW * short.PH * 3


def test_a_close_up_opening_can_come_back(state_dir, monkeypatch):
    monkeypatch.setattr(explainer, "SCALE", 1)
    monkeypatch.setattr(short, "HOOK_ZOOM", 4.0)
    frames = short.build(state_dir, since_gen=0)
    assert frames[0].zoom == 4.0 and frames[int(2.6 * short.FPS)].zoom < 1.1  # close up, then the whole board


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
