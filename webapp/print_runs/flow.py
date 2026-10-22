"""The sleeve-labels flow: four steps from record selection to PDF.

Replaces the old one-page print-run editor. The run itself is the flow —
there is no separate wizard. A PrintRun is created as a `draft` the moment
the user enters, so closing the tab never loses work; `PrintRun.step`
records how far they got and the flow reopens there. Step 4 flips the
draft to `saved`, which is also the moment its releases start counting as
"printed" (see render.already_printed_ids).

Each step is a plain form POST that advances and redirects, so Back/Forward
and refresh behave. Only two things use HTMX: the live label preview in
step 3 and the coverage/job poll in step 2.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from sleeve_notes import sticker_layout as L
from sleeve_notes.preview import render_release_stickers_svg, render_sheet_svg

from core.models import LockHeld, UserJobLock
from records.jobs import enqueue_bpm_cascade
from records.models import Release
from records.services.bpm_cascade import (
    parse_override_post,
    precise_overrides,
    save_track_overrides,
)

from print_runs.models import PrintRun
from print_runs.services.render import (
    DEFAULT_SETTINGS,
    already_printed_ids,
    build_bpm_lookup,
    create_print_run,
    normalize_settings,
    open_draft,
    releases_by_ids,
    settings_summary,
)


STEP_COUNT = 4

STEP_TITLES = {
    1: "Records",
    2: "BPM",
    3: "Design",
    4: "Get them",
}

_BOOL_SETTINGS = (
    "tile", "qr",
    "show_artist", "show_title", "show_rpm",
    "show_key", "show_bpm", "show_duration",
    "show_track_title", "show_sides",
)
_NUMERIC_SETTINGS = ("sticker_w", "sticker_h", "tile_cols", "tile_rows")

# (key, label, description) — the order the preset chips appear in.
PRESET_OPTIONS = (
    ("minimal", "Minimal", "Position and BPM. Nothing else."),
    ("dj", "DJ", "BPM and Camelot key, grouped per side."),
    ("full", "Everything", "Titles, durations, RPM and a Discogs QR."),
)

# (settings key, label) for the checkbox grid under the presets.
TOGGLE_OPTIONS = (
    ("show_artist", "Release artist"),
    ("show_title", "Release title"),
    ("show_rpm", "RPM"),
    ("qr", "Discogs QR"),
    ("show_track_title", "Track titles"),
    ("show_duration", "Duration"),
    ("show_key", "Camelot key"),
    ("show_bpm", "BPM"),
    ("show_sides", "Group by side"),
)

# What each style preset sets. The checkboxes in step 3 stay authoritative:
# a chip is a bulk-set applied client-side, and editing any checkbox
# afterwards just deselects the chip. Shipped to the template as JSON so the
# two can't drift.
PRESETS = {
    "minimal": {
        "show_artist": True, "show_title": False, "show_rpm": False, "qr": False,
        "show_track_title": False, "show_duration": False,
        "show_key": False, "show_bpm": True, "show_sides": False,
    },
    "dj": {
        "show_artist": True, "show_title": True, "show_rpm": False, "qr": False,
        "show_track_title": True, "show_duration": False,
        "show_key": True, "show_bpm": True, "show_sides": True,
    },
    "full": {
        "show_artist": True, "show_title": True, "show_rpm": True, "qr": True,
        "show_track_title": True, "show_duration": True,
        "show_key": True, "show_bpm": True, "show_sides": True,
    },
}


# (key, label, description, settings) — paper choices for step 3. Like the
# style presets these are bulk-sets over the real inputs; "custom" is
# whatever doesn't match one of them.
STOCK_OPTIONS = (
    ("standard", "96 × 50.8 mm", "Gap between labels. The default.",
     {"tile": False, "sticker_w": 96, "sticker_h": 50.8, "tile_cols": 2, "tile_rows": 5}),
    ("tile_2x5", "2 × 5 per sheet", "Edge to edge, 105 × 59.4 mm.",
     {"tile": True, "sticker_w": 105, "sticker_h": 59.4, "tile_cols": 2, "tile_rows": 5}),
    ("tile_2x4", "2 × 4 per sheet", "Edge to edge, 105 × 74.3 mm.",
     {"tile": True, "sticker_w": 105, "sticker_h": 74.3, "tile_cols": 2, "tile_rows": 4}),
    ("tile_3x7", "3 × 7 per sheet", "Edge to edge, 70 × 42.4 mm.",
     {"tile": True, "sticker_w": 70, "sticker_h": 42.4, "tile_cols": 3, "tile_rows": 7}),
)
STOCKS = {key: values for key, _l, _d, values in STOCK_OPTIONS}

PREVIEW_RELEASE_CAP = 30


def settings_from_post(post) -> dict:
    """Build a settings dict from step 3's POST data.

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


