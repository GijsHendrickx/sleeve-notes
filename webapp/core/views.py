"""Cross-cutting views — landing, dashboard, healthz."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render


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
