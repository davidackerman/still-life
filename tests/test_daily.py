"""The daily Short (daily.py) through tick.run, with a fake YouTube and clock."""
import datetime as dt
import json
import os

import pytest

import engine
import tick
from admission import Comment
from universe import Universe, flatline_gens
import daily
import life

KEY = b"test-state-key-0123456789"
LAUNCH = dt.datetime(2027, 1, 15, 12, 0, tzinfo=dt.timezone.utc).timestamp()  # noon UTC


class DailyYT:
    videos, video_id = ["MAIN"], "MAIN"

    def __init__(self):
        self.comments, self.uploads, self.posted, self.existing = [], [], [], {}
        self.stream_titles, self.notified = [], []

    def fetch_comments(self, since, watch=(), now=None):
        return list(self.comments)

    def set_title(self, t, grid_text=""):
        pass

    def set_thumbnail(self, path):
        pass

    def find_upload(self, marker):
        return self.existing.get(marker)

    def set_stream_title(self, title):
        self.stream_titles.append(title)
        return "LIVE1"

    def upload_video(self, path, snippet, notify=True):
        self.uploads.append(snippet)
        self.notified.append(notify)
        vid = f"SHORT{len(self.uploads)}"
        self.existing[snippet["description"].rsplit("(", 1)[1].rstrip(")")] = vid
        return vid

    def post_comment(self, vid, text):
        self.posted.append((vid, text))
        return "UgS"


@pytest.fixture(autouse=True)
def recap_by_default(monkeypatch):
    import daily
    monkeypatch.setattr(daily, "DEFAULT_KINDS", {})  # written for the recap; the newer kinds have their own tests


ONE_A_DAY = daily.SLOTS_ET, daily.NOTIFY_SLOTS  # the real slots (now one, at 8 PM)


@pytest.fixture(autouse=True)
def two_slots(monkeypatch):  # written for noon and 8 PM: the slot machinery, which still takes any number
    monkeypatch.setattr(daily, "SLOTS_ET", (12, 20))
    monkeypatch.setattr(daily, "NOTIFY_SLOTS", (12, 20))


@pytest.fixture
def d(monkeypatch, tmp_path):
    for k in ("SEED", "GEN_SECONDS", "FLATLINE_HOURS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LAUNCH_TIME", str(int(LAUNCH)))
    monkeypatch.chdir(tmp_path)
    return str(tmp_path / "state")


def state(d):
    return json.load(open(os.path.join(d, "state.json")))


