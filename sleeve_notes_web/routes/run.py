"""Background job runner — slide-out panel + SSE stream + status pill.

POST /run/start launches a CLI subcommand as a subprocess. GET /run/events
is a Server-Sent Events stream of every output line + status transitions.
The header status pill (HTMX-polled via the ``runStatus`` event triggered
by SSE messages) shows what's currently running.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import StreamingResponse

from sleeve_notes_web._deps import templates
from sleeve_notes_web.services.jobs import JobEvent, runner


router = APIRouter()


@router.get("/run/panel")
def panel(request: Request):
    job = runner.current
    return templates.TemplateResponse(
        "run/panel.html",
        {"request": request, "job": job, "running": job is not None and job.status == "running"},
    )


@router.get("/run/status")
def status_pill(request: Request):
    job = runner.current
    if job is None:
        return templates.TemplateResponse(
            "run/_pill.html", {"request": request, "job": None}
        )
    elapsed = int(time.time() - job.started_at)
    return templates.TemplateResponse(
        "run/_pill.html",
        {"request": request, "job": job, "elapsed": elapsed},
    )


@router.post("/run/start")
def start(request: Request, kind: str = Form(...), extra: str = Form("")):
    valid = {"fetch", "bpm", "render", "run"}
    if kind not in valid:
        kind = "run"
    extra_argv: list[str] = []
    if extra.strip():
        try:
            extra_argv = shlex.split(extra)
        except ValueError:
            extra_argv = []
    runner.start(kind, extra_argv)
    return templates.TemplateResponse(
        "run/panel.html",
        {"request": request, "job": runner.current, "running": True},
        headers={"HX-Trigger": "runStatus"},
    )


@router.post("/run/cancel")
def cancel(request: Request):
    runner.cancel()
    job = runner.current
    return templates.TemplateResponse(
        "run/panel.html",
        {"request": request, "job": job, "running": job is not None and job.status == "running"},
        headers={"HX-Trigger": "runStatus"},
    )


@router.get("/run/events")
async def events(request: Request):
    """SSE feed of the currently running job's output + status transitions."""
    q: asyncio.Queue[JobEvent] = runner.subscribe()

    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Heartbeat keeps the connection alive through proxies.
                    yield ": ping\n\n"
                    continue
                payload = {
                    "kind": event.kind,
                    "text": event.text,
                    "status": event.status,
                    "seq": event.seq,
                }
                yield f"data: {json.dumps(payload)}\n\n"
                if event.kind == "status" and event.status in ("done", "failed", "cancelled"):
                    break
        finally:
            runner.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")
