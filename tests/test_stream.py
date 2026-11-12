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
  out.paint = Array.from(paint);
  for (let n = 0; n < 10; n++) {
    const next = LifeCore.step(grid, TL.h, TL.w);
    paint = History.paintStep(grid, next, paint, TL.h, TL.w);
    grid = next;
  }
  out.ahead = Array.from(paint);
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
def test_the_live_page_paints_the_same_cells_as_the_bot(tmp_path, monkeypatch):
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
    got = run_page(tmp_path, {"log": str(d / "log.jsonl"), "gen": h.now_gen})
    assert got["paint"] == u.paint.ravel().tolist()
    h.goto(u, h.now_gen + 10)
    assert got["ahead"] == u.paint.ravel().tolist()

    rng = np.random.default_rng(7)  # and the tie-breaks: a busy board, every cell a random color
    g = (rng.random((72, 128)) < 0.35).astype(np.uint8)
    paint = np.where(g == 1, rng.integers(0, 8, g.shape), 0).astype(np.uint8)
    got = run_page(tmp_path, {"grid": g.ravel().tolist(), "paint": paint.ravel().tolist(), "shape": [72, 128]})
    assert got["step"] == life.paint_step(g, life.step(g), paint).ravel().tolist()


def render_paint(name):
    import render
    return render.PAINT[name]


def state_dir_copy_without_color(tmp_path, d):
    out = tmp_path / "plain"
    shutil.copytree(d, out)
    log = (out / "log.jsonl").read_text().replace('"color": "red", ', "").replace(', "color": "red"', "")
    (out / "log.jsonl").write_text(log)
    return str(out)
