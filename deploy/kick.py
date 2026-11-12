#!/usr/bin/env python3
"""Start the bot's GitHub workflow whenever GitHub's own schedule falls behind.

GitHub runs the "every 5 minutes" schedule late, often 10-25 minutes apart. The
stream VM runs this every minute (deploy/still-life-kick.timer): if no tick run
has started in KICK_AFTER_S (3 minutes, so the live chat is read about that
often; the videos' comments every ~5, see tick.COMMENTS_EVERY_S) and none is
queued or running, it starts one (workflow_dispatch). GitHub's own schedule
stays on, as the backup.

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
KICK_AFTER_S = 3 * 60  # was 5 min until 2026-11-12
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


def due(runs: list[dict], now: dt.datetime) -> tuple[bool, float | None]:
    """(start one now?, minutes since the last run started)."""
    if any(r.get("status") in BUSY for r in runs):
        return False, None
    starts = [dt.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) for r in runs]
    if not starts:
        return True, None
    ago = (now - max(starts)).total_seconds()
    return ago >= KICK_AFTER_S, ago / 60


def main(now: dt.datetime | None = None, http=call) -> int:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return 0  # not set up yet
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        runs = http("GET", f"{API}/runs?per_page=5", token)["workflow_runs"]
        go, ago = due(runs, now)
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
