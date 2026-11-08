import engine
import life
from admission import Comment
from universe import Universe, flatline_gens

LAUNCH = 1_800_000_000.0


class FakeYT:
    def __init__(self, comments=(), fail=()):
        self.comments, self.fail = list(comments), set(fail)
        self.titles, self.thumbs = [], 0

    def fetch_comments(self):
        if "fetch" in self.fail:
            raise RuntimeError("fetch failed")
        return self.comments

    def set_title(self, t):
        if "title" in self.fail:
            raise RuntimeError("title failed")
        self.titles.append(t)

    def set_thumbnail(self, path):
        if "thumb" in self.fail:
            raise RuntimeError("thumb failed")
        self.thumbs += 1


def world():
    return engine.World(Universe(life.mask(life.centered("acorn")), flatline_gens()), LAUNCH)


def comment(cid, author="u1", text="glider 10,10"):
    return Comment(cid, author, "2027-01-15T08:00:00Z", text)


def snapshot(w):
    u = w.universe
    return (u.generation, u.phase, u.hash, u.deadline_gen, w.admission.to_dict(), w.last_title,
            w.last_thumb_at, sorted(w.highlight))


def test_tick_twice_at_the_same_time_changes_nothing():
    w, yt = world(), FakeYT([comment("a"), comment("b", "u2")])
    now = LAUNCH + 3600
    engine.tick(w, now, yt)
    before = snapshot(w)
    ev = engine.tick(w, now, yt)
    assert snapshot(w) == before
    assert [e["type"] for e in ev] == ["tick"]
    assert len(yt.titles) == 1 and yt.thumbs == 1


def test_injection_is_applied_before_stepping():
    w, yt = world(), FakeYT([comment("a")])
    ev = engine.tick(w, LAUNCH + 600, yt)
    inj = [e for e in ev if e["type"] == "injection"][0]
    assert inj["gen"] == 0 and inj["run"] == 1 and w.universe.generation == 5


def test_youtube_failures_never_touch_the_simulation():
    ok, bad = world(), world()
    yt_ok = FakeYT([comment("a")])
    yt_bad = FakeYT([comment("a")], fail={"title", "thumb"})
    for t in range(0, 7200, 300):
        engine.tick(ok, LAUNCH + t, yt_ok)
        ev = engine.tick(bad, LAUNCH + t, yt_bad)
    assert ok.universe.hash == bad.universe.hash and ok.universe.generation == bad.universe.generation
    assert {e["op"] for e in ev if e["type"] == "youtube_error"} == {"set_title", "set_thumbnail"}
    assert bad.last_title is None  # will retry on a later run


def test_fetch_failure_skips_input_but_still_steps():
    w = world()
    ev = engine.tick(w, LAUNCH + 1200, FakeYT([comment("a")], fail={"fetch"}))
    assert w.universe.generation == 10 and not [e for e in ev if e["type"] == "injection"]
    assert "a" not in w.admission.processed_ids  # picked up on a later run


def test_title_at_most_every_15_minutes():
    w, yt = world(), FakeYT()
    for t in range(0, 3600, 300):  # 12 runs, 5 minutes apart
        engine.tick(w, LAUNCH + t, yt)
    assert len(yt.titles) == 4  # minutes 0, 15, 30, 45


def test_thumbnail_every_3_hours_and_on_phase_change():
    w, yt = world(), FakeYT()
    for t in range(0, 4 * 86400, 300):
        engine.tick(w, LAUNCH + t, yt)
    # acorn: FLATLINING at gen 1855 (61.83 h), DIED at gen 2575 (85.83 h).
    # 0, 3, ..., 60 h (21) + FLATLINING (1) + 64.83 ... 82.83 h (7) + DIED (1);
    # nothing after death even though the loop runs to 96 h.
    assert yt.thumbs == 30
