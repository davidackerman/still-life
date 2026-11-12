"""youtube.YouTube against a fake googleapiclient service."""
import datetime as dt

import pytest

import youtube

T0 = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc).timestamp()


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reply(cid, t, text="re", author="UCr"):
    return {"id": cid, "snippet": {"publishedAt": iso(t), "textDisplay": text, "authorDisplayName": "Rae",
                                   "authorChannelId": {"value": author}, "parentId": cid.split(".")[0]}}


def thread(cid, t, text="hi", author="UCa", with_author=True, replies=(), total=None, video="VID"):
    sn = {"publishedAt": iso(t), "textDisplay": text, "authorDisplayName": "Ann"}
    if with_author:
        sn["authorChannelId"] = {"value": author}
    item = {"id": cid, "snippet": {"channelId": "UCme", "videoId": video,
                                   "totalReplyCount": len(replies) if total is None else total,
                                   "topLevelComment": {"id": cid, "snippet": sn}}}
    if replies:
        item["replies"] = {"comments": list(replies)}
    return item


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeService:
    def __init__(self, pages, snippet=None, replies=None, by_id=None, video_pages=None):
        self.pages, self.calls = pages, []
        self.replies = replies or {}  # thread id -> full reply list (comments.list)
        self.by_id = by_id or {}  # thread id -> thread (commentThreads.list id=)
        self.video_pages = video_pages or {}  # extra video id -> pages
        self.snippet = snippet or {"title": "old", "description": "about", "tags": ["a"], "categoryId": "22",
                                   "defaultLanguage": "en", "channelId": "UCme", "thumbnails": {}, "localized": {}}

    def commentThreads(self):
        return self

    def videos(self):
        return self

    def thumbnails(self):
        return self

    def comments(self):
        return self

    def list(self, **kw):
        self.calls.append(("list", kw))
        if "parentId" in kw:
            return Req(lambda: {"items": self.replies[kw["parentId"]]})
        if "id" in kw and "part" in kw and "replies" in kw["part"]:
            return Req(lambda: {"items": [self.by_id[i] for i in kw["id"].split(",") if i in self.by_id]})
        if "videoId" in kw:
            pages = self.video_pages.get(kw["videoId"], self.pages)
            i = int(kw.get("pageToken") or 0)
            resp = {"items": pages[i] if pages else []}
            if i + 1 < len(pages):
                resp["nextPageToken"] = str(i + 1)
            return Req(lambda: resp)
        return Req(lambda: {"items": [{"snippet": dict(self.snippet)}]})

    def update(self, part, body):
        self.calls.append(("update", body))
        return Req(lambda: body)

    def set(self, **kw):
        self.calls.append(("thumb", kw))
        return Req(lambda: {})


def yt(svc, used=0, grid=False, extra=(), counts=None):
    q = youtube.Quota({}, T0)
    q.state["used"] = used
    return youtube.YouTube(svc, "VID", q, description_grid=grid, extra_video_ids=extra,
                           reply_counts=counts, key=b"k" * 16)


def test_fetch_requests_plain_text_newest_first():
    svc = FakeService([[thread("a", T0)]])
    yt(svc).fetch_comments(T0 - 3600)
    kw = svc.calls[0][1]
    assert kw["order"] == "time" and kw["textFormat"] == "plainText" and kw["maxResults"] == 100
    assert kw["part"] == "snippet,replies" and kw["videoId"] == "VID"


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


def test_owner_comments_are_flagged():
    owner = thread("a", T0, author="UCme")
    owner["snippet"]["channelId"] = "UCme"
    other = thread("b", T0, author="UCyou")
    other["snippet"]["channelId"] = "UCme"
    assert youtube.comment_from_thread(owner).by_owner
    assert not youtube.comment_from_thread(other).by_owner


def test_update_comment_rewrites_text_and_counts_quota():
    svc = FakeService([])
    y = yt(svc)
    y.update_comment("Ugpin", "Latest changes to the universe ...")
    kind, body = svc.calls[-1]
    assert kind == "update" and body == {"id": "Ugpin", "snippet": {"textOriginal": "Latest changes to the universe ..."}}
    assert y.quota.used == 50


def test_inline_replies_play_and_carry_the_thread_owner():
    svc = FakeService([[thread("t1", T0, replies=[reply("t1.r1", T0 + 60, author="UCme"),
                                                   reply("t1.r2", T0 + 90)])]])
    out = yt(svc).fetch_comments(T0 - 3600, now=T0 + 100)
    assert [(c.id, c.reply, c.by_owner) for c in out] == [("t1", False, False), ("t1.r1", True, True),
                                                           ("t1.r2", True, False)]
    assert svc.calls[0][1]["part"] == "snippet,replies"


