# still-life

### A YouTube video that dies when its universe gets stuck in a loop.

**[▶ The video](https://www.youtube.com/watch?v=oFWeLMe4bhI)** · **[Watch it live](https://davidackerman.github.io/still-life/)** · [The Short](https://youtube.com/shorts/m-JcepjW41U) · [How it lives and dies](#how-the-universe-lives-and-dies)

<p align="center"><img src="docs/acorn.gif" width="640" alt="An acorn, seven living cells, exploding into a whole universe of Conway's Game of Life"></p>

**Every comment changes this universe. Some save it. Some kill it.**

- 🧬 **A real Game of Life**, running around the clock: 128×72 cells on a torus, one generation every 2 minutes, from a 7-cell acorn.
- 💬 **Every comment adds cells.** `glider 40,20 ne`, `acorn 64,30`, or just say anything. Replies count, and so do comments on the Short.
- ⏳ **The video updates itself.** Its title counts down to its own death ("dies in 8d 21h 30m if nobody comments") and its thumbnail is the universe.
- 🔁 **Left alone, it always gets stuck.** A finite universe must eventually repeat a state, and then it loops forever. That starts a 24-hour countdown.
- ⚫ **If nobody breaks the loop, it dies.** An obituary video replays its life, the video goes private, and a bigger universe is born on the obituary.
- 🔍 **Nothing to trust.** Every run is in a hash-chained public log; anyone can replay the whole life from the seed and check it.

No server: a Python bot on GitHub Actions wakes every 5 minutes, steps the universe, reads comments and rewrites the video. The [live page](https://davidackerman.github.io/still-life/) computes the same universe in your browser, forecasts when it gets stuck, and rewinds through its whole life (`?gen=1234` links to a moment).

**Status:** live since November 8, 2026. The death sequence is built and rehearsed, and runs in dry-run mode for now.

## Rules for viewers

Comment on the video. The first command in your comment wins (case-insensitive):

| Command | Effect |
|---|---|
| `glider x,y [ne\|nw\|se\|sw]` | a glider heading that way (default `se`) |
| `block x,y` | a 2×2 still life |
| `blinker x,y` | a period-2 oscillator |
| `rpent x,y` | the R-pentomino |
| `acorn x,y` | the acorn |
| `cells x,y x,y ...` | up to 25 individual cells |
| anything else | a 5×5 random patch at 50% density, derived from a hash of your comment's ID and text |

- `x` is 0–127, `y` is 0–71, and the origin is the top-left. `(x, y)` is the
  top-left corner of the pattern, and patterns that cross an edge wrap around.
- A command with a coordinate out of range is ignored. If your comment has no
  valid command, it counts as a random patch.
- Cells are added to whatever is already there; nothing is erased.
- Each channel gets one injection per hour. Each run applies at most 20
  injections, and the rest wait for the next run.
- Replies count exactly like comments, including replies under the pinned
  status comment.
- Comments on the still-life Short count too: it feeds the same universe.
- Deleting your comment does not undo it.
- Comments that YouTube holds for review (spam filter) are invisible to the
  API, so they do nothing while held.
- Every run is recorded in a hash-chained public log (`state/log.jsonl`), and
  `python replay.py` rebuilds the whole life from the seed to verify it.
- The log holds game data only: generation, the parsed command and its cells.
  Your comment's text, ID and channel are never stored (see
  [privacy](https://davidackerman.github.io/still-life/privacy.html)). To find
  your injection, look for your command's cells around the time you commented.
  For a random patch, recompute it from your comment ID and text using the
  formula in `commands.py`.

### How the universe lives and dies

One generation every 2 minutes, around the clock.

- **ALIVE**: the universe is evolving.
- **FLATLINING**: it has repeated a state. Left alone, a finite universe
  eventually always does. A 24-hour countdown starts.
- **CONTESTED**: a comment disturbed it during the countdown. The countdown
  keeps running, but the universe cannot die while contested:
  - If it falls back into the same loop, it is FLATLINING again with the same
    deadline.
  - If it settles into a *different* loop, it is FLATLINING with a fresh
    24 hours.
  - If it is still changing when the deadline passes, it is ALIVE again.
- **DIED**: the countdown ran out while FLATLINING. An obituary video is
  uploaded and this one goes private.

A comment that adds no new live cells changes nothing.

## Setup (owner)

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
This video is alive: the thumbnail is its universe, the title counts down to its death, and every comment (replies too) adds cells.

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

## Data policy

YouTube's developer policies allow storing other users' API data for at most
30 days and require deleting it on request. This repository is public and its
git history is permanent, so the bot never writes comment text, comment IDs
or channel IDs to it. Duplicate detection and the one-per-hour limit use
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
| `admission.py` | dedupe, one injection per author per hour, 20 per run with a queue |
| `engine.py` | one run: comments in, simulation forward, title/thumbnail out |
| `tick.py` | the scheduled entry point: load state, run once, save |
| `death.py` | the death sequence: verify, obituary, upload, next universe, retire |
| `obituary.py` | renders the obituary video from the log |
| `alerts.py` | GitHub issues for the owner when something happens |
| `rehearse.py` | a scratch death, end to end, in dry-run mode |
| `store.py` | `state.json`, `history.bin`, and the hash-chained `log.jsonl` |
| `replay.py` | verifies the log and rebuilds the life from the seed (no secrets needed) |
| `.github/workflows/tick.yml` | runs `tick.py` every 5 minutes and commits `state/` |
| `docs/` | GitHub Pages: the live view (`index.html`, `life.js`), the rewind slider and history (`history.js`, which replays the public log in the browser), and the privacy policy |
| `explainer.py` | renders the ~2 min explainer video that hosts the universe (`out/explainer.mp4`) |
| `short.py` | renders a vertical Short from the real log (`out/short.mp4`) |
| `render.py` | status title and the 1280×720 thumbnail |
| `simulate.py` | local mode: fake YouTube, fake clock, synthetic commenters |
| `auth.py` | one-time OAuth desktop flow (loopback + PKCE); prints a refresh token |
| `smoke_test.py` | milestone 1: title and thumbnail round trip on `VIDEO_ID` |
| `youtube.py` | YouTube client: comments, title, thumbnail, quota by Pacific day |
| `tests/` | pytest suite |
