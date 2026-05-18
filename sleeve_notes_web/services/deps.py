"""FastAPI auth dependencies.

``current_user`` is the gate every data route puts in front of itself; it
reads the session cookie, returns a populated ``User`` if all four bits
are present, otherwise raises ``NotAuthenticated`` which the registered
handler turns into a redirect to ``/auth/login`` (or an ``HX-Redirect``
header for HTMX requests).

``optional_user`` is the same lookup but returns ``None`` instead of
raising — used by the dashboard route to decide whether to render the
landing page or the logged-in dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response


@dataclass(frozen=True)
class User:
    id: int
    username: str
    discogs_token: str
    discogs_token_secret: str


class NotAuthenticated(Exception):
    """Raised by ``current_user`` when the session lacks a valid login."""


def _user_from_session(request: Request) -> User | None:
    s = request.session
    uid = s.get("user_id")
    username = s.get("username")
    token = s.get("discogs_token")
    token_secret = s.get("discogs_token_secret")
    if not (uid and username and token and token_secret):
        return None
    return User(
        id=int(uid),
        username=str(username),
        discogs_token=str(token),
        discogs_token_secret=str(token_secret),
    )


def current_user(request: Request) -> User:
    user = _user_from_session(request)
    if user is None:
        raise NotAuthenticated()
    return user


def optional_user(request: Request) -> User | None:
    return _user_from_session(request)


def install_auth_handlers(app: FastAPI) -> None:
    """Register the redirect-to-login handler for ``NotAuthenticated``.

    HTMX requests get an ``HX-Redirect`` header so the client does a full
    page navigation instead of swapping a redirect HTML body into a
    partial; regular browser navigations get a normal 307.
    """

    @app.exception_handler(NotAuthenticated)
    async def _redirect_to_login(request: Request, exc: NotAuthenticated):
        if request.headers.get("hx-request"):
            return Response(status_code=200, headers={"HX-Redirect": "/auth/login"})
        return RedirectResponse(url="/auth/login", status_code=307)
