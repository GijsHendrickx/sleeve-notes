"""FastAPI app factory + uvicorn entry point.

Routes are registered from ``sleeve_notes_web.routes``. The job runner's
asyncio loop is bound at startup so worker threads can publish events.

Auth model: a Discogs OAuth 1.0a login (see ``services.auth``) writes the
user_id, username, token and token_secret into a ``SessionMiddleware``
cookie. Data routes depend on ``services.deps.current_user`` which reads
that cookie or raises ``NotAuthenticated`` → redirect to ``/auth/login``.
"""

from __future__ import annotations

import asyncio
import os
import webbrowser
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from sleeve_notes_web._deps import STATIC_DIR
from sleeve_notes_web.routes import (
    actions,
    auth,
    collection,
    dashboard,
    print_runs,
    run,
    tracks,
)
from sleeve_notes_web.services.deps import install_auth_handlers
from sleeve_notes_web.services.jobs import runner


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles that forces revalidation on every request — fine for a
    localhost dev tool, and avoids stale app.js after backend redeploys."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response


@asynccontextmanager
async def _lifespan(app: FastAPI):
    runner.bind_loop(asyncio.get_running_loop())
    yield


def _require_session_secret() -> str:
    secret = os.environ.get("SESSION_SECRET")
    if not secret:
        raise RuntimeError(
            "SESSION_SECRET must be set in .env. Generate a long random "
            "string (e.g. `python -c 'import secrets; print(secrets.token_urlsafe(64))'`)."
        )
    return secret


def create_app() -> FastAPI:
    app = FastAPI(title="sleeve-notes", lifespan=_lifespan)
    # SessionMiddleware sits in front of every request and reads/writes a
    # signed cookie. Discogs OAuth tokens are stored here — no DB row.
    app.add_middleware(
        SessionMiddleware,
        secret_key=_require_session_secret(),
        max_age=30 * 24 * 3600,
        same_site="lax",
        https_only=os.environ.get("SESSION_HTTPS_ONLY", "false").lower() == "true",
    )
    install_auth_handlers(app)
    app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(collection.router)
    app.include_router(tracks.router)
    app.include_router(print_runs.router)
    app.include_router(run.router)
    app.include_router(actions.router)
    return app


app = create_app()


def serve(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = True) -> None:
    """Run the app under uvicorn. Used by ``sleeve-notes web``."""
    import uvicorn

    if open_browser:
        url = f"http://{host}:{port}/"
        try:
            webbrowser.open_new_tab(url)
        except Exception:
            pass
    uvicorn.run(app, host=host, port=port, log_level="info")