def test_partial_replies_are_read_in_full_once_per_count():
    inline = [reply("t1.r1", T0)]
    full = [reply(f"t1.r{i}", T0 + i) for i in range(1, 8)]
    svc = FakeService([[thread("t1", T0, replies=inline, total=7)]], replies={"t1": full})
    counts = {}
    y = yt(svc, counts=counts)
    assert len(y.fetch_comments(T0 - 3600, now=T0)) == 1 + 7
    assert y.quota.used == 2 and len(counts) == 1 and "t1" not in str(counts)  # keyed, not raw
    y.fetch_comments(T0 - 3600, now=T0 + 300)  # same count: inline only, no comments.list
    assert y.quota.used == 3
    svc.pages = [[thread("t1", T0, replies=inline, total=8)]]
    svc.replies["t1"] = full + [reply("t1.r8", T0 + 400)]
    assert "t1.r8" in {c.id for c in y.fetch_comments(T0 - 3600, now=T0 + 600)}
    assert y.quota.used == 5


def test_reply_counts_are_forgotten_after_retention():
    from admission import RETENTION_S
    counts = {"old": [3, T0 - RETENTION_S - 1], "new": [3, T0]}
    yt(FakeService([[]]), counts=counts).fetch_comments(T0 - 60, now=T0)
    assert set(counts) == {"new"}


def test_watched_threads_are_fetched_even_when_old():
    old = thread("pin", T0 - 9 * 86400, replies=[reply("pin.r1", T0 - 60)])
    svc = FakeService([[thread("t1", T0)]], by_id={"pin": old})
    out = yt(svc).fetch_comments(T0 - 3600, watch=["pin", "t1"], now=T0)
    assert {c.id for c in out} == {"t1", "pin", "pin.r1"}
    by_id = [kw for _, kw in svc.calls if "id" in kw]
    assert by_id == [{"part": "snippet,replies", "id": "pin", "textFormat": "plainText"}]  # t1 already seen


def test_extra_videos_are_read_and_tagged():
    svc = FakeService([[thread("m1", T0)]], video_pages={"SHORT": [[thread("s1", T0, video="SHORT")]]})
    out = yt(svc, extra=["SHORT", "VID", ""]).fetch_comments(T0 - 3600, now=T0)
    assert sorted((c.id, c.video) for c in out) == [("m1", 0), ("s1", 1)]
    assert [kw["videoId"] for _, kw in svc.calls] == ["VID", "SHORT"]  # duplicates and blanks dropped


def test_the_description_says_when_the_title_last_changed():
    from youtube import with_title_stamp
    desc = "This title updates itself: it counts down to the moment this video dies.\n\nMore text."
    once = with_title_stamp(desc, "Mon Nov 9, 3:15 PM ET")
    assert once == ("This title updates itself: it counts down to the moment this video dies. "
                    "Last update: Mon Nov 9, 3:15 PM ET.\n\nMore text.")
    twice = with_title_stamp(once, "Mon Nov 9, 3:30 PM ET")
    assert twice.count("Last update") == 1 and "3:30 PM ET." in twice and twice.endswith("\n\nMore text.")
    assert with_title_stamp("Some other description", "x") == "Some other description"
    assert with_title_stamp("", "x") == ""


def test_clock_reads_like_a_person_wrote_it():
    import render
    t = 1794255300  # 2026-11-09 20:15 UTC
    assert render.clock(t) == "Mon Nov 9, 3:15 PM ET"
    assert render.clock(t, date=False) == "Mon 3:15 PM ET"


class ChatService:
    """liveBroadcasts.list and liveChatMessages.list, scripted."""

    def __init__(self, broadcasts=(), pages=(), errors=()):
        self.broadcasts, self.pages, self.errors, self.calls = list(broadcasts), list(pages), list(errors), []

    def liveBroadcasts(self):
        return self

    def liveChatMessages(self):
        return self

    def list(self, **kw):
        self.calls.append(kw)
        if "broadcastStatus" in kw:
            return Req(lambda: {"items": self.broadcasts})

        def run():
            if self.errors:
                raise self.errors.pop(0)
            return self.pages.pop(0)
        return Req(run)


class FakeHttpError(Exception):
    def __init__(self, status, reason):
        import json
        super().__init__(reason)
        self.resp = type("R", (), {"status": status})()
        self.content = json.dumps({"error": {"errors": [{"reason": reason}], "message": reason}}).encode()


def chat_msg(mid, text, t, author="UCv", owner=False, kind="textMessageEvent"):
    return {"id": mid, "snippet": {"type": kind, "publishedAt": iso(t).replace("Z", ".123456+00:00"),
                                   "textMessageDetails": {"messageText": text}, "displayMessage": text},
            "authorDetails": {"channelId": author, "isChatOwner": owner, "displayName": "Viv"}}


def chat_yt(svc, chat=None):
    q = youtube.Quota({}, T0)
    return youtube.YouTube(svc, "VID", q, key=b"k" * 16, chat={} if chat is None else chat)


