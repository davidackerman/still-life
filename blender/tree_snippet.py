"""The tree-of-life Short's title and description, as the bot's 8 PM Short would write them, with the slot marker
(so the bot's 8 PM run adopts this upload instead of posting its own countdown).

    python blender/tree_snippet.py OUT.json VOXELS.npz

VOXELS.npz is the build_tree_future.py output the Short was rendered from (its example comment goes in the
description). Run it from fresh state/ (git pull) shortly before the upload: the forecast in it moves.
"""
import json
import os
import sys
import datetime as dt
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import numpy as np

import daily, engine, store
from store import LOG, read_log

AT = 20  # the 8 PM slot (daily.SLOTS_ET)
ET = ZoneInfo("America/New_York")
TITLE = "Hundreds of YouTubers are building a tree of life #GameOfLife"
REPLAY = "This Short is a replay: your comment lands in the live universe within minutes."

out, voxels = sys.argv[1], sys.argv[2]
comment = str(np.load(voxels)["comment"])
st = store.Store(os.path.join(ROOT, "state"))
world = st.load(b"not-needed-for-this")
day = daily.day_number(world)
if world.forecast is None:
    engine.refresh_forecast(world)
hours = daily.recap_hours(AT)
since = max(0, world.universe.generation - hours * 3600 // world.gen_seconds)
entries = read_log(st.path(LOG))
count = sum(e.get("comments_new", 0) for e in entries if e["type"] == "tick" and e["gen_end"] > since)
seen = engine.comment_counts(world, world.universe.generation)
total = seen[0] if seen else sum(1 for e in entries if e["type"] == "injection")
mark = daily.marker(world, day, AT)
# the day at 8 PM must be the day now, or the bot looks for another marker
now = dt.datetime.now(ET)
gens_to_8pm = max(0.0, (now.replace(hour=20, minute=30, second=0) - now).total_seconds() / world.gen_seconds)
assert int((world.universe.generation + gens_to_8pm) * world.gen_seconds // 86400) + 1 == day, \
    "the day number changes before tonight's slot run: the marker would be wrong"

sn = daily.snippet(world, day, count, mark, total, world.video_id or "oFWeLMe4bhI",
                   "Hundreds of YouTubers are building a tree of life", "made", hours, True)
sn["title"] = TITLE
# this Short shows a forecast and an example comment, not only a replay: say so
assert REPLAY in sn["description"]
sn["description"] = sn["description"].replace(REPLAY,
    "Each leaf color is one comment and everything that grew from it. The part that withers is a forecast of what "
    "happens if nobody comments; the comment that brings it back (" + comment + ") is an example. Your comment lands "
    "in the live universe within minutes.")
assert sn["description"].rstrip().endswith(f"({mark})"), "the slot marker must end the description"
json.dump(sn, open(out, "w", encoding="utf-8"), ensure_ascii=False)
print("day", day, "| marker:", mark)
print(sn["title"])
print("---")
print(sn["description"])
print("---", sn["tags"])
