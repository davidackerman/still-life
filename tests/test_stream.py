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


def test_it_says_when_comments_were_last_checked(state_dir):
    snap = stream.load(state_dir, now=LAUNCH + 3700)
    assert snap.checked == LAUNCH + 3000  # the last tick ran at +3000
    assert stream.checked_text(snap.checked, LAUNCH + 3030) == "chat checked just now"
    assert stream.checked_text(snap.checked, LAUNCH + 3000 + 4 * 60 + 5) == "chat checked 4 min ago"
    assert stream.checked_text(None, LAUNCH) == ""


def test_the_soundtrack_follows_the_board():
    import numpy as np
    import stream_audio as sa
    s = sa.Soundtrack()
    s.set_phase("ALIVE")
    n = sa.SR // 10
    quiet = np.frombuffer(b"".join(s.chunk(n) for _ in range(10)), "<i2")  # a second: pulse and chord only
    assert len(quiet) == 2 * n * 10 and np.abs(quiet).max() < 32767
    before = np.zeros((72, 128), np.uint8)
    after = before.copy()
    after[10:14, 20:24] = 1  # 16 cells born
    s.generation(before, after)
    loud = np.frombuffer(b"".join(s.chunk(n) for _ in range(10)), "<i2")
    assert np.sqrt((loud.astype(float) ** 2).mean()) > 2 * np.sqrt((quiet.astype(float) ** 2).mean())
    s2 = sa.Soundtrack()
    s2.set_phase("ALIVE")
    s2.generation(before, after, steps=sa.MAX_STEP + 1)  # a jump, not a generation: no notes
    assert not s2.buf.any()
    for _ in range(20):
        s2.landed(5, 128)  # a burst of comments: chimes, but not piled up for ever
    assert s2.last_chime - s2.t <= 3 * sa.SR


def test_streams_with_sound_to_a_file(state_dir, tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    shutil.copytree(state_dir, repo / "state")
    out = tmp_path / "s.mp4"
    assert stream.stream(str(repo), str(out), None, 2.0, False, 800, sound=True) == 0
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,sample_rate",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout
    assert "audio,48000" in probe and "video" in probe


def test_rewinds_run_at_the_hour_and_half_hour():
    assert stream.rewind_slot(LAUNCH + 1800 + 5) == LAUNCH + 1800  # LAUNCH is on the hour
    assert stream.rewind_slot(LAUNCH + 1800 + stream.REWIND_S + 1) is None


def test_the_rewind_ends_on_the_live_board(state_dir):
    import numpy as np
    snap = stream.load(state_dir, now=LAUNCH + 3700)
    r = stream.Rewind(state_dir, 0.0, snap.u.generation)
    r.advance(stream.REWIND_S / 2)
    assert 0 < r.u.generation < snap.u.generation
    r.advance(stream.REWIND_S)
    assert r.u.generation == snap.u.generation and np.array_equal(r.u.grid, snap.u.grid)
    assert r.done(stream.REWIND_S)


def test_the_screen_rewinds_then_goes_live_again(state_dir):
    import stream_audio
    now = LAUNCH + 3600 + 10  # 10 s into the rewind that began on the hour
    screen = stream.Screen(stream.load(state_dir, now=now))
    screen.sound = stream_audio.Soundtrack()
    screen.tick(now, state_dir)
    assert screen.rewind is not None and screen.animating(now)
    assert screen.compose(now).size == (stream.SW, stream.SH)
    screen.tick(now + stream.REWIND_S, state_dir)
    assert screen.rewind is None  # live again
    screen.tick(now + stream.REWIND_S + 1, state_dir)
    assert screen.rewind is None  # one rewind per slot


def test_the_puller_only_pulls_when_github_has_a_new_commit(tmp_path):
    import subprocess

    def git(*a, cwd=tmp_path):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True,
                       capture_output=True)

    def commit(repo, text):
        (repo / "f").write_text(text)
        git("add", "f", cwd=repo)
        git("commit", "-qm", text, cwd=repo)
        git("push", "-q", "origin", "main", cwd=repo)
    git("init", "-q", "--bare", "-b", "main", "origin")
    for name in ("work", "other"):
        git("init", "-q", "-b", "main", name)
        git("remote", "add", "origin", str(tmp_path / "origin"), cwd=tmp_path / name)
    commit(tmp_path / "work", "1")
    p = stream.Puller(str(tmp_path / "work"), str(tmp_path / "work"), 0, pull=False)
    assert p._unchanged()
    git("pull", "-q", "origin", "main", cwd=tmp_path / "other")
    commit(tmp_path / "other", "2")
    assert not p._unchanged()
    assert not stream.Puller(str(tmp_path / "nowhere"), str(tmp_path), 0, pull=False)._unchanged()  # can't tell: pull


