# still-life: running it

How the bot is set up, how it works and where everything lives. For what it is and how to play, see the [README](README.md).

## Setup

### 1. Google Cloud project and OAuth client

1. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project and enable **YouTube Data API v3**.
2. Configure the **OAuth consent screen** (External). Add your own Google
   account as a test user.
3. **Set the publishing status to "In production"** before any multi-day run.
   While it is "Testing", Google expires refresh tokens after **7 days**, which
   would quietly stop the bot partway through the week-long dry run. You do not
   need Google verification to use your own app. You will see an "unverified
   app" warning on the consent screen; click through it.
4. Create an **OAuth client ID** of type **Desktop app** and download its
   JSON as `client_secret.json` in this directory. It is gitignored.

### 2. Get a refresh token

```sh
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt google-auth-oauthlib   # oauthlib is only for auth.py
python auth.py --client-secrets client_secret.json
```

If your channel is a Brand Account, choose that channel on the consent screen.
The script prints the channel it authorized and the refresh token. Treat the
token like a password. Put it in a secret store; never paste it into a file in
this repo.

### 3. Smoke test (milestone 1)

Use an **unlisted** test video. The script refuses to run on a public video
unless you pass `--allow-public`.

```sh
export CLIENT_ID=... CLIENT_SECRET=... REFRESH_TOKEN=... VIDEO_ID=...
python smoke_test.py
```

The script checks that the token's channel owns the video, saves the original
snippet to `smoke_backup.json`, and sets a test title. It then reads the video
back to confirm that the description, tags, category and languages did not
change. Next it uploads a test thumbnail and waits for you to press Enter.
Finally it restores the original title and snippet. It costs about 160–190 quota
units.

