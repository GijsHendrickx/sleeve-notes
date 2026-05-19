"""Fetch a Discogs collection into the Release + Track tables.

ORM-port of sleeve_notes/fetch_discogs_collection.py. The pure helpers
(join_artists, transform_tracks, extract_rpms, classify_from_basic) still
live in the engine package and are imported as-is — only the DB layer is
new.

Callable from anywhere with a User + OAuth tokens:
- the `manage.py sync_discogs` command (CLI / cron)
- the future Django-Q2 task (web-triggered sync)
"""
from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Callable

from django.db import transaction
from django.utils import timezone
from requests_oauthlib import OAuth1Session
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from sleeve_notes.classify import classify_from_basic
from sleeve_notes.ingest import (
    extract_rpms,
    join_artists,
    transform_tracks,
)

from records.models import Release, Track


API_BASE = "https://api.discogs.com"
USER_AGENT = "sleeve-notes/0.4 +https://github.com/GijsHendrickx/sleeve-notes"
REQUEST_GAP_S = 1.1  # authenticated rate limit is 60/min


Logger = Callable[[str], None]


class RetryableHTTPError(Exception):
    pass


# ─── HTTP layer ──────────────────────────────────────────────────────────────


def make_oauth_session(
    consumer_key: str, consumer_secret: str, token: str, token_secret: str,
) -> OAuth1Session:
    sess = OAuth1Session(
        consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=token,
        resource_owner_secret=token_secret,
    )
    sess.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return sess


@retry(
    retry=retry_if_exception_type(RetryableHTTPError),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    stop=stop_after_attempt(6),
    reraise=True,
)
def _get(session: OAuth1Session, url: str, params: dict | None = None) -> dict:
    resp = session.get(url, params=params, timeout=30)
    if resp.status_code == 429 or resp.status_code >= 500:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                time.sleep(float(retry_after))
            except ValueError:
                pass
        raise RetryableHTTPError(f"{resp.status_code} on {url}")
    resp.raise_for_status()
    return resp.json()


def list_folders(session: OAuth1Session, username: str) -> list[dict]:
    data = _get(session, f"{API_BASE}/users/{username}/collection/folders")
    return data.get("folders", [])


def resolve_folder(
    folder_arg: str | None, session: OAuth1Session, username: str,
) -> tuple[int, str]:
    if not folder_arg:
        return 0, "All"
    if folder_arg.isdigit():
        return int(folder_arg), f"id={folder_arg}"
    needle = folder_arg.strip().lower()
    for f in list_folders(session, username):
        if (f.get("name") or "").strip().lower() == needle:
            return int(f["id"]), f["name"]
    raise ValueError(f"Folder {folder_arg!r} not found")


def fetch_collection_page(
    session: OAuth1Session, username: str, folder: int, page: int,
) -> dict:
    url = f"{API_BASE}/users/{username}/collection/folders/{folder}/releases"
    return _get(session, url, params={"per_page": 100, "page": page})


def fetch_release_detail(
    session: OAuth1Session, release_id: int, gap_s: float = REQUEST_GAP_S,
) -> dict:
    detail = _get(session, f"{API_BASE}/releases/{release_id}")
    time.sleep(gap_s)
    return detail


def basic_from_detail(detail: dict) -> dict:
    """Shape a release-detail JSON to match the collection-API basic_information."""
    images = detail.get("images") or []
    return {
        "id": detail.get("id"),
        "master_id": detail.get("master_id"),
        "master_url": detail.get("master_url"),
        "resource_url": detail.get("resource_url"),
        "thumb": detail.get("thumb"),
        "cover_image": images[0].get("uri") if images else None,
        "title": detail.get("title"),
        "year": detail.get("year"),
        "formats": detail.get("formats") or [],
        "labels": detail.get("labels") or [],
        "artists": detail.get("artists") or [],
        "genres": detail.get("genres") or [],
        "styles": detail.get("styles") or [],
    }


# ─── CSV path ────────────────────────────────────────────────────────────────


def load_release_ids_from_csv(
    csv_path: Path, folder_filter: str | None,
) -> list[int]:
    keep_all = folder_filter is None or folder_filter.strip().lower() in {
        "all", "0", "",
    }
    needle = None if keep_all else folder_filter.strip().lower()

    ids: list[int] = []
    seen: set[int] = set()
    available_folders: set[str] = set()
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if "release_id" not in (reader.fieldnames or []):
            raise ValueError(
                f"CSV {csv_path} has no 'release_id' column. "
                f"Columns: {reader.fieldnames}"
            )
        for row in reader:
            folder = (row.get("CollectionFolder") or "").strip()
            available_folders.add(folder)
            if needle is not None and folder.lower() != needle:
                continue
            rid_raw = (row.get("release_id") or "").strip()
            if not rid_raw.isdigit():
                continue
            rid = int(rid_raw)
            if rid in seen:
                continue
            seen.add(rid)
            ids.append(rid)

    if needle is not None and not ids:
        names = ", ".join(sorted(repr(n) for n in available_folders if n))
        raise ValueError(
            f"Folder {folder_filter!r} not in CSV. Available: {names}"
        )
    return ids


# ─── ORM writes ──────────────────────────────────────────────────────────────


def is_cached_release(user, discogs_release_id: int) -> bool:
    return Release.objects.filter(
        user=user,
        discogs_release_id=discogs_release_id,
        raw_tracklist__isnull=False,
    ).exists()


