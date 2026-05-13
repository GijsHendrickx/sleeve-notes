"""FastAPI app factory + uvicorn entry point.

Routes are registered from ``sleeve_notes_web.routes``. The job runner's
asyncio loop is bound at startup so worker threads can publish events.
"""

from __future__ import annotations

import asyncio
import webbrowser
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sleeve_notes_web._deps import STATIC_DIR
from sleeve_notes_web.routes import actions, collection, dashboard, preview, run, tracks
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


def create_app() -> FastAPI:
    app = FastAPI(title="sleeve-notes", lifespan=_lifespan)
    app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(dashboard.router)
    app.include_router(collection.router)
    app.include_router(tracks.router)
    app.include_router(preview.router)
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
