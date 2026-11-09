"""stream.py: loads the committed state, keeps time like the tick, draws, and
never lets the stream key into its messages."""
import datetime as dt
import shutil

import pytest

import stream
import tick
from admission import Comment

KEY = b"test-state-key-0123456789"
LAUNCH = 1_800_000_000.0


class FakeYT:
    def __init__(self, comments):
        self.comments = comments

    def fetch_comments(self, since, watch=(), now=None):
        return [c for c in self.comments if c.published_at <= iso(now)] if now else list(self.comments)

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
    yt = FakeYT([Comment("c1", "u1", iso(LAUNCH + 900), "glider 64,71 nw"),
                 Comment("c2", "u2", iso(LAUNCH + 2500), "hello")])
    for t in range(0, 3600, 600):
        tick.run(str(tmp_path), LAUNCH + t, lambda w: yt, KEY, thumb_path=None)
    return str(tmp_path)


def test_load_steps_to_the_wall_clock_and_finds_new_moves(state_dir):
    first = stream.load(state_dir, now=LAUNCH + 3000)
    assert first.u.generation == 25 and first.video == stream.MAIN_VIDEO and first.universe_no == 1
    assert first.new == []  # only moves after `after_seq` are new
    later = stream.load(state_dir, after_seq=0, now=LAUNCH + 3600 + 250)
    assert later.u.generation == 32  # the last tick stopped at gen 25; the clock says 32
    assert [e["command"].split()[0] for e in later.new] == ["glider", "random"]
    assert all(e["cells"] for e in later.new)
    assert stream.load(state_dir, after_seq=later.seq, now=LAUNCH + 3600).new == []


def test_a_frame_is_full_size_and_shows_a_ripple(state_dir):
    snap = stream.load(state_dir, now=LAUNCH + 3700)
    screen = stream.Screen(snap)
    newer = stream.load(state_dir, after_seq=0, now=LAUNCH + 3700)
    screen.swap(newer, LAUNCH + 3700)
    assert len(screen.tags) == len(newer.new) >= 1
    assert screen.animating(LAUNCH + 3701) and not screen.animating(LAUNCH + 3700 + stream.RIPPLE_S + 1)
    im = screen.compose(LAUNCH + 3701)
    assert im.size == (stream.SW, stream.SH)
    # the clock moves the board on by itself
    gen = screen.snap.u.generation
    screen.tick(LAUNCH + 3700 + 2 * 120)
    assert screen.snap.u.generation == gen + 2


def test_centre_px_handles_the_wrap():
    # a glider straddling the right edge is centred near that edge, not mid-board
    cx, _ = stream.centre_px([(127, 5), (0, 5), (1, 6)], (72, 128))
    cell, left, _ = stream.geometry((72, 128))
    assert abs(cx - (left + 128 * cell)) < 2 * cell or abs(cx - left) < 2 * cell


def test_effect_colours():
    assert stream.effect_colour("2d 23h → 8d 22h") == stream.PHASE_COLOR["ALIVE"]
    assert stream.effect_colour("8d 21h → 2d 23h") == (240, 90, 90)
    assert stream.effect_colour("still 3d 0h") == stream.MUTED
    assert stream.effect_colour("3d 0h → 42d+") == stream.PHASE_COLOR["ALIVE"]


def test_the_stream_key_is_never_logged():
    assert stream.redact("rtmps://a.rtmps.youtube.com/live2/abcd-1234: I/O error", "abcd-1234") == \
        "rtmps://a.rtmps.youtube.com/live2/[stream key]: I/O error"
    cmd = stream.ffmpeg_cmd(stream.INGEST + "abcd-1234", 5500)
    assert cmd[-1].endswith("abcd-1234") and "-f" in cmd and "flv" in cmd


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_streams_to_a_file(state_dir, tmp_path):
    import os
    repo = tmp_path / "repo"
    shutil.copytree(state_dir, repo / "state")
    out = str(tmp_path / "s.flv")
    assert stream.stream(str(repo), out, None, 1.0, False, 2000) == 0
    assert os.path.getsize(out) > 10_000
