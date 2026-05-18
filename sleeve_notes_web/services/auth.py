"""Discogs OAuth 1.0a flow.

Three helpers wrap ``requests_oauthlib.OAuth1Session`` to build the
Sign-in-with-Discogs experience:

  start_oauth(request)       → step 1. Asks Discogs for a request token,
                                stashes its secret in the user session, and
                                returns the URL the browser should be sent
                                to so the user can authorize the app.
  complete_oauth(req, verif) → step 3. Exchanges the request token +
                                verifier (received on the callback) for an
                                access token + access-token secret. Returns
                                the pair so the caller can write it into
                                the session as the per-user credential.
  fetch_identity(t, secret)  → calls /oauth/identity and returns the
                                Discogs user_id + username, which the
                                caller uses to mark the session as logged
                                in and to UPSERT the users row.

OAuth 1.0a is a signing protocol: every Discogs request is HMAC-SHA1
signed with the consumer credentials (identifying *this app*) combined
with the user's token (identifying *this user*). The consumer credentials
live in ``DISCOGS_CONSUMER_KEY`` / ``DISCOGS_CONSUMER_SECRET`` from the
process env; the user token lives in the session cookie.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import Request
from requests_oauthlib import OAuth1Session


REQUEST_TOKEN_URL = "https://api.discogs.com/oauth/request_token"
AUTHORIZE_URL = "https://www.discogs.com/oauth/authorize"
ACCESS_TOKEN_URL = "https://api.discogs.com/oauth/access_token"
IDENTITY_URL = "https://api.discogs.com/oauth/identity"

# Discogs rejects requests without a descriptive User-Agent.
USER_AGENT = "SleeveNotes/0.3 +https://github.com/GijsHendrickx/sleeve-notes"

_REQ_TOKEN_KEY = "discogs_oauth_request_token"
_REQ_TOKEN_SECRET_KEY = "discogs_oauth_request_token_secret"


@dataclass(frozen=True)
class Identity:
    id: int
    username: str


def _consumer_credentials() -> tuple[str, str]:
    key = os.environ.get("DISCOGS_CONSUMER_KEY")
    secret = os.environ.get("DISCOGS_CONSUMER_SECRET")
    if not key or not secret:
        raise RuntimeError(
            "DISCOGS_CONSUMER_KEY and DISCOGS_CONSUMER_SECRET must be set in "
            ".env. Register an application at "
            "https://www.discogs.com/settings/developers."
        )
    return key, secret


def start_oauth(request: Request, callback_url: str) -> str:
    """Step 1: ask Discogs for a request token and return the authorize URL.

    Stores the request-token secret in the session so ``complete_oauth``
    can pick it up when Discogs redirects the user to our callback.
    """
    consumer_key, consumer_secret = _consumer_credentials()
    sess = OAuth1Session(
        consumer_key,
        client_secret=consumer_secret,
        callback_uri=callback_url,
    )
    sess.headers.update({"User-Agent": USER_AGENT})
    resp = sess.fetch_request_token(REQUEST_TOKEN_URL)
    request.session[_REQ_TOKEN_KEY] = resp["oauth_token"]
    request.session[_REQ_TOKEN_SECRET_KEY] = resp["oauth_token_secret"]
    return f"{AUTHORIZE_URL}?oauth_token={resp['oauth_token']}"


def complete_oauth(request: Request, oauth_verifier: str) -> tuple[str, str]:
    """Step 3: exchange the request token + verifier for an access token.

    Reads the request-token secret stashed by ``start_oauth`` from the
    session, then clears those temporary keys whether the call succeeds
    or fails.
    """
    consumer_key, consumer_secret = _consumer_credentials()
    request_token = request.session.get(_REQ_TOKEN_KEY)
    request_token_secret = request.session.get(_REQ_TOKEN_SECRET_KEY)
    if not request_token or not request_token_secret:
        raise RuntimeError(
            "OAuth session missing — restart the login from /auth/login."
        )
    try:
        sess = OAuth1Session(
            consumer_key,
            client_secret=consumer_secret,
            resource_owner_key=request_token,
            resource_owner_secret=request_token_secret,
            verifier=oauth_verifier,
        )
        sess.headers.update({"User-Agent": USER_AGENT})
        tokens = sess.fetch_access_token(ACCESS_TOKEN_URL)
    finally:
        request.session.pop(_REQ_TOKEN_KEY, None)
        request.session.pop(_REQ_TOKEN_SECRET_KEY, None)
    return tokens["oauth_token"], tokens["oauth_token_secret"]


def fetch_identity(token: str, token_secret: str) -> Identity:
    """Look up who owns this access token. Returns Discogs user_id +
    username, which the caller writes into the session as the login
    marker (and UPSERTs into the ``users`` table)."""
    consumer_key, consumer_secret = _consumer_credentials()
    sess = OAuth1Session(
        consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=token,
        resource_owner_secret=token_secret,
    )
    sess.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    resp = sess.get(IDENTITY_URL)
    resp.raise_for_status()
    data = resp.json()
    return Identity(id=int(data["id"]), username=str(data["username"]))
