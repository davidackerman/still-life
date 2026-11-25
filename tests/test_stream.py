"""stream.py: loads the committed state, keeps time like the tick, draws, and
never lets the stream key into its messages."""
import datetime as dt
import os
import shutil

import numpy as np
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


def test_effect_colors():
    assert stream.effect_color("2d 23h → 8d 22h") == stream.PHASE_COLOR["ALIVE"]
    assert stream.effect_color("8d 21h → 2d 23h") == (240, 90, 90)
    assert stream.effect_color("still 3d 0h") == stream.MUTED
    assert stream.effect_color("3d 0h → 42d+") == stream.PHASE_COLOR["ALIVE"]


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
    assert np.abs(np.frombuffer(s.chunk(n), "<i2")).max() < 32767 * 10 ** (-45 / 20)  # silent (< -45 dBFS) once it died


def test_new_moves_replay_from_where_they_landed_then_go_live(state_dir):
    import numpy as np
    now = LAUNCH + 3700
    old = stream.load(state_dir, now=now)
    screen = stream.Screen(old)
    new = stream.load(state_dir, after_seq=0, now=now)  # both comments' moves are new
    screen.swap(new, now, state_dir)
    r = screen.replay
    assert r is not None and screen.snap is old  # the panel waits for the replay
    assert r.first == min(e["gen"] for e in new.new) and r.u.generation == r.first and not screen.tags
    assert r.end - r.start <= stream.REPLAY_MAX_S + 1e-6  # a long gap plays faster
    screen.tick(now + stream.REPLAY_HOLD_S + 0.01)
    assert len(screen.tags) == 1 and screen.animating(now + 2)  # the first one has landed
    assert screen.compose(now + 2).size == (stream.SW, stream.SH)  # with its banner
    t = now
    while screen.replay and t < now + 30:
        t += 0.1
        screen.tick(t)
    assert screen.replay is None and screen.snap is new and len(screen.tags) == len(new.new)
    assert np.array_equal(r.u.grid, new.u.grid)  # it ends on the live board
    # an update in the middle of a replay finishes it first
    screen = stream.Screen(old)
    screen.swap(new, now, state_dir)
    screen.swap(stream.load(state_dir, after_seq=new.seq, now=now), now + 1, state_dir)
    assert screen.replay is None and len(screen.tags) == len(new.new)


def test_latest_moves_say_how_long_ago_they_came_in(state_dir):
    assert stream.ago(30) == "just now" and stream.ago(12 * 60 + 5) == "12 min ago"
    assert stream.ago(2 * 3600 + 300) == "2h 05m ago" and stream.ago(5 * 3600 + 900) == "5h ago"
    assert stream.ago(86400 + 3 * 3600) == "1d 3h ago" and stream.ago(-5) == "just now"
    snap = stream.load(state_dir, now=LAUNCH + 3700)
    # an effect long enough to push the age onto its own line, and a line with no generation
    snap.recent = ["gen 7: glider 64,71 nw · 2d 17h → 2d 22h", "gen 20: random 5x5 at 1,2 · " + "very " * 30,
                   "a line with no generation"] + snap.recent
    assert stream.Screen(snap).compose(LAUNCH + 3700).size == (stream.SW, stream.SH)


def test_the_rewind_is_counted_down_and_the_graph_and_labels_follow_the_past(state_dir):
    slot = LAUNCH // stream.REWIND_EVERY_S * stream.REWIND_EVERY_S + stream.REWIND_EVERY_S
    assert stream.rewind_text(slot + 10) == "rewinding: the story so far"
    assert stream.rewind_text(slot + 600) == "next rewind in 20:00"
    assert stream.rewind_text(slot - 61) == "next rewind in 1:01"
    pts = [(0, 100), (50, 50), (50, 80), (100, 30)]  # a fall, a jump at 50, a fall
    assert stream.curve_until(pts, 25) == [(0, 100), (25, 75.0)]
    assert stream.curve_until(pts, 50) == [(0, 100), (50, 50), (50, 80)]  # the jump has happened
    assert stream.curve_until(pts, 75) == [(0, 100), (50, 50), (50, 80), (75, 55.0)]
    assert stream.curve_until(pts, 200) == pts
    assert stream.curve_from(pts, 75) == [(75, 55.0), (100, 30)]
    lo, hi, mean = stream.typical(pts, 0, 100, 2)[1]  # 50 to 100: the jump to 80, then down to 30
    assert (lo, hi) == (30, 80) and 50 < mean < 60
    snap = stream.load(state_dir, now=LAUNCH + 3700)
    screen = stream.Screen(snap)
    screen.rewind = r = stream.Rewind(state_dir, slot, snap.u.generation)
    assert set(r.big) <= set(r.h.effects) and len(r.big) <= stream.REWIND_TAGS
    seen, t = [], slot
    while not r.done(t):
        r.advance(t)
        seen += [tg.text for tg in r.tags if tg.text not in seen]
        assert all(t - tg.t0 < stream.REWIND_TAG_S for tg in r.tags)  # each label goes after a few seconds
        if r.tags:
            assert screen.compose(t).size == (stream.SW, stream.SH)  # the graph drawn every frame
        t += 0.1
    assert len(seen) == len(r.big) >= 1 and all(" · " in s for s in seen)  # 'glider … · 2d → 5d'