def test_labels_never_hunt_for_a_free_row_for_ever(state_dir):
    """Ten moves at once near the bottom edge (03:24 UTC, Nov 11) froze the stream:
    the old loop sent a label back and forth between two blocked rows."""
    import signal
    assert stream.free_row(500, 0, 100, [(0, 100, 500), (0, 100, 534), (0, 100, 466)], 0, 520) == 432
    assert stream.free_row(500, 0, 100, [(0, 100, y) for y in range(0, 1000, 10)], 0, 520) == 500  # none free
    now = LAUNCH + 3700
    screen = stream.Screen(stream.load(state_dir, now=now))
    h, w = screen.snap.u.grid.shape
    screen.tags = [stream.Tag(f"random 5x5 at {x},{y}", [(x, y)], now)  # same column, 2 rows apart, at the bottom
                   for x, y in [(6, 17), (64, h - 3), (64, h - 1), (90, h - 1)]]
    signal.signal(signal.SIGALRM, lambda *_: pytest.fail("a frame took over 5 s"))
    signal.alarm(5)
    try:
        assert screen.compose(now + 1).size == (stream.SW, stream.SH)
    finally:
        signal.alarm(0)


def test_a_frame_that_never_comes_ends_the_process(state_dir, tmp_path, monkeypatch):
    """No frame for STALL_S: exit, so systemd starts it again (YouTube ends a broadcast that gets nothing)."""
    import os
    import threading
    repo = tmp_path / "repo"
    shutil.copytree(state_dir, repo / "state")
    exits, exited = [], threading.Event()
    monkeypatch.setattr(stream, "STALL_S", 0.4)
    monkeypatch.setattr(os, "_exit", lambda code: (exits.append(code), exited.set()))
    real, calls = stream.Screen.compose, []

    def stuck(self, now):
        calls.append(now)
        if len(calls) > 1:  # the second frame never finishes
            exited.wait(10)
            raise BrokenPipeError
        return real(self, now)
    monkeypatch.setattr(stream.Screen, "compose", stuck)
    assert stream.stream(str(repo), str(tmp_path / "s.flv"), None, 30.0, False, 800, sound=False) == 1
    assert exits == [1]


def test_the_countdown_ticks_to_the_death_date_and_the_curve_jumps_at_moves(state_dir):
    assert stream.clock(3 * 86400 + 3723) == "3d 01:02:03" and stream.clock(59) == "00:00:59" and stream.clock(-5) == "00:00:00"
    now = LAUNCH + 3700
    snap = stream.load(state_dir, now=now)
    die = stream.death_gen(snap)
    assert snap.u.phase == "ALIVE" and die is not None
    left = int(snap.launch + die * snap.gen_seconds - now)
    assert stream.countdown_text(snap, now) == "in " + stream.clock(left)
    assert stream.countdown_text(snap, now + 1) == "in " + stream.clock(left - 1)  # it ticks
    # the curve: from launch to now, falling a generation per generation, jumping at the moves
    pts, jumps = stream.time_left_curve(snap)
    assert pts[0][0] == 0 and pts[-1] == (snap.u.generation, die - snap.u.generation)
    assert all(a[0] <= b[0] for a, b in zip(pts, pts[1:]))
    dies = [d for _, d in snap.deaths] + [die]
    assert snap.deaths and len(jumps) == sum(a != b for a, b in zip(dies, dies[1:]))  # one per move that changed it
    for (g0, v0), (g1, v1) in zip(pts, pts[1:]):
        if g1 > g0:
            assert v0 - v1 == g1 - g0  # between moves it just runs down
    snap.deaths = []  # no moves yet: a straight line
    assert stream.time_left_curve(snap)[1] == []


def test_the_bed_drifts_through_its_chords_and_darkens_when_stuck():
    import numpy as np
    import stream_audio as sa
    s = sa.Soundtrack()
    s.set_phase("ALIVE")
    chords = [s.chord(int(i * sa.CHORD_S * sa.SR)) for i in range(len(sa.PROGRESSION) + 1)]
    assert chords[:-1] == sa.PROGRESSION and chords[-1] == sa.PROGRESSION[0]  # and round again
    n = sa.SR // 10
    for _ in range(300):
        s.chunk(n)
    root, notes = sa.PROGRESSION[0]
    assert s.bed[("root", root)] > 0.9 and all(s.bed[("note", f)] > 0.9 for f in notes)
    s.set_phase("FLATLINING")
    for _ in range(40):  # four seconds: it has moved to the stuck chord
        s.chunk(n)
    root, notes = sa.HOLD["FLATLINING"]
    assert all(s.bed[("note", f)] > 0.9 for f in notes)
    assert all(g < 0.1 for (kind, f), g in s.bed.items() if kind == "note" and f not in notes)
    s.set_phase("DIED")
    for _ in range(60):
        s.chunk(n)
    assert all(g < 0.01 for g in s.bed.values())
    assert np.abs(np.frombuffer(s.chunk(n), "<i2")).max() < 60  # the bed is silent once it died
