"""YouTube Data API v3 access.

YouTube is display and input only. Nothing in this module may touch the
authoritative simulation state; callers decide what to do with failures.

Step 1 contents: credentials from env and snippet helpers used by smoke_test.py.
The full client (comments, titles, thumbnails, quota, uploads) arrives in step 3.
"""

from __future__ import annotations

import os

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
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise ConfigError(f"missing environment variable(s): {', '.join(missing)}")
    return {n: os.environ[n] for n in names}


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