@transaction.atomic
def upsert_release_detail(
    user, discogs_release_id: int, basic: dict, tracklist: list, notes: str | None,
):
    """Insert/update a Release + its Tracks. Idempotent.

    Mirrors sleeve_notes/ingest.normalize_release: the same normalization
    pipeline runs, only the DB layer is Django ORM instead of raw sqlite3.
    """
    release_artists = basic.get("artists") or []
    release_artist = join_artists(release_artists)
    compilation = any(
        (a.get("name") or "").strip().lower() == "various"
        for a in release_artists
    )
    title = (basic.get("title") or "").strip()
    formats = basic.get("formats") or []
    track_dicts = transform_tracks(tracklist, release_artist or "V/A")
    release_type, fmt = classify_from_basic(basic, len(track_dicts) or None)

    release, _ = Release.objects.update_or_create(
        user=user,
        discogs_release_id=discogs_release_id,
        defaults={
            "artist": release_artist or "V/A",
            "title": title,
            "year": basic.get("year"),
            "compilation": compilation,
            "labels": [(l.get("name") or "").strip() for l in basic.get("labels") or []],
            "genres": basic.get("genres") or [],
            "styles": basic.get("styles") or [],
            "rpm": extract_rpms(formats),
            "notes": notes or "",
            "basic_information": basic or {},
            "raw_tracklist": tracklist or [],
            "release_type": release_type or "",
            "format": fmt or "",
            "thumb_url": basic.get("thumb") or basic.get("cover_image") or "",
            "fetched_at": timezone.now(),
        },
    )

    # Idempotent track refresh: drop old rows then insert fresh ones.
    release.tracks.all().delete()
    Track.objects.bulk_create([
        Track(
            release=release,
            position=t["position"],
            side=t["side"] or "",
            artist=t["artist"] or "",
            title=t["title"],
            duration=t["duration"] or "",
            duration_s=t["duration_s"],
        )
        for t in track_dicts
    ])
    return release


# ─── Top-level sync flows ────────────────────────────────────────────────────


def _noop_log(_msg: str) -> None:
    pass


def sync_via_csv(
    *,
    user,
    session: OAuth1Session,
    csv_path: Path,
    folder_filter: str | None = None,
    limit: int | None = None,
    log: Logger = print,
) -> dict:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    if folder_filter and folder_filter.isdigit():
        raise ValueError(
            "--csv expects a folder name (the CSV has no folder ids); "
            f"got {folder_filter!r}."
        )

    release_ids = load_release_ids_from_csv(csv_path, folder_filter)
    if limit:
        release_ids = release_ids[:limit]
    folder_label = folder_filter or "All"
    log(
        f"Loaded {len(release_ids)} release ids from {csv_path.name} "
        f"(folder {folder_label!r})"
    )

    return _fetch_each_detail(user, session, release_ids, log=log)


def sync_via_api(
    *,
    user,
    session: OAuth1Session,
    username: str,
    folder: str | None = None,
    limit: int | None = None,
    log: Logger = print,
) -> dict:
    folder_id, folder_name = resolve_folder(folder, session, username)
    log(f"Fetching collection for {username}, folder {folder_name!r} (id={folder_id})...")
    first = fetch_collection_page(session, username, folder_id, 1)
    pages = first.get("pagination", {}).get("pages", 1)
    total = first.get("pagination", {}).get("items", 0)
    log(f"  pagination: {pages} pages, {total} items")

    basics: list[dict] = list(first.get("releases", []))
    for page in range(2, pages + 1):
        time.sleep(REQUEST_GAP_S)
        data = fetch_collection_page(session, username, folder_id, page)
        basics.extend(data.get("releases", []))
        log(f"  page {page}/{pages}: {len(basics)} releases so far")
        if limit and len(basics) >= limit:
            basics = basics[:limit]
            break
    if limit:
        basics = basics[:limit]
    log(f"Fetched {len(basics)} release rows; fetching tracklists...")

    release_basics = [
        (b.get("basic_information", {}).get("id"), b.get("basic_information", {}))
        for b in basics
        if b.get("basic_information", {}).get("id")
    ]
    return _fetch_each_detail(
        user, session,
        [rid for rid, _ in release_basics],
        log=log,
        basics_override={rid: basic for rid, basic in release_basics},
    )


def _fetch_each_detail(
    user, session: OAuth1Session, release_ids: list[int],
    *, log: Logger = print, basics_override: dict[int, dict] | None = None,
) -> dict:
    missing = [rid for rid in release_ids if not is_cached_release(user, rid)]
    if missing:
        eta_min = len(missing) * REQUEST_GAP_S / 60
        log(
            f"  {len(missing)} releases need a tracklist fetch "
            f"({REQUEST_GAP_S:.1f}s gap, ~{eta_min:.1f} min); rest served from cache."
        )
    else:
        log("  all releases already cached; no API calls needed.")

    fetched = 0
    for i, rid in enumerate(release_ids, start=1):
        if is_cached_release(user, rid):
            continue
        try:
            detail = fetch_release_detail(session, rid)
        except Exception as e:
            log(f"  [{i}/{len(release_ids)}] release {rid}: ERROR {e}")
            continue
        basic = (basics_override or {}).get(rid) or basic_from_detail(detail)
        upsert_release_detail(
            user, rid, basic, detail.get("tracklist") or [], detail.get("notes"),
        )
        fetched += 1
        if fetched % 25 == 0:
            log(f"  [{i}/{len(release_ids)}] cached (fetched {fetched} this run)")

    total_cached = Release.objects.filter(
        user=user, raw_tracklist__isnull=False,
    ).count()
    log(
        f"Stored {len(release_ids)} releases (fetched {fetched} this run, "
        f"total cached: {total_cached})."
    )
    return {
        "release_ids": release_ids,
        "fetched_this_run": fetched,
        "total_cached": total_cached,
    }
