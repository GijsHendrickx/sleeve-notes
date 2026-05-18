"""Sign-in-with-Discogs routes.

Three endpoints implement the OAuth 1.0a flow plus logout. The heavy
lifting (signing, token exchange, identity lookup) is in
``sleeve_notes_web.services.auth``; this module is just the FastAPI glue
that wires those calls to URLs and the session cookie.

  GET  /auth/login    → kick off the OAuth dance; redirect to Discogs.
  GET  /auth/callback → finish the dance; mark the session as logged in.
  POST /auth/logout   → clear the session.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from sleeve_notes import db as dbmod
from sleeve_notes_web.services.auth import (
    complete_oauth,
    fetch_identity,
    start_oauth,
)


router = APIRouter(prefix="/auth")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@router.get("/login")
def login(request: Request):
    # If they're already logged in, don't restart the OAuth dance — just
    # send them home.
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=303)
    callback_url = str(request.url_for("auth_callback"))
    authorize_url = start_oauth(request, callback_url)
    return RedirectResponse(url=authorize_url, status_code=307)


@router.get("/callback", name="auth_callback")
def callback(request: Request, oauth_token: str | None = None,
             oauth_verifier: str | None = None, denied: str | None = None):
    # User clicked "deny" on Discogs, or the redirect arrived without the
    # expected params — bail back to the landing page.
    if denied or not oauth_verifier:
        request.session.pop("discogs_oauth_request_token", None)
        request.session.pop("discogs_oauth_request_token_secret", None)
        return RedirectResponse(url="/", status_code=303)
    token, token_secret = complete_oauth(request, oauth_verifier)
    identity = fetch_identity(token, token_secret)
    now = _now_iso()
    with dbmod.session() as conn:
        conn.execute(
            "INSERT INTO users (id, username, created_at, last_seen_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  username = excluded.username, "
            "  last_seen_at = excluded.last_seen_at",
            (identity.id, identity.username, now, now),
        )
    request.session["user_id"] = identity.id
    request.session["username"] = identity.username
    request.session["discogs_token"] = token
    request.session["discogs_token_secret"] = token_secret
    return RedirectResponse(url="/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
