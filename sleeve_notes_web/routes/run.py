"""Background job runner — inline toast + SSE stream.

POST /run/start launches a CLI subcommand as a subprocess. GET /run/events
is a Server-Sent Events stream of every output line + status transitions.
The bottom-right ``#run-banner`` toast (in base.html) HTMX-loads /run/banner
on the ``runStatus`` event and shows live status.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import StreamingResponse

from sleeve_notes_web._deps import templates
from sleeve_notes_web.services.jobs import Job, JobEvent, runner


router = APIRouter()

# Banner dismissal — once the user closes a finished job's banner, we hide it
# until a new job is started. Tracked by job id to avoid race conditions.
_dismissed_job_id: str | None = None


def _job_title(job: Job) -> str:
    """User-facing label for a job — replaces the bare ``kind`` string."""
    if job.kind == "bpm":
        return "BPM lookup"
    if job.kind == "fetch":
        return "Discogs CSV import" if "--csv" in job.argv else "Discogs sync"
    if job.kind == "render":
        return "Generate stickers"
    if job.kind == "run":
        return "Pipeline"
    return job.kind


def _done_text(job: Job) -> str:
    if job.kind == "bpm":
        return "BPM lookup complete."
    if job.kind == "fetch":
        return "CSV imported." if "--csv" in job.argv else "Collection synced."
    if job.kind == "render":
        return "PDF generated."
    return "Done."


def _running_fallback(job: Job) -> str:
    if job.kind == "bpm":
        return "Looking up BPM and key for your collection…"
    if job.kind == "fetch":
        return "Importing CSV…" if "--csv" in job.argv else "Syncing your Discogs collection…"
    if job.kind == "render":
        return "Rendering sticker PDF…"
    return "Working…"


# Lines that are mostly internal accounting — hide from the user-facing toast.
_NOISY_PATTERNS = (
    re.compile(r"^\$ "),
    re.compile(r"^\[\d+/\d+\]\s+progress:"),
    re.compile(r"^new this run by source:"),
)


def _filter_line(line: str) -> bool:
    """True if this log line is fit to show in the toast."""
    if not line:
        return False
    stripped = line.strip()
    if not stripped:
        return False
    return not any(p.search(stripped) for p in _NOISY_PATTERNS)


def _prettify_line(line: str) -> str:
    """Rewrite a few common CLI lines into something readable for end users."""
    s = line.strip()

    # "Looking up BPM/key for N tracks across M releases (A fully cached, B overridden, C to cascade with W workers)..."
    # Surface the *work to do* (to-cascade), not the misleading total.
    m = re.match(
        r"^Looking up BPM/key for (\d+) tracks? across (\d+) releases?\s*"
        r"\((\d+) fully cached, (\d+) overridden, (\d+) to cascade with \d+ workers\)\.{0,3}$",
        s,
    )
    if m:
        total, releases_n, cached, overridden, to_cascade = (int(g) for g in m.groups())
        if to_cascade == 0:
            return f"All {total} tracks already have BPM cached — nothing to look up."
        if cached + overridden == 0:
            return f"Looking up BPM and key for {to_cascade} tracks across {releases_n} releases…"
        return f"Looking up BPM and key for {to_cascade} of {total} tracks ({cached + overridden} already cached)…"

    # Per-track cascade line:
    #   "  [12/445] ●● Daft Punk - Around the World -> 121 key=8A  sources=['songbpm', 'deezer']"
    # Marker is 1–2 of {● ○ ? ·}; key= and sources= tails are optional.
    m = re.match(
        r"^\[(\d+)/(\d+)\]\s+\S+\s+(.+?)\s+->\s+(\S+)(?:\s+key=(\S+))?(?:\s+sources=.*)?$",
        s,
    )
    if m:
        done, total, label, bpm, key = m.groups()
        suffix = f" · key {key}" if key else ""
        return f"{done} of {total} · {label} → {bpm} BPM{suffix}"

    # Final BPM summary line.
    m = re.match(
        r"^Cache populated:\s*(\d+)/(\d+) BPMs\s*\([^)]+\),\s*(\d+)/\d+ keys\.?$",
        s,
    )
    if m:
        bpm_found, total, key_found = m.groups()
        return f"Found BPM for {bpm_found} of {total} tracks ({key_found} with key)."

    return s


_PROGRESS_RE = re.compile(r"^\s*\[(\d+)/(\d+)\]")


def _extract_progress(lines: list[str]) -> int | None:
    """Scan lines (newest first) for a `[done/total]` counter and return percent."""
    for line in reversed(lines):
        m = _PROGRESS_RE.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if total > 0:
                return min(100, max(0, round(done / total * 100)))
    return None


def _banner_context(request: Request, job: Job | None, *, dismissed: bool = False) -> dict:
    if job is None or dismissed:
        return {"request": request, "job": None}
    elapsed = int((job.finished_at or time.time()) - job.started_at)
    filtered = [ln for ln in job.lines if _filter_line(ln)]
    last_line = _prettify_line(filtered[-1]) if filtered else _running_fallback(job)
    progress_pct = _extract_progress(job.lines) if job.status == "running" else None
    return {
        "request": request,
        "job": job,
        "elapsed": elapsed,
        "title": _job_title(job),
        "done_text": _done_text(job),
        "last_line": last_line,
        "progress_pct": progress_pct,
    }


@router.get("/run/banner")
def banner(request: Request):
    job = runner.current
    dismissed = job is not None and job.id == _dismissed_job_id
    return templates.TemplateResponse(
        "run/banner.html", _banner_context(request, job, dismissed=dismissed)
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
    global _dismissed_job_id
    _dismissed_job_id = None
    runner.start(kind, extra_argv)
    return templates.TemplateResponse(
        "run/banner.html",
        _banner_context(request, runner.current),
        headers={"HX-Trigger": "runStatus"},
    )


@router.post("/run/cancel")
def cancel(request: Request):
    runner.cancel()
    job = runner.current
    return templates.TemplateResponse(
        "run/banner.html",
        _banner_context(request, job),
        headers={"HX-Trigger": "runStatus"},
    )


@router.post("/run/dismiss")
def dismiss(request: Request):
    global _dismissed_job_id
    if runner.current is not None:
        _dismissed_job_id = runner.current.id
    return templates.TemplateResponse(
        "run/banner.html", {"request": request, "job": None}
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
