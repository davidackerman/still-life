"""Upload a hand-made Short to the channel: public, subscribers notified (as the bot's slot Shorts are).

    python blender/upload_short.py VIDEO.mp4 SNIPPET.json

The channel login (CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN) comes from the environment, or else from the file named
by STILL_LIFE_ENV (default ~/.config/still-life/youtube.env, KEY=value lines, chmod 600). Nothing here prints it.
It refuses to upload a second Short with the same slot marker.
"""
import json
import os
import re
import stat
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import youtube

KEYS = ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")
video, snippet_path = sys.argv[1], sys.argv[2]

if not all(os.environ.get(k) for k in KEYS):
    path = os.path.expanduser(os.environ.get("STILL_LIFE_ENV", "~/.config/still-life/youtube.env"))
    if os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        sys.exit(f"{path} is readable by others: chmod 600 it")
    for line in open(path, encoding="utf-8"):
        k, _, v = line.strip().partition("=")
        if k in KEYS:
            os.environ[k] = v.strip().strip('"').strip("'")
    missing = [k for k in KEYS if not os.environ.get(k)]
    if missing:
        sys.exit(f"{path} lacks {', '.join(missing)}")

snippet = json.load(open(snippet_path, encoding="utf-8"))
m = re.search(r"\((still-life universe \d+ day \d+ slot \d+)\)\s*$", snippet["description"])
if not m:
    sys.exit("the description doesn't end with a slot marker: the bot would not adopt this upload")
mark = m.group(1)

svc = youtube.build_service(youtube.credentials_from_env())
yt = youtube.YouTube(svc, "oFWeLMe4bhI", youtube.Quota({}, time.time()))
already = yt.find_upload(mark)
if already:
    sys.exit(f"already uploaded with {mark!r}: {already}")
print("uploading:", snippet["title"], "|", mark)
print("uploaded:", yt.upload_video(video, snippet, notify=True))
