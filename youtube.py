"""YouTube Data API v3 access.

YouTube is display and input only. Nothing in this module may touch the
authoritative simulation state; callers decide what to do with failures.

Quota costs (checked 2026-11-07, developers.google.com/youtube/v3/determine_quota_cost):
commentThreads.list 1 (any parts), comments.list 1, videos.list 1,
videos.update 50, thumbnails.set 50, comments.update 50, channels.list 1. videos.insert is billed in its own "Video Uploads" bucket
(1 unit, 100 uploads/day), not the 10,000-unit pool. Every call costs at least
1 unit even when it fails. The pool resets at midnight Pacific.

Uploads and privacy changes (the death sequence) arrive in step 4.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from zoneinfo import ZoneInfo

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

# videos.update "part=snippet" overwrites every mutable snippet property: any
# property we omit that already has a value is deleted. These are the
# properties the videos.update docs list as writable (checked 2026-11-07).
# defaultAudioLanguage is not on that list but is returned by videos.list; we
# send it back when present so it cannot be cleared if it is in fact mutable.
WRITABLE_SNIPPET_FIELDS = (
    "title",
    "description",
    "tags",
    "categoryId",
    "defaultLanguage",
    "defaultAudioLanguage",
)

TITLE_MAX_CHARS = 100  # docs: max 100 characters, no '<' or '>'
DESCRIPTION_MAX_BYTES = 5000  # docs: max 5000 bytes (UTF-8), no '<' or '>'


class ConfigError(RuntimeError):
    pass


def require_env(*names: str) -> dict[str, str]:
    # Strip: values piped into `gh secret set` may carry a trailing newline.
    values = {n: os.environ.get(n, "").strip() for n in names}
    missing = [n for n, v in values.items() if not v]
    if missing:
        raise ConfigError(f"missing environment variable(s): {', '.join(missing)}")
    return values


def credentials_from_env():
    """OAuth user credentials built from CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN."""
    from google.oauth2.credentials import Credentials

    env = require_env("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")
    return Credentials(
        token=None,
        refresh_token=env["REFRESH_TOKEN"],
        token_uri=TOKEN_URI,
        client_id=env["CLIENT_ID"],
        client_secret=env["CLIENT_SECRET"],
        scopes=SCOPES,
    )


def build_service(credentials):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def writable_snippet(snippet: dict) -> dict:
    """The subset of a fetched video snippet that videos.update accepts back."""
    return {k: snippet[k] for k in WRITABLE_SNIPPET_FIELDS if k in snippet}


def validate_title(title: str) -> str:
    if not title.strip():
        raise ValueError("title is empty")
    if len(title) > TITLE_MAX_CHARS:
        raise ValueError(f"title is {len(title)} chars; max is {TITLE_MAX_CHARS}")
    if "<" in title or ">" in title:
        raise ValueError("title may not contain '<' or '>'")
    return title


def my_channel(service) -> dict:
    """The channel the credentials act as (1 quota unit)."""
    resp = service.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise ConfigError("these credentials have no YouTube channel")
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


def http_error_reason(err) -> tuple[int, str, str]:
    """(status, reason, message) from a googleapiclient HttpError."""
    import json

    status = getattr(getattr(err, "resp", None), "status", 0) or 0
    reason, message = "", str(err)
    try:
        body = json.loads(err.content.decode("utf-8"))
        e = body.get("error", {})
        message = e.get("message", message)
        errs = e.get("errors") or [{}]
        reason = errs[0].get("reason", "")
    except Exception:
        pass
    return int(status), reason, message


# --------------------------------------------------------------------------- quota
DAILY_BUDGET = 8000  # self-imposed; Google's default pool is 10,000/day
PACIFIC = ZoneInfo("America/Los_Angeles")
COST = {"commentThreads.list": 1, "comments.list": 1, "videos.list": 1, "videos.update": 50,
        "thumbnails.set": 50, "comments.update": 50, "commentThreads.insert": 50, "channels.list": 1,
        "playlistItems.list": 1, "liveBroadcasts.list": 1, "liveChatMessages.list": 5}
# videos.insert is not in this pool: it has its own bucket of 100 uploads a day (checked 2026-11-08).

# videos.update "part=status" also deletes any status property left out (docs,
# checked 2026-11-08), so a privacy change sends every writable one back.
# selfDeclaredMadeForKids and containsSyntheticMedia only change when sent.
WRITABLE_STATUS_FIELDS = ("embeddable", "license", "privacyStatus", "publicStatsViewable", "publishAt",
                          "selfDeclaredMadeForKids", "containsSyntheticMedia")


class QuotaExceeded(RuntimeError):
    skip = True  # engine treats this as a skip, not a failure


class Quota:
    """Estimated units used per Pacific day, kept in a dict owned by the caller."""

    def __init__(self, state: dict, now: float, budget: int = DAILY_BUDGET):
        self.state, self.budget = state, budget
        day = dt.datetime.fromtimestamp(now, PACIFIC).date().isoformat()
        if state.get("day") != day:
            state.clear()
            state.update({"day": day, "used": 0})

    @property
    def used(self) -> int:
        return self.state["used"]

    def spend(self, units: int, critical: bool = False) -> None:
        """Record units about to be spent. Non-critical spends over budget raise."""
        if not critical and self.state["used"] + units > self.budget:
            raise QuotaExceeded(f"daily budget {self.budget} would be exceeded "
                                f"({self.state['used']} used, {units} needed)")
        self.state["used"] += units


# --------------------------------------------------------------------------- client
GRID_BEGIN = "\u2500\u2500 live universe \u2500\u2500"  # markers around the description grid
GRID_END = "\u2500\u2500 end of universe \u2500\u2500"
MAX_PAGES = 20


TITLE_LINE = "This title updates itself"  # how the main video's description opens


def with_title_stamp(description: str, stamp: str) -> str:
    """Viewers can't tell a self-updating title from a static one, so the
    description's first line ("This title updates itself: …") ends with when it
    last changed. A description that doesn't open that way is left alone."""
    first, sep, rest = description.partition("\n")
    if not first.startswith(TITLE_LINE):
        return description
    first = first.split(" Last update: ")[0].rstrip()
    return f"{first} Last update: {stamp}." + sep + rest


