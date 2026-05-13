"""Sidebar action handlers — modal partials + start endpoints.

Each action shows a tailored form in #modal-root; submitting kicks off a
sleeve-notes CLI subprocess via the shared job runner and dismisses the
modal. The inline run banner picks up the new job via the ``runStatus``
HTMX trigger.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from sleeve_notes import db as sleeve_db, project_root
from sleeve_notes_web._deps import templates
from sleeve_notes_web.services.jobs import runner


router = APIRouter(prefix="/actions")


# Modal partials ---------------------------------------------------------------

@router.get("/discogs-sync", response_class=HTMLResponse)
def modal_discogs_sync(request: Request):
    return templates.TemplateResponse("actions/discogs_sync.html", {"request": request})


@router.get("/discogs-import", response_class=HTMLResponse)
def modal_discogs_import(request: Request):
    return templates.TemplateResponse("actions/discogs_import.html", {"request": request})


@router.get("/bpm", response_class=HTMLResponse)
def modal_bpm(request: Request):
    return templates.TemplateResponse("actions/bpm.html", {"request": request})


@router.get("/close", response_class=HTMLResponse)
def close() -> HTMLResponse:
    """Clear #modal-root — used by Cancel buttons and Escape."""
    return HTMLResponse("")


# Job-start endpoints. Each returns an empty body + HX-Trigger to clear the
# modal and prompt the banner to refresh.

def _started_response() -> HTMLResponse:
    return HTMLResponse("", headers={"HX-Trigger": "runStatus"})


def _refused_response(reason: str) -> HTMLResponse:
    body = (
        f'<div class="modal-backdrop" onclick="if (event.target === this) htmx.ajax(\'GET\', \'/actions/close\', \'#modal-root\')">'
        f'  <div class="modal-card p-5">'
        f'    <div class="text-[13px] text-rose-300">{reason}</div>'
        f'    <div class="flex justify-end mt-3">'
        f'      <button class="btn btn-ghost" hx-get="/actions/close" hx-target="#modal-root" hx-swap="innerHTML">Close</button>'
        f'    </div>'
        f'  </div>'
        f'</div>'
    )
    return HTMLResponse(body)


@router.post("/discogs-sync/start", response_class=HTMLResponse)
def start_discogs_sync(folder: str = Form(""), limit: str = Form("")) -> HTMLResponse:
    if runner.current is not None and runner.current.status == "running":
        return _refused_response("Another job is already running — wait for it to finish.")
    argv: list[str] = []
    if folder.strip():
        argv += ["--folder", folder.strip()]
    if limit.strip():
        try:
            n = int(limit)
            if n > 0:
                argv += ["--limit", str(n)]
        except ValueError:
            pass
    runner.start("fetch", argv)
    return _started_response()


@router.post("/discogs-import/start", response_class=HTMLResponse)
async def start_discogs_import(
    csv: UploadFile = File(...),
    folder: str = Form(""),
) -> HTMLResponse:
    if runner.current is not None and runner.current.status == "running":
        return _refused_response("Another job is already running — wait for it to finish.")
    if not csv.filename:
        return _refused_response("No CSV file selected.")
    uploads = project_root() / ".tmp" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    safe = csv.filename.replace("/", "_").replace("\\", "_")
    dest = uploads / f"{uuid.uuid4().hex[:8]}-{safe}"
    with dest.open("wb") as f:
        shutil.copyfileobj(csv.file, f)
    argv: list[str] = ["--csv", str(dest)]
    if folder.strip():
        argv += ["--folder", folder.strip()]
    runner.start("fetch", argv)
    return _started_response()


@router.post("/bpm/start", response_class=HTMLResponse)
def start_bpm(force: str = Form("")) -> HTMLResponse:
    if runner.current is not None and runner.current.status == "running":
        return _refused_response("Another job is already running — wait for it to finish.")
    if force == "1":
        conn = sleeve_db.connect()
        try:
            with conn:
                conn.execute("DELETE FROM bpm_source_hits")
                conn.execute("DELETE FROM bpm_cache")
        finally:
            conn.close()
    runner.start("bpm", [])
    return _started_response()
