"""HTMX action endpoints for the sidebar buttons.

Each action is a pair: a GET that returns a modal partial with optional
parameter inputs, and a POST that enqueues the job via records.jobs and
responds with HX-Trigger so the run-toast refreshes immediately.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

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
    try:
        enqueue_bpm_cascade(request.user, workers=workers)
    except LockHeld as e:
        return _trigger_response(f"Already running: {e}", status=409)
    return _trigger_response()
