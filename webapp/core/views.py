"""Cross-cutting views — landing, home, healthz, run-toast banner."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from core.models import UserJobLock
from print_runs.services.render import (
    already_printed_ids,
    last_print_timestamp,
    open_draft,
    saved_runs,
    settings_summary,
)
from records.models import Override, Release
from records.services.bpm_cascade import bpm_coverage_breakdown


_KIND_LABEL = {
    "discogs_sync": "Discogs sync",
    "bpm_cascade": "BPM lookup",
}


def landing(request):
    """Public homepage. Authenticated users get Home; visitors the pitch."""
    if request.user.is_authenticated:
        return _render_home(request)
    return render(request, "landing.html", {})


@login_required
def home(request):
    """Authenticated homepage. One primary action, your saved sets, then
    the library. Deliberately not a metrics dashboard — the job to be done
    is making labels, so that is the only thing given weight."""
    return _render_home(request)


def _render_home(request):
    user = request.user
    coverage = bpm_coverage_breakdown(user)

    total_releases = Release.objects.filter(user=user).count()
    printed_ids = already_printed_ids(user)
    new_count = (
        Release.objects.filter(user=user, tracks__isnull=False)
        .distinct()
        .exclude(discogs_release_id__in=printed_ids)
        .count()
    )

    recent = [
        {
            "id": r.id,
            "name": r.display_name,
            "created_at": r.created_at,
            "release_count": len(r.release_ids or []),
            "settings_summary": settings_summary(r.settings),
        }
        for r in saved_runs(user)[:5]
    ]

    return render(request, "home.html", {
        "active": "home",
        "new_count": new_count,
        "last_print_ts": last_print_timestamp(user),
        "draft": open_draft(user),
        "recent_runs": recent,
        "run_count": saved_runs(user).count(),
        "lib_releases": total_releases,
        "lib_tracks": coverage["total"],
        "lib_missing": coverage["missing"],
        "lib_overrides": Override.objects.filter(user=user).count(),
    })


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
        "task_id": lock.task_id,
    })
