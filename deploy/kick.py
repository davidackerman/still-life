#!/usr/bin/env python3
"""Start the bot's GitHub workflow whenever GitHub's own schedule falls behind.

GitHub runs the "every 5 minutes" schedule late, often 10-25 minutes apart. The
stream VM runs this every minute (deploy/still-life-kick.timer): if no tick run
has started in the gap below and none is queued or running, it starts one
(workflow_dispatch). GitHub's own schedule stays on, as the backup.

The gap follows the players (each run reads the live chat; the videos' comments
every ~5 minutes whatever the gap, see tick.COMMENTS_EVERY_S). Every run's log
line says how many new comments and chat messages it read; this reads that from
the repo checkout the stream keeps up to date:
  - a new chat message in the last 10 minutes: a run every minute
  - any new comment or chat message in the last hour: every 3 minutes
  - quieter than that: every 5 minutes
and it backs off as the day's YouTube quota fills (the bot's count, in
state.json): no 1-minute runs past 6,500 units, every 5 minutes past 7,500.

It needs GITHUB_TOKEN in /etc/still-life/github.env (root-only): a fine-grained
token for this repository only, with "Actions: read and write" and nothing else.
It can start and cancel the bot's runs; it can't change code, settings or
secrets. Without it this does nothing. The token is never printed.
Standard library only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

REPO = "davidackerman/still-life"
WORKFLOW = "tick.yml"
CHAT_WITHIN_S, CHAT_GAP_S = 10 * 60, 60  # chat lately: a run every minute
ANY_WITHIN_S, ANY_GAP_S = 60 * 60, 3 * 60  # any comment this hour: every 3
QUIET_GAP_S = 5 * 60
BUDGET_GAPS = [(7500, QUIET_GAP_S), (6500, ANY_GAP_S)]  # (quota used today at least, shortest gap)
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")
LOG = os.path.join(STATE, "log.jsonl")
API = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}"
BUSY = {"queued", "in_progress", "waiting", "requested", "pending"}


def call(method: str, url: str, token: str, body: dict | None = None):
    req = urllib.request.Request(
        url, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "still-life-kick"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    return json.loads(data) if data else None


def last_activity(path: str = LOG, tail: int = 200_000) -> tuple[float | None, float | None]:
    """(when a run last read a new chat message, when it last read anything new),
    epochs from the end of the game's log; None where it can't tell."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - tail))
            lines = f.read().decode("utf-8", "replace").splitlines()[1:]
    except OSError:
        return None, None
    chat = anything = None
    for line in reversed(lines):
        if '"tick"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "tick":
            continue
        if anything is None and e.get("comments_new"):
            anything = e.get("now")
        if chat is None and e.get("chat_new"):
            chat = e.get("now")
        if chat is not None and anything is not None:
            break
    return chat, anything


def quota_used(now: dt.datetime, path: str = os.path.join(STATE, "state.json")) -> int:
    """The bot's count of today's (Pacific) YouTube quota units, or 0 if unknown."""
    try:
        with open(path, encoding="utf-8") as f:
            q = json.load(f).get("quota") or {}
    except (OSError, ValueError):
        return 0
    from zoneinfo import ZoneInfo
    today = now.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
    return int(q.get("used", 0)) if q.get("day") == today else 0


def gap(now: dt.datetime, chat: float | None, anything: float | None, used: int = 0) -> int:
    """Seconds between runs: shorter while people play, longer as the quota fills."""
    t = now.timestamp()
    every = (CHAT_GAP_S if chat is not None and t - chat <= CHAT_WITHIN_S
             else ANY_GAP_S if anything is not None and t - anything <= ANY_WITHIN_S else QUIET_GAP_S)
    for at_least, shortest in BUDGET_GAPS:
        if used >= at_least:
            return max(every, shortest)
    return every


def due(runs: list[dict], now: dt.datetime, every: int = QUIET_GAP_S) -> tuple[bool, float | None]:
    """(start one now?, minutes since the last run started)."""
    if any(r.get("status") in BUSY for r in runs):
        return False, None
    starts = [dt.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) for r in runs]
    if not starts:
        return True, None
    ago = (now - max(starts)).total_seconds()
    return ago >= every - 5, ago / 60  # (the timer itself wanders a few seconds)


def main(now: dt.datetime | None = None, http=call, log: str = LOG, state: str | None = None) -> int:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return 0  # not set up yet
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        runs = http("GET", f"{API}/runs?per_page=5", token)["workflow_runs"]
        used = quota_used(now, state) if state else quota_used(now)
        go, ago = due(runs, now, gap(now, *last_activity(log), used))
        if go:
            http("POST", f"{API}/dispatches", token, {"ref": "main"})
            print(f"started a run (the last one began {ago:.0f} min ago)" if ago is not None else "started a run")
    except urllib.error.HTTPError as e:
        print(f"GitHub said {e.code} {e.reason}", file=sys.stderr)  # 401/403: the token is wrong or expired
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"couldn't reach GitHub: {e}", file=sys.stderr)  # try again next minute
    return 0


if __name__ == "__main__":
    sys.exit(main())
