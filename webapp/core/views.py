"""Cross-cutting views — landing, dashboard, healthz, run-toast banner."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from core.models import UserJobLock
from print_runs.models import PrintRun
from print_runs.services.render import (
    already_printed_ids,
    last_print_timestamp,
    settings_summary,
)
from records.models import Release
from records.services.bpm_cascade import bpm_coverage_breakdown


_KIND_LABEL = {
    "discogs_sync": "Discogs sync",
    "bpm_cascade": "BPM lookup",
}


def landing(request):
    """Public homepage. Authenticated users see the dashboard;
    visitors see the marketing landing."""
    if request.user.is_authenticated:
        return _render_dashboard(request)
    return render(request, "landing.html", {})


@login_required
def dashboard(request):
    """Authenticated homepage. Sidebar wordmark links here."""
    return _render_dashboard(request)


def _render_dashboard(request):
    user = request.user
    coverage = bpm_coverage_breakdown(user)

    total_releases = Release.objects.filter(user=user).count()
    by_type = list(
        Release.objects.filter(user=user)
        .exclude(release_type="")
        .values("release_type")
        .annotate(n=Count("id"))
        .order_by("-n")[:6]
    )

    printed_ids = already_printed_ids(user)
    new_count = (
        Release.objects.filter(user=user)
        .exclude(discogs_release_id__in=printed_ids)
        .count()
    )
    last_print_ts = last_print_timestamp(user)

    recent_runs_qs = PrintRun.objects.filter(user=user)[:6]
    recent_runs = [
        {
            "id": r.id,
            "name": r.name,
            "created_at": r.created_at,
            "release_count": len(r.release_ids or []),
            "settings_summary": settings_summary(r.settings),
        }
        for r in recent_runs_qs
    ]
    run_count = PrintRun.objects.filter(user=user).count()

    coverage_total = coverage["total"] or 1
    coverage_segments = [
        ("high", coverage["high"], "bg-emerald-500", "high"),
        ("octave", coverage["octave"], "bg-cyan-500", "oct."),
        ("shared", coverage["shared"], "bg-yellow-500", "shared"),
        ("single", coverage["single"], "bg-sky-500", "single"),
        ("disputed", coverage["disputed"], "bg-amber-500", "disp."),
        ("missing", coverage["missing"], "bg-app-s3", "miss"),
    ]
    coverage_segments = [
        {
            "key": k, "n": n, "pct": round(100 * n / coverage_total, 2),
            "color": color, "label": label,
        }
        for (k, n, color, label) in coverage_segments
    ]

    return render(request, "dashboard.html", {
        "active": "dashboard",
        "coverage": coverage,
        "coverage_segments": coverage_segments,
        "totals_total": total_releases,
        "totals_by_type": by_type,
        "new_count": new_count,
        "last_print_ts": last_print_ts,
        "recent_runs": recent_runs,
        "run_count": run_count,
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
