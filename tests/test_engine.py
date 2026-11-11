import engine
import life
import render
from admission import Comment
from universe import Universe, flatline_gens

LAUNCH = 1_800_000_000.0


class FakeYT:
    def __init__(self, comments=(), fail=()):
        self.comments, self.fail = list(comments), set(fail)
        self.titles, self.thumbs = [], 0

    def fetch_comments(self, since, watch=(), now=None):
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


def test_title_at_most_every_45_minutes_while_alive_and_only_when_changed():
    w, yt = world(), FakeYT()
    stamps = []
    real = yt.set_title
    yt.set_title = lambda t, g="": (stamps.append(w_now[0]), real(t, g))
    w_now = [0]
    for t in range(0, 12 * 3600, 300):  # 12 hours, a run every 5 minutes
        w_now[0] = t
        engine.tick(w, LAUNCH + t, yt)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert stamps and min(gaps) >= 45 * 60  # writes are 50 units: paced by the clock, not by the runs
    assert all(a != b for a, b in zip(yt.titles, yt.titles[1:]))  # never re-sent unchanged
    assert len(yt.titles) >= 14  # the quarter-hour clock changes at nearly every allowed slot


def test_thumbnail_every_4_hours_and_on_phase_change():
    w, yt = world(), FakeYT()
    for t in range(0, 4 * 86400, 300):
        engine.tick(w, LAUNCH + t, yt)
    # acorn: FLATLINING at gen 1855 (61.83 h), DIED at gen 2575 (85.83 h).
    # Every 3 h, on each phase change, and (at most hourly) when the countdown
    # headline changes: days until the last day, then hours. Nothing after death.
    assert 20 <= yt.thumbs <= 26  # ~every 4 h + phase changes: the 429 limit is tight
    assert w.last_thumb_headline == "DIED"


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
    assert yt.titles[-1].startswith("This Game of Life video dies in ") and yt.titles[-1].endswith("if nobody comments")


def test_force_thumbnail_uploads_even_when_not_due():
    w, yt = world(), FakeYT()
    engine.tick(w, LAUNCH, yt)
    engine.tick(w, LAUNCH + 300, yt)
    assert yt.thumbs == 1
    engine.tick(w, LAUNCH + 600, yt, force_thumbnail=True)
    assert yt.thumbs == 2


class PinYT(FakeYT):
    def __init__(self, comments=()):
        super().__init__(comments)
        self.updates = []

    def update_comment(self, cid, text):
        self.updates.append((cid, text))


def owner_status(cid="Ugpin"):
    return Comment(cid, "UCowner", "2027-01-15T08:00:00Z", "Latest changes to the universe will appear here",
                   by_owner=True)


def test_owner_status_comment_is_found_and_not_played():
    w, yt = world(), PinYT([owner_status(), comment("a", "u1", "glider 10,10")])
    ev = engine.tick(w, LAUNCH + 600, yt)
    assert w.pinned_id == "Ugpin"
    assert [e["command"] for e in ev if e["type"] == "injection"] == ["glider 10,10 se"]  # the status comment adds nothing
    cid, text = yt.updates[-1]
    assert cid == "Ugpin" and text.startswith("🟢 ALIVE · this video dies in ~")
    assert "The video is the explainer." in text and "• gen 0: glider 10,10 se" in text
    assert "Try one: " in text and "davidackerman.github.io/still-life" in text
    assert "📺 Watch it live, 24/7, with sound, and see your comment land: https://www.youtube.com/channel/" in text


def test_owner_comments_without_the_marker_still_play():
    w, yt = world(), PinYT([Comment("o1", "UCowner", "2027-01-15T08:00:00Z", "block 5,5", by_owner=True)])
    ev = engine.tick(w, LAUNCH + 600, yt)
    assert [e["command"] for e in ev if e["type"] == "injection"] == ["block 5,5"] and w.pinned_id is None