def test_nothing_before_the_hour_or_when_off(d):
    yt = DailyYT()
    tick.run(d, LAUNCH + 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live")  # 13:00 UTC
    tick.run(d, LAUNCH + 11.5 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="off")  # 23:30, off
    assert yt.uploads == [] and state(d)["daily"] == {}


def test_live_posts_once_a_day_and_its_comments_count(d):
    yt = DailyYT()
    yt.comments = [Comment("c1", "u1", "2027-01-15T13:00:00Z", "glider 10,10")]
    tick.run(d, LAUNCH + 3600, lambda w: yt, KEY, thumb_path=None)
    ev = tick.run(d, LAUNCH + 11.2 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live")  # 23:12 UTC
    posted = [e for e in ev if e["type"] == "daily_short" and e.get("posted")]
    assert len(yt.uploads) == 1 and posted and posted[0]["video"] == "SHORT1"
    assert not any(e.get("alert", "").startswith("📼") for e in ev)  # no issue per Short (LIVE_ALERTS is off)
    up = yt.uploads[0]
    assert up["title"] == "This Short dies if nobody comments · Universe 1, Day 1 #GameOfLife"
    assert "1 comment in the last 24 hours" in up["description"] and "still-life universe 1 day 1" in up["description"]
    assert "/live" in up["description"]  # the 24/7 stream
    assert "As of day 1, it" in up["description"] and "counts down live: https://youtu.be/MAIN" in up["description"]
    assert yt.stream_titles == ["This stream dies if nobody chats · Game of Life, Universe 1, Day 1"]
    assert yt.posted[0][0] == "SHORT1"
    s = state(d)
    assert s["config"]["extra_videos"] == ["SHORT1"] and s["daily"]["video"] == "SHORT1"
    tick.run(d, LAUNCH + 11.5 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live")
    assert len(yt.uploads) == 1  # once a day
    tick.run(d, LAUNCH + 35.1 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live")  # next day 23:06
    assert len(yt.uploads) == 2 and yt.uploads[1]["title"].endswith("Universe 1, Day 2 #GameOfLife")
    assert "0 comments in the last 24 hours (1 since it began)." in yt.uploads[1]["description"]
    assert "since it began" not in yt.uploads[0]["description"]  # day 1: the same number
    assert state(d)["config"]["extra_videos"] == ["SHORT1", "SHORT2"]


def test_dry_run_renders_but_never_uploads(d):
    yt = DailyYT()
    ev = tick.run(d, LAUNCH + 11.2 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="dry")
    assert yt.uploads == [] and any(e.get("alert", "").startswith("📼 Day 1 Short rendered (dry run") for e in ev)
    assert os.path.exists("out/daily.mp4")


def test_a_failure_is_alerted_and_not_retried_all_evening(d, monkeypatch):
    import short
    yt = DailyYT()
    calls = []

    def broken(*a, **kw):
        calls.append(1)
        raise RuntimeError("ffmpeg missing")
    monkeypatch.setattr(short, "render_video", broken)
    ev = tick.run(d, LAUNCH + 11.2 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live")
    tick.run(d, LAUNCH + 11.5 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live")
    assert len(calls) == 1 and any(e.get("alert", "").startswith("⚠️ This slot's Short failed") for e in ev)


def test_older_shorts_are_read_every_3_hours():
    w = engine.World(Universe(life.mask(life.centered("acorn")), flatline_gens()), LAUNCH, key=KEY)
    w.extra_videos = ["S1", "S2", "S3", "S4", "S5"]
    extras, skip = tick.choose_extras(w, ["FIRST"], LAUNCH)
    assert extras == ["FIRST", "S1", "S2", "S3", "S4", "S5"] and skip == set()  # never read yet
    extras, skip = tick.choose_extras(w, ["FIRST"], LAUNCH + 600)
    assert skip == {"FIRST", "S1", "S2"}  # the newest three are always read
    extras, skip = tick.choose_extras(w, ["FIRST"], LAUNCH + 2 * 3600)
    assert skip == {"FIRST", "S1", "S2"}  # still: they had a turn at the start
    extras, skip = tick.choose_extras(w, ["FIRST"], LAUNCH + 3 * 3600)
    assert skip == set()
    w.retired = ["S5"]
    assert "S5" not in tick.choose_extras(w, ["FIRST"], LAUNCH + 4 * 3600)[0]


def test_older_shorts_status_comments_update_rarely():
    w = engine.World(Universe(life.mask(life.centered("acorn")), flatline_gens()), LAUNCH, key=KEY)

    class YT:
        def __init__(self):
            self.updates = []

        def fetch_comments(self, since, watch=(), now=None):
            return [Comment(f"pin{v}", "UCo", "2027-01-15T12:00:00Z", "Latest changes to the universe", by_owner=True,
                            video=v) for v in range(4)] + self.extra

        def set_title(self, t, g=""):
            pass

        def set_thumbnail(self, p):
            pass

        def update_comment(self, cid, text):
            self.updates.append(cid)
    yt = YT()
    yt.extra = []
    engine.tick(w, LAUNCH, yt)
    assert sorted(yt.updates) == ["pin0", "pin1", "pin2", "pin3"]
    yt.updates, yt.extra = [], [Comment("m", "u", "2027-01-15T12:10:00Z", "block 5,5")]
    engine.tick(w, LAUNCH + 3700, yt)  # an hour later, something changed
    assert sorted(yt.updates) == ["pin0", "pin3"]  # the newest Short too; the next waits 3 h, the oldest 12 h


def test_a_planned_day_posts_the_glider_short_with_its_title(d, monkeypatch):
    import daily
    import short
    yt = DailyYT()
    when = LAUNCH + 11.2 * 3600
    monkeypatch.setattr(daily, "PLAN", {daily.today(when): "flight"})
    monkeypatch.setattr(short, "pick_flight", lambda h, pick=None: (88, 10, {"command": "glider 1,1 se"}, None))
    calls = []

    def render(state_dir, out, since, **kw):
        calls.append(kw)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        open(out, "wb").write(b"mp4")
        return out
    tick.run(d, LAUNCH + 3600, lambda w: yt, KEY, thumb_path=None)
    tick.run(d, when, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=render)
    assert calls == [{"kind": "flight"}]
    up = yt.uploads[0]
    assert up["title"] == "Someone's comment became this glider · Universe 1, Day 1 #GameOfLife"
    assert up["description"].startswith("Someone's comment became this glider: this Short follows it")
    monkeypatch.setattr(short, "pick_flight", lambda h, pick=None: None)  # no glider flew far: the recap, as usual
    tick.run(d, when + 86400, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=render)
    assert calls[-1] == {} and yt.uploads[-1]["title"].startswith("This Short dies if nobody comments")


def fake_render(calls):
    def render(state_dir, out, since, **kw):
        calls.append(kw)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        open(out, "wb").write(b"mp4")
        return out
    return render


def test_two_shorts_a_day_noon_and_8_pm(d):
    yt, calls = DailyYT(), []
    noon = LAUNCH + 5.2 * 3600  # 12:12 PM in New York (LAUNCH is 7 AM there)
    tick.run(d, LAUNCH + 3600, lambda w: yt, KEY, thumb_path=None)
    tick.run(d, noon, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=fake_render(calls))
    tick.run(d, noon + 3 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=fake_render(calls))
    assert len(yt.uploads) == 1 and state(d)["daily"]["slot"] == 12  # once a slot
    tick.run(d, LAUNCH + 13.1 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live",
             render_short=fake_render(calls))  # 8:06 PM
    assert len(yt.uploads) == 2
    first, second = yt.uploads
    assert first["title"].endswith("Universe 1, Day 1 #GameOfLife") and "in the last 24 hours" in first["description"]
    assert second["title"].endswith("Universe 1, Day 1, evening #GameOfLife")
    assert "in the last 8 hours" in second["description"]
    assert "still-life universe 1 day 1 slot 20" in second["description"]
    assert "slot 20" not in first["description"] and "slot 12" not in second["description"]
    assert yt.notified == [True, True]  # both go out to subscribers
    s = state(d)
    assert s["daily"]["slot"] == 20 and s["config"]["extra_videos"] == ["SHORT1", "SHORT2"]
    assert yt.stream_titles == ["This stream dies if nobody chats · Game of Life, Universe 1, Day 1"]  # once a day
    tick.run(d, LAUNCH + 15 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live",
             render_short=fake_render(calls))  # 10 PM
    assert len(yt.uploads) == 2


def test_one_short_a_day_at_8_pm(d, monkeypatch):
    monkeypatch.setattr(daily, "SLOTS_ET", ONE_A_DAY[0])
    monkeypatch.setattr(daily, "NOTIFY_SLOTS", ONE_A_DAY[1])
    assert daily.SLOTS_ET == (20,)
    yt, calls = DailyYT(), []
    tick.run(d, LAUNCH + 3600, lambda w: yt, KEY, thumb_path=None)
    tick.run(d, LAUNCH + 5.2 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live",
             render_short=fake_render(calls))  # 12:12 PM: no Short at noon any more
    assert yt.uploads == [] and not daily.due(state(d), LAUNCH + 5.2 * 3600)
    tick.run(d, LAUNCH + 13.1 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live",
             render_short=fake_render(calls))  # 8:06 PM
    tick.run(d, LAUNCH + 15 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live",
             render_short=fake_render(calls))  # 10 PM: once
    (only,) = yt.uploads
    assert only["title"].endswith("Universe 1, Day 1 #GameOfLife")  # the day's Short, not "evening"
    assert "in the last 24 hours" in only["description"]
    assert "still-life universe 1 day 1 slot 20" in only["description"]  # the marker a hand-made Short carries
    assert yt.notified == [True] and state(d)["daily"]["slot"] == 20
    assert yt.stream_titles == ["This stream dies if nobody chats · Game of Life, Universe 1, Day 1"]


def test_a_record_from_before_the_slots_counts_as_the_first_slot():
    import daily
    afternoon, eight_pm = LAUNCH + 7.5 * 3600, LAUNCH + 13.1 * 3600  # 2:30 PM, 8:06 PM in New York
    s = {"universe": {"phase": "ALIVE"}, "daily": {"date": daily.today(eight_pm), "day": 1, "video": "X"}}
    assert not daily.due(s, afternoon)  # posted at 2 PM the old way: noon's slot is done
    assert daily.due(s, eight_pm)
    s["daily"]["slot"] = 20
    assert not daily.due(s, eight_pm)
    assert not daily.due(s, LAUNCH + 3600)  # 8 AM: before the first slot


def test_a_planned_slot_posts_the_rewind_short(d, monkeypatch):
    import daily
    yt, calls = DailyYT(), []
    noon = LAUNCH + 5.2 * 3600
    monkeypatch.setattr(daily, "PLAN", {daily.today(noon): {12: "rewind"}})
    tick.run(d, LAUNCH + 3600, lambda w: yt, KEY, thumb_path=None)
    tick.run(d, noon, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=fake_render(calls))
    tick.run(d, LAUNCH + 13.1 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live",
             render_short=fake_render(calls))
    assert calls == [{"kind": "rewind"}, {}]  # the plan's slot, then the recap
    assert yt.uploads[0]["title"] == "Can a zero-player game have players? #satisfying #GameOfLife"
    assert yt.uploads[0]["description"].startswith("Can a zero-player game have players? Conway's Game of Life is a "
                                                   "zero-player game")
    assert yt.uploads[1]["title"].startswith("This Short dies if nobody comments")


def test_noon_is_the_stranger_and_8_pm_the_countdown_each_falling_back_to_the_recap(d, monkeypatch):
    import daily
    import short
    monkeypatch.setattr(daily, "DEFAULT_KINDS", {12: "stranger", 20: "countdown"})
    noon, evening = LAUNCH + 5.2 * 3600, LAUNCH + 13.1 * 3600
    assert daily.kind(noon) == "stranger" and daily.kind(evening) == "countdown"
    monkeypatch.setattr(daily, "PLAN", {daily.today(noon): {20: "rewind"}})
    assert daily.kind(noon) == "stranger" and daily.kind(evening) == "rewind"  # a plan still wins its slot
    monkeypatch.setattr(daily, "PLAN", {})
    move = (40, {"command": "acorn 1,1", "cells": [[1, 1]]}, {"before": 100, "after": 100 + 9 * 720})
    monkeypatch.setattr(short, "opening_move", lambda h, since, lead=None: move)
    monkeypatch.setattr(short, "countdown", lambda h, now=None, stuck_now=None: ([], "This universe has 4 days left"))
    yt, calls = DailyYT(), []
    tick.run(d, LAUNCH + 3600, lambda w: yt, KEY, thumb_path=None)
    tick.run(d, noon, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=fake_render(calls))
    tick.run(d, evening, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=fake_render(calls))
    assert calls == [{"kind": "stranger"}, {"kind": "countdown"}]
    first, second = yt.uploads
    assert first["title"] == "One comment gave this universe 9 more days to live #GameOfLife"  # no day count
    assert second["title"] == "This universe has 4 days left #satisfying #GameOfLife"  # drawn in 3D
    assert "#satisfying" in second["description"] and "satisfying" in second["tags"]
    assert "#satisfying" not in first["title"] + first["description"]  # the stranger is flat
    assert first["description"].startswith("One comment gave this universe 9 more days to live: every comment")
    assert "live stream" in second["description"] and yt.notified == [True, True]
    monkeypatch.setattr(short, "opening_move", lambda h, since, lead=None: None)  # nothing moved it much
    monkeypatch.setattr(short, "countdown", lambda h, now=None, stuck_now=None: None)  # no loop in sight
    tick.run(d, noon + 86400, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=fake_render(calls))
    tick.run(d, evening + 86400, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=fake_render(calls))
    assert calls[-2:] == [{}, {}] and all(u["title"].startswith("This Short dies") for u in yt.uploads[-2:])


def test_the_stranger_title_says_what_one_comment_did():
    import short
    G = 120
    assert short.stranger_title((1, {}, {"before": 100, "after": 100 + 9 * 720}), G) == \
        "One comment gave this universe 9 more days to live"
    assert short.stranger_title((1, {}, {"before": 100 + 30 * 30, "after": 100}), G) == \
        "One comment took 1 day off this universe's life"


def test_a_planned_slot_posts_the_made_short(d, monkeypatch):
    import daily
    import short
    yt, calls = DailyYT(), []
    noon = LAUNCH + 5.2 * 3600
    monkeypatch.setattr(daily, "PLAN", {daily.today(noon): {12: "made"}})
    monkeypatch.setattr(short, "made_count", lambda h: 1037)
    tick.run(d, LAUNCH + 3600, lambda w: yt, KEY, thumb_path=None)
    tick.run(d, noon, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=fake_render(calls))
    assert calls == [{"kind": "made"}]
    assert yt.uploads[0]["title"] == "1,000 comments built this simulation #satisfying #GameOfLife"
    assert yt.uploads[0]["description"].startswith("1,000 comments built this simulation: it started as 7 cells, and every comment since has added more.")


def test_a_dates_extra_slot(d, monkeypatch):
    """EXTRA_SLOTS adds a slot on one date only: noon that day, back to 8 PM alone the next."""
    import short
    noon, evening = LAUNCH + 5.2 * 3600, LAUNCH + 13.1 * 3600  # 12:12 PM and 8:06 PM New York
    monkeypatch.setattr(daily, "SLOTS_ET", (20,))
    monkeypatch.setattr(daily, "NOTIFY_SLOTS", (20,))
    monkeypatch.setattr(daily, "EXTRA_SLOTS", {daily.today(noon): (12,)})
    monkeypatch.setattr(daily, "PLAN", {daily.today(noon): {12: "countdown", 20: "rewind"}})
    assert daily.slots(noon) == (12, 20) and daily.slots(noon + 86400) == (20,)
    assert daily.slot(noon) == 12 and daily.slot(noon + 86400) is None
    assert daily.kind(noon) == "countdown" and daily.kind(evening) == "rewind"
    assert daily.recap_hours(20, evening) == 8 and daily.recap_hours(20, evening + 86400) == 24
    monkeypatch.setattr(short, "countdown", lambda h, now=None, stuck_now=None: ([], "This universe has 4 days left"))
    yt, calls = DailyYT(), []
    tick.run(d, LAUNCH + 3600, lambda w: yt, KEY, thumb_path=None)
    tick.run(d, noon, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=fake_render(calls))
    tick.run(d, evening, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=fake_render(calls))
    tick.run(d, noon + 86400, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=fake_render(calls))
    assert len(yt.uploads) == 2 and yt.notified == [True, True]  # the extra slot goes out to subscribers too
    assert "slot 12" in yt.uploads[0]["description"] and "slot 20" in yt.uploads[1]["description"]
    assert "in the last 8 hours" in yt.uploads[1]["description"]  # the 8 PM Short is the day's second
    tick.run(d, evening + 86400, lambda w: yt, KEY, thumb_path=None, daily_mode="live", render_short=fake_render(calls))
    assert len(yt.uploads) == 3  # the next day: 8 PM only


def test_a_short_its_slot_run_missed_is_adopted_once_after_the_one_before_it(d, monkeypatch):
    yt = DailyYT()
    tick.run(d, LAUNCH + 11.2 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live")  # posts SHORT1
    assert state(d)["config"]["extra_videos"] == ["SHORT1"]  # a new world: the real LATE_ADOPT isn't for it
    monkeypatch.setattr(tick, "LATE_ADOPT", {"LATE1": ("SHORT1", "the Day 1 evening Short"), "LATE2": ("NOPE", "x")})
    monkeypatch.setattr(tick, "LATE_ADOPT_MARK", "late-adopt-test")
    read = []

    def factory(w):  # what this run reads: the hand uploads first, then the daily Shorts, the late one last
        read.append(tick.choose_extras(w, ["HAND"], LAUNCH + 12 * 3600, mark=False)[0])
        return yt
    tick.run(d, LAUNCH + 12 * 3600, factory, KEY, thumb_path=None)
    s = state(d)
    assert read == [["HAND", "SHORT1", "LATE1"]] and s["config"]["extra_videos"] == ["SHORT1", "LATE1"]
    assert s["video_names"]["LATE1"] == "the Day 1 evening Short" and "LATE2" not in s["video_names"]
    assert "late-adopt-test" in s["display"]["migrations"]
    tick.run(d, LAUNCH + 35.1 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live")  # the next day's Short
    assert state(d)["config"]["extra_videos"] == ["SHORT1", "LATE1", "SHORT2"]  # after it, once


def test_a_hand_upload_adopted_before_its_slot_run_is_read_at_once_and_that_run_only_records_it(d, monkeypatch):
    yt = DailyYT()
    tick.run(d, LAUNCH + 11.2 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live")  # posts SHORT1
    monkeypatch.setattr(tick, "EARLY_ADOPT", {"EARLY1": ("SHORT1", "the Day 2 evening Short")})
    monkeypatch.setattr(tick, "EARLY_ADOPT_MARK", "early-adopt-test")
    read = []

    def factory(w):
        read.append(tick.choose_extras(w, [], LAUNCH + 34 * 3600, mark=False)[0])
        return yt
    tick.run(d, LAUNCH + 34 * 3600, factory, KEY, thumb_path=None)  # before the next slot: read from now on
    assert read == [["SHORT1", "EARLY1"]] and state(d)["config"]["extra_videos"] == ["SHORT1", "EARLY1"]
    yt.find_upload = lambda marker: "EARLY1"  # the slot run finds it by its marker (uploaded by hand with it)
    tick.run(d, LAUNCH + 35.1 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live")
    s = state(d)
    assert s["config"]["extra_videos"] == ["SHORT1", "EARLY1"] and len(yt.uploads) == 1  # recorded, not posted again
    assert [v for v, _ in yt.posted].count("EARLY1") == 1  # its status comment, once
    tick.run(d, LAUNCH + 35.3 * 3600, lambda w: yt, KEY, thumb_path=None, daily_mode="live")
    assert len(yt.uploads) == 1 and [v for v, _ in yt.posted].count("EARLY1") == 1  # that slot is done
