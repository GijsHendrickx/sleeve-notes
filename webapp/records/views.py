"""Records app views — collection (records list) and tracks list."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import render

from records.models import Release, Track
from records.services.bpm_cascade import (
    bpm_coverage_for_releases,
    derive_track_result,
    load_all_cache_entries,
    load_overrides,
)


LIST_LIMIT = 500
TRACKS_LIMIT = 2000


@login_required
def collection(request):
    q = (request.GET.get("q") or "").strip()
    type_filter = (request.GET.get("type") or "").strip()
    format_filter = (request.GET.get("format") or "").strip()
    bpm_filter = (request.GET.get("bpm") or "").strip()

    qs = Release.objects.filter(user=request.user)
    total = qs.count()

    if q:
        qs = qs.filter(Q(artist__icontains=q) | Q(title__icontains=q))
    if type_filter:
        qs = qs.filter(release_type=type_filter)
    if format_filter:
        qs = qs.filter(format=format_filter)

    releases = list(
        qs.order_by("artist", "title")
        .prefetch_related("tracks")[:LIST_LIMIT]
    )
    coverage = bpm_coverage_for_releases(request.user, releases)

    rows = []
    for rel in releases:
        total_t, with_bpm = coverage.get(rel.id, (0, 0))
        if bpm_filter == "missing" and (total_t == 0 or with_bpm == total_t):
            continue
        if bpm_filter == "complete" and (total_t == 0 or with_bpm != total_t):
            continue
        rows.append({
            "id": rel.id,
            "discogs_release_id": rel.discogs_release_id,
            "artist": rel.artist,
            "title": rel.title,
            "year": rel.year,
            "release_type": rel.release_type,
            "format": rel.format,
            "thumb_url": rel.thumb_url,
            "tracks_total": total_t,
            "tracks_with_bpm": with_bpm,
        })

    type_options = list(
        Release.objects.filter(user=request.user)
        .exclude(release_type="")
        .values_list("release_type", flat=True)
        .annotate(n=Count("id"))
        .order_by("-n")
        .distinct()
    )
    format_options = list(
        Release.objects.filter(user=request.user)
        .exclude(format="")
        .values_list("format", flat=True)
        .annotate(n=Count("id"))
        .order_by("-n")
        .distinct()
    )

    return render(request, "records/collection.html", {
        "active": "collection",
        "rows": rows,
        "shown_count": len(rows),
        "total": total,
        "q": q,
        "type_filter": type_filter,
        "format_filter": format_filter,
        "bpm_filter": bpm_filter,
        "type_options": type_options,
        "format_options": format_options,
    })


@login_required
def tracks(request):
    q = (request.GET.get("q") or "").strip()
    filter_key = (request.GET.get("filter") or "all").strip()

    qs = (
        Track.objects.filter(release__user=request.user)
        .select_related("release")
    )
    total = qs.count()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(artist__icontains=q))

    tracks_db = list(
        qs.order_by("release__artist", "release__title", "position")[:TRACKS_LIMIT]
    )

    by_rp, by_tk, _ = load_overrides(request.user)
    cache_entries = load_all_cache_entries(request.user)

    rows = []
    for t in tracks_db:
        rel = t.release
        release_artist = rel.artist or "V/A"
        result = derive_track_result(
            request.user, rel.discogs_release_id,
            t.position or "",
            t.artist or release_artist,
            t.title or "",
            by_rp, by_tk,
            cache_entries=cache_entries,
        )
        bpm = result.get("bpm")
        if filter_key == "needs_attention" and bpm:
            continue
        rows.append({
            "release_id": rel.discogs_release_id,
            "release_artist": release_artist,
            "release_title": rel.title,
            "position": t.position,
            "artist": t.artist or release_artist,
            "title": t.title,
            "bpm": bpm,
            "bpm_confidence": result.get("bpm_confidence"),
            "bpm_sources": result.get("bpm_sources"),
            "key_camelot": result.get("key_camelot"),
        })

    return render(request, "records/tracks.html", {
        "active": "tracks",
        "rows": rows,
        "shown_count": len(rows),
        "total": total,
        "q": q,
        "filter": filter_key,
        "filter_presets": [
            ("all", "All"),
            ("needs_attention", "Without BPM"),
        ],
    })