def test_pinned_comment_updates_only_when_changed_and_at_most_every_45_minutes():
    w, yt = world(), PinYT([owner_status()])
    engine.tick(w, LAUNCH, yt)
    assert len(yt.updates) == 1 and "none yet" in yt.updates[0][1]
    engine.tick(w, LAUNCH + 300, yt)
    assert len(yt.updates) == 1  # nothing new
    yt.comments.append(comment("b", "u2", "rpent 90,20"))
    engine.tick(w, LAUNCH + 1800, yt)
    assert len(yt.updates) == 1  # new event, but too soon
    engine.tick(w, LAUNCH + 2700, yt)
    assert len(yt.updates) == 2 and "rpent 90,20" in yt.updates[-1][1]


def test_pinned_comment_lists_phase_changes_newest_first():
    w, yt = world(), PinYT([owner_status()])
    for t in range(0, 70 * 3600, 1800):  # past the acorn's loop at gen 1855 (61.8 h)
        engine.tick(w, LAUNCH + t, yt)
    assert w.recent[0] == "gen 1,855: got stuck in a loop: the 24-hour countdown started"


def test_pinned_link_can_be_turned_off():
    w, yt = world(), PinYT([owner_status()])
    engine.tick(w, LAUNCH, yt, pinned_link=False)
    assert "davidackerman.github.io" not in yt.updates[-1][1]


def test_status_comment_stays_out_of_play_after_its_text_changes():
    w, yt = world(), PinYT([owner_status()])
    engine.tick(w, LAUNCH, yt)
    # the bot rewrote it; the old marker phrase is gone, but it is still recognised by ID
    yt.comments = [Comment("Ugpin", "UCowner", "2027-01-15T08:00:00Z", "🟢 ALIVE · glider 1,1 ...", by_owner=True)]
    ev = engine.tick(w, LAUNCH + 600, yt)
    assert not [e for e in ev if e["type"] == "injection"]


def test_suggestion_only_changes_when_something_real_changes():
    w, yt = world(), PinYT([owner_status()])
    engine.tick(w, LAUNCH, yt)
    first = w.pinned_suggest
    for t in range(1800, 3 * 3600, 1800):  # board evolves; countdown stays "~3 days"
        engine.tick(w, LAUNCH + t, yt)
    assert w.pinned_suggest == first and len(yt.updates) == 1
    # every 3 hours it is re-stamped with the time, with the same suggestion
    engine.tick(w, LAUNCH + 3 * 3600, yt)
    assert w.pinned_suggest == first and len(yt.updates) == 2
    assert yt.updates[0][1].split("\n", 1)[1] == yt.updates[1][1].split("\n", 1)[1]


def test_the_status_comment_says_when_it_was_updated():
    w, yt = world(), PinYT([owner_status()])
    engine.tick(w, LAUNCH, yt)
    first_line = yt.updates[-1][1].split("\n")[0]
    assert first_line.endswith(" · updated " + render.clock(LAUNCH, date=False))


def test_a_save_credits_the_comment_that_broke_the_loop():
    w, yt = world(), PinYT([owner_status()])
    for t in range(0, 63 * 3600, 1800):  # acorn gets stuck at 61.8 h
        engine.tick(w, LAUNCH + t, yt)
    assert w.universe.phase == "FLATLINING"
    # posted at 62.5 h = gen 1,875, where this glider breaks the loop for good
    yt.comments.append(Comment("save", "UCsaver", "2027-01-17T22:30:00Z", "glider 4,20 ne"))
    for t in range(63 * 3600, 90 * 3600, 1800):
        engine.tick(w, LAUNCH + t, yt)
    assert w.universe.phase == "ALIVE"
    assert w.recent[0] == "🫀 gen 2,575: SAVED, thanks to glider 4,20 ne (gen 1,875)"
    assert w.saves == 1 and w.last_title.endswith(" · saved 1×")
    assert w.contest_by is None  # cleared once it is ALIVE again


def test_replies_play_like_comments():
    w, yt = world(), PinYT([owner_status(), Comment("Ugpin.r1", "u9", "2027-01-15T08:00:00Z", "rpent 30,30",
                                                    reply=True)])
    ev = engine.tick(w, LAUNCH + 600, yt)
    assert [e["command"] for e in ev if e["type"] == "injection"] == ["rpent 30,30"]