def test_chat_messages_play_like_comments_and_pick_up_where_they_left_off():
    svc = ChatService(broadcasts=[{"id": "B1", "snippet": {"liveChatId": "LC1"}}],
                      pages=[{"items": [chat_msg("m1", "glider 3,4", T0),
                                        chat_msg("m2", "pinned: comment on the video", T0, owner=True),
                                        chat_msg("m3", "", T0, kind="superStickerEvent")],
                              "nextPageToken": "p1", "pollingIntervalMillis": 1000},
                             {"items": [chat_msg("m4", "hi", T0 + 60)], "nextPageToken": "p2"}])
    state = {}
    y = chat_yt(svc, state)
    out = y.fetch_chat(T0)
    assert [(c.id, c.text, c.video, c.author) for c in out] == [("chat:m1", "glider 3,4", youtube.CHAT, "UCv")]
    assert abs(out[0].epoch - (T0 + 0.123456)) < 1e-6  # fractional seconds and +00:00 parse
    assert state == {"looked": T0, "id": "LC1", "token": "p1"} and y.quota.used == 1 + 5
    assert [c.id for c in chat_yt(svc, state).fetch_chat(T0 + 300)] == ["chat:m4"]
    assert svc.calls[-1]["pageToken"] == "p1" and "broadcastStatus" not in svc.calls[-1]  # no second lookup
    assert state["token"] == "p2"


def test_no_live_broadcast_means_no_chat_and_a_slow_look():
    svc = ChatService()
    state = {}
    assert chat_yt(svc, state).fetch_chat(T0) == [] and len(svc.calls) == 1
    assert chat_yt(svc, state).fetch_chat(T0 + 600) == [] and len(svc.calls) == 1  # looks again after 30 min
    chat_yt(svc, state).fetch_chat(T0 + youtube.CHAT_LOOKUP_S + 1)
    assert len(svc.calls) == 2


def test_an_ended_chat_is_forgotten_and_a_stale_token_restarts():
    svc = ChatService(errors=[FakeHttpError(403, "liveChatEnded")])
    state = {"id": "LC1", "token": "p9"}
    assert chat_yt(svc, state).fetch_chat(T0) == [] and state == {}
    svc = ChatService(pages=[{"items": [chat_msg("m1", "hi", T0)], "nextPageToken": "p1"}],
                      errors=[FakeHttpError(400, "pageTokenInvalid")])
    state = {"id": "LC1", "token": "old"}
    assert [c.id for c in chat_yt(svc, state).fetch_chat(T0)] == ["chat:m1"]
    assert svc.calls[-1]["pageToken"] is None and state["token"] == "p1"
    svc = ChatService(errors=[FakeHttpError(403, "quotaExceeded")])
    with pytest.raises(FakeHttpError):  # anything else is the engine's to log
        chat_yt(svc, {"id": "LC1"}).fetch_chat(T0)


class StreamService:
    """liveBroadcasts.list plus videos.list/update, for retitling the live stream."""

    def __init__(self, live=("B1",), title="old"):
        self.live, self.calls = list(live), []
        self.snippet = {"title": title, "description": "about", "categoryId": "28", "channelId": "UCme"}

    def liveBroadcasts(self):
        return self

    def videos(self):
        return self

    def list(self, **kw):
        self.calls.append(("list", kw))
        if "broadcastStatus" in kw:
            return Req(lambda: {"items": [{"id": b} for b in self.live]})
        return Req(lambda: {"items": [{"snippet": dict(self.snippet)}]})

    def update(self, part, body):
        self.calls.append(("update", body))
        return Req(lambda: body)


def test_the_live_streams_title_is_changed_and_nothing_else():
    svc = StreamService()
    yt = youtube.YouTube(svc, "MAIN", youtube.Quota({}, T0))
    assert yt.set_stream_title("Day 4") == "B1"
    up = [c[1] for c in svc.calls if c[0] == "update"]
    assert up == [{"id": "B1", "snippet": {"title": "Day 4", "description": "about", "categoryId": "28"}}]
    assert yt.quota.used == 52


def test_the_live_streams_title_is_left_alone_when_right_or_nothing_is_live():
    svc = StreamService(title="Day 4")
    yt = youtube.YouTube(svc, "MAIN", youtube.Quota({}, T0))
    assert yt.set_stream_title("Day 4") == "B1" and not [c for c in svc.calls if c[0] == "update"]
    svc = StreamService(live=())
    assert youtube.YouTube(svc, "MAIN", youtube.Quota({}, T0)).set_stream_title("Day 4") is None
    assert len(svc.calls) == 1


def test_the_chats_history_gives_names_without_moving_our_place():
    svc = ChatService(pages=[{"items": [chat_msg("m1", "hi", T0), chat_msg("m2", "x", T0, owner=True)]}])
    chat = {"id": "LC1", "token": "T5"}
    yt = chat_yt(svc, chat)
    got = yt.chat_history()
    assert [(c.id, c.name) for c in got] == [("chat:m1", "Viv")] and chat["token"] == "T5"
    assert "pageToken" not in svc.calls[-1] and yt.quota.used == 5
    assert chat_yt(ChatService(), {}).chat_history() == []


def test_even_the_reads_stop_short_of_googles_limit():
    import pytest
    q = youtube.Quota({}, 1_800_000_000)
    q.state["used"] = youtube.HARD_CAP - 5
    q.spend(5, critical=True)  # reads go on past the bot's own budget...
    with pytest.raises(youtube.QuotaExceeded):
        q.spend(1, critical=True)  # ...but not past the hard cap
