#!/usr/bin/env python3
"""One-time local script: run the OAuth desktop flow and print a refresh token.

Usage:
    python auth.py --client-secrets path/to/client_secret.json
    # or, with CLIENT_ID and CLIENT_SECRET in the environment:
    python auth.py

Uses the loopback redirect with PKCE (Google's recommended desktop flow; the
old copy/paste "OOB" flow no longer works). Requires google-auth-oauthlib,
which only this script needs:  pip install google-auth-oauthlib

The refresh token is printed to the terminal and nowhere else. Put it in a
GitHub Actions secret named REFRESH_TOKEN. Never commit it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from youtube import SCOPES, TOKEN_URI, build_service, my_channel

READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"


def load_client_config(path: str | None) -> dict:
    if path:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        if "installed" not in cfg:
            kind = next(iter(cfg), "unknown")
            sys.exit(
                f"{path} is a '{kind}' client. Create an OAuth client of type "
                "'Desktop app' in Google Cloud Console and download its JSON."
            )
        return cfg
    cid, secret = os.environ.get("CLIENT_ID"), os.environ.get("CLIENT_SECRET")
    if not (cid and secret):
        sys.exit("Pass --client-secrets FILE, or set CLIENT_ID and CLIENT_SECRET.")
    return {
        "installed": {
            "client_id": cid,
            "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


def _register_clipboard_browser() -> bool:
    """Register a webbrowser 'clipboard' that copies the URL (macOS pbcopy)."""
    import shutil
    import subprocess
    import webbrowser

    if not shutil.which("pbcopy"):
        return False

    class Clipboard(webbrowser.BaseBrowser):
        def open(self, url, new=0, autoraise=True):
            subprocess.run(["pbcopy"], input=url.encode("utf-8"), check=True)
            print("The sign-in link is on your clipboard. Paste it (Cmd-V) into the address bar")
            print("of the browser profile signed in as the channel's Google account.")
            print("Waiting for you to finish signing in...\n")
            return True

    webbrowser.register("clipboard", None, Clipboard("clipboard"))
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--client-secrets", help="Desktop-app client JSON from Cloud Console")
    ap.add_argument("--no-browser", action="store_true",
                    help="copy the sign-in link to the clipboard instead of opening a browser")
    ap.add_argument("--readonly", action="store_true",
                    help="ask only to read (youtube.readonly): for the stream VM's live chat, which must not be able "
                         "to change the channel")
    ap.add_argument("--keychain", metavar="SERVICE",
                    help="store the refresh token in the macOS Keychain under this name instead of printing it")
    args = ap.parse_args()
    scopes = [READONLY_SCOPE] if args.readonly else SCOPES

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("auth.py needs google-auth-oauthlib:  pip install google-auth-oauthlib")

    cfg = load_client_config(args.client_secrets)
    flow = InstalledAppFlow.from_client_config(cfg, scopes=scopes)  # PKCE S256 on by default
    # The sign-in link is ~400 characters. Copying it out of a terminal can pick
    # up a stray character where the line wraps, which corrupts redirect_uri and
    # makes Google answer "Access blocked ... Error 400: invalid_request".
    # So with --no-browser, put it on the clipboard instead of printing it.
    copied = args.no_browser and _register_clipboard_browser()
    if copied:
        browser, prompt = "clipboard", None
    elif args.no_browser:
        browser, prompt = None, "Open this URL (copy it exactly, as one line): {url}"
    else:
        browser, prompt = None, None
    print("If your channel is a Brand Account, pick the channel that owns the video.\n")
    creds = flow.run_local_server(
        host="127.0.0.1",  # loopback IP literal, as Google's desktop-app docs specify
        port=0,
        access_type="offline",  # ask for a refresh token
        prompt="consent",  # re-issue one even if this client was authorized before
        open_browser=bool(copied) or not args.no_browser,
        browser=browser,
        authorization_prompt_message=prompt,
    )

    if not creds.refresh_token:
        sys.exit("Google returned no refresh token. Revoke the app at "
                 "https://myaccount.google.com/permissions and run again.")
    granted = creds.granted_scopes or creds.scopes or []
    if isinstance(granted, str):
        granted = granted.split()
    if scopes[0] not in granted:
        sys.exit(f"The YouTube scope was not granted (got: {granted}). "
                 "Run again and tick the YouTube permission on the consent screen.")

    try:
        channel = my_channel(build_service(creds))  # 1 quota unit
        print(f"\nAuthorized as channel: {channel['title']}  ({channel['id']})")
    except Exception as e:  # still print the token: it was issued and is valid
        print(f"\nWARNING: could not look up the YouTube channel ({e}).")
        print("If this account has no channel yet, create one at "
              "https://www.youtube.com/create_channel. The token below still works.")
    if args.keychain:
        subprocess.run(["security", "add-generic-password", "-U", "-a", "still-life", "-s", args.keychain,
                        "-w", creds.refresh_token], check=True, capture_output=True)
        print(f"\nThe refresh token is in the Keychain as {args.keychain!r} (not printed).")
        return
    print("\nREFRESH_TOKEN (store as a secret; do not commit):\n")
    print(creds.refresh_token)
    print(
        "\nReminder: if the OAuth consent screen's publishing status is 'Testing',\n"
        "Google expires this refresh token after 7 days. Set it to 'In production'\n"
        "before any multi-day run (see SETUP.md)."
    )


if __name__ == "__main__":
    main()