def test_an_owner_reply_quoting_the_marker_is_still_a_move():
    r = Comment("x.r1", "UCowner", "2027-01-15T08:00:00Z", "This video is alive, glider 5,5", by_owner=True,
                reply=True)
    w, yt = world(), PinYT([r])
    ev = engine.tick(w, LAUNCH + 600, yt)
    assert w.pinned == {} and [e["command"] for e in ev if e["type"] == "injection"] == ["glider 5,5 se"]


def test_each_video_keeps_its_own_status_comment():
    short = Comment("Ugshort", "UCowner", "2027-01-15T08:01:00Z", "Latest changes to the universe will appear here",
                    by_owner=True, video=1)
    w, yt = world(), PinYT([owner_status(), short, Comment("s1", "u5", "2027-01-15T08:02:00Z", "hello", video=1)])
    ev = engine.tick(w, LAUNCH + 600, yt)
    assert {v: p["id"] for v, p in w.pinned.items()} == {"0": "Ugpin", "1": "Ugshort"}
    assert len([e for e in ev if e["type"] == "injection"]) == 1  # the Short's comment played
    texts = dict(yt.updates)
    assert "The video is the explainer" in texts["Ugpin"] and "This Short is a replay" in texts["Ugshort"]
    assert texts["Ugpin"].splitlines()[0] == texts["Ugshort"].splitlines()[0]  # same status line
    # the main comment and the newest Short's are refreshed at most every 45 min
    yt.comments.append(comment("b", "u2", "rpent 90,20"))
    engine.tick(w, LAUNCH + 600 + 1800, yt)
    assert yt.updates[2:] == []
    engine.tick(w, LAUNCH + 600 + 2700, yt)
    assert [cid for cid, _ in yt.updates[2:]] == ["Ugpin", "Ugshort"]


def test_status_comments_are_watched_so_old_threads_still_count():
    seen = []

    class WatchYT(PinYT):
        def fetch_comments(self, since, watch=(), now=None):
            seen.append(list(watch))
            return self.comments

    w, yt = world(), WatchYT([owner_status()])
    engine.tick(w, LAUNCH, yt)
    engine.tick(w, LAUNCH + 300, yt)
    assert seen == [[], ["Ugpin"]]


def test_old_state_with_a_single_pinned_id_still_loads():
    import json
    w, yt = world(), PinYT([owner_status()])
    engine.tick(w, LAUNCH, yt)
    d, hist = w.to_state()
    disp = d["display"]
    p = disp.pop("pinned")["0"]
    disp.update({"pinned_id": p["id"], "pinned_text": p["text"], "pinned_at": p["at"]})
    d.pop("reply_counts")
    w2 = engine.World.from_state(json.loads(json.dumps(d)), hist, w.key)
    assert w2.pinned == w.pinned and w2.pinned_id == "Ugpin" and w2.reply_counts == {}


def test_a_bigger_universe_runs_the_same_way():
    shape = (99, 176)
    w = engine.World(Universe(life.mask(life.centered("acorn", shape), shape), flatline_gens()), LAUNCH,
                     key=b"k" * 16)
    yt = FakeYT([comment("a", "u1", "glider 170,95 nw"), comment("b", "u2", "hello")])
    ev = engine.tick(w, LAUNCH + 1200, yt)
    inj = [e for e in ev if e["type"] == "injection"]
    assert len(inj) == 2 and all(0 <= x < 176 and 0 <= y < 99 for e in inj for x, y in e["cells"])
    assert w.universe.grid.shape == shape and w.universe.generation == 10