def active_preset(settings: dict) -> str | None:
    """Which preset chip to light up, if the settings match one exactly."""
    for name, preset in PRESETS.items():
        if all(bool(settings.get(k)) == v for k, v in preset.items()):
            return name
    return None


def active_stock(settings: dict) -> str:
    """Which paper choice matches these settings, or "custom"."""
    for name, stock in STOCKS.items():
        if bool(settings.get("tile")) != stock["tile"]:
            continue
        if stock["tile"]:
            same = (settings.get("tile_cols"), settings.get("tile_rows")) == (
                stock["tile_cols"], stock["tile_rows"])
        else:
            same = (float(settings.get("sticker_w", 0)), float(settings.get("sticker_h", 0))) == (
                float(stock["sticker_w"]), float(stock["sticker_h"]))
        if same:
            return name
    return "custom"


def _get_run(request, run_id) -> PrintRun:
    return get_object_or_404(PrintRun, pk=run_id, user=request.user)


def _rail(run: PrintRun, current: int) -> list[dict]:
    reachable = max(run.step, current)
    return [
        {
            "n": n,
            "title": STEP_TITLES[n],
            "current": n == current,
            "done": n < current,
            # Never let the rail jump past what the user has actually reached;
            # step 3 with no records selected would render an empty label.
            "reachable": n <= reachable,
        }
        for n in range(1, STEP_COUNT + 1)
    ]


def _base_ctx(run: PrintRun, step: int) -> dict:
    return {
        "run": run,
        "step": step,
        "rail": _rail(run, step),
        "in_flow": True,
    }


def _advance(run: PrintRun, to_step: int) -> None:
    """Remember the furthest step reached, so reopening a draft resumes there."""
    if run.step < to_step:
        run.step = to_step
        run.save(update_fields=["step", "updated_at"])


# ─── Entry ───────────────────────────────────────────────────────────────────


@login_required
def start(request):
    """Open the flow: resume the existing draft, or create one.

    At most one draft per user — starting again reuses it rather than
    littering half-finished sets across the home screen.
    """
    draft = open_draft(request.user)
    if draft is None:
        printed = already_printed_ids(request.user)
        new_ids = list(
            Release.objects.filter(user=request.user, tracks__isnull=False)
            .exclude(discogs_release_id__in=printed)
            .values_list("discogs_release_id", flat=True)
        )
        draft = create_print_run(
            request.user, new_ids, settings=dict(DEFAULT_SETTINGS),
            status=PrintRun.STATUS_DRAFT, step=1,
        )
    return redirect("print_runs:step", run_id=draft.id, step=draft.step)


@login_required
def step(request, run_id, step: int):
    if step < 1 or step > STEP_COUNT:
        raise Http404
    run = _get_run(request, run_id)
    # A saved set is what went to the printer; editing it would rewrite print
    # history (already_printed_ids) and make "Download again" a different PDF.
    # Duplicate is the way to change one.
    if not run.is_draft:
        return redirect("print_runs:detail", run_id=run.id)
    if request.method == "POST":
        return _handle_step_post(request, run, step)
    return _STEP_RENDERERS[step](request, run)


