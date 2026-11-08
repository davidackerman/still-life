# still-life

**This video dies when its universe repeats itself.**

A YouTube video hosts a live Conway's Game of Life universe: 128×72 cells on a
torus, one generation every two minutes. The thumbnail shows the grid and the
title shows its status. Every comment injects cells. A finite universe left
alone must eventually repeat. When it does, a 24-hour flatline countdown
starts. If no comment breaks the cycle in time, the universe dies. An obituary
video is uploaded and this one goes private.

*Every comment changes this universe. Some save it. Some kill it.*

> Status: under construction. Step 1 of 4 (credentials and smoke test) is done.

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

- `x` is 0–127, `y` is 0–71, and the origin is the top-left. Coordinates wrap.
- Cells are added to whatever is already there; nothing is erased.
- Each channel gets one injection per hour. Each run applies at most 20
  injections, and the rest wait for the next run.
- Replies are ignored. Only top-level comments count.
- Deleting your comment does not undo it.
- Comments that YouTube holds for review (spam filter) are invisible to the
  API, so they do nothing while held.
- Every event is recorded in a hash-chained log (`state/log.jsonl`), and
  `replay.py` rebuilds the whole life from the seed to verify it.

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

## Files

| File | Purpose |
|---|---|
| `auth.py` | one-time OAuth desktop flow (loopback + PKCE); prints a refresh token |
| `smoke_test.py` | milestone 1: title and thumbnail round trip on `VIDEO_ID` |
| `youtube.py` | YouTube API access (display and input only; never authoritative) |