def test_each_comment_logs_what_it_did_to_the_lifespan():
    w, yt = world(), PinYT([owner_status(), comment("a", "u1", "glider 64,71 nw")])
    ev = engine.tick(w, LAUNCH + 600, yt)
    kinds = [e["type"] for e in ev]
    eff = ev[kinds.index("injection") + 1]
    assert eff["type"] == "effect" and eff["before"] == 1855 and eff["command"] == "glider 64,71 nw"
    assert w.forecast["stuck_gen"] == eff["after"]  # reused, not recomputed
    assert " → " in w.recent[0] or "still" in w.recent[0]
    assert "Latest moves (time it had left: before → after):" in yt.updates[-1][1]


def test_a_comment_that_changes_nothing_has_no_effect_line():
    w = world()
    ev = engine.tick(w, LAUNCH + 600, FakeYT([comment("a", "u1", "cells 0,0")]))  # one lonely cell
    ev2 = engine.tick(w, LAUNCH + 1200, FakeYT([comment("a", "u1", "cells 0,0"), comment("b", "u2", "cells 0,0")]))
    assert all(e["type"] != "effect" or e["before"] is not None for e in ev + ev2)


def test_effects_can_be_rebuilt_from_an_old_log():
    import tick
    w = world()
    yt = FakeYT([comment("a", "u1", "glider 64,71 nw"), comment("b", "u2", "rpent 90,20")])
    ev = engine.tick(w, LAUNCH + 600, yt)
    live = [e for e in ev if e["type"] == "effect"]
    genesis = {"type": "genesis", "seed": "acorn", "shape": [72, 128], "flatline_gens": 720}
    log = [genesis] + [e for e in ev if e["type"] != "effect"]
    assert engine.effects_from_log(log) == live


def test_a_burst_of_comments_shares_one_line():
    w, yt = world(), PinYT([owner_status()] + [Comment(f"c{i}", "kid", "2027-01-15T08:00:00Z", t)
                                              for i, t in enumerate("ABCDEFG")])
    engine.tick(w, LAUNCH + 600, yt)
    assert w.recent[0].startswith("gen 0: 7 comments") and len(w.recent) == 1


def test_comments_dropped_under_the_old_hourly_rule_come_back_once(monkeypatch):
    from collections import Counter
    import admission
    kid = [Comment(f"k{i}", "kid", "2027-01-15T08:00:00Z", t) for i, t in enumerate("ABCDEFG")]
    w, yt = world(), FakeYT(kid + [comment("g", "u2", "glider 10,10")])
    monkeypatch.setattr(admission, "AUTHOR_COOLDOWN_S", 3600)  # the old rule
    ev = engine.tick(w, LAUNCH + 600, yt)
    applied = [e for e in ev if e["type"] == "injection"]
    assert len(applied) == 2  # the kid's first letter and the glider
    monkeypatch.setattr(admission, "AUTHOR_COOLDOWN_S", 0)  # every comment counts
    logged = Counter(e["command"] for e in applied)
    ev2 = engine.tick(w, LAUNCH + 900, yt, logged=logged)
    assert {"type": "readmitted", "count": 6, "reason": "every comment counts now"} in ev2
    assert len([e for e in ev2 if e["type"] == "injection"]) == 6  # B..G, nothing twice
    ev3 = engine.tick(w, LAUNCH + 1200, yt, logged=logged)
    assert not [e for e in ev3 if e["type"] in ("injection", "readmitted")]  # only once


def test_force_pinned_skips_the_wait():
    w, yt = world(), PinYT([owner_status()])
    engine.tick(w, LAUNCH, yt)
    yt.comments.append(comment("b", "u2", "rpent 90,20"))
    engine.tick(w, LAUNCH + 300, yt)
    assert len(yt.updates) == 1  # throttled
    engine.tick(w, LAUNCH + 600, yt, force_pinned=True)
    assert len(yt.updates) == 2 and "rpent 90,20" in yt.updates[-1][1]


