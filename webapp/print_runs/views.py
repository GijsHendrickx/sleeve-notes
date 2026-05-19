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
    DEFAULT_SETTINGS,
    already_printed_ids,
    build_bpm_lookup,
    create_print_run,
    releases_by_ids,
    render_pdf,
    settings_summary,
)


_BOOL_SETTINGS = (
    "tile", "qr",
    "show_artist", "show_title", "show_rpm",
    "show_key", "show_bpm", "show_duration",
    "show_track_title", "show_sides",
)
_NUMERIC_SETTINGS = ("sticker_w", "sticker_h", "tile_cols", "tile_rows")


def _settings_from_post(post) -> dict:
    """Build a settings dict from editor POST data.

    Numeric fields pass through verbatim (normalize_settings coerces).
    Bool fields are forced: present → "on", absent → "" — so an unchecked
    checkbox resolves to False instead of falling back to DEFAULT_SETTINGS.
    """
    out: dict = {}
    for f in _NUMERIC_SETTINGS:
        v = (post.get(f) or "").strip()
        if v:
            out[f] = v
    for f in _BOOL_SETTINGS:
        out[f] = "on" if post.get(f) else ""
    return out


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@login_required
def index(request):
    runs = list(PrintRun.objects.filter(user=request.user))
    rows = [
        {
            "id": r.id,
            "name": r.name,
            "created_at": r.created_at,
            "release_count": len(r.release_ids or []),
            "settings_summary": settings_summary(r.settings),
        }
        for r in runs
    ]
    return render(request, "print_runs/index.html", {
        "active": "print_runs",
        "rows": rows,
        "run_count": len(rows),
    })


@login_required
def detail(request, run_id):
    run = get_object_or_404(PrintRun, pk=run_id, user=request.user)
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
        settings_in = _settings_from_post(request.POST)
        name = (request.POST.get("name") or "").strip()
        if not ids:
            return render(request, "print_runs/editor.html", {
                "active": "print_runs",
                "error": "Select at least one release.",
                "name": name,
                "rows": _editor_rows(request.user, preselected=set()),
                "settings": settings_in or dict(DEFAULT_SETTINGS),
            })
        run = create_print_run(request.user, ids, settings=settings_in, name=name)
        return redirect("print_runs:detail", run_id=run.id)

    return render(request, "print_runs/editor.html", {
        "active": "print_runs",
        "name": "",
        "rows": _editor_rows(request.user),
        "settings": dict(DEFAULT_SETTINGS),
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
