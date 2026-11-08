"""youtube.YouTube against a fake googleapiclient service."""
import datetime as dt

import pytest

import youtube

T0 = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc).timestamp()


def thread(cid, t, text="hi", author="UCa", with_author=True):
    sn = {"publishedAt": dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "textDisplay": text, "authorDisplayName": "Ann"}
    if with_author:
        sn["authorChannelId"] = {"value": author}
    return {"snippet": {"topLevelComment": {"id": cid, "snippet": sn}}}


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeService:
    def __init__(self, pages, snippet=None):
        self.pages, self.calls = pages, []
        self.snippet = snippet or {"title": "old", "description": "about", "tags": ["a"], "categoryId": "22",
                                   "defaultLanguage": "en", "channelId": "UCme", "thumbnails": {}, "localized": {}}

    def commentThreads(self):
        return self

    def videos(self):
        return self

    def thumbnails(self):
        return self

    def list(self, **kw):
        self.calls.append(("list", kw))
        if "videoId" in kw:
            i = int(kw.get("pageToken") or 0)
            resp = {"items": self.pages[i]}
            if i + 1 < len(self.pages):
                resp["nextPageToken"] = str(i + 1)
            return Req(lambda: resp)
        return Req(lambda: {"items": [{"snippet": dict(self.snippet)}]})

    def update(self, part, body):
        self.calls.append(("update", body))
        return Req(lambda: body)

    def set(self, **kw):
        self.calls.append(("thumb", kw))
        return Req(lambda: {})


def yt(svc, used=0, grid=False):
    q = youtube.Quota({}, T0)
    q.state["used"] = used
    return youtube.YouTube(svc, "VID", q, description_grid=grid)


def test_fetch_requests_plain_text_newest_first():
    svc = FakeService([[thread("a", T0)]])
    yt(svc).fetch_comments(T0 - 3600)
    kw = svc.calls[0][1]
    assert kw["order"] == "time" and kw["textFormat"] == "plainText" and kw["maxResults"] == 100
    assert kw["part"] == "snippet" and kw["videoId"] == "VID"


def test_fetch_pages_until_older_than_since():
    pages = [[thread(f"p0-{i}", T0 - i * 60) for i in range(3)],
             [thread("p1", T0 - 3600)],
             [thread("p2", T0 - 7200)],
             [thread("p3", T0 - 9000)]]
    svc = FakeService(pages)
    out = yt(svc).fetch_comments(T0 - 5000)
    assert [c.id for c in out] == ["p0-0", "p0-1", "p0-2", "p1", "p2"]  # stopped after page 2


def test_fetch_counts_quota_even_over_budget():
    y = yt(FakeService([[thread("a", T0)]]), used=8000)
    y.fetch_comments(T0 - 60)  # critical: never skipped
    assert y.quota.used == 8001


def test_missing_author_channel_falls_back_to_display_name():
    c = youtube.comment_from_thread(thread("a", T0, with_author=False))
    assert c.author == "name:Ann"


def test_set_title_sends_full_writable_snippet_only():
    svc = FakeService([])
    y = yt(svc)
    y.set_title("new title")
    body = [c for c in svc.calls if c[0] == "update"][0][1]
    assert body["snippet"] == {"title": "new title", "description": "about", "tags": ["a"],
                               "categoryId": "22", "defaultLanguage": "en"}
    assert y.quota.used == 51


def test_set_title_over_budget_is_a_skip():
    y = yt(FakeService([]), used=7960)
    with pytest.raises(youtube.QuotaExceeded) as e:
        y.set_title("t")
    assert e.value.skip and y.quota.used == 7960


def test_description_grid_only_when_enabled():
    svc = FakeService([])
    yt(svc, grid=True).set_title("t", "⠁⠂")
    desc = [c for c in svc.calls if c[0] == "update"][0][1]["snippet"]["description"]
    assert desc.startswith("about\n\n") and "⠁⠂" in desc
    svc2 = FakeService([])
    yt(svc2, grid=False).set_title("t", "⠁")
    assert [c for c in svc2.calls if c[0] == "update"][0][1]["snippet"]["description"] == "about"


def test_quota_resets_at_pacific_midnight():
    state = {}
    pacific = youtube.PACIFIC
    before = dt.datetime(2026, 11, 8, 23, 59, tzinfo=pacific).timestamp()
    youtube.Quota(state, before).spend(100)
    youtube.Quota(state, before + 30).spend(1)
    assert state["used"] == 101
    youtube.Quota(state, before + 120).spend(1)  # 00:01 Pacific, next day
    assert state == {"day": "2026-11-09", "used": 1}


def test_braille_grid_fits_the_description_limit():
    import life
    import numpy as np
    import render
    full = np.ones((life.H, life.W), np.uint8)
    text = render.braille(full)
    assert len(text.splitlines()) == 18 and all(len(r) == 64 for r in text.splitlines())
    assert youtube.with_grid("", text) is not None