def with_grid(description: str, grid_text: str) -> str | None:
    """Description with the grid block replaced/appended, or None if it won't fit."""
    if GRID_BEGIN in description and GRID_END in description:
        head, rest = description.split(GRID_BEGIN, 1)
        description = (head + rest.split(GRID_END, 1)[1]).rstrip()
    out = f"{description}\n\n{GRID_BEGIN}\n{grid_text}\n{GRID_END}".lstrip()
    if len(out.encode("utf-8")) > DESCRIPTION_MAX_BYTES or "<" in out or ">" in out:
        return None
    return out


def _comment(c: dict, video_channel: str | None, video: int, reply: bool):
    """admission.Comment from a comment resource (top-level or reply)."""
    from admission import Comment

    sn = c["snippet"]
    channel = (sn.get("authorChannelId") or {}).get("value")
    author = channel or "name:" + sn.get("authorDisplayName", "")
    owner = bool(channel) and channel == video_channel  # the video's own channel
    return Comment(id=c["id"], author=author, published_at=sn["publishedAt"],
                   text=sn.get("textDisplay", ""), by_owner=owner, reply=reply, video=video,
                   name=sn.get("authorDisplayName", ""))


def comment_from_thread(item: dict, video: int = 0):
    """admission.Comment for a commentThreads item's top-level comment."""
    return _comment(item["snippet"]["topLevelComment"], item["snippet"].get("channelId"), video, False)


def replies_in_thread(item: dict, video: int = 0) -> list:
    """The replies a commentThreads item carries inline (often only some of them).
    Replies have no videoId of their own, so ownership is judged against the thread's channel."""
    channel = item["snippet"].get("channelId")
    return [_comment(r, channel, video, True) for r in (item.get("replies") or {}).get("comments", [])]


CHAT = -1  # Comment.video for a message in the live stream's chat
CHAT_LOOKUP_S = 30 * 60  # with no live chat found, look for one again after this (1 unit)
CHAT_PAGE = 2000  # liveChatMessages.list maxResults (200..2000)
CHAT_GONE = ("liveChatEnded", "liveChatNotFound", "liveChatDisabled")


def chat_comment(m: dict):
    """admission.Comment for a live chat text message, or None (other events, and
    the chat owner's own messages: announcements, not moves)."""
    from admission import Comment

    sn, who = m.get("snippet") or {}, m.get("authorDetails") or {}
    if sn.get("type") != "textMessageEvent" or who.get("isChatOwner"):
        return None
    text = (sn.get("textMessageDetails") or {}).get("messageText") or sn.get("displayMessage", "")
    author = who.get("channelId") or sn.get("authorChannelId") or "name:" + who.get("displayName", "")
    return Comment(id="chat:" + m["id"], author=author, published_at=sn["publishedAt"], text=text, video=CHAT,
                   name=who.get("displayName", ""))