def _handle_step_post(request, run: PrintRun, step: int):
    if step == 1:
        ids = sorted({
            int(x) for x in request.POST.getlist("release_id") if str(x).isdigit()
        })
        name = (request.POST.get("name") or "").strip()
        if not ids:
            ctx = _step1_ctx(request, run)
            ctx["error"] = "Pick at least one record."
            return render(request, "flow/step1_records.html", ctx)
        run.release_ids = ids
        run.name = name
        run.save(update_fields=["release_ids", "name", "updated_at"])
        _advance(run, 2)
        return redirect("print_runs:step", run_id=run.id, step=2)

    if step == 2:
        save_track_overrides(request.user, parse_override_post(request.POST))
        _advance(run, 3)
        return redirect("print_runs:step", run_id=run.id, step=3)

    if step == 3:
        run.settings = normalize_settings(settings_from_post(request.POST))
        run.save(update_fields=["settings", "updated_at"])
        _advance(run, 4)
        return redirect("print_runs:step", run_id=run.id, step=4)

    # Step 4 posts only to finish.
    if not run.release_ids:
        return redirect("print_runs:step", run_id=run.id, step=1)
    run.status = PrintRun.STATUS_SAVED
    run.save(update_fields=["status", "updated_at"])
    return redirect("home")


# ─── Step 1 — records ────────────────────────────────────────────────────────


def _step1_ctx(request, run: PrintRun) -> dict:
    printed = already_printed_ids(request.user)
    selected = {int(r) for r in (run.release_ids or [])}
    qs = (
        Release.objects.filter(user=request.user, tracks__isnull=False)
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
            "checked": rid in selected,
        })
    ctx = _base_ctx(run, 1)
    ctx.update({
        "rows": rows,
        "library_total": len(rows),
        "selected_count": sum(1 for r in rows if r["checked"]),
    })
    return ctx


def _render_step1(request, run: PrintRun):
    return render(request, "flow/step1_records.html", _step1_ctx(request, run))


# ─── Step 2 — BPM ────────────────────────────────────────────────────────────


def _coverage_ctx(user, run: PrintRun) -> dict:
    """Coverage over this set only, plus the tracks that need a human.

    "Needs your eye" is deliberately narrow: missing, disputed, or resolved
    from a single source. Confidently-derived tracks stay out of the way.
    """
    releases = releases_by_ids(user, [int(r) for r in (run.release_ids or [])])
    lookup = build_bpm_lookup(user, releases) if releases else {}
    overrides = precise_overrides(user)

    total = high = single = disputed = missing = 0
    fixes: list[dict] = []
    for rel in releases:
        rid = rel["id"]
        per_pos = lookup.get(rid, {})
        for t in rel["tracks"]:
            pos = t.get("position", "")
            res = per_pos.get(pos, {})
            bpm = res.get("bpm")
            conf = res.get("bpm_confidence") or ""
            total += 1

            reason = ""
            if bpm is None:
                missing += 1
                reason = "not found"
            elif conf == "disputed":
                disputed += 1
                reason = "sources disagree"
            elif conf in ("high", "octave", "shared"):
                high += 1
            else:
                single += 1
                reason = (
                    "measured from audio"
                    if res.get("bpm_sources") == ["audio_analysis"]
                    else "one source only"
                )

            if reason:
                ov = overrides.get((rid, pos))
                fixes.append({
                    "release_id": rid,
                    "position": pos,
                    "title": t.get("title") or "",
                    "release_title": rel.get("title") or "",
                    "reason": reason,
                    "bpm": ov.bpm if ov and ov.bpm else (bpm or ""),
                    "key": (ov.key_camelot if ov else "") or res.get("key_camelot") or "",
                    "sources": ", ".join(res.get("bpm_source_labels") or []),
                    "audio_analysis": res.get("audio_analysis"),
                })

    with_bpm = total - missing
    pct = (lambda n: round(100 * n / total, 2) if total else 0)
    return {
        "cov_total": total,
        "cov_with_bpm": with_bpm,
        "cov_high": high,
        "cov_single": single,
        "cov_disputed": disputed,
        "cov_missing": missing,
        "pct_high": pct(high),
        "pct_single": pct(single),
        "pct_disputed": pct(disputed),
        "pct_missing": pct(missing),
        "fixes": fixes,
        "release_count": len(releases),
    }


