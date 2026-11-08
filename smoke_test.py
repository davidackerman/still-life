#!/usr/bin/env python3
"""Milestone 1: prove the credentials can change VIDEO_ID's title and thumbnail.

Sequence (about 160-190 quota units; read-back polling adds a few):
  1. channels.list mine + videos.list: confirm the token's channel owns VIDEO_ID
  2. save the original snippet to smoke_backup.json (gitignored)
  3. videos.update: set a test title, poll until it reads back, and check nothing else
     in the snippet changed (description, tags, category, languages)
  4. thumbnails.set: upload a 1280x720 test PNG of the acorn on the 128x72 grid
  5. wait for you to look (Enter, or --hold SECONDS)
  6. videos.update: restore the original title, re-fetch and verify

The title is restored even if a later step fails or you press Ctrl-C. The
thumbnail is NOT restored: the API cannot remove a custom thumbnail or bring
back an auto-generated one. Use your unlisted test video.

Env: CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, VIDEO_ID.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

from youtube import (
    ConfigError,
    build_service,
    credentials_from_env,
    http_error_reason,
    my_channel,
    require_env,
    validate_title,
    writable_snippet,
)

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP_PATH = os.path.join(HERE, "smoke_backup.json")
THUMB_PATH = os.path.join(HERE, "out", "smoke_thumbnail.png")

W, H, CELL = 128, 72, 10
ACORN = [(1, 0), (3, 1), (0, 2), (1, 2), (4, 2), (5, 2), (6, 2)]


def render_test_thumbnail(path: str, stamp: str) -> int:
    """Draw the acorn seed at grid center with a test banner. Returns PNG bytes."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (W * CELL, H * CELL), (10, 12, 16))
    d = ImageDraw.Draw(img)
    for gx in range(0, W * CELL, CELL):  # faint grid
        d.line([(gx, 0), (gx, H * CELL)], fill=(22, 25, 32))
    for gy in range(0, H * CELL, CELL):
        d.line([(0, gy), (W * CELL, gy)], fill=(22, 25, 32))
    ox, oy = W // 2 - 3, H // 2 - 1
    for x, y in ACORN:
        px, py = (ox + x) * CELL, (oy + y) * CELL
        d.rectangle([px + 1, py + 1, px + CELL - 2, py + CELL - 2], fill=(120, 230, 160))

    big, small = ImageFont.load_default(size=64), ImageFont.load_default(size=28)
    d.rectangle([0, 0, W * CELL, 120], fill=(0, 0, 0))
    d.text((40, 22), "SMOKE TEST", font=big, fill=(255, 210, 90))
    d.text((40, 640), f"still-life · {stamp}", font=small, fill=(200, 200, 200))

    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "PNG", optimize=True)
    size = os.path.getsize(path)
    if size >= 2 * 1024 * 1024:
        raise RuntimeError(f"thumbnail is {size} bytes; must be under 2 MB")
    return size


def fetch_video(service, video_id: str) -> dict:
    resp = service.videos().list(part="snippet,status", id=video_id).execute()
    items = resp.get("items", [])
    if not items:
        raise ConfigError(f"video {video_id} not found or not visible to these credentials")
    return items[0]


def update_snippet(service, video_id: str, overrides: dict) -> dict:
    """Fetch the current snippet, apply overrides, send the full writable snippet.

    Returns the snippet from the update response (what YouTube accepted)."""
    body = writable_snippet(fetch_video(service, video_id)["snippet"])
    body.update(overrides)
    validate_title(body["title"])
    resp = service.videos().update(part="snippet", body={"id": video_id, "snippet": body}).execute()
    return resp.get("snippet", {})


# Observed 2026-11-07: videos.list can lag a write by tens of seconds, so an
# immediate read-back still shows the previous title. Poll instead.
def wait_for_title(service, video_id: str, expected: str, timeout: float = 90, every: float = 5):
    """Poll videos.list (1 unit per poll) until the title reads back as expected.

    Returns (snippet, seconds_waited) or (last_snippet, None) on timeout."""
    start = time.monotonic()
    while True:
        snippet = fetch_video(service, video_id)["snippet"]
        waited = time.monotonic() - start
        if snippet.get("title") == expected:
            return snippet, waited
        if waited >= timeout:
            return snippet, None
        time.sleep(every)


def lost_fields(original: dict, now: dict) -> list[str]:
    """Writable snippet fields (except title) that the original had and that are now changed or gone."""
    o, n = writable_snippet(original), writable_snippet(now)
    return sorted(k for k in o if k != "title" and n.get(k) != o[k])


def added_fields(original: dict, now: dict) -> dict:
    """Writable snippet fields YouTube filled in that the original did not have."""
    o, n = writable_snippet(original), writable_snippet(now)
    return {k: n[k] for k in n if k not in o}


