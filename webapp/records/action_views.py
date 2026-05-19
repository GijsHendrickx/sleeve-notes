"""HTMX action endpoints for the sidebar buttons.

Each action is a pair: a GET that returns a modal partial with optional
parameter inputs, and a POST that enqueues the job via records.jobs and
responds with HX-Trigger so the run-toast refreshes immediately.
"""
from __future__ import annotations

import shutil
import uuid

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from sleeve_notes import project_root

from core.models import LockHeld
from records.jobs import enqueue_bpm_cascade, enqueue_discogs_sync


def _trigger_response(content: str = "", *, status: int = 200) -> HttpResponse:
    """Empty 200 + HX-Trigger so the toast refreshes and the modal closes.

    Event names are kebab-case so the hx-on:* attribute listeners in
    base.html survive HTML attribute lowercasing.
    """
    resp = HttpResponse(content, status=status)
    resp["HX-Trigger"] = "run-status, close-modal"
    return resp


@login_required
@require_http_methods(["GET", "POST"])
def discogs_sync(request):
    if request.method == "GET":
        return render(request, "actions/discogs_sync.html", {
            "title": "Discogs sync",
            "description": (
                "Fetch your collection via the Discogs API. "
                "Releases already cached are skipped."
            ),
        })
    folder = (request.POST.get("folder") or "").strip() or None
    limit_raw = (request.POST.get("limit") or "").strip()
    try:
        limit = int(limit_raw) if limit_raw else None
    except ValueError:
        return _trigger_response(f"Invalid limit: {limit_raw!r}", status=400)
    try:
        enqueue_discogs_sync(
            request.user, source="api", folder=folder, limit=limit,
        )
    except LockHeld as e:
        return _trigger_response(f"Already running: {e}", status=409)
    return _trigger_response()


@login_required
@require_http_methods(["GET", "POST"])
def discogs_import(request):
    if request.method == "GET":
        return render(request, "actions/discogs_import.html", {
            "title": "Import Discogs CSV",
            "description": (
                "Use a Discogs collection export instead of the API. "
                "Per-release tracklists are still fetched from the API as needed."
            ),
        })
    upload = request.FILES.get("csv")
    if not upload or not upload.name:
        return _trigger_response("No CSV file selected.", status=400)
    # Persist the upload outside the request lifecycle so the Q2 worker
    # subprocess can read it. The worker doesn't yet delete it — for now
    # these accumulate in .tmp/uploads/; cleanup is a manual sweep.
    uploads_dir = project_root() / ".tmp" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    safe_name = upload.name.replace("/", "_").replace("\\", "_")
    dest = uploads_dir / f"{uuid.uuid4().hex[:8]}-{safe_name}"
    with dest.open("wb") as f:
        for chunk in upload.chunks():
            f.write(chunk)
    folder = (request.POST.get("folder") or "").strip() or None
    try:
        enqueue_discogs_sync(
            request.user, source="csv", csv_path=str(dest), folder=folder,
        )
    except LockHeld as e:
        dest.unlink(missing_ok=True)
        return _trigger_response(f"Already running: {e}", status=409)
    return _trigger_response()


@login_required
@require_http_methods(["GET", "POST"])
def bpm_cascade(request):
    if request.method == "GET":
        return render(request, "actions/bpm.html", {
            "title": "BPM lookup",
            "description": (
                "Run the 5-source BPM/key cascade across every track in your "
                "collection. Fully-cached tracks are skipped."
            ),
        })
    workers_raw = (request.POST.get("workers") or "8").strip()
    try:
        workers = int(workers_raw)
    except ValueError:
        return _trigger_response(f"Invalid workers: {workers_raw!r}", status=400)
    force = (request.POST.get("force") or "").strip() in ("1", "true", "on", "yes")
    try:
        enqueue_bpm_cascade(request.user, workers=workers, force=force)
    except LockHeld as e:
        return _trigger_response(f"Already running: {e}", status=409)
    return _trigger_response()
