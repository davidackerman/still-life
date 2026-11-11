"""The daily Short (daily.py) through tick.run, with a fake YouTube and clock."""
import datetime as dt
import json
import os

import pytest

import engine
import tick
from admission import Comment
from universe import Universe, flatline_gens
import life

KEY = b"test-state-key-0123456789"
LAUNCH = dt.datetime(2027, 1, 15, 12, 0, tzinfo=dt.timezone.utc).timestamp()  # noon UTC


class DailyYT:
    videos, video_id = ["MAIN"], "MAIN"

    def __init__(self):
        self.comments, self.uploads, self.posted, self.existing = [], [], [], {}
        self.stream_titles = []

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

    def upload_video(self, path, snippet):
        self.uploads.append(snippet)
        vid = f"SHORT{len(self.uploads)}"
        self.existing[snippet["description"].rsplit("(", 1)[1].rstrip(")")] = vid
        return vid

    def post_comment(self, vid, text):
        self.posted.append((vid, text))
        return "UgS"


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
    posted = [e for e in ev if e.get("alert", "").startswith("📼")]
    assert len(yt.uploads) == 1 and posted and "https://youtube.com/shorts/SHORT1" in posted[0]["alert"]
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
    assert len(calls) == 1 and any(e.get("alert", "").startswith("⚠️ Today's Short failed") for e in ev)


def test_older_shorts_are_read_every_30_minutes():
    w = engine.World(Universe(life.mask(life.centered("acorn")), flatline_gens()), LAUNCH, key=KEY)
    w.extra_videos = ["S1", "S2", "S3", "S4", "S5"]
    extras, skip = tick.choose_extras(w, ["FIRST"], LAUNCH)
    assert extras == ["FIRST", "S1", "S2", "S3", "S4", "S5"] and skip == set()  # never read yet
    extras, skip = tick.choose_extras(w, ["FIRST"], LAUNCH + 600)
    assert skip == {"FIRST", "S1", "S2"}  # the newest three are always read
    extras, skip = tick.choose_extras(w, ["FIRST"], LAUNCH + 1800)
    assert skip == set()
    w.retired = ["S5"]
    assert "S5" not in tick.choose_extras(w, ["FIRST"], LAUNCH + 3600)[0]


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
    assert sorted(yt.updates) == ["pin0", "pin2", "pin3"]  # the oldest Short waits 12 h
