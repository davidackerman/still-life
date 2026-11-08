#!/usr/bin/env python3
"""Alerts for the owner: a GitHub issue (GitHub emails it) when the universe
gets stuck, is saved, dies, or when the death sequence needs a human.

tick.py writes the run's alerts to out/alerts.json; the workflow then runs

    python alerts.py post out/alerts.json      # needs GH_TOKEN and gh

Issues are public, like the log: game data only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
LIVE = "https://davidackerman.github.io/still-life/"


def when(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, ET).strftime("%a %b %-d, %-I:%M %p ET")


def alerts_for(events: list[dict], launch_time: float, gen_seconds: int, video_url: str | None = None) -> list[dict]:
    """The alerts a run's events deserve: stuck (from ALIVE), saved, died, and
    death-sequence milestones. Re-entering or switching loops mid-countdown, and
    comments contesting it, don't alert: that would be noisy."""
    t = lambda gen: launch_time + gen * gen_seconds  # noqa: E731
    links = f"\n\nLive: {LIVE}" + (f"\nVideo: {video_url}" if video_url else "")
    out = []
    for e in events:
        if e["type"] == "phase" and e["reason"] == "cycle_detected":
            out.append({"title": f"🔴 Stuck in a loop at gen {e['gen']:,}: dies {when(t(e['deadline_gen']))} unless saved",
                        "body": "The 24-hour countdown has started. A comment that breaks the loop can save it.\n\n"
                                "Good moment for a new Short: `python short.py`, upload it, and add its ID to "
                                "EXTRA_VIDEO_IDS." + links})
        elif e["type"] == "phase" and e["reason"] == "survived_deadline":
            out.append({"title": f"🫀 Saved at gen {e['gen']:,}",
                        "body": "It was still changing when the countdown ran out, so it lives on." + links})
        elif e["type"] == "phase" and e["to"] == "DIED":
            out.append({"title": f"⚫ The universe died at gen {e['gen']:,}",
                        "body": "Nobody broke the loop in time. The death sequence starts on the next run." + links})
        elif e["type"] == "death" and e.get("alert"):
            out.append({"title": e["alert"], "body": e.get("detail", "") + links})
    return out


def post(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        alerts = json.load(f)
    repo = os.environ.get("GITHUB_REPOSITORY")
    failed = 0
    for a in alerts:
        cmd = ["gh", "issue", "create", "--title", a["title"], "--body", a["body"]]
        if repo:
            cmd += ["--repo", repo]
        r = subprocess.run(cmd, capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
        failed += r.returncode != 0
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "post":
        sys.exit(post(sys.argv[2]))
    print(__doc__)
    sys.exit(2)
