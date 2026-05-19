"""Print runs app views — index, detail, PDF download.

All stubs during fase 5; the real listing + editor + render endpoint
ship in follow-up commits.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from print_runs.models import PrintRun


@login_required
def index(request):
    runs = PrintRun.objects.filter(user=request.user)
    return render(request, "print_runs/index.html", {
        "active": "print_runs",
        "runs": runs,
        "run_count": runs.count(),
    })


@login_required
def detail(request, run_id):
    run = get_object_or_404(PrintRun, pk=run_id, user=request.user)
    return render(request, "print_runs/detail.html", {
        "active": "print_runs",
        "run": run,
    })
