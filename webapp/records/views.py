"""Records app views — collection (records list), tracks list, release detail."""
from __future__ import annotations

from urllib.parse import quote_plus

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponseBadRequest, HttpResponseNotFound, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from sleeve_notes import sticker_layout as L
from sleeve_notes.fetch_bpm import (
    BPM_MAX,
    BPM_MIN,
    RateLimiter,
    cache_key,
    cascade_all,
    parse_key_to_camelot,
)
from sleeve_notes.preview import render_release_stickers_svg
from sleeve_notes.youtube import resolve_video_id

from records.models import BpmCache, Override, Release, Track
from records.services.bpm_cascade import (
    bpm_coverage_for_releases,
    derive_track_result,
    load_all_cache_entries,
    load_overrides,
    save_cache_entry,
)
from print_runs.services.render import build_bpm_lookup, releases_by_ids


LIST_LIMIT = 500
TRACKS_LIMIT = 2000


def _build_track_row(
    user, track, *, by_rp, by_tk, cache_entries, precise,
    override_display=None,
):
    """Build the row dict for one Track. ``override_display`` lets callers
    (sync endpoint) preserve in-progress form edits across a re-render."""
    rel = track.release
    release_artist = rel.artist or "V/A"
    result = derive_track_result(
        user, rel.discogs_release_id,
        track.position or "",
        track.artist or release_artist,
        track.title or "",
        by_rp, by_tk,
        cache_entries=cache_entries,
    )
    if override_display is None:
        ovr = precise.get((rel.discogs_release_id, track.position or ""))
        override_display = {
            "bpm": ovr.bpm if ovr else None,
            "key_camelot": ovr.key_camelot if ovr else "",
            "note": ovr.note if ovr else "",
        }
    return {
        "release_id": rel.discogs_release_id,
        "release_artist": release_artist,
        "release_title": rel.title,
        "position": track.position,
        "artist": track.artist or release_artist,
        "title": track.title,
        "bpm": result.get("bpm"),
        "bpm_confidence": result.get("bpm_confidence"),
        "bpm_sources": result.get("bpm_sources"),
        "key_camelot": result.get("key_camelot"),
        "spotify_track_id": track.spotify_track_id,
        "override": override_display,
    }


def _load_precise_overrides(user) -> dict[tuple[int, str], Override]:
    """Map (discogs_release_id, position) → Override row (precise form only).

    Broad overrides (artist+title without release+position) live in by_tk
    from load_overrides() and aren't editable per-row from /tracks.
    """
    out: dict[tuple[int, str], Override] = {}
    for o in (
        Override.objects.filter(user=user, release__isnull=False)
        .select_related("release")
    ):
        out[(o.release.discogs_release_id, o.position or "")] = o
    return out


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
    precise = _load_precise_overrides(request.user)

    rows = []
    for t in tracks_db:
        row = _build_track_row(
            request.user, t,
            by_rp=by_rp, by_tk=by_tk,
            cache_entries=cache_entries, precise=precise,
        )
        if filter_key == "needs_attention" and (row["bpm"] or row["override"]["bpm"]):
            continue
        if filter_key == "has_override" and not row["override"]["bpm"] and \
                not row["override"]["key_camelot"] and not row["override"]["note"]:
            continue
        rows.append(row)

    return render(request, "records/tracks.html", {
        "active": "tracks",
        "rows": rows,
        "shown_count": len(rows),
        "total": total,
        "q": q,
        "filter": filter_key,
        "saved": request.GET.get("saved"),
        "filter_presets": [
            ("all", "All"),
            ("needs_attention", "Without BPM"),
            ("has_override", "Has override"),
        ],
    })