def _job_ctx(user) -> dict:
    """The user's live job, if it's the cascade. Step 2 renders progress
    inline — during the flow the job is the screen, not a corner toast."""
    try:
        lock = user.job_lock
    except UserJobLock.DoesNotExist:
        return {"job": None}
    if lock.kind != "bpm_cascade":
        return {"job": None}
    pct = (
        int(100 * lock.progress_done / lock.progress_total)
        if lock.progress_total else 0
    )
    return {"job": {
        "state": lock.state,
        "text": lock.progress_text,
        "done": lock.progress_done,
        "total": lock.progress_total,
        "pct": pct,
        "result": lock.result_text,
        "running": lock.state == UserJobLock.STATE_RUNNING,
    }}


def _render_step2(request, run: PrintRun):
    ctx = _base_ctx(run, 2)
    ctx.update(_coverage_ctx(request.user, run))
    ctx.update(_job_ctx(request.user))
    return render(request, "flow/step2_bpm.html", ctx)


@login_required
def step2_status(request, run_id):
    """HTMX poll: coverage bar + inline job progress. Polls every 2s while
    a cascade runs, then stops (the partial drops the hx-trigger when idle)."""
    run = _get_run(request, run_id)
    ctx = _base_ctx(run, 2)
    ctx.update(_coverage_ctx(request.user, run))
    ctx.update(_job_ctx(request.user))
    return render(request, "flow/_step2_status.html", ctx)


@login_required
@require_POST
def step2_lookup(request, run_id):
    """Start the cascade for this set's records only."""
    run = _get_run(request, run_id)
    ids = [int(r) for r in (run.release_ids or [])]
    force = (request.POST.get("force") or "").strip() in ("1", "true", "on", "yes")
    try:
        enqueue_bpm_cascade(request.user, force=force, release_ids=ids)
    except LockHeld:
        pass  # Another job is already running; the poll will show it.
    return redirect("print_runs:step", run_id=run.id, step=2)


# ─── Step 3 — design ─────────────────────────────────────────────────────────


def _preview(user, run: PrintRun, settings: dict, view: str, idx: int) -> dict:
    """Render the step 3 preview: one A4 sheet, or one label up close.

    Both go through the engine path the PDF takes (derive_layout →
    render_release_stickers_svg), so what the user tweaks here is what
    comes out of the printer.
    """
    ids = [int(r) for r in (run.release_ids or [])]
    s = normalize_settings(settings)
    out = {"svg": "", "error": None, "label_mm": None, "per_page": 0, "view": view}
    if not ids:
        out["error"] = "No records selected yet."
        return out
    try:
        layout = L.derive_layout(
            s["sticker_w"], s["sticker_h"],
            tile=s["tile"], tile_cols=s["tile_cols"], tile_rows=s["tile_rows"],
        )
    except ValueError as e:
        out["error"] = str(e)
        return out
    out["label_mm"] = (layout.sticker_w / L.mm, layout.sticker_h / L.mm)
    out["per_page"] = layout.per_page

    if view == "sheet":
        wanted = ids[:min(layout.per_page, PREVIEW_RELEASE_CAP)]
    else:
        wanted = [ids[min(max(idx, 0), len(ids) - 1)]]
    releases = releases_by_ids(user, wanted)
    if not releases:
        out["error"] = "None of the selected records have tracks yet."
        return out
    try:
        lookup = build_bpm_lookup(user, releases)
        svgs: list[str] = []
        for rel in releases:
            svgs += render_release_stickers_svg(
                rel, lookup[rel["id"]], layout,
                qr=s["qr"],
                show_artist=s["show_artist"],
                show_title=s["show_title"],
                show_rpm=s["show_rpm"],
                show_key=s["show_key"],
                show_bpm=s["show_bpm"],
                show_duration=s["show_duration"],
                show_track_title=s["show_track_title"],
                show_sides=s["show_sides"],
            )
    except (ValueError, KeyError) as e:
        out["error"] = str(e)
        return out
    if view == "sheet":
        out["svg"] = render_sheet_svg(svgs, layout)
    else:
        out["svg"] = svgs[0] if svgs else ""
    return out