def check_title(service, video_id, original, expected, accepted) -> bool:
    """Report on one title write: accepted by the update call, visible via list, nothing lost."""
    ok = True
    if accepted.get("title") == expected:
        print("  ✓ update accepted")
    else:
        ok = False
        print(f"  ✗ update response has title {accepted.get('title')!r}")
    snippet, waited = wait_for_title(service, video_id, expected)
    if waited is None:
        ok = False
        print(f"  ✗ videos.list still shows {snippet.get('title')!r} after 90 s")
    else:
        print(f"  ✓ visible via videos.list after {waited:.0f} s")
    lost = lost_fields(original, snippet)
    if lost:
        ok = False
        print(f"  ✗ fields changed or lost: {lost}")
    else:
        print("  ✓ description, tags, category and languages preserved")
    added = added_fields(original, snippet)
    if added:
        print(f"  · YouTube added (not a failure): {added}")
    return ok


def explain_http_error(step: str, err) -> None:
    status, reason, message = http_error_reason(err)
    print(f"  ✗ {step} failed: HTTP {status} {reason}: {message}")
    if step == "thumbnails.set" and status == 403:
        print("    Custom thumbnails usually require a verified channel: "
              "https://www.youtube.com/verify")
    if status == 429 or reason == "uploadRateLimitExceeded":
        print("    YouTube is rate-limiting thumbnail uploads for this channel; try later.")
    if reason in ("quotaExceeded", "dailyLimitExceeded"):
        print("    Daily API quota exhausted; it resets at midnight Pacific.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Change VIDEO_ID's title and thumbnail once, then restore the title.")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--hold", type=float, default=None,
                    help="seconds to wait before restoring (default: wait for Enter)")
    ap.add_argument("--allow-public", action="store_true", help="permit running against a public video")
    args = ap.parse_args()

    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    try:
        video_id = require_env("VIDEO_ID")["VIDEO_ID"]
        service = build_service(credentials_from_env())
        me = my_channel(service)
        video = fetch_video(service, video_id)
    except (ConfigError, HttpError) as e:
        if isinstance(e, HttpError):
            explain_http_error("setup", e)
        else:
            print(f"✗ {e}")
        return 2

    original = video["snippet"]
    privacy = video["status"]["privacyStatus"]
    print(f"Channel:  {me['title']} ({me['id']})")
    print(f"Video:    {video_id}  [{privacy}]  category={original.get('categoryId')}")
    print(f"Title:    {original['title']!r}")

    if original["channelId"] != me["id"]:
        print(f"✗ This video belongs to channel {original['channelId']}, but the token acts as "
              f"{me['id']}. Re-run auth.py and pick the right channel / Brand Account.")
        return 2
    if privacy == "public" and not args.allow_public:
        print("✗ The video is public. Use your unlisted test video, or pass --allow-public.")
        return 2

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    test_title = validate_title(f"Smoke test · still-life · {stamp}")
    size = render_test_thumbnail(THUMB_PATH, stamp)
    print(f"Thumbnail: {THUMB_PATH} ({size:,} bytes)")

    if not args.yes:
        ans = input(f"\nSet title to {test_title!r} and replace the thumbnail "
                    "(the thumbnail cannot be restored)? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("Aborted; nothing changed.")
            return 1

    with open(BACKUP_PATH, "w", encoding="utf-8") as f:
        json.dump({"video_id": video_id, "saved_at": stamp, "snippet": original,
                   "status": video["status"]}, f, indent=2, ensure_ascii=False)
    print(f"Saved original snippet to {BACKUP_PATH}")

    ok = True
    title_changed = False
    try:
        print("\n[1/3] videos.update: test title")
        accepted = update_snippet(service, video_id, {"title": test_title})
        title_changed = True
        ok &= check_title(service, video_id, original, test_title, accepted)

        print("[2/3] thumbnails.set")
        try:
            service.thumbnails().set(
                videoId=video_id, media_body=MediaFileUpload(THUMB_PATH, mimetype="image/png")
            ).execute()
            print("  ✓ thumbnail uploaded (YouTube may take a few minutes to show it)")
        except HttpError as e:
            ok = False
            explain_http_error("thumbnails.set", e)

        url = f"https://www.youtube.com/watch?v={video_id}"
        if args.hold is not None:
            print(f"\nHolding {args.hold:g}s. Look at {url}")
            time.sleep(args.hold)
        else:
            input(f"\nLook at {url} (or YouTube Studio), then press Enter to restore the title. ")
    except HttpError as e:
        ok = False
        explain_http_error("videos.update", e)
    except KeyboardInterrupt:
        ok = False
        print("\nInterrupted.")
    finally:
        if title_changed:
            # Lay every original writable field back over the current snippet, so
            # anything step 1 clobbered is repaired and anything YouTube added
            # since (e.g. defaultAudioLanguage after processing) is kept.
            print("[3/3] videos.update: restore original title (and snippet)")
            try:
                accepted = update_snippet(service, video_id, writable_snippet(original))
                ok &= check_title(service, video_id, original, original["title"], accepted)
            except HttpError as e:
                ok = False
                explain_http_error("restore", e)
                print(f"  Original title is in {BACKUP_PATH}: {original['title']!r}")

    print("\nSMOKE TEST PASSED" if ok else "\nSMOKE TEST FAILED (see ✗ lines above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
