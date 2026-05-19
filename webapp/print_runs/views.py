"""Print runs app views — index, detail, editor, PDF download."""
from __future__ import annotations

import re

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from records.models import Release

from print_runs.models import PrintRun
from print_runs.services.render import (
    already_printed_ids,
    create_print_run,
    releases_by_ids,
    render_pdf,
)


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
@require_POST
def delete(request, run_id):
    run = get_object_or_404(PrintRun, pk=run_id, user=request.user)
    run.delete()
    return redirect("print_runs:index")


@login_required
def editor(request):
    """Pick releases and create a new PrintRun.

    GET: render a checkbox table over every release in the user's
    collection. Already-printed releases are tagged so a "Select new
    only" JS button can match the legacy --new-only flag.

    POST: create a PrintRun with the selected discogs_release_ids and
    redirect to its detail page so the user can download immediately.
    """
    if request.method == "POST":
        ids = [
            int(x) for x in request.POST.getlist("release_id")
            if str(x).isdigit()
        ]
        if not ids:
            return render(request, "print_runs/editor.html", {
                "active": "print_runs",
                "error": "Select at least one release.",
                "name": (request.POST.get("name") or "").strip(),
                "rows": _editor_rows(request.user, preselected=set()),
            })
        name = (request.POST.get("name") or "").strip()
        run = create_print_run(request.user, ids, settings={}, name=name)
        return redirect("print_runs:detail", run_id=run.id)

    return render(request, "print_runs/editor.html", {
        "active": "print_runs",
        "name": "",
        "rows": _editor_rows(request.user),
    })


def _editor_rows(request_user, preselected: set[int] | None = None) -> list[dict]:
    """Build the editor's release rows. ``preselected`` lets the POST
    handler re-render with the user's last selection on validation error."""
    printed = already_printed_ids(request_user)
    qs = (
        Release.objects.filter(user=request_user, tracks__isnull=False)
        .distinct()
        .order_by("artist", "title")
        .values("discogs_release_id", "artist", "title", "year",
                "release_type", "format", "thumb_url")
    )
    rows = []
    for r in qs:
        rid = r["discogs_release_id"]
        rows.append({
            **r,
            "is_new": rid not in printed,
            "checked": rid in preselected if preselected is not None else rid not in printed,
        })
    return rows


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