def test_the_future_races_into_the_loop_goes_grey_and_a_move_ends_it(state_dir):
    slot = ((LAUNCH + 3700) // stream.REWIND_EVERY_S + 1) * stream.REWIND_EVERY_S + stream.FUTURE_AT_S
    assert stream.future_slot(slot + 1) == slot and stream.future_slot(slot + stream.FUTURE_S + 1) is None
    assert stream.future_slot(slot - stream.FUTURE_AT_S) is None  # :00 and :30 are the rewind's
    snap = stream.load(state_dir, now=slot - 1)
    stuck = snap.u.forecast_stuck(stream.FORECAST_GENS)
    assert stuck is not None
    snap.forecast = {"stuck_gen": stuck}
    screen = stream.Screen(snap)
    screen.tick(slot + 0.1, state_dir)
    f, t, parts = screen.future, slot + 0.1, []
    assert f is not None and f.to == stuck and f.dies == stuck + snap.u.flatline_gens
    while screen.future is not None:
        parts.append(f.part(t))
        if len(parts) % 50 == 0:
            assert screen.compose(t).size == (stream.SW, stream.SH)  # the banner, every part
        t += 0.1
        screen.tick(t, state_dir)
    assert parts[0] == "race" and "loop" in parts and parts[-1] == "dies"
    assert f.u.generation >= stuck and f.u.phase == stream.DIED  # into the loop, then grey
    assert snap.u.generation < stuck and snap.u.phase == "ALIVE"  # the live board never moved on
    screen.tick(t + 1, state_dir)
    assert screen.future is None  # once a slot
    old = stream.load(state_dir, now=slot - 1)  # someone comments while it plays: that future is gone
    screen = stream.Screen(old)
    screen.tick(slot + 1, state_dir)
    assert screen.future is not None
    screen.swap(stream.load(state_dir, after_seq=0, now=slot - 1), slot + 2, state_dir)
    assert screen.future is None


def test_the_panel_shows_a_replays_moves_as_they_land(state_dir):
    now = LAUNCH + 3700
    old = stream.load(state_dir, now=now)
    new = stream.load(state_dir, after_seq=0, now=now)
    screen = stream.Screen(old)
    screen.swap(new, now, state_dir)
    r = screen.replay
    assert screen.panel() is old and r.shown == r.first - 1 and not r.landed()
    last = max(t for t, g in r.steps if g in r.new)
    screen.tick(last - 0.01)
    assert screen.panel() is old  # one still to land
    screen.tick(last + 0.01)
    assert r.landed() and screen.panel() is new and screen.snap is old  # the countdown jumps now
    assert r.shown == max(r.new)
    assert screen.compose(last + 0.5).size == (stream.SW, stream.SH)
    order = []  # the banner is drawn last, over the move labels, and the labels steer clear of it
    moves, banner = screen._moves, screen._replay_banner
    screen._moves = lambda *a, **k: (order.append(("moves", k.get("avoid"))), moves(*a, **k))[1]
    screen._replay_banner = lambda *a, **k: (order.append(("banner", None)), banner(*a, **k))[1]
    screen.compose(last + 0.6)
    assert [o for o, _ in order] == ["moves", "banner"] and order[0][1]


def test_the_legend_films_move_and_redraw_the_frame(state_dir):
    snap = stream.load(state_dir, now=LAUNCH + 3700)
    screen = stream.Screen(snap)
    t = LAUNCH + 3700.0
    a = screen.compose(t)
    assert len(screen._demos) == len(stream.LEGEND)
    k = screen.key(t)
    assert screen.key(t + 1 / stream.DEMO_RATE) != k  # the loop draws a new frame for each film frame
    loop = len(screen._demos[0][2])
    pics = {screen.compose(t + j / stream.DEMO_RATE).tobytes() for j in range(0, loop, 4)}
    assert len(pics) > 3  # the films move


def test_the_stream_paints_the_cells_and_keeps_the_paint_as_they_move(tmp_path, monkeypatch):
    import numpy as np
    import short
    for k in ("SEED", "GEN_SECONDS", "FLATLINE_HOURS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LAUNCH_TIME", str(int(LAUNCH)))
    d = str(tmp_path)
    yt = FakeYT([Comment("c1", "u1", iso(LAUNCH + 900), "glider 64,60 nw red"),
                 Comment("c2", "u2", iso(LAUNCH + 1000), "no color here")])
    for t in range(0, 3600, 600):
        tick.run(d, LAUNCH + t, lambda w: yt, KEY, thumb_path=None)
    snap = stream.load(d, now=LAUNCH + 3600 + 1200)  # ten generations past the last tick
    assert snap.paint is not None and (snap.paint == 1).any()  # red
    h = short.History(d)
    u = h.fresh()
    h.goto(u, snap.u.generation)
    assert np.array_equal(u.grid, snap.u.grid) and np.array_equal(u.paint, snap.paint)  # stepped like the log says
    img = stream.board_rgb(snap.u.grid, snap.u.phase, None, snap.paint)
    cell, _, _ = stream.geometry(snap.u.grid.shape)
    y, x = np.argwhere(snap.paint == 1)[0]
    assert tuple(img[y * cell + cell // 2, x * cell + cell // 2]) == render_paint("red")
    assert stream.Screen(snap).compose(LAUNCH + 3600 + 1200).size == (stream.SW, stream.SH)
    lit = np.ones_like(snap.u.grid)  # a new move's glow: white, but a color shows from the moment it lands
    img = stream.board_rgb(snap.u.grid, snap.u.phase, lit, snap.paint)
    assert tuple(img[y * cell + cell // 2, x * cell + cell // 2]) == render_paint("red")
    y, x = np.argwhere((snap.u.grid == 1) & (snap.paint == 0))[0]
    assert tuple(img[y * cell + cell // 2, x * cell + cell // 2]) == tuple(stream.HIGHLIGHT)
    plain = stream.load(state_dir_copy_without_color(tmp_path, d), now=LAUNCH + 3600)
    assert plain.paint is None  # nothing painted: nothing replayed


NODE_PAGE = """
const fs = require("fs"), docs = process.argv[2];
global.LifeCore = require(docs + "/life.js");
const History = require(docs + "/history.js");
const job = JSON.parse(fs.readFileSync(process.argv[3], "utf8")), out = {};
if (job.log) {  // the page's replay of the log, then 10 generations stepped on as the page does live
  const TL = new History.Timeline(History.parse(fs.readFileSync(job.log, "utf8")));
  let grid = TL.gridAt(job.gen), paint = TL.paintAt(job.gen);
  out.grid = Array.from(grid);
  out.paint = Array.from(paint);
  for (let n = 0; n < 10; n++) {
    const next = LifeCore.step(grid, TL.h, TL.w);
    paint = History.paintStep(grid, next, paint, TL.h, TL.w);
    grid = next;
  }
  out.ahead = Array.from(paint);
  out.without = Array.from(TL.without(job.g0, job.g0 + 10));  // as if the moves at g0 never landed
}
if (job.grid) {  // one step of a given board and paint
  const [h, w] = job.shape, g = Uint8Array.from(job.grid);
  out.step = Array.from(History.paintStep(g, LifeCore.step(g, h, w), Uint8Array.from(job.paint), h, w));
}
console.log(JSON.stringify(out));
"""


def run_page(tmp_path, job):
    import json
    import os
    import subprocess
    (tmp_path / "page.js").write_text(NODE_PAGE)
    (tmp_path / "job.json").write_text(json.dumps(job))
    docs = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
    r = subprocess.run(["node", str(tmp_path / "page.js"), docs, str(tmp_path / "job.json")],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the live page's JavaScript")
def test_the_live_page_paints_the_same_cells_as_the_bot_and_finds_what_each_move_did(tmp_path, monkeypatch):
    import numpy as np
    import life
    import short
    for k in ("SEED", "GEN_SECONDS", "FLATLINE_HOURS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LAUNCH_TIME", str(int(LAUNCH)))
    d = tmp_path / "state"
    yt = FakeYT([Comment("c1", "u1", iso(LAUNCH + 900), "glider 64,60 nw red"),
                 Comment("c2", "u2", iso(LAUNCH + 1000), "acorn 70,40 blue"),
                 Comment("c3", "u3", iso(LAUNCH + 1300), "no color here"),
                 Comment("c4", "u4", iso(LAUNCH + 2000), "rpent 60,44 purple")])
    for t in range(0, 7200, 600):
        tick.run(str(d), LAUNCH + t, lambda w: yt, KEY, thumb_path=None)
    h = short.History(str(d))
    u = h.fresh()
    h.goto(u, h.now_gen)
    assert len(set(u.paint[u.grid == 1].tolist())) >= 3  # several colors, and cells with none
    g0 = sorted(h.batches)[1]
    got = run_page(tmp_path, {"log": str(d / "log.jsonl"), "gen": h.now_gen, "g0": g0})
    assert got["paint"] == u.paint.ravel().tolist()
    h.goto(u, h.now_gen + 10)
    assert got["ahead"] == u.paint.ravel().tolist()
    h = short.History(str(d))  # what the page shows a move did: the same board without it
    del h.batches[g0]
    u = h.fresh()
    h.goto(u, g0 + 10)
    assert got["without"] == u.grid.ravel().tolist() and 0 < sum(got["without"]) < u.grid.size

    rng = np.random.default_rng(7)  # and the tie-breaks: a busy board, every cell a random color
    g = (rng.random((72, 128)) < 0.35).astype(np.uint8)
    paint = np.where(g == 1, rng.integers(0, 8, g.shape), 0).astype(np.uint8)
    got = run_page(tmp_path, {"grid": g.ravel().tolist(), "paint": paint.ravel().tolist(), "shape": [72, 128]})
    assert got["step"] == life.paint_step(g, life.step(g), paint).ravel().tolist()


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the live page's JavaScript")
def test_black_erases_the_same_cells_for_the_bot_the_log_the_live_page_and_the_stream(tmp_path, monkeypatch):
    import replay
    import render
    import short
    for k in ("SEED", "GEN_SECONDS", "FLATLINE_HOURS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LAUNCH_TIME", str(int(LAUNCH)))
    d = tmp_path / "state"
    # a red acorn, then a black block over it a few generations on, then a comment that only says black
    acorn = "acorn 70,40 red"
    yt = FakeYT([Comment("c1", "u1", iso(LAUNCH + 900), acorn)])
    for t in range(0, 1800, 600):
        tick.run(str(d), LAUNCH + t, lambda w: yt, KEY, thumb_path=None)
    h = short.History(str(d))
    u = h.fresh()
    h.goto(u, h.now_gen)
    ys, xs = np.nonzero(u.grid)
    aim = f"block {xs[0]},{ys[0]}"
    yt.comments += [Comment("c2", "u2", iso(LAUNCH + 1900), f"{aim} black"),
                    Comment("c3", "u3", iso(LAUNCH + 1950), "black cats are cool")]
    for t in range(1800, 4800, 600):
        tick.run(str(d), LAUNCH + t, lambda w: yt, KEY, thumb_path=None)
    replay.verify(str(d))  # the public log still replays to the bot's state
    h = short.History(str(d))
    moves = [e for g in sorted(h.batches) for e in h.batches[g]]
    eraser = next(e for e in moves if e.get("erase"))
    assert eraser["changed"] and eraser["killed"] > 0 and render.move_text(eraser) == f"{aim} black"
    assert "erased" in render.move_label(eraser)
    said = next(e for e in moves if e is not eraser and e["command"].startswith("random"))
    assert not said.get("erase")  # no pattern named: it added cells as usual
    u = h.fresh()
    h.goto(u, h.now_gen)
    got = run_page(tmp_path, {"log": str(d / "log.jsonl"), "gen": h.now_gen, "g0": eraser["gen"]})
    assert got["grid"] == u.grid.ravel().tolist() and got["paint"] == u.paint.ravel().tolist()
    snap = stream.load(str(d), now=LAUNCH + 4800)  # stepped on to the wall clock
    h.goto(u, snap.u.generation)
    assert snap.u.generation > eraser["gen"] and np.array_equal(snap.u.grid, u.grid)
    assert stream.Screen(snap).compose(LAUNCH + 4800).size == (stream.SW, stream.SH)


def render_paint(name):
    import render
    return render.PAINT[name]


def state_dir_copy_without_color(tmp_path, d):
    out = tmp_path / "plain"
    shutil.copytree(d, out)
    log = (out / "log.jsonl").read_text().replace('"color": "red", ', "").replace(', "color": "red"', "")
    (out / "log.jsonl").write_text(log)
    return str(out)



def test_the_rewind_says_what_was_new_as_it_passes_it(state_dir):
    import json
    import os
    docs = os.path.join(os.path.dirname(os.path.abspath(state_dir)), "docs")
    os.makedirs(docs, exist_ok=True)
    with open(os.path.join(docs, "changes.json"), "w") as f:
        json.dump([{"at": LAUNCH + 600, "text": "Colors arrive"}], f)
    snap = stream.load(state_dir, now=LAUNCH + 3700)
    screen = stream.Screen(snap)
    slot = LAUNCH // stream.REWIND_EVERY_S * stream.REWIND_EVERY_S + stream.REWIND_EVERY_S
    screen.rewind = r = stream.Rewind(state_dir, slot, snap.u.generation)
    assert r.news == [(5, "Colors arrive")]  # 600 s in: generation 5
    t = slot
    while not r.news_shown:
        t += 0.1
        r.advance(t)
    assert r.news_shown[0] == "Colors arrive" and r.u.generation >= 5
    assert screen.compose(t).size == (stream.SW, stream.SH)
    assert stream.changes("/nonexistent/state", 120) == []



@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the live page's JavaScript")
def test_the_live_pages_little_films_are_the_streams(tmp_path):
    import json
    import os
    import subprocess
    import render
    names = ["glider_se", "block", "blinker", "rpent", "acorn", "random", "cells"]
    script = tmp_path / "films.js"
    script.write_text("""
const docs = process.argv[2];
global.LifeCore = require(docs + "/life.js");
const History = require(docs + "/history.js");
const out = {};
for (const n of %s) out[n] = History.film(n, 9).map((f) => Array.from(f));
console.log(JSON.stringify(out));
""" % json.dumps(names))
    docs = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
    got = json.loads(subprocess.run(["node", str(script), docs], capture_output=True, text=True, check=True).stdout)
    for n in names:
        assert got[n] == [f.ravel().tolist() for f in render.demo(n, 9)], n


def test_an_erasers_shape_is_outlined_where_its_cells_died_and_fades():
    g = np.zeros((72, 128), np.uint8)
    g[10, 11] = 1  # one cell of the shape still alive (it was not hit, or came back)
    ghost = np.zeros_like(g)
    ghost[10, 10] = ghost[10, 11] = 1
    cell, = stream.geometry(g.shape)[:1]
    img = stream.board_rgb(g, "ALIVE", None, ghost=ghost)
    assert tuple(img[10 * cell + 1, 10 * cell + cell // 2]) == stream.GHOST  # the dead one: outlined
    assert tuple(img[10 * cell + cell // 2, 10 * cell + cell // 2]) != stream.GHOST  # hollow
    assert tuple(img[10 * cell + 1, 11 * cell + cell // 2]) != stream.GHOST  # the live one: drawn as usual
    faint = stream.board_rgb(g, "ALIVE", None, ghost=ghost, ghost_a=0.3)
    assert int(faint[10 * cell + 1, 10 * cell + cell // 2].astype(int).sum()) < int(img[10 * cell + 1, 10 * cell + cell // 2].astype(int).sum())
    assert np.array_equal(stream.board_rgb(g, "ALIVE", None, ghost=ghost, ghost_a=0.0), stream.board_rgb(g, "ALIVE", None))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_the_tall_stream_goes_to_a_file_at_its_own_size(state_dir, tmp_path):
    import os
    import subprocess
    import vertical
    repo = tmp_path / "repo"
    shutil.copytree(state_dir, repo / "state")
    out = str(tmp_path / "tall.flv")
    assert stream.stream(str(repo), out, None, 1.0, False, 1500, sound=False, tall=True) == 0
    size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", out], capture_output=True, text=True).stdout.strip()
    assert size == f"{vertical.TW},{vertical.TH}" and os.path.getsize(out) > 10_000
    assert "vertical.py" in vertical.CODE and "vertical.py" not in stream.CODE  # its changes restart only the tall one


def test_the_tall_frame_shows_the_board_the_moves_and_each_banner(state_dir):
    import vertical
    snap = stream.load(state_dir, now=LAUNCH + 3700)
    screen = stream.Screen(snap)
    e = [x for x in snap.new if x.get("cells")] or [{"command": "glider 64,71 nw", "cells": [[64, 71], [65, 71]]}]
    screen.tags.append(stream.Tag(render_move(e[0]), [tuple(c) for c in e[0]["cells"]], LAUNCH + 3699))
    im = vertical.compose(screen, LAUNCH + 3700)
    assert im.size == (vertical.TW, vertical.TH)
    slot = LAUNCH // stream.REWIND_EVERY_S * stream.REWIND_EVERY_S + stream.REWIND_EVERY_S
    screen.rewind = stream.Rewind(state_dir, slot, snap.u.generation)
    screen.rewind.advance(slot + 5)
    got = vertical._banner_lines(screen, slot + 5)
    assert got["line1"].startswith(("REWIND", "NEW")) and "_banner" not in vars(screen)  # the wide one's own, untouched
    assert vertical.compose(screen, slot + 5).size == (vertical.TW, vertical.TH)
    screen.rewind, screen.future = None, stream.Future(snap, LAUNCH + 3700)
    assert vertical._banner_lines(screen, LAUNCH + 3710)["line1"].startswith(("THE FUTURE", "STUCK", "IT"))
    assert vertical.compose(screen, LAUNCH + 3710).size == (vertical.TW, vertical.TH)


def render_move(e):
    import render
    return render.move_label(e)


# ---- the 3D look (STREAM_LOOK=3d, board3d.py)

def test_the_3d_board_fits_its_area_with_up_still_up():
    import board3d
    v = board3d.View((72, 128))
    x0, y0, x1, y1 = v.area
    (fl, fr, nr, nl) = v.edge  # far left, far right, near right, near left
    for x, y in v.edge:
        assert x0 + board3d.PAD - 1 <= x <= x1 - board3d.PAD + 1 and y0 + board3d.PAD - 1 <= y <= y1 - board3d.PAD + 1
    assert fl[1] < nl[1] and fr[1] < nr[1]  # row 0 is at the top, as on the flat board
    assert fl[0] < fr[0] and nl[0] < nr[0]  # column 0 on the left
    assert fr[0] - fl[0] < nr[0] - nl[0]  # tipped away: the far edge is the shorter
    assert v.cx < stream.COL_X - 20 and v.bottom < stream.BOARD_Y + stream.BOARD_H + 25  # clear of the column and the text


def test_a_window_onto_the_board_numbers_the_full_boards_columns_and_rows():
    import board3d

    def labels(v):
        out = []

        class D:
            def text(self, xy, s, **kw):
                out.append(s)
        v.numbers(D(), lambda weight, size: None)
        return set(out)
    assert labels(board3d.View((72, 128))) == {str(n) for n in range(0, 128, 16)} | {str(n) for n in range(0, 72, 8)} | {"x"}
    # the creature cam's window from column 125 (it wraps), row 1: every 4th of the full board's, as they are
    assert labels(board3d.View((22, 40), origin=(125, 1), full=(72, 128), steps=(4, 4))) == \
        {str(n) for n in range(0, 37, 4)} | {"x"}


def test_the_creature_cam_looks_up_close_for_a_while_every_few_minutes_while_nothing_plays(state_dir, monkeypatch):
    monkeypatch.setattr(stream, "LOOK", "3d")
    snap = stream.load(state_dir, now=LAUNCH + 3700)
    screen = stream.Screen(snap)
    t0 = (LAUNCH + 3700) // stream.CAM_EVERY_S * stream.CAM_EVERY_S + stream.CAM_EVERY_S
    screen._last_move = (t0 - 60, [(10, 20), (11, 20), (12, 20)])
    (x0, y0), v, a = screen.cam(t0 + 5)
    assert a == 1.0 and v.shape == (stream.CAM_H, stream.CAM_W) and v.origin == (x0, y0)
    assert all((x - x0) % 128 < stream.CAM_W and (y - y0) % 72 < stream.CAM_H for x, y in screen._last_move[1])
    assert 0 < screen.cam(t0 + 0.2)[2] < 1  # fading in
    assert screen.cam(t0 + stream.CAM_HOLD_S + 1) is None  # the whole board again
    assert screen.compose(t0 + 5).size == (stream.SW, stream.SH)
    assert screen.cam(t0 + stream.CAM_EVERY_S + 5)[0] != (x0, y0)  # the next visit: the move once, then elsewhere
    screen.replay = object()  # anything playing: the whole board, its labels on their cells
    assert screen.cam(t0 + 5) is None
    screen.replay = None
    monkeypatch.setattr(stream, "CAM", False)
    assert screen.cam(t0 + 5) is None


def test_in_3d_a_moves_label_and_ripple_land_on_its_cells(state_dir, monkeypatch):
    monkeypatch.setattr(stream, "LOOK", "3d")
    snap = stream.load(state_dir, now=LAUNCH + 3700)
    screen = stream.Screen(snap)
    assert screen.look3d
    v = screen.view(snap.u.grid.shape)
    cells = [(30, 20), (31, 20), (32, 20)]
    cx, cy = screen._centre(cells, snap.u.grid.shape)
    a, b = v.at([(31, 20), (32, 21)])  # the middle cell's far-left and near-right corners
    assert a[0] < cx < b[0] and a[1] < cy < b[1]
    wrapped = [(127, 5), (0, 5), (1, 5)]  # across the torus edge: the centre is cell 0, not the board's middle
    cx, _ = screen._centre(wrapped, snap.u.grid.shape)
    assert abs(cx - v.at([(0.5, 5.5)])[0, 0]) < 1
    # the frame: full size, with the latest moves' labels and ripples, live and while a rewind plays
    newer = stream.load(state_dir, after_seq=0, now=LAUNCH + 3700)
    screen.swap(newer, LAUNCH + 3700)
    assert screen.compose(LAUNCH + 3701).size == (stream.SW, stream.SH)
    now = LAUNCH + 3600 + 10
    screen = stream.Screen(stream.load(state_dir, now=now))
    screen.tick(now, state_dir)
    assert screen.rewind is not None and screen.compose(now).size == (stream.SW, stream.SH)


def test_3d_is_the_default_and_flat_still_draws(state_dir, monkeypatch):
    monkeypatch.delenv("STREAM_LOOK", raising=False)
    import importlib
    assert importlib.reload(stream).LOOK == "3d"
    monkeypatch.setattr(stream, "LOOK", "flat")
    screen = stream.Screen(stream.load(state_dir, now=LAUNCH + 3700))
    assert not screen.look3d and screen.compose(LAUNCH + 3700).size == (stream.SW, stream.SH)


def test_a_slow_3d_live_board_draws_simpler_then_flat(state_dir, monkeypatch):
    monkeypatch.setattr(stream, "LOOK", "3d")
    monkeypatch.setattr(stream, "LOOK_BUDGET_S", 0.0)  # every board too slow
    screen = stream.Screen(stream.load(state_dir, now=LAUNCH + 3700))
    seen = []
    for k in range(3 * stream.LOOK_SAMPLES):
        screen._timed(lambda: None)
        seen.append(screen.simpler)
    assert seen[stream.LOOK_SAMPLES - 1] == 1 and seen[2 * stream.LOOK_SAMPLES - 1] == 2
    assert screen.simpler == 3 and not screen.look3d  # flat, and it goes on drawing
    assert screen.compose(LAUNCH + 3700).size == (stream.SW, stream.SH)


def test_a_slow_machine_plays_the_past_in_3d_with_fewer_boards(state_dir, monkeypatch):
    import board3d
    monkeypatch.setattr(stream, "LOOK", "3d")
    now = LAUNCH + 3600 + 10  # a rewind: a new board every frame
    screen = stream.Screen(stream.load(state_dir, now=now))
    screen.tick(now, state_dir)
    drawn, real, clock = [], board3d.View.render, [0.0]

    def slow(self, *a, **k):  # each board takes 0.12 s (on the clock the screen times it by)
        drawn.append(k)
        clock[0] += 0.12
        return real(self, *a, **k)
    monkeypatch.setattr(board3d.View, "render", slow)
    monkeypatch.setattr(stream.time, "perf_counter", lambda: clock[0])
    for k in range(0, 11):
        assert screen.compose(now + k * 0.1).size == (stream.SW, stream.SH)
    assert 4 <= len(drawn) <= 6  # a board every other frame: 0.12 s of every 0.2 (PLAY_SHARE), and it keeps moving
    assert screen.look3d and screen.simpler == 0  # still 3D
    assert all(k.get("glow") is False and k.get("ss") == 1 for k in drawn)  # drawn plain


def test_in_3d_new_cells_rise_and_dying_ones_sink():
    import board3d
    import stream as st
    from universe import ALIVE
    v = board3d.View((72, 128))
    grid = np.zeros((72, 128), np.uint8)
    grid[30:32, 60:62] = 1  # a block, standing
    born = grid.copy()
    born[40, 40] = 1
    full = v.render(born, ALIVE, font=st.font, glow=False)
    assert np.array_equal(v.render(born, ALIVE, before=grid, rise=1.0, font=st.font, glow=False), full)
    half = v.render(born, ALIVE, before=grid, rise=0.5, font=st.font, glow=False)
    none = v.render(grid, ALIVE, font=st.font, glow=False)
    assert not np.array_equal(half, full) and not np.array_equal(half, none)  # on its way up
    sinking = v.render(grid, ALIVE, before=born, rise=0.5, font=st.font, glow=False)
    assert np.array_equal(sinking, half)  # dying sinks the way being born rises


def test_a_rewind_drawn_ahead_plays_from_its_frames(state_dir, tmp_path, monkeypatch):
    import board3d
    monkeypatch.setattr(stream, "LOOK", "3d")
    monkeypatch.setattr(stream, "CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(stream, "REWIND_S", 1.0)  # ten frames
    slot = LAUNCH + 3600.0
    out = stream.prebuild_rewind(_repo_of(state_dir, tmp_path), slot)
    assert sorted(n for n in os.listdir(out) if n.endswith(".png")) == sorted(f"{k}.png" for k in range(10))
    assert os.path.exists(os.path.join(out, "done"))
    screen = stream.Screen(stream.load(state_dir, now=slot))
    screen.tick(slot, state_dir)
    assert screen.rewind is not None
    drawn, real = [], board3d.View.render
    monkeypatch.setattr(board3d.View, "render", lambda self, *a, **k: drawn.append(1) or real(self, *a, **k))
    gens = []
    for k in range(10):
        screen.tick(slot + k * 0.1, state_dir)
        gens.append(screen.rewind.u.generation)
        assert screen.compose(slot + k * 0.1).size == (stream.SW, stream.SH)
    assert gens[-1] > gens[0] and drawn == []  # it played on, every board from the frames drawn ahead
    # a move logged since: from its generation on, the boards are drawn as they play
    screen = stream.Screen(stream.load(state_dir, now=slot))
    screen.tick(slot, state_dir)
    late = screen.rewind.end // 2
    screen.rewind.h.batches[late] = []
    for k in range(10):
        screen.tick(slot + k * 0.1, state_dir)
        screen.compose(slot + k * 0.1)
    assert 0 < len(drawn) < 10  # the early half from the frames, the late half drawn live


def _repo_of(state_dir, tmp_path):
    """prebuild_rewind takes the repo; the fixture's state is its own folder: a repo around a link to it."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    if not (repo / "state").exists():
        (repo / "state").symlink_to(state_dir)
    return str(repo)


def test_the_prebuilder_starts_once_per_rewind_and_clears_old_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(stream, "CACHE", str(tmp_path))
    started = []

    class Proc:
        def poll(self):
            return 0
    monkeypatch.setattr(stream.subprocess, "Popen", lambda cmd, **k: started.append(cmd) or Proc())
    (tmp_path / "rewind-100").mkdir()
    p = stream.Prebuilder("/repo")
    slot = 10 * stream.REWIND_EVERY_S
    p.poll(slot - stream.PREBUILD_LEAD_S - 5)
    assert started == []  # too early
    p.poll(slot - stream.PREBUILD_LEAD_S + 5)
    p.poll(slot - stream.PREBUILD_LEAD_S + 6)
    assert len(started) == 1 and started[0][-1] == str(int(slot)) and "--prebuild-rewind" in started[0]
    assert not (tmp_path / "rewind-100").exists()  # an old one, gone
