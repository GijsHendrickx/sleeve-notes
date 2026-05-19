"""BPM cascade orchestration over the Django ORM.

The cascade itself (HTTP, source plugins, consensus) is still imported
from sleeve_notes.fetch_bpm — pure Python, no DB dependency. This module
provides the ORM bindings for everything around it:

- overrides loaded from records.Override
- per-song cache stored in records.BpmCache (one row per cache_key,
  shared across multiple Track rows that hold the same song)
- the top-level orchestrator that wires it all together

Called from `manage.py lookup_bpm` (CLI / cron) and the future
Django-Q2 task (web-triggered cascade).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from django.db import transaction

from records.models import BpmCache, Override, Release, Track

from sleeve_notes.fetch_bpm import (
    ALL_SOURCES,
    BPM_MAX,
    BPM_MIN,
    RateLimiter,
    build_track_result,
    cache_key,
    cascade_all,
    override_to_result,
    parse_key_to_camelot,
)


Logger = Callable[[str], None]


# ─── Overrides ───────────────────────────────────────────────────────────────


def load_overrides(user) -> tuple[dict, dict, list[str]]:
    """Validate + bucket the user's overrides into precise and broad lookups.

    Returns (by_release_position, by_track_key, warnings).
    """
    by_rp: dict[tuple[int, str], dict] = {}
    by_tk: dict[str, dict] = {}
    warnings: list[str] = []

    rows = Override.objects.filter(user=user).select_related("release")
    for o in rows:
        entry = {
            "id": str(o.id),
            "release_id": o.release.discogs_release_id if o.release else None,
            "position": o.position or None,
            "artist": o.artist or None,
            "title": o.title or None,
            "bpm": o.bpm,
            "key_camelot": o.key_camelot or None,
            "note": o.note or None,
        }

        if entry["bpm"] is not None:
            try:
                bpm_int = int(round(float(entry["bpm"])))
            except (TypeError, ValueError):
                warnings.append(f"override {o.id}: invalid bpm {entry['bpm']!r}")
                continue
            if not (BPM_MIN <= bpm_int <= BPM_MAX):
                warnings.append(
                    f"override {o.id}: bpm {bpm_int} outside {BPM_MIN}-{BPM_MAX}"
                )
                continue
            entry["bpm"] = bpm_int

        raw_key = entry.get("key_camelot")
        if raw_key is not None:
            normalized = parse_key_to_camelot(str(raw_key))
            if normalized is None:
                warnings.append(
                    f"override {o.id}: unrecognised key_camelot {raw_key!r}"
                )
                continue
            entry["key_camelot"] = normalized

        if entry["release_id"] is not None and entry["position"]:
            by_rp[(int(entry["release_id"]), str(entry["position"]))] = entry
        elif entry["artist"] and entry["title"]:
            by_tk[cache_key(str(entry["artist"]), str(entry["title"]))] = entry
        else:
            warnings.append(
                f"override {o.id}: must have (release_id, position) or (artist, title)"
            )

    return by_rp, by_tk, warnings


# ─── BPM cache I/O ───────────────────────────────────────────────────────────


def load_all_cache_entries(user) -> dict[str, dict]:
    """Pre-fetch every BpmCache row for the user into a `cache_key → entry` dict.

    Lets per-track callers in tight loops avoid an N+1 round-trip. The shape
    matches `load_cache_entry` so it's a drop-in for `derive_track_result`'s
    `cache_entries` kwarg.
    """
    out: dict[str, dict] = {}
    for row in BpmCache.objects.filter(user=user).only("cache_key", "sources_tried", "source_hits"):
        out[row.cache_key] = {
            "sources": dict(row.source_hits or {}),
            "sources_tried": list(row.sources_tried or []),
        }
    return out


def load_cache_entry(user, ck: str) -> dict | None:
    try:
        row = BpmCache.objects.only("sources_tried", "source_hits").get(
            user=user, cache_key=ck,
        )
    except BpmCache.DoesNotExist:
        return None
    return {
        "sources": dict(row.source_hits or {}),
        "sources_tried": list(row.sources_tried or []),
    }


@transaction.atomic
def save_cache_entry(user, ck: str, artist: str, title: str, entry: dict) -> None:
    sources = entry.get("sources") or {}
    # Drop None values from per-source hit dicts to match the legacy storage.
    clean_sources = {
        src: {k: v for k, v in (hit or {}).items() if v is not None}
        for src, hit in sources.items()
        if isinstance(hit, dict)
    }
    BpmCache.objects.update_or_create(
        user=user,
        cache_key=ck,
        defaults={
            "artist": artist,
            "title": title,
            "sources_tried": list(entry.get("sources_tried") or []),
            "source_hits": clean_sources,
        },
    )


# Process-local URL-share-count cache; invalidated after each cascade save.
_URL_SHARE_CACHE: dict[int, dict[tuple[str, str], int]] = {}


def url_share_counts(user) -> dict[tuple[str, str], int]:
    """How many distinct cache_keys each (source, url) pair appears under."""
    cached = _URL_SHARE_CACHE.get(user.id)
    if cached is not None:
        return cached
    counts: dict[tuple[str, str], int] = {}
    for row in BpmCache.objects.filter(user=user).only("source_hits"):
        for src, hit in (row.source_hits or {}).items():
            url = (hit or {}).get("url")
            if url:
                counts[(src, url)] = counts.get((src, url), 0) + 1
    _URL_SHARE_CACHE[user.id] = counts
    return counts


def invalidate_url_share_counts(user=None) -> None:
    if user is None:
        _URL_SHARE_CACHE.clear()
    else:
        _URL_SHARE_CACHE.pop(user.id, None)


# ─── Per-track result derivation (used by the renderer) ──────────────────────


def derive_track_result(
    user,
    discogs_release_id: int,
    position: str,
    artist: str,
    title: str,
    overrides_rp: dict,
    overrides_tk: dict,
    cache_entries: dict[str, dict] | None = None,
) -> dict:
    """Derive the per-track BPM/key result. Override > cache > nothing.

    ``cache_entries`` is an optional pre-fetched ``{cache_key: entry}`` map
    (from :func:`load_all_cache_entries`). Callers in tight loops pass it
    to avoid one DB round-trip per track.
    """
    base = {"position": position, "artist": artist, "title": title}
    override = overrides_rp.get((discogs_release_id, position))
    if override is None:
        ck = cache_key(artist, title)
        if ck in overrides_tk:
            override = overrides_tk[ck]
    if override is not None:
        return {**base, **override_to_result(override)}

    ck = cache_key(artist, title)
    if cache_entries is not None:
        entry = cache_entries.get(ck)
    else:
        entry = load_cache_entry(user, ck)
    if entry is None:
        entry = {"sources": {}, "sources_tried": []}
    return build_track_result(
        position, artist, title, entry,
        share_counts=url_share_counts(user),
    )


# ─── Coverage helpers (used by listing views) ────────────────────────────────


def bpm_coverage_for_releases(user, releases) -> dict:
    """Return ``{release.id: (tracks_total, tracks_with_bpm)}`` for the given
    Release objects. ``releases`` must already have ``tracks`` prefetched.

    Reuses :func:`derive_track_result` for parity with the legacy listing,
    but loads overrides + the full BpmCache once up front so a 500-release
    page is O(tracks) in-memory work instead of O(tracks) DB round-trips.
    """
    by_rp, by_tk, _ = load_overrides(user)
    cache_entries = load_all_cache_entries(user)
    out: dict = {}
    for rel in releases:
        total = 0
        with_bpm = 0
        release_artist = rel.artist or "V/A"
        rid = rel.discogs_release_id
        for t in rel.tracks.all():
            total += 1
            result = derive_track_result(
                user, rid,
                t.position or "",
                t.artist or release_artist,
                t.title or "",
                by_rp, by_tk,
                cache_entries=cache_entries,
            )
            if result.get("bpm"):
                with_bpm += 1
        out[rel.id] = (total, with_bpm)
    return out


# ─── Top-level orchestrator ──────────────────────────────────────────────────


def run_bpm_cascade(user, *, workers: int = 8, log: Logger = print) -> dict:
    """Cascade BPM/key lookups for every track in the user's collection.

    Returns a stats dict. Idempotent: tracks whose BPM cache already has
    every source attempted are skipped.
    """
    rl = RateLimiter()

    by_rp, by_tk, override_warnings = load_overrides(user)
    for w in override_warnings:
        log(f"  override warning: {w}")
    if by_rp or by_tk:
        log(f"Loaded {len(by_rp) + len(by_tk)} override(s) from DB.")
    used_overrides: set[tuple] = set()

    releases = list(
        Release.objects.filter(user=user, tracks__isnull=False)
        .distinct()
        .order_by("discogs_release_id")
        .values("discogs_release_id", "artist", "id")
    )
    if not releases:
        log("ERROR: no releases with tracks found. Run `sleeve-notes fetch` first.")
        return {"error": "no_releases"}

    release_ids_uuid = [r["id"] for r in releases]
    total_tracks = Track.objects.filter(release_id__in=release_ids_uuid).count()
    tracks_by_release: dict = {}
    for t in Track.objects.filter(release_id__in=release_ids_uuid).values(
        "release_id", "position", "artist", "title",
    ):
        tracks_by_release.setdefault(t["release_id"], []).append(t)

    # Pass 1: classify every track without doing network I/O.
    cached_hits = 0
    override_hits = 0
    worklist: list[dict] = []
    for release in releases:
        rid = release["discogs_release_id"]
        release_artist = release["artist"] or "V/A"
        for track in tracks_by_release.get(release["id"], []):
            artist = track["artist"] or release_artist
            title = track["title"] or ""
            position = track["position"] or ""

            override = by_rp.get((rid, position))
            override_id: tuple | None = None
            if override is not None:
                override_id = ("rp", rid, position)
            else:
                tk = cache_key(artist, title)
                if tk in by_tk:
                    override = by_tk[tk]
                    override_id = ("tk", tk)
            if override is not None:
                used_overrides.add(override_id)
                override_hits += 1
                continue

            ck = cache_key(artist, title)
            cached = load_cache_entry(user, ck)
            tried = set((cached or {}).get("sources_tried") or [])
            if cached and set(ALL_SOURCES).issubset(tried):
                cached_hits += 1
                continue

            worklist.append({
                "rid": rid, "position": position,
                "artist": artist, "title": title,
                "cache_key": ck, "cached": cached,
            })

    log(
        f"Looking up BPM/key for {total_tracks} tracks across "
        f"{len(releases)} releases "
        f"({cached_hits} fully cached, {override_hits} overridden, "
        f"{len(worklist)} to cascade with {workers} workers)..."
    )

    # Pass 2: cascade in parallel.
    hits_by_source: dict[str, int] = {s: 0 for s in ALL_SOURCES}
    done_cascade = 0

    if worklist:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(cascade_all, w["artist"], w["title"], w["cached"], rl): w
                for w in worklist
            }
            for fut in as_completed(futures):
                w = futures[fut]
                try:
                    entry = fut.result()
                except Exception as e:
                    log(f"  ERROR cascading {w['artist']} - {w['title']}: {e}")
                    continue
                save_cache_entry(user, w["cache_key"], w["artist"], w["title"], entry)
                spot_id = entry.get("spotify_track_id")
                if spot_id:
                    Track.objects.filter(
                        release__user=user,
                        release__discogs_release_id=w["rid"],
                        position=w["position"],
                        spotify_track_id="",
                    ).update(spotify_track_id=spot_id)
                done_cascade += 1
                cached_src = (w["cached"] or {}).get("sources") or {}
                for src in entry.get("sources") or {}:
                    if src not in cached_src:
                        hits_by_source[src] = hits_by_source.get(src, 0) + 1
                result = build_track_result(
                    w["position"], w["artist"], w["title"], entry
                )
                if result["bpm"]:
                    marker = {
                        "high": "●●", "octave": "●○",
                        "shared": "●≈",
                        "single": "○", "disputed": "?",
                    }.get(result["bpm_confidence"], "·")
                    key_str = (
                        f" key={result['key_camelot']}"
                        if result["key_camelot"] else ""
                    )
                    log(
                        f"  [{done_cascade}/{len(worklist)}] {marker} "
                        f"{w['artist']} - {w['title']} -> "
                        f"{result['bpm']}{key_str}  "
                        f"sources={result['bpm_sources']}"
                    )
                if done_cascade % 25 == 0 or done_cascade == len(worklist):
                    hits_str = " ".join(
                        f"{s}={hits_by_source[s]}" for s in ALL_SOURCES
                    )
                    log(f"  [{done_cascade}/{len(worklist)}] progress: {hits_str}")

    invalidate_url_share_counts(user)

    # Stats summary across all tracks.
    found_bpm = 0
    found_key = 0
    high_conf = 0
    octave_conf = 0
    shared_conf = 0
    disputed = 0
    for release in releases:
        rid = release["discogs_release_id"]
        release_artist = release["artist"] or "V/A"
        for track in tracks_by_release.get(release["id"], []):
            tr = derive_track_result(
                user, rid,
                track["position"] or "",
                track["artist"] or release_artist,
                track["title"] or "",
                by_rp, by_tk,
            )
            if tr.get("bpm"):
                found_bpm += 1
            if tr.get("key_camelot"):
                found_key += 1
            conf = tr.get("bpm_confidence")
            if conf == "high":
                high_conf += 1
            elif conf == "octave":
                octave_conf += 1
            elif conf == "shared":
                shared_conf += 1
            elif conf == "disputed":
                disputed += 1

    hits_str = ", ".join(f"{s}={hits_by_source[s]}" for s in ALL_SOURCES)
    log(
        f"Cache populated: {found_bpm}/{total_tracks} BPMs "
        f"({high_conf} multi-source consensus, {octave_conf} octave-matched, "
        f"{shared_conf} shared-URL, {disputed} disputed), "
        f"{found_key}/{total_tracks} keys."
    )
    log(
        f"  new this run by source: {hits_str}   "
        f"(cached hits: {cached_hits}, overrides: {override_hits})"
    )

    unused_rp = [k for k in by_rp if ("rp", k[0], k[1]) not in used_overrides]
    unused_tk = [k for k in by_tk if ("tk", k) not in used_overrides]
    if unused_rp or unused_tk:
        log(
            f"  WARNING: {len(unused_rp) + len(unused_tk)} override entry/entries "
            "matched nothing — check via `sleeve-notes overrides list`:"
        )
        for rid, pos in unused_rp:
            log(f"    - release_id={rid} position={pos!r}")
        for tk in unused_tk:
            e = by_tk[tk]
            log(f"    - artist={e.get('artist')!r} title={e.get('title')!r}")

    return {
        "total_tracks": total_tracks,
        "found_bpm": found_bpm,
        "found_key": found_key,
        "cached_hits": cached_hits,
        "override_hits": override_hits,
        "cascaded": done_cascade,
        "hits_by_source": hits_by_source,
    }