@login_required
@require_POST
def tracks_save(request):
    """Batch-save per-row overrides from the /tracks form.

    Form fields look like ``bpm_<discogs_release_id>_<position>`` etc.
    A row with all three fields blank deletes the override; any combination
    of non-blank fields creates or updates it.
    """
    grouped: dict[tuple[int, str], dict[str, str]] = {}
    for key, value in request.POST.items():
        for prefix in ("bpm_", "key_", "note_"):
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            try:
                rid_s, pos = rest.split("_", 1)
                rid = int(rid_s)
            except ValueError:
                continue
            grouped.setdefault((rid, pos), {})[prefix[:-1]] = value.strip()
            break

    tracks_url = "/tracks"
    if not grouped:
        return redirect(f"{tracks_url}?saved=0")

    existing = _load_precise_overrides(request.user)
    releases_by_did = {
        r.discogs_release_id: r
        for r in Release.objects.filter(
            user=request.user,
            discogs_release_id__in=[rid for (rid, _) in grouped],
        )
    }

    changed = 0
    for (rid, pos), entry in grouped.items():
        bpm_raw = entry.get("bpm") or ""
        key_raw = entry.get("key") or ""
        note_raw = entry.get("note") or ""

        bpm_val: int | None = None
        if bpm_raw:
            try:
                bpm_val = int(bpm_raw)
            except ValueError:
                continue
            if not (BPM_MIN <= bpm_val <= BPM_MAX):
                continue

        key_val = ""
        if key_raw:
            normalized = parse_key_to_camelot(key_raw)
            if normalized is None:
                continue
            key_val = normalized

        empty = bpm_val is None and not key_val and not note_raw
        current = existing.get((rid, pos))

        if empty and current is None:
            continue
        if empty and current is not None:
            current.delete()
            changed += 1
            continue

        rel = releases_by_did.get(rid)
        if rel is None:
            continue  # Release dropped out of the collection; skip.

        if current is None:
            Override.objects.create(
                user=request.user, release=rel, position=pos,
                bpm=bpm_val, key_camelot=key_val, note=note_raw,
            )
            changed += 1
            continue

        same = (
            current.bpm == bpm_val
            and (current.key_camelot or "") == key_val
            and (current.note or "") == note_raw
        )
        if same:
            continue
        current.bpm = bpm_val
        current.key_camelot = key_val
        current.note = note_raw
        current.save(update_fields=["bpm", "key_camelot", "note", "updated_at"])
        changed += 1

    # Preserve the filter/search context so the user lands back on the
    # same view they submitted from.
    qs = [f"saved={changed}"]
    if request.GET.get("filter"):
        qs.append(f"filter={request.GET['filter']}")
    if request.GET.get("q"):
        qs.append(f"q={request.GET['q']}")
    return redirect(f"{tracks_url}?{'&'.join(qs)}")


@login_required
def release_detail(request, release_id):
    """HTMX partial for the right-side drawer on /collection.

    Returns the release header + tracklist with derived BPM/key + a list of
    inline SVG sticker previews. Falls through to a full-page render when
    accessed without HTMX (so deep-linking still works).
    """
    rel = get_object_or_404(
        Release.objects.prefetch_related("tracks"),
        pk=release_id, user=request.user,
    )
    release_artist = rel.artist or "V/A"

    by_rp, by_tk, _ = load_overrides(request.user)
    cache_entries = load_all_cache_entries(request.user)

    tracks_out = []
    for t in rel.tracks.all().order_by("position"):
        result = derive_track_result(
            request.user, rel.discogs_release_id,
            t.position or "",
            t.artist or release_artist,
            t.title or "",
            by_rp, by_tk,
            cache_entries=cache_entries,
        )
        tracks_out.append({
            "release_id": rel.discogs_release_id,
            "position": t.position,
            "artist": t.artist or release_artist,
            "title": t.title,
            "duration": t.duration,
            "bpm": result.get("bpm"),
            "bpm_confidence": result.get("bpm_confidence"),
            "bpm_sources": result.get("bpm_sources"),
            "key_camelot": result.get("key_camelot"),
            "spotify_track_id": t.spotify_track_id,
        })

    # Build sticker preview SVGs by reusing the print_runs render service.
    svgs: list[str] = []
    svg_error: str | None = None
    render_releases = releases_by_ids(request.user, [rel.discogs_release_id])
    if render_releases:
        render_release = render_releases[0]
        try:
            layout = L.derive_layout(L.DEFAULT_STICKER_W_MM, L.DEFAULT_STICKER_H_MM)
            bpm_lookup = build_bpm_lookup(request.user, [render_release])
            svgs = render_release_stickers_svg(
                render_release, bpm_lookup[render_release["id"]], layout,
            )
        except (ValueError, KeyError) as e:
            svg_error = str(e)

    template = (
        "records/_release_detail.html"
        if request.headers.get("HX-Request")
        else "records/release_detail.html"
    )
    return render(request, template, {
        "active": "collection",
        "release": {
            "id": rel.id,
            "discogs_release_id": rel.discogs_release_id,
            "artist": rel.artist,
            "title": rel.title,
            "year": rel.year,
            "compilation": rel.compilation,
            "release_type": rel.release_type,
            "format": rel.format,
            "thumb_url": rel.thumb_url,
            "tracks": tracks_out,
        },
        "svgs": svgs,
        "svg_error": svg_error,
    })


