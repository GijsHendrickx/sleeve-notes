"""Print runs app views — index, detail, PDF download."""
from __future__ import annotations

import re

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render

from print_runs.models import PrintRun
from print_runs.services.render import releases_by_ids, render_pdf


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


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


@login_required
def pdf_download(request, run_id):
    """Render this print run to a PDF and stream it back as a download.

    Render is synchronous: typical runs (10-100 releases) finish in a
    second or two and we'd rather block the request than orchestrate a
    background task + polling for a one-shot download. Heavy renders
    could be moved to Django-Q2 later — RenderResult.pdf_bytes is the
    artifact in either case.
    """
    run = get_object_or_404(PrintRun, pk=run_id, user=request.user)
    releases = releases_by_ids(request.user, [int(r) for r in run.release_ids])
    if not releases:
        return HttpResponse(
            "Print run has no renderable releases — every source release "
            "was removed from your collection or lacks tracks.",
            status=410,  # gone
        )
    try:
        result = render_pdf(
            request.user, releases=releases, settings=run.settings,
        )
    except ValueError as e:
        return HttpResponse(f"Render failed: {e}", status=400)

    # Cheap-insurance: stamp the hash so future requests can verify the
    # rendered bytes match what we delivered. See migration_plan.md.
    if run.pdf_hash != result.pdf_hash:
        run.pdf_hash = result.pdf_hash
        run.save(update_fields=["pdf_hash"])

    base = run.name or f"print-run-{str(run.id)[:8]}"
    filename = _FILENAME_SAFE.sub("-", base).strip("-") or "sleeve-notes"
    return HttpResponse(
        result.pdf_bytes,
        content_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}.pdf"',
            "Content-Length": str(len(result.pdf_bytes)),
        },
    )