def test_comments_land_at_the_generation_they_were_posted_in():
    w = world()
    engine.tick(w, LAUNCH, FakeYT())
    at = lambda s: Comment(f"c{s}", f"u{s}", f"2027-01-15T08:{s // 60:02d}:{s % 60:02d}Z", "block 5,5")  # noqa: E731
    yt = FakeYT([at(130), at(250), at(250 + 3), at(610)])  # gens 1, 2, 2, 5
    ev = engine.tick(w, LAUNCH + 900, yt)  # one run catches up 0 -> 7
    assert [e["gen"] for e in ev if e["type"] == "injection"] == [1, 2, 2, 5]
    assert w.universe.generation == 7


class ChatYT(FakeYT):
    def __init__(self, comments=(), chat=(), chat_fails=False):
        super().__init__(comments)
        self.chat, self.chat_fails = list(chat), chat_fails

    def fetch_chat(self, now=None):
        if self.chat_fails:
            raise RuntimeError("chat failed")
        return self.chat


def test_live_chat_messages_play_and_a_chat_failure_does_not_stop_comments():
    import datetime as dt
    from admission import Comment
    import youtube
    w = world()
    t = LAUNCH + 600
    at = dt.datetime.fromtimestamp(t - 30, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    chat = [Comment("chat:m1", "UCv", at, "block 20,20", video=youtube.CHAT)]
    events = engine.tick(w, t, ChatYT(chat=chat))
    assert [e["command"] for e in events if e["type"] == "injection"] == ["block 20,20"]
    w2 = world()
    events = engine.tick(w2, t, ChatYT(comments=[comment("c1")], chat_fails=True))
    assert [e["command"] for e in events if e["type"] == "injection"] == ["glider 10,10 se"]
    assert any(e["type"] == "youtube_error" and e["op"] == "fetch_chat" for e in events)


# ---------------------------------------------------------------- the leaderboard
def at(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def player(cid, author, text, t, video=0, name=""):
    return Comment(cid, author, at(t), text, video=video, name=name)


def keyed(author):
    import admission
    return admission.keyed(b"k" * 16, "author", author)


def test_every_move_scores_for_its_author_on_any_video_or_the_chat():
    import youtube
    w = world()
    cs = [player("a", "u1", "glider 10,10", LAUNCH + 60), player("b", "u2", "rpent 90,20", LAUNCH + 60, video=1),
          player("chat:c", "u1", "block 40,40", LAUNCH + 60, video=youtube.CHAT)]
    ev = engine.tick(w, LAUNCH + 600, PinYT(cs))
    effs = [e for e in ev if e["type"] == "effect"]
    assert len(effs) == 3
    assert w.scores[keyed("u1")][1] == 2 and w.scores[keyed("u2")][1] == 1
    assert sum(s[0] for s in w.scores.values()) == sum(engine.effect_gens(e) for e in effs)
    w2 = engine.World.from_state(*w.to_state(), key=b"k" * 16)
    assert w2.scores == w.scores


def test_a_move_with_no_loop_in_sight_counts_up_to_the_forecast_horizon():
    assert engine.effect_gens({"gen": 100, "before": 3000, "after": 3500, "horizon": 30000}) == 500
    assert engine.effect_gens({"gen": 100, "before": 3000, "after": None, "horizon": 30000}) == 30100 - 3000
    assert engine.effect_gens({"gen": 100, "before": None, "after": 900, "horizon": 30000}) == 900 - 30100


def test_the_backfill_credits_old_moves_as_if_they_had_been_scored_live():
    cs = [player("a", "u1", "glider 10,10", LAUNCH + 60), player("b", "u2", "rpent 90,20", LAUNCH + 1300),
          player("c", "u1", "hello there", LAUNCH + 4000), player("d", "u3", "glider 10,10", LAUNCH + 4100)]
    live, log = world(), []
    for t in (700, 1900, 4600):
        log += engine.tick(live, LAUNCH + t, FakeYT([c for c in cs if c.epoch <= LAUNCH + t]))
    later = world()
    assert engine.backfill_scores(later, cs, log, later.universe.grid.shape) == 4
    assert later.scores == live.scores


def test_the_backfill_runs_once_before_new_moves_are_scored():
    cs = [player("a", "u1", "glider 10,10", LAUNCH + 60)]
    old, log = world(), []
    log += engine.tick(old, LAUNCH + 700, FakeYT(cs))
    old.scores = {}  # as if scores weren't kept yet
    new = cs + [player("b", "u2", "rpent 90,20", LAUNCH + 1300)]
    ev = engine.tick(old, LAUNCH + 1900, FakeYT(new), log_entries=log)
    assert [e["moves"] for e in ev if e["type"] == "scores_backfilled"] == [1]
    assert old.scores[keyed("u1")][1] == 1 and old.scores[keyed("u2")][1] == 1  # the old move once, the new one live
    assert engine.SCORES in old.migrations
    assert not [e for e in engine.tick(old, LAUNCH + 2500, FakeYT(new), log_entries=log) if e["type"] == "scores_backfilled"]


class NamesYT(PinYT):
    """Pinned updates, plus a full read of every comment (for names) when asked."""

    def __init__(self, comments=(), everyone=()):
        super().__init__(comments)
        self.everyone, self.reads = list(everyone), []

    def fetch_comments(self, since, watch=(), now=None):
        self.reads.append(since)
        return self.comments + (self.everyone if since == LAUNCH else [])  # the read since launch


def test_the_status_comment_names_the_top_savers_and_killers():
    w = world()
    w.scores = {keyed("u1"): [4320, 3], keyed("u2"): [-6000, 1], keyed("u3"): [10, 1], keyed("u4"): [-90, 2],
                keyed("u5"): [100, 1]}
    yt = NamesYT([owner_status(), player("x", "u1", "glider 10,10", LAUNCH + 60, name="@ann")],
                 everyone=[player("y", "u2", "block 5,5", LAUNCH + 30, name="@bob")])
    engine.tick(w, LAUNCH + 600, yt)
    text = yt.updates[-1][1]
    saver, killer = [ln for ln in text.splitlines() if ln.startswith(("🏆", "💀"))]
    assert saver.startswith("🏆 Top savers: @ann +") and saver.endswith(" · someone +3h")  # u3's 20 min: too little
    assert killer.startswith("💀 Top killers: @bob −8d 8h · someone −3h")
    assert LAUNCH in yt.reads  # names it hadn't seen this run: one read of every comment since launch
    assert "@ann" not in str(w.to_state()[0]) and "@bob" not in str(w.to_state()[0])  # names are never stored


def test_no_leaderboard_until_someone_has_moved_it_an_hour():
    w, yt = world(), PinYT([owner_status()])
    w.scores = {keyed("u1"): [29, 1]}
    engine.tick(w, LAUNCH + 600, yt)
    assert "Top savers" not in yt.updates[-1][1] and "Top killers" not in yt.updates[-1][1]


def test_latest_moves_name_who_made_them_and_the_names_are_never_stored():
    import youtube
    w = world()
    cs = [owner_status(), player("a", "u1", "glider 10,10", LAUNCH + 60, name="@ann"),
          player("chat:c", "u2", "block 40,40", LAUNCH + 60, video=youtube.CHAT, name="Viv")]
    yt = PinYT(cs)
    engine.tick(w, LAUNCH + 600, yt)
    text = yt.updates[-1][1]
    assert "• gen 0 · @ann: glider 10,10 se" in text and "• gen 0 · live chat: block 40,40" in text
    assert w.recent_by[:2] and set(w.recent_by[:2]) == {keyed("u1"), "chat"}
    state = str(engine.World.from_state(*w.to_state(), key=b"k" * 16).to_state()[0])
    assert "@ann" not in state and "Viv" not in state


def test_a_mover_with_no_name_found_is_just_not_named():
    assert render.with_author("gen 5: glider 1,1 se · 3d → 2d", "abc", {}) == "gen 5: glider 1,1 se · 3d → 2d"
    assert render.with_author("gen 5: glider 1,1 se", None, {"abc": "@x"}) == "gen 5: glider 1,1 se"
    assert render.with_author("gen 5: glider 1,1 se", "abc", {"abc": "@x"}) == "gen 5 · @x: glider 1,1 se"
