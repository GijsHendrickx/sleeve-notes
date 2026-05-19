"""Cross-cutting views — landing, dashboard, healthz, run-toast banner."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from core.models import UserJobLock


_KIND_LABEL = {
    "discogs_sync": "Discogs sync",
    "bpm_cascade": "BPM lookup",
}


def landing(request):
    """Public homepage. Authenticated users see the dashboard;
    visitors see the marketing landing."""
    if request.user.is_authenticated:
        return render(request, "dashboard.html", {"active": "dashboard"})
    return render(request, "landing.html", {})


@login_required
def dashboard(request):
    """Authenticated homepage. Sidebar wordmark links here."""
    return render(request, "dashboard.html", {"active": "dashboard"})


def healthz(_request):
    return HttpResponse("ok", content_type="text/plain")


@login_required
def run_banner(request):
    """HTMX partial for the fixed bottom-right toast.

    Renders empty when the user has no active job; renders a "running for Ns"
    card while UserJobLock exists. The card re-polls itself every 2s so the
    toast vanishes within 2s of the worker's `finally` releasing the lock.
    """
    try:
        lock = request.user.job_lock
    except UserJobLock.DoesNotExist:
        return HttpResponse("")
    elapsed_s = int((timezone.now() - lock.started_at).total_seconds())
    return render(request, "_run_toast.html", {
        "title": _KIND_LABEL.get(lock.kind, lock.kind),
        "elapsed_s": elapsed_s,
        "progress_text": lock.progress_text,
    })
