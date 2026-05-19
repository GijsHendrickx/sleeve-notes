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

    Three render paths:
    - no lock: empty body, toast vanishes
    - state=running: live progress, re-polls every 2s
    - state=done/failed: completion message; after COMPLETION_LINGER_S the
      lock row is lazy-deleted and the next poll returns empty
    """
    try:
        lock = request.user.job_lock
    except UserJobLock.DoesNotExist:
        return HttpResponse("")

    now = timezone.now()
    if lock.state != UserJobLock.STATE_RUNNING and lock.finished_at:
        age = (now - lock.finished_at).total_seconds()
        if age >= UserJobLock.COMPLETION_LINGER_S:
            lock.delete()
            return HttpResponse("")

    elapsed_s = int((now - lock.started_at).total_seconds())
    pct = (
        int(100 * lock.progress_done / lock.progress_total)
        if lock.progress_total else None
    )
    return render(request, "_run_toast.html", {
        "title": _KIND_LABEL.get(lock.kind, lock.kind),
        "elapsed_s": elapsed_s,
        "progress_text": lock.progress_text,
        "progress_pct": pct,
        "state": lock.state,
        "result_text": lock.result_text,
    })