def _release_choices(user, run: PrintRun) -> list[tuple[int, str]]:
    ids = [int(r) for r in (run.release_ids or [])]
    names = {
        r["discogs_release_id"]: f'{r["artist"] or "Various"} — {r["title"]}'
        for r in Release.objects.filter(user=user, discogs_release_id__in=ids)
        .values("discogs_release_id", "artist", "title")
    }
    return [(i, names[rid]) for i, rid in enumerate(ids) if rid in names]


def _step3_ctx(request, run: PrintRun, settings: dict, view: str = "sheet", idx: int = 0) -> dict:
    s = normalize_settings(settings)
    prev = _preview(request.user, run, settings, view, idx)
    ctx = _base_ctx(run, 3)
    ctx.update({
        "settings": s,
        "preview": prev,
        "view": view,
        "preview_idx": idx,
        "release_choices": _release_choices(request.user, run),
        "preset": active_preset(s),
        "stock": active_stock(s),
        "sheets": -(-len(run.release_ids or []) // prev["per_page"]) if prev["per_page"] else 0,
        "label_count": len(run.release_ids or []),
        "preset_options": PRESET_OPTIONS,
        "toggle_options": TOGGLE_OPTIONS,
        "stock_options": STOCK_OPTIONS,
        "presets": PRESETS,
        "stocks": STOCKS,
    })
    return ctx


def _render_step3(request, run: PrintRun):
    return render(request, "flow/step3_design.html",
                  _step3_ctx(request, run, run.settings or {}))


@login_required
@require_POST
def step3_preview(request, run_id):
    """HTMX: re-render the preview from the live form state."""
    run = _get_run(request, run_id)
    view = "label" if request.POST.get("view") == "label" else "sheet"
    try:
        idx = int(request.POST.get("preview_idx") or 0)
    except ValueError:
        idx = 0
    return render(request, "flow/_step3_preview.html",
                  _step3_ctx(request, run, settings_from_post(request.POST), view, idx))


# ─── Step 4 — get them ───────────────────────────────────────────────────────


def _render_step4(request, run: PrintRun):
    ids = [int(r) for r in (run.release_ids or [])]
    releases = releases_by_ids(request.user, ids)
    lookup = build_bpm_lookup(request.user, releases) if releases else {}

    tracks = sum(len(r["tracks"]) for r in releases)
    with_bpm = sum(
        1
        for r in releases
        for t in r["tracks"]
        if lookup.get(r["id"], {}).get(t.get("position", ""), {}).get("bpm")
    )
    s = normalize_settings(run.settings or {})
    try:
        layout = L.derive_layout(
            s["sticker_w"], s["sticker_h"],
            tile=s["tile"], tile_cols=s["tile_cols"], tile_rows=s["tile_rows"],
        )
        per_page = layout.per_page
        sheets = (len(releases) + per_page - 1) // per_page if per_page else 0
    except ValueError:
        per_page, sheets = 0, 0

    ctx = _base_ctx(run, 4)
    ctx.update({
        "record_count": len(releases),
        "track_count": tracks,
        "with_bpm": with_bpm,
        "sheet_count": sheets,
        "per_page": per_page,
        "per_page_range": range(min(per_page, 24)),
        "settings_summary": settings_summary(run.settings),
    })
    return render(request, "flow/step4_get.html", ctx)


_STEP_RENDERERS = {
    1: _render_step1,
    2: _render_step2,
    3: _render_step3,
    4: _render_step4,
}