**The thumbnail is not restored.** The API cannot remove a custom thumbnail.
Custom thumbnails also need a verified channel
(<https://www.youtube.com/verify>). Without one, `thumbnails.set` returns 403.

### 4. Public pages and publishing the OAuth app

Google needs a home page and a privacy policy before the OAuth app can leave
Testing mode, where refresh tokens expire after 7 days. Both pages live in
`docs/` and are served by GitHub Pages:

- Home: <https://davidackerman.github.io/still-life/>
- Privacy: <https://davidackerman.github.io/still-life/privacy.html>

1. In Google Cloud → Google Auth Platform → **Branding**, enter both URLs and
   add the authorized domain `davidackerman.github.io`, then **Save**.
2. Go to **Audience** → **Publish app**.
3. Remove the old grant at <https://myaccount.google.com/connections>, run
   `auth.py` again, and store the new token. Tokens issued in Testing mode
   keep their 7-day expiry even after you publish.

### 5. Run the bot on GitHub Actions

Set the repository secrets. These commands pipe each value straight into
`gh`, so nothing is echoed:

```sh
python -c "import json;print(json.load(open('client_secret.json'))['installed']['client_id'])" | gh secret set CLIENT_ID
python -c "import json;print(json.load(open('client_secret.json'))['installed']['client_secret'])" | gh secret set CLIENT_SECRET
security find-generic-password -s still-life-refresh-token -w | gh secret set REFRESH_TOKEN
echo -n "YOUR_VIDEO_ID" | gh secret set VIDEO_ID
echo -n "SHORT_ID,OTHER_ID" | gh secret set EXTRA_VIDEO_IDS   # optional, see below
python -c "import secrets;print(secrets.token_urlsafe(32))" | gh secret set STATE_KEY
```

`STATE_KEY` keys the hashes of commenter IDs. Nobody needs to know it, but
keep it stable: changing it makes the bot forget who already commented.

`EXTRA_VIDEO_IDS` (optional) lists more of the channel's videos, such as a
Short, whose comments and replies also play. The title and thumbnail stay on
`VIDEO_ID`. Each extra video costs about 290 quota units a day to read.

Then switch the workflow on and start the first run:

```sh
gh variable set TICK_ENABLED --body true
gh workflow run tick.yml
```

The first run creates `state/` with generation 0 at that moment. Every 5
minutes after that, `.github/workflows/tick.yml` runs `tick.py` and commits
`state/`. `DRY_RUN` is `"true"` in the workflow, so the death sequence never
uploads anything or changes privacy until you change that line yourself.

To pause, run `gh variable set TICK_ENABLED --body false`. Missed runs are
harmless: each run catches the universe up to the current time.

**Quota**: about 5,700 of the default 10,000 units a day, mostly title updates:
the countdown ("dies in 3d 13h 45m") ticks every 15 minutes at 51 units each. The bot skips title and thumbnail updates past a
self-imposed 8,000. It never skips reading comments.

**Known limitations**: comments that YouTube holds for review are invisible
until approved, and comments older than 48 hours are never read. Replies are
read from the threads the bot pages through (newest first, back 48 hours) and
from the pinned status comments; a reply to an older thread on a busy video can
be missed. A thread whose replies don't all arrive inline is read in full only
when its reply count changes. GitHub may
delay or drop scheduled runs during heavy load. GitHub disables scheduled
workflows in repositories with no activity for 60 days; the bot's own commits
probably count as activity, but GitHub's documentation doesn't say.

**Thumbnail limit**: YouTube answers HTTP 429 ("uploaded too many thumbnails
recently") after a handful of thumbnail uploads in a day. The bot refreshes the
thumbnail every 4 hours and on phase changes only, and backs off when refused;
avoid forcing extra uploads.

### 6. Pinned status comment (optional)

Post a comment on the video that contains **Latest changes to the universe**,
then pin it (⋮ → Pin). Within about 5 minutes the bot finds it (your own
comment, with that phrase), remembers its ID, leaves it out of the game, and
from then on rewrites it:

```
🟢 ALIVE · this video dies in ~9 days if nobody comments
The video is the explainer. The universe itself lives in this video's thumbnail and on the live page (link at the bottom), and the title counts down to its death. Every comment adds cells (up to 3 moves per person every 2 minutes; replies count too).

Latest moves:
• gen 220: glider 16,61 sw
• gen 166: glider 64,71 nw

Try one: rpent 39,6 (a quiet corner right now) · or just say anything
Rewind every move: davidackerman.github.io/still-life
```

When a comment saves the universe from a countdown, it is credited:
`🫀 gen 2,575: SAVED, thanks to glider 4,20 ne (gen 1,875)`. The comment is
edited only when something real changes (a move, a phase change, the coarse
countdown), at most every 30 minutes, at 50 quota units each. If YouTube holds
it because of the link, run `gh variable set PINNED_LINK --body false`.

Each video in `EXTRA_VIDEO_IDS` can have its own status comment, posted and
pinned the same way. It shows the same status and moves, with a line saying
it feeds the same universe as the main video, and is edited at most hourly.

### 7. A Short (optional)

```sh
python short.py          # out/short.mp4: 1080x1920, ~40 s, from state/ (needs ffmpeg)
```

It replays the real log: the acorn, every comment at the generation it landed
(zooming in on where it landed), the board as it stands, and its future if
nobody comments (stuck, then grey). The board is the right way up, as in the
main video's thumbnail, so a comment's x,y matches the screen; the wrap-around
continues faintly above and below it (it is a torus). Re-render when
there is news, such as the 24-hour countdown starting.

Upload it in YouTube Studio, set its **Related video** to the main video (the
only clickable link a Short can have), and add its ID to `EXTRA_VIDEO_IDS` so
its comments play. Give it its own status comment as in step 6.

**Daily Short** (`daily.py`): on the first run after 23:00 UTC (6 PM in New
York) the bot renders a recap of the last 24 hours (`python short.py
--since-hours 24` does the same locally): day N, the day's moves with the
biggest zoomed in and what each did to its lifespan, and how its expected death
moved. Set the repository variable `DAILY_SHORT`:

- `off` (default): nothing.
- `dry`: render it and attach it to the run; nothing is uploaded.
- `live`: upload it publicly, register it so its comments play, post its status
  comment, and open an issue with its link. Setting its Related video, pinning
  its status comment and picking its thumbnail frame are optional and Studio-only
  (the API can't).

Older Shorts keep counting: their comments are read every 30 minutes and their
status comments refreshed every 12 hours.

### 8. When it dies

The run after a death starts the death sequence (`death.py`), recording its
progress in `state.json` so an interrupted run picks up where it stopped:

1. **Verify**: replay the whole log (`replay.py`). Any mismatch halts the
   sequence and opens an issue; nothing is uploaded or changed.
2. **Render** the obituary (`obituary.py`, about 90 s): the universe's whole life,
   the loop it died in, an end card crediting the last comment that changed it
   (with the commenter's display name, looked up fresh and never stored), and
   the next universe starting.
3. **Upload** it publicly. A marker in its description makes sure a crashed run
   never uploads twice.
4. Wait for YouTube to **process** it.
5. **Launch** the next universe on the obituary video: the dead one is archived
   under `archive/universe-N/` (and still verifies), `state/` starts over with
   a bigger board, and the bot posts the new video's status comment for you to pin.
6. **Retire** the dead universe's videos: the main video and every Short in
   `EXTRA_VIDEO_IDS` go private.

While `DRY_RUN` is `"true"` (the workflow's default) it stops after step 2:
the obituary is attached to the run as an artifact, an issue says what would
have happened, and nothing on YouTube changes. To go live, change `DRY_RUN`
in `.github/workflows/tick.yml` yourself.

**Next board size** (`death.next_shape`, still to be confirmed): the smallest
16:9 board bigger than the last on which the acorn, left alone, lasts longer.
Bigger alone isn't enough on a torus: 128×72 lasts 2.6 days, 160×90 only 1.9,
176×99 8.7.

**Alerts**: the bot opens a GitHub issue (GitHub emails you) when the universe
gets stuck, is saved or dies, and at each step above that needs you.

**If it halts**: read the issue, fix the cause, then set `death.stage` in
`state/state.json` back to `"verify"` (or `"render"` after deleting a failed
upload) and push.

**Rehearsal**: `python rehearse.py` (or the manual *rehearsal* workflow) kills a
scratch universe and runs the real death sequence in dry-run mode, with no
secrets and no YouTube, to check the obituary renders.

## Live stream (optional)

`stream.py` streams the same universe to YouTube Live around the clock: the
board ticking on the wall clock, each new move rippling in when the tick commits
it, the countdown and the latest moves. It reads only the public repo (no API,
no OAuth token); its one secret is the stream key.

1. **YouTube:** Studio → Create → Go live (the first time, YouTube takes up to
   24 hours to allow streaming). In the Stream tab copy the **stream key**. In
   the stream's settings turn **auto-start on** and **auto-stop off**, so a
   restart reconnects to the same broadcast.
2. **A machine that stays on:** e.g. Oracle Cloud's free Arm VM
   (`VM.Standard.A1.Flex`, 2 OCPUs, 12 GB, Ubuntu 24.04, "Always Free-eligible").
   Free Arm machines are often "out of capacity": `deploy/oracle-retry.py` keeps
   asking until one is free (needs `brew install oci-cli` or `pip install oci-cli`, an API key
   in `~/.oci/config` and the SSH public key; it runs on a Mac or any always-on Linux machine).
3. **On it:** `curl -fsSL https://raw.githubusercontent.com/davidackerman/still-life/main/deploy/setup-stream.sh | bash`
   It asks for the key (hidden), stores it root-only in `/etc/still-life/stream.env`
   and starts the `still-life-stream` service. Logs: `journalctl -u still-life-stream -f`.

The service pulls every minute and restarts itself when the streamer's code
changes. About half a fast core at 1080p30; `STREAM_FPS=15` in the env file
roughly halves that. Try it locally first: `python stream.py --preview out/stream.png`.

## Data policy

YouTube's developer policies allow storing other users' API data for at most
30 days and require deleting it on request. This repository is public and its
git history is permanent, so the bot never writes comment text, comment IDs
or channel IDs to it. Duplicate detection and the per-person limit use
HMAC-SHA256 hashes under `STATE_KEY`, pruned after 72 hours. The log holds
game data only.

## Development

```sh
pip install -r requirements-dev.txt
pytest                                                  # about 5 s
python simulate.py --rate 0.5 --days 30                 # one lifetime, with timeline
python simulate.py --rate 0.1 --thumbs out/sim          # also write every thumbnail
python simulate.py --sweep 0,0.1,0.25,1 --seeds 5       # compare comment rates
python simulate.py --flatline-hours 6 --gen-seconds 30  # try other tunings
python explainer.py                                     # render the host video (needs ffmpeg)
python short.py                                         # render a vertical Short from state/
```

## Archive

`archive/test-2026-11-07/` holds the universe that ran on the unlisted test video
before launch. It still verifies: `python replay.py --state-dir archive/test-2026-11-07`.

## Files

| File | Purpose |
|---|---|
| `life.py` | B3/S23 on a 128×72 torus, patterns, BLAKE2b state hashes |
| `universe.py` | the state machine (ALIVE / FLATLINING / CONTESTED / DIED), on generation time; `replay()` |
| `commands.py` | comment → cells parser, including the hashed random patch |
| `admission.py` | dedupe, up to 3 moves per person per generation, 200 per run with a queue (taking turns) |
| `engine.py` | one run: comments in, simulation forward, title/thumbnail out |
| `tick.py` | the scheduled entry point: load state, run once, save |
| `death.py` | the death sequence: verify, obituary, upload, next universe, retire |
| `obituary.py` | renders the obituary video from the log |
| `alerts.py` | GitHub issues for the owner when something happens |
| `rehearse.py` | a scratch death, end to end, in dry-run mode |
| `store.py` | `state.json`, `history.bin`, and the hash-chained `log.jsonl` |
| `replay.py` | verifies the log and rebuilds the life from the seed (no secrets needed) |
| `.github/workflows/tick.yml` | runs `tick.py` (every 5 minutes by GitHub's schedule; the VM's kick starts one whenever 3 have passed) and commits `state/` |
| `docs/` | GitHub Pages: the live view (`index.html`, `life.js`), the rewind slider and history (`history.js`, which replays the public log in the browser), and the privacy policy |
| `explainer.py` | renders the ~2 min explainer video that hosts the universe (`out/explainer.mp4`) |
| `short.py` | renders a vertical Short from the real log (`out/short.mp4`) |
| `stream.py` | the 24/7 YouTube Live stream of the universe (`deploy/setup-stream.sh` sets up a server) |
| `render.py` | status title and the 1280×720 thumbnail |
| `simulate.py` | local mode: fake YouTube, fake clock, synthetic commenters |
| `auth.py` | one-time OAuth desktop flow (loopback + PKCE); prints a refresh token |
| `smoke_test.py` | milestone 1: title and thumbnail round trip on `VIDEO_ID` |
| `youtube.py` | YouTube client: comments, title, thumbnail, quota by Pacific day |
| `tests/` | pytest suite |
