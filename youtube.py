"""YouTube Data API v3 access.

YouTube is display and input only. Nothing in this module may touch the
authoritative simulation state; callers decide what to do with failures.

Quota costs (checked 2026-11-07, developers.google.com/youtube/v3/determine_quota_cost):
commentThreads.list 1, videos.list 1, videos.update 50, thumbnails.set 50,
channels.list 1. videos.insert is billed in its own "Video Uploads" bucket
(1 unit, 100 uploads/day), not the 10,000-unit pool. Every call costs at least
1 unit even when it fails. The pool resets at midnight Pacific.

Uploads and privacy changes (the death sequence) arrive in step 4.
"""

from __future__ import annotations

import datetime as dt
import os
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
COST = {"commentThreads.list": 1, "videos.list": 1, "videos.update": 50, "thumbnails.set": 50}


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


def with_grid(description: str, grid_text: str) -> str | None:
    """Description with the grid block replaced/appended, or None if it won't fit."""
    if GRID_BEGIN in description and GRID_END in description:
        head, rest = description.split(GRID_BEGIN, 1)
        description = (head + rest.split(GRID_END, 1)[1]).rstrip()
    out = f"{description}\n\n{GRID_BEGIN}\n{grid_text}\n{GRID_END}".lstrip()
    if len(out.encode("utf-8")) > DESCRIPTION_MAX_BYTES or "<" in out or ">" in out:
        return None
    return out


def comment_from_thread(item: dict):
    """admission.Comment from a commentThreads.list item (top-level comment only)."""
    from admission import Comment

    top = item["snippet"]["topLevelComment"]
    sn = top["snippet"]
    author = (sn.get("authorChannelId") or {}).get("value") or "name:" + sn.get("authorDisplayName", "")
    return Comment(id=top["id"], author=author, published_at=sn["publishedAt"],
                   text=sn.get("textDisplay", ""))


class YouTube:
    """The engine's view of YouTube: comments in, title and thumbnail out."""

    def __init__(self, service, video_id: str, quota: Quota, description_grid: bool = False):
        self.service, self.video_id, self.quota = service, video_id, quota
        self.description_grid = description_grid

    def fetch_comments(self, since: float) -> list:
        """Top-level comments, newest first, paging until older than `since`."""
        from admission import parse_time

        out, token = [], None
        for _ in range(MAX_PAGES):
            self.quota.spend(COST["commentThreads.list"], critical=True)
            resp = self.service.commentThreads().list(
                part="snippet", videoId=self.video_id, order="time", maxResults=100,
                textFormat="plainText", pageToken=token,
            ).execute()
            items = [comment_from_thread(i) for i in resp.get("items", [])]
            out += items
            token = resp.get("nextPageToken")
            if not token or not items or min(parse_time(c.published_at) for c in items) < since:
                break
        return out

    def set_title(self, title: str, grid_text: str = "") -> None:
        """Change only the title (and, if enabled, the grid block in the description)."""
        validate_title(title)
        self.quota.spend(COST["videos.list"] + COST["videos.update"])
        items = self.service.videos().list(part="snippet", id=self.video_id).execute().get("items", [])
        if not items:
            raise ConfigError(f"video {self.video_id} not found")
        body = writable_snippet(items[0]["snippet"])
        body["title"] = title
        if self.description_grid and grid_text:
            desc = with_grid(body.get("description", ""), grid_text)
            if desc is not None:
                body["description"] = desc
        self.service.videos().update(part="snippet", body={"id": self.video_id, "snippet": body}).execute()

    def set_thumbnail(self, path: str | None) -> None:
        from googleapiclient.http import MediaFileUpload

        if not path:
            return
        self.quota.spend(COST["thumbnails.set"])
        self.service.thumbnails().set(
            videoId=self.video_id, media_body=MediaFileUpload(path, mimetype="image/png")
        ).execute()
