"""One-time Beatport authentication for the BPM cascade.

Uses the same fully-scripted authorization_code flow as the beets-beatport4
plugin: POST credentials to /v4/auth/login/ to get session cookies, then
GET /v4/auth/o/authorize/ to extract the code from the Location header,
then exchange for tokens at /v4/auth/o/token/. No browser, no popup.

Setup:
  1. Add to .env:
       BEATPORT_USERNAME=your.beatport.email@example.com
       BEATPORT_PASSWORD=your-password
  2. Run once:
       python tools/beatport_auth.py
     Writes .tmp/beatport_tokens.json. fetch_bpm.py auto-refreshes from there.

If the refresh_token later expires or is revoked, fetch_bpm.py re-runs this
flow automatically (using the same .env credentials), so token lifecycle is
hands-off.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

try:
    from tools import project_root
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import project_root
ROOT = project_root()
TMP = ROOT / ".tmp"
TOKENS_FILE = TMP / "beatport_tokens.json"

CLIENT_ID = "0GIvkCltVIuPkkwSJHp6NDb3s0potTjLBQr388Dd"
API_BASE = "https://api.beatport.com/v4"
LOGIN_URL = f"{API_BASE}/auth/login/"
AUTHORIZE_URL = f"{API_BASE}/auth/o/authorize/"
TOKEN_URL = f"{API_BASE}/auth/o/token/"
REDIRECT_URI = f"{API_BASE}/auth/o/post-message/"


def authenticate(username: str, password: str) -> dict:
    """Run the full Beatport OAuth dance and return a token bundle.

    Three steps in one HTTP session (cookies preserved across calls):
      1. POST /auth/login/ — exchanges username+password for a session cookie.
      2. GET /auth/o/authorize/ — with the session cookie, Beatport returns a
         302 whose Location header contains ?code=...
      3. POST /auth/o/token/ — exchange the auth code for access+refresh tokens.
    """
    with requests.Session() as s:
        # 1. Login
        resp = s.post(
            LOGIN_URL,
            json={"username": username, "password": password},
            timeout=20,
        )
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code != 200 or not isinstance(data, dict) or "email" not in data:
            raise RuntimeError(
                f"Beatport login failed (status {resp.status_code}): {resp.text[:300]}"
            )

        # 2. Authorize — get the redirect with ?code=... in the Location header.
        resp = s.get(
            AUTHORIZE_URL,
            params={
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
            },
            allow_redirects=False,
            timeout=20,
        )
        if resp.status_code not in (302, 303):
            raise RuntimeError(
                f"Beatport authorize didn't redirect (status {resp.status_code}): {resp.text[:300]}"
            )
        location = resp.headers.get("Location")
        if not location:
            raise RuntimeError("Beatport authorize redirect missing Location header.")
        codes = parse_qs(urlparse(location).query).get("code")
        if not codes:
            raise RuntimeError(f"No 'code' in authorize redirect: {location}")
        code = codes[0]

        # 3. Exchange code for tokens.
        resp = s.post(
            TOKEN_URL,
            data={
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Token exchange failed (status {resp.status_code}): {resp.text[:300]}"
            )
        body = resp.json()
        if "access_token" not in body or "refresh_token" not in body:
            raise RuntimeError(f"Token response missing fields: {body}")
        return {
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
            "expires_at": time.time() + int(body.get("expires_in", 3600)),
        }


def save_tokens(tokens: dict) -> None:
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TOKENS_FILE.open("w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)


def main(argv: list[str] | None = None) -> int:
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    username = os.environ.get("BEATPORT_USERNAME")
    password = os.environ.get("BEATPORT_PASSWORD")
    if not username or not password:
        print(
            "ERROR: BEATPORT_USERNAME and BEATPORT_PASSWORD must be set in .env.\n"
            "Use the credentials you log in to beatport.com with.",
            file=sys.stderr,
        )
        return 1

    print(f"Authenticating {username} with Beatport...")
    try:
        tokens = authenticate(username, password)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    save_tokens(tokens)
    expires_in = int(tokens["expires_at"] - time.time())
    print(f"Saved tokens to {TOKENS_FILE}.")
    print(f"  access_token expires in ~{expires_in}s; fetch_bpm.py auto-refreshes (with full-reauth fallback).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