class YouTube:
    """The engine's view of YouTube: comments in, title and thumbnail out.

    Title and thumbnail belong to `video_id`. Comments (and replies) are read
    from it and from every id in `extra_video_ids` (e.g. a Short that feeds the
    same universe); Comment.video is the index into [video_id, *extra_video_ids].

    `reply_counts` (a dict owned by the caller and persisted, like the quota
    state) remembers how many replies each busy thread had when we last fetched
    them all, keyed by HMAC under `key`, so a thread is re-read only when its
    count changes.

    `chat` (also owned by the caller and persisted) is where fetch_chat keeps
    the channel's live chat: {"id": liveChatId, "token": next page, "looked": t}.
    These identify our own broadcast's chat, not anyone's messages.
    """

    def __init__(self, service, video_id: str, quota: Quota, description_grid: bool = False,
                 extra_video_ids=(), reply_counts: dict | None = None, key: bytes = b"", skip_video_ids=(),
                 chat: dict | None = None):
        self.service, self.video_id, self.quota = service, video_id, quota
        self.description_grid = description_grid
        self.videos = [video_id]
        for v in extra_video_ids:
            if v and v not in self.videos:
                self.videos.append(v)
        self.skip = set(skip_video_ids)  # not read this run (older Shorts are read every 30 min)
        self.reply_counts = {} if reply_counts is None else reply_counts
        self.key = key
        self.chat = {} if chat is None else chat

    def _live_chat(self, now: float) -> str | None:
        """The live chat of the channel's broadcast that is live now (remembered)."""
        if self.chat.get("id"):
            return self.chat["id"]
        if now - self.chat.get("looked", 0) < CHAT_LOOKUP_S:
            return None
        self.chat["looked"] = now
        self.quota.spend(COST["liveBroadcasts.list"], critical=True)
        resp = self.service.liveBroadcasts().list(part="snippet", broadcastStatus="active", broadcastType="all",
                                                  maxResults=5).execute()
        for b in resp.get("items", []):
            if (b.get("snippet") or {}).get("liveChatId"):
                self.chat.update(id=b["snippet"]["liveChatId"], token=None)
                return self.chat["id"]
        return None

    def fetch_chat(self, now: float | None = None) -> list:
        """New messages in the live stream's chat, as Comments (video=CHAT).

        Picks up where the last run stopped (the page token is kept), so nothing
        posted between runs is missed. When the chat has ended it is forgotten,
        and the next run looks for a new broadcast."""
        now = time.time() if now is None else now
        chat = self._live_chat(now)
        out = []
        for _ in range(MAX_PAGES if chat else 0):
            self.quota.spend(COST["liveChatMessages.list"], critical=True)
            try:
                resp = self.service.liveChatMessages().list(
                    liveChatId=chat, part="snippet,authorDetails", maxResults=CHAT_PAGE,
                    pageToken=self.chat.get("token")).execute()
            except Exception as e:  # noqa: BLE001 - sorted below; anything else goes up
                status, reason, _ = http_error_reason(e)
                if reason in CHAT_GONE or status == 404:
                    self.chat.clear()
                    return out
                if status == 400 and self.chat.get("token"):  # a stale page token: start from the latest
                    self.chat["token"] = None
                    continue
                raise
            items = resp.get("items", [])
            out += [c for c in map(chat_comment, items) if c]
            if resp.get("nextPageToken"):
                self.chat["token"] = resp["nextPageToken"]
            if len(items) < CHAT_PAGE or not resp.get("nextPageToken"):
                break
        return out

    def _threads(self, video: int, since: float) -> list[dict]:
        """Threads on one video, newest first, paging until older than `since`."""
        from admission import parse_time

        out, token = [], None
        for _ in range(MAX_PAGES):
            self.quota.spend(COST["commentThreads.list"], critical=True)
            resp = self.service.commentThreads().list(
                part="snippet,replies", videoId=self.videos[video], order="time", maxResults=100,
                textFormat="plainText", pageToken=token,
            ).execute()
            items = resp.get("items", [])
            out += items
            token = resp.get("nextPageToken")
            if not token or not items or min(
                    parse_time(i["snippet"]["topLevelComment"]["snippet"]["publishedAt"]) for i in items) < since:
                break
        return out

    def _all_replies(self, item: dict, video: int) -> list:
        """Every reply in a thread, via comments.list (the thread's inline list is partial)."""
        out, token = [], None
        channel = item["snippet"].get("channelId")
        for _ in range(MAX_PAGES):
            self.quota.spend(COST["comments.list"], critical=True)
            resp = self.service.comments().list(
                part="snippet", parentId=item["id"], maxResults=100, textFormat="plainText", pageToken=token,
            ).execute()
            out += [_comment(r, channel, video, True) for r in resp.get("items", [])]
            token = resp.get("nextPageToken")
            if not token:
                break
        return out

    def fetch_comments(self, since: float, watch=(), now: float | None = None) -> list:
        """Top-level comments and their replies on every configured video.

        Threads are paged newest first until older than `since`. The thread of
        each comment ID in `watch` (the pinned status comments) is fetched too,
        even when it is older, because people reply under it. A thread whose
        inline replies are partial is read in full, but only when its reply
        count changed since the last full read.
        """
        from admission import RETENTION_S, keyed

        now = time.time() if now is None else now
        found: dict[str, tuple[dict, int]] = {}
        for v in range(len(self.videos)):
            if self.videos[v] in self.skip:
                continue
            for item in self._threads(v, since):
                found.setdefault(item["id"], (item, v))
        missing = [t for t in watch if t and t not in found]
        if missing:
            self.quota.spend(COST["commentThreads.list"], critical=True)
            resp = self.service.commentThreads().list(
                part="snippet,replies", id=",".join(missing), textFormat="plainText").execute()
            for item in resp.get("items", []):
                vid = item["snippet"].get("videoId")
                found.setdefault(item["id"], (item, self.videos.index(vid) if vid in self.videos else 0))

        for k in [k for k, v in self.reply_counts.items() if v[1] < now - RETENTION_S]:
            del self.reply_counts[k]
        out = []
        for item, v in found.values():
            out.append(comment_from_thread(item, v))
            inline = replies_in_thread(item, v)
            total = item["snippet"].get("totalReplyCount", len(inline))
            k = keyed(self.key, "thread", item["id"])
            if total > len(inline) and (self.reply_counts.get(k) or [None])[0] != total:
                out += self._all_replies(item, v)
                self.reply_counts[k] = [total, now]
            else:
                out += inline
        return out

    def set_title(self, title: str, grid_text: str = "") -> None:
        """Change the title, the "Last update" time in the description's first
        line and, if enabled, the grid block in the description."""
        import render
        validate_title(title)
        self.quota.spend(COST["videos.list"] + COST["videos.update"])
        items = self.service.videos().list(part="snippet", id=self.video_id).execute().get("items", [])
        if not items:
            raise ConfigError(f"video {self.video_id} not found")
        body = writable_snippet(items[0]["snippet"])
        body["title"] = title
        desc = with_title_stamp(body.get("description", ""), render.clock(time.time()))
        if self.description_grid and grid_text:
            desc = with_grid(desc, grid_text) or desc
        if len(desc.encode("utf-8")) <= DESCRIPTION_MAX_BYTES:
            body["description"] = desc
        self.service.videos().update(part="snippet", body={"id": self.video_id, "snippet": body}).execute()

    def set_stream_title(self, title: str) -> str | None:
        """Retitle the channel's broadcast that is live now. Its id, or None if nothing is live."""
        validate_title(title)
        self.quota.spend(COST["liveBroadcasts.list"] + COST["videos.list"])
        live = self.service.liveBroadcasts().list(part="id", broadcastStatus="active", broadcastType="all",
                                                  maxResults=5).execute().get("items", [])
        if not live:
            return None
        vid = live[0]["id"]  # a broadcast's id is its video's id
        items = self.service.videos().list(part="snippet", id=vid).execute().get("items", [])
        if not items:
            return None
        body = writable_snippet(items[0]["snippet"])
        if body.get("title") != title:
            self.quota.spend(COST["videos.update"])
            body["title"] = title
            self.service.videos().update(part="snippet", body={"id": vid, "snippet": body}).execute()
        return vid

    def update_comment(self, comment_id: str, text: str) -> None:
        """Rewrite one of the channel owner's own comments (the pinned status comment)."""
        self.quota.spend(COST["comments.update"])
        self.service.comments().update(
            part="snippet", body={"id": comment_id, "snippet": {"textOriginal": text}}).execute()

    def set_thumbnail(self, path: str | None) -> None:
        from googleapiclient.http import MediaFileUpload

        if not path:
            return
        self.quota.spend(COST["thumbnails.set"])
        self.service.thumbnails().set(
            videoId=self.video_id, media_body=MediaFileUpload(path, mimetype="image/png")
        ).execute()

    # ------------------------------------------------------------ death sequence
    def upload_video(self, path: str, snippet: dict, privacy: str = "public") -> str:
        """Resumable upload; returns the new video's ID. (Uploads from unverified API
        projects are not restricted to private, per the videos.insert docs, 2026-11-08.)"""
        from googleapiclient.http import MediaFileUpload

        body = {"snippet": snippet,
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False,
                           "containsSyntheticMedia": False, "embeddable": True, "license": "youtube",
                           "publicStatsViewable": True}}
        media = MediaFileUpload(path, mimetype="video/mp4", chunksize=16 * 1024 * 1024, resumable=True)
        req = self.service.videos().insert(part="snippet,status", body=body, media_body=media,
                                           notifySubscribers=True)
        resp = None
        while resp is None:
            _, resp = req.next_chunk(num_retries=5)
        return resp["id"]

    def video_state(self, video_id: str) -> dict:
        """Upload/processing/privacy status of one of our videos (owner-only fields)."""
        self.quota.spend(COST["videos.list"], critical=True)
        items = self.service.videos().list(part="status,processingDetails", id=video_id).execute().get("items", [])
        if not items:
            return {"exists": False}
        st, pd = items[0].get("status", {}), items[0].get("processingDetails", {})
        return {"exists": True, "upload": st.get("uploadStatus"), "privacy": st.get("privacyStatus"),
                "processing": pd.get("processingStatus"), "failure": st.get("failureReason") or
                st.get("rejectionReason") or pd.get("processingFailureReason")}

    def set_private(self, video_id: str) -> None:
        """Make a video private, sending back every writable status property."""
        self.quota.spend(COST["videos.list"] + COST["videos.update"], critical=True)
        items = self.service.videos().list(part="status", id=video_id).execute().get("items", [])
        if not items:
            raise ConfigError(f"video {video_id} not found")
        status = {k: v for k, v in items[0]["status"].items() if k in WRITABLE_STATUS_FIELDS}
        status.pop("publishAt", None)  # only for never-published private videos
        status["privacyStatus"] = "private"
        self.service.videos().update(part="status", body={"id": video_id, "status": status}).execute()

    def snippet_of(self, video_id: str) -> dict:
        """The writable snippet of one of our videos (a template for the next upload)."""
        self.quota.spend(COST["videos.list"], critical=True)
        items = self.service.videos().list(part="snippet", id=video_id).execute().get("items", [])
        return writable_snippet(items[0]["snippet"]) if items else {}

    def find_upload(self, marker: str, limit: int = 50) -> str | None:
        """The ID of a recent upload of ours whose description contains `marker`
        (so a run that crashed after uploading never uploads twice)."""
        self.quota.spend(COST["channels.list"] + COST["playlistItems.list"], critical=True)
        ch = self.service.channels().list(part="contentDetails", mine=True).execute()["items"][0]
        uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
        items = self.service.playlistItems().list(part="snippet", playlistId=uploads,
                                                  maxResults=limit).execute().get("items", [])
        for it in items:
            if marker in it["snippet"].get("description", ""):
                return it["snippet"]["resourceId"]["videoId"]
        return None

    def post_comment(self, video_id: str, text: str) -> str:
        self.quota.spend(COST["commentThreads.insert"], critical=True)
        r = self.service.commentThreads().insert(part="snippet", body={"snippet": {
            "videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": text}}}}).execute()
        return r["id"]

    def display_name_of(self, keyed_comment: str) -> str | None:
        """The display name of the commenter whose comment ID hashes (HMAC under
        self.key) to `keyed_comment`, looked up fresh for the obituary's credit.
        Searches every thread and reply on the configured videos; never stored."""
        from admission import keyed

        for v in range(len(self.videos)):
            for item in self._threads(v, since=0.0):
                comments = [item["snippet"]["topLevelComment"]]
                inline = (item.get("replies") or {}).get("comments", [])
                if item["snippet"].get("totalReplyCount", 0) > len(inline):
                    token = None
                    for _ in range(MAX_PAGES):
                        self.quota.spend(COST["comments.list"], critical=True)
                        resp = self.service.comments().list(part="snippet", parentId=item["id"], maxResults=100,
                                                            textFormat="plainText", pageToken=token).execute()
                        inline = inline + resp.get("items", [])
                        token = resp.get("nextPageToken")
                        if not token:
                            break
                for c in comments + inline:
                    if keyed(self.key, "comment", c["id"]) == keyed_comment:
                        return c["snippet"].get("authorDisplayName")
        return None
