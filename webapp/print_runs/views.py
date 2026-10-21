"""Sleeve-label sets outside the flow — index, detail, duplicate, PDF.

The four-step flow lives in print_runs/flow.py. What's left here is what
you do with a set once it's saved: browse them, look inside one, copy one
as the starting point for the next, download the PDF, throw it away.
"""
from __future__ import annotations

import re

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from print_runs.models import PrintRun
from print_runs.services.render import (
    build_bpm_lookup,
    create_print_run,
    open_draft,
    releases_by_ids,
    render_pdf,
    saved_runs,
    settings_summary,
)


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _row(run: PrintRun) -> dict:
    return {
        "id": run.id,
        "name": run.display_name,
        "created_at": run.created_at,
        "release_count": len(run.release_ids or []),
        "settings_summary": settings_summary(run.settings),
    }


@login_required
def index(request):
    runs = list(saved_runs(request.user))
    return render(request, "print_runs/index.html", {
        "active": "print_runs",
        "rows": [_row(r) for r in runs],
        "run_count": len(runs),
        "draft": open_draft(request.user),
    })


@login_required
def detail(request, run_id):
    run = get_object_or_404(PrintRun, pk=run_id, user=request.user)
    if run.is_draft:
        return redirect("print_runs:step", run_id=run.id, step=run.step)

    requested_ids = [int(r) for r in (run.release_ids or [])]
    releases = releases_by_ids(request.user, requested_ids)
    present_ids = {r["id"] for r in releases}
    missing_ids = [rid for rid in requested_ids if rid not in present_ids]

    bpm_lookup = build_bpm_lookup(request.user, releases) if releases else {}
    show = run.settings or {}

    return render(request, "print_runs/detail.html", {
        "active": "print_runs",
        "run": run,
        "settings_summary": settings_summary(run.settings),
        "releases": releases,
        "missing_ids": missing_ids,
        "bpm_lookup": bpm_lookup,
        "show_bpm": show.get("show_bpm", True) if "show_bpm" in show else True,
        "show_key": show.get("show_key", True) if "show_key" in show else True,
    })


@login_required
@require_POST
def delete(request, run_id):
    run = get_object_or_404(PrintRun, pk=run_id, user=request.user)
    run.delete()
    return redirect("print_runs:index")


@login_required
@require_POST
def duplicate(request, run_id):
    """Copy a saved set into a fresh draft and drop the user into step 1.

    The common case for a returning user: same layout, different records.
    Any existing draft is replaced — one draft per user (see flow.start).
    """
    source = get_object_or_404(PrintRun, pk=run_id, user=request.user)
    existing = open_draft(request.user)
    if existing is not None:
        existing.delete()
    draft = create_print_run(
        request.user,
        [int(r) for r in (source.release_ids or [])],
        settings=source.settings or {},
        name=f"{source.display_name} (copy)",
        status=PrintRun.STATUS_DRAFT,
        step=1,
    )
    return redirect("print_runs:step", run_id=draft.id, step=1)


@login_required
def pdf_download(request, run_id):
    """Render this set to a PDF and stream it back as a download.

    Render is synchronous: typical sets (10-100 records) finish in a
    second or two and we'd rather block the request than orchestrate a
    background task + polling for a one-shot download. Heavy renders
    could be moved to Django-Q2 later — RenderResult.pdf_bytes is the
    artifact in either case.
    """
    run = get_object_or_404(PrintRun, pk=run_id, user=request.user)
    releases = releases_by_ids(request.user, [int(r) for r in run.release_ids])
    if not releases:
        return HttpResponse(
            "This set has no renderable records — every one of them was "
            "removed from your collection or lacks tracks.",
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

    base = run.name or f"sleeve-labels-{str(run.id)[:8]}"
    filename = _FILENAME_SAFE.sub("-", base).strip("-") or "sleeve-labels"
    return HttpResponse(
        result.pdf_bytes,
        content_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}.pdf"',
            "Content-Length": str(len(result.pdf_bytes)),
        },
    )
