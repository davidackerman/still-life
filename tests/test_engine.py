import engine
import life
from admission import Comment
from universe import Universe, flatline_gens

LAUNCH = 1_800_000_000.0


class FakeYT:
    def __init__(self, comments=(), fail=()):
        self.comments, self.fail = list(comments), set(fail)
        self.titles, self.thumbs = [], 0

    def fetch_comments(self, since):
        if "fetch" in self.fail:
            raise RuntimeError("fetch failed")
        return self.comments

    def set_title(self, t, grid_text=""):
        if "title" in self.fail:
            raise RuntimeError("title failed")
        self.titles.append(t)

    def set_thumbnail(self, path):
        if "thumb" in self.fail:
            raise RuntimeError("thumb failed")
        self.thumbs += 1


def world():
    return engine.World(Universe(life.mask(life.centered("acorn")), flatline_gens()), LAUNCH, key=b"k" * 16)


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
    assert ev == []  # nothing happened, so nothing is logged
    assert len(yt.titles) == 1 and yt.thumbs == 1


def test_injection_is_applied_before_stepping():
    w, yt = world(), FakeYT([comment("a")])
    ev = engine.tick(w, LAUNCH + 600, yt)
    inj = [e for e in ev if e["type"] == "injection"][0]
    assert inj["gen"] == 0 and inj["run_at"] == LAUNCH + 600 and w.universe.generation == 5
    assert set(inj) == {"type", "gen", "command", "cells", "run_at", "changed", "hash_after"}


def test_youtube_failures_never_touch_the_simulation():
    ok, bad = world(), world()
    yt_ok = FakeYT([comment("a")])
    yt_bad = FakeYT([comment("a")], fail={"title", "thumb"})
    ev = []
    for t in range(0, 7200, 300):
        engine.tick(ok, LAUNCH + t, yt_ok)
        ev += engine.tick(bad, LAUNCH + t, yt_bad)
    assert ok.universe.hash == bad.universe.hash and ok.universe.generation == bad.universe.generation
    assert {e["op"] for e in ev if e["type"] == "youtube_error"} == {"set_title", "set_thumbnail"}
    assert bad.last_title is None  # will retry on a later run
    assert set(bad.backoff) == {"set_title", "set_thumbnail"}


def test_fetch_failure_skips_input_but_still_steps():
    w = world()
    ev = engine.tick(w, LAUNCH + 1200, FakeYT([comment("a")], fail={"fetch"}))
    assert w.universe.generation == 10 and not [e for e in ev if e["type"] == "injection"]
    assert not w.admission.seen  # picked up on a later run


def test_title_at_most_every_15_minutes_and_only_when_changed():
    w, yt = world(), FakeYT()
    stamps = []
    real = yt.set_title
    yt.set_title = lambda t, g="": (stamps.append(w_now[0]), real(t, g))
    w_now = [0]
    for t in range(0, 12 * 3600, 300):  # 12 hours, a run every 5 minutes
        w_now[0] = t
        engine.tick(w, LAUNCH + t, yt)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert stamps and min(gaps) >= 15 * 60
    assert all(a != b for a, b in zip(yt.titles, yt.titles[1:]))  # never re-sent unchanged
    assert len(yt.titles) >= 40  # the quarter-hour clock changes at nearly every allowed slot


def test_thumbnail_every_3_hours_and_on_phase_change():
    w, yt = world(), FakeYT()
    for t in range(0, 4 * 86400, 300):
        engine.tick(w, LAUNCH + t, yt)
    # acorn: FLATLINING at gen 1855 (61.83 h), DIED at gen 2575 (85.83 h).
    # 0, 3, ..., 60 h (21) + FLATLINING (1) + 64.83 ... 82.83 h (7) + DIED (1);
    # nothing after death even though the loop runs to 96 h.
    assert yt.thumbs == 30


def test_failures_back_off_exponentially_and_reset_on_success():
    w, yt = world(), FakeYT(fail={"thumb"})
    engine.tick(w, LAUNCH, yt)
    assert w.backoff["set_thumbnail"] == {"until": LAUNCH + 900, "failures": 1}
    engine.tick(w, LAUNCH + 300, yt)  # inside the backoff: not even attempted
    assert w.backoff["set_thumbnail"]["failures"] == 1
    engine.tick(w, LAUNCH + 900, yt)
    assert w.backoff["set_thumbnail"] == {"until": LAUNCH + 900 + 1800, "failures": 2}
    yt.fail = set()
    engine.tick(w, LAUNCH + 2700, yt)
    assert "set_thumbnail" not in w.backoff and yt.thumbs == 1


def test_quota_skip_is_not_a_failure():
    class Skip(Exception):
        skip = True

    class QuotaYT(FakeYT):
        def set_title(self, t, grid_text=""):
            raise Skip("over budget")

    w = world()
    ev = engine.tick(w, LAUNCH, QuotaYT())
    assert any(e["type"] == "display_skipped" for e in ev) and "set_title" not in w.backoff


def test_last_changer_is_keyed_not_raw():
    w = world()
    engine.tick(w, LAUNCH + 600, FakeYT([comment("Ugx-raw-id", "UC-raw-author")]))
    lc = w.last_changer
    assert lc["command"] == "glider 10,10 se" and "Ugx-raw-id" not in str(lc) and "UC-raw-author" not in str(lc)


def test_world_state_roundtrip():
    w, yt = world(), FakeYT([comment("a"), comment("b", "u2", "hello")])
    for t in range(0, 3 * 3600, 300):
        engine.tick(w, LAUNCH + t, yt)
    import json
    d, hist = w.to_state()
    w2 = engine.World.from_state(json.loads(json.dumps(d)), hist, w.key)
    assert snapshot(w2) == snapshot(w)
    # and both continue identically
    e1, e2 = engine.tick(w, LAUNCH + 4 * 3600, yt), engine.tick(w2, LAUNCH + 4 * 3600, yt)
    assert e1 == e2 and w.universe.hash == w2.universe.hash


def test_forecast_is_cached_until_a_comment_changes_the_board(monkeypatch):
    w = world()
    calls = []
    real = Universe.forecast_stuck
    monkeypatch.setattr(Universe, "forecast_stuck", lambda self, n: calls.append(self.generation) or real(self, n))
    yt = FakeYT()
    for t in range(0, 3600, 300):
        engine.tick(w, LAUNCH + t, yt)
    assert len(calls) == 1 and w.forecast["stuck_gen"] == 1855
    yt.comments = [comment("a")]
    engine.tick(w, LAUNCH + 3600, yt)
    assert len(calls) == 2 and w.forecast["stuck_gen"] != 1855
    assert yt.titles[-1].startswith("This video dies in ") and yt.titles[-1].endswith("if nobody comments · Game of Life")