@login_required
@require_POST
def tracks_sync(request):
    """Re-fetch BPM/key for one track from every source, return the new row.

    The current row's override inputs are submitted with the request (via
    HTMX hx-include="closest tr") so any unsaved edits survive the swap.
    """
    try:
        rid = int((request.POST.get("release_id") or "").strip())
    except ValueError:
        return HttpResponseBadRequest("invalid release_id")
    pos = (request.POST.get("position") or "").strip()

    track = (
        Track.objects.filter(
            release__user=request.user,
            release__discogs_release_id=rid,
            position=pos,
        )
        .select_related("release")
        .first()
    )
    if track is None:
        return HttpResponseNotFound(f"track {rid}/{pos} not found")

    release_artist = track.release.artist or "V/A"
    track_artist = track.artist or release_artist
    title = track.title or ""

    # Force-refetch: drop any cached row so cascade_all re-queries every source.
    ck = cache_key(track_artist, title)
    BpmCache.objects.filter(user=request.user, cache_key=ck).delete()

    rl = RateLimiter()
    entry = cascade_all(track_artist, title, prev=None, rl=rl)
    save_cache_entry(request.user, ck, track_artist, title, entry)

    spot_id = entry.get("spotify_track_id")
    if spot_id and not track.spotify_track_id:
        track.spotify_track_id = spot_id
        track.save(update_fields=["spotify_track_id"])

    # Preserve any unsaved override edits from the submitted form.
    bpm_raw = (request.POST.get(f"bpm_{rid}_{pos}") or "").strip()
    key_raw = (request.POST.get(f"key_{rid}_{pos}") or "").strip()
    note_raw = (request.POST.get(f"note_{rid}_{pos}") or "").strip()
    override_display = {
        "bpm": bpm_raw or None,
        "key_camelot": key_raw,
        "note": note_raw,
    }

    by_rp, by_tk, _ = load_overrides(request.user)
    cache_entries = load_all_cache_entries(request.user)
    precise = _load_precise_overrides(request.user)
    row = _build_track_row(
        request.user, track,
        by_rp=by_rp, by_tk=by_tk,
        cache_entries=cache_entries, precise=precise,
        override_display=override_display,
    )

    return render(request, "records/_track_row.html", {
        "r": row, "just_synced": True,
    })


@login_required
def listen_redirect(request):
    """Resolve a track to a Spotify or YouTube URL and 302 to it.

    Spotify wins when we have a cached track id (captured opportunistically
    by the BPM cascade via ReccoBeats). Otherwise lazily resolve the top
    YouTube hit on first click, persist the video id on the Track row, and
    redirect there. Falls back to a YouTube search URL when the YouTube API
    is unavailable or returns nothing, so the click always lands somewhere.
    """
    try:
        rid = int((request.GET.get("release_id") or "").strip())
    except ValueError:
        return HttpResponseBadRequest("invalid release_id")
    pos = (request.GET.get("position") or "").strip()

    track = (
        Track.objects.filter(
            release__user=request.user,
            release__discogs_release_id=rid,
            position=pos,
        )
        .select_related("release")
        .first()
    )

    if track and track.spotify_track_id:
        return HttpResponseRedirect(
            f"https://open.spotify.com/track/{track.spotify_track_id}"
        )
    if track and track.youtube_video_id:
        return HttpResponseRedirect(
            f"https://www.youtube.com/watch?v={track.youtube_video_id}"
        )

    if track is None:
        return HttpResponseRedirect(
            "https://www.youtube.com/results?search_query="
            + quote_plus(pos or "")
        )

    release_artist = track.release.artist or ""
    artist = track.artist or release_artist
    title = track.title or ""

    video_id = resolve_video_id(artist, title)
    if video_id:
        track.youtube_video_id = video_id
        track.save(update_fields=["youtube_video_id"])
        return HttpResponseRedirect(f"https://www.youtube.com/watch?v={video_id}")

    q = f"{artist} {title}".strip() or title or pos
    return HttpResponseRedirect(
        "https://www.youtube.com/results?search_query=" + quote_plus(q)
    )
