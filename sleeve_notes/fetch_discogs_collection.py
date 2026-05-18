"""Fetch a Discogs collection folder + per-release tracklists into the DB.

Two sources are supported for the release listing:
- Default: paginated Discogs collection API (needs DISCOGS_USERNAME).
- ``--csv PATH``: a Discogs CSV export (Collection → Export). The CSV provides
  release_ids (and ``CollectionFolder`` for folder filtering); tracklists are
  still fetched from the per-release endpoint.

Per-release detail responses are written to the ``releases`` table, which
doubles as the cache: rows with ``raw_tracklist`` already populated are
skipped on subsequent runs.

Authenticates via Discogs OAuth 1.0a — every request is HMAC-SHA1 signed
with the app's consumer credentials (``DISCOGS_CONSUMER_KEY`` /
``..._SECRET``) plus the per-user token (``DISCOGS_OAUTH_TOKEN`` /
``..._SECRET``). The user_id and username come from
``DISCOGS_USER_ID`` / ``DISCOGS_USERNAME``. All four env vars are
injected by the web ``JobRunner``; for standalone CLI use, set them in
``.env`` after logging in via the web UI.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from requests_oauthlib import OAuth1Session
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

try:
    from sleeve_notes import project_root
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sleeve_notes import project_root

from sleeve_notes import db as dbmod
from sleeve_notes.ingest import normalize_release

ROOT = project_root()

API_BASE = "https://api.discogs.com"
USER_AGENT = "sleeve-notes/0.3 (+https://github.com/GijsHendrickx/sleeve-notes)"
REQUEST_GAP_S = 1.1  # authenticated: 60 req/min


class RetryableHTTPError(Exception):
    pass


def make_oauth_session(token: str, token_secret: str) -> OAuth1Session:
    """Build a signed session that talks to the Discogs API on behalf of a
    specific user. Every request gets the User-Agent and Accept headers."""
    consumer_key = os.environ.get("DISCOGS_CONSUMER_KEY")
    consumer_secret = os.environ.get("DISCOGS_CONSUMER_SECRET")
    if not consumer_key or not consumer_secret:
        raise SystemExit(
            "ERROR: set DISCOGS_CONSUMER_KEY and DISCOGS_CONSUMER_SECRET in .env"
        )
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


def fetch_collection_page(
    session: OAuth1Session, username: str, folder: int, page: int
) -> dict:
    url = f"{API_BASE}/users/{username}/collection/folders/{folder}/releases"
    return _get(session, url, params={"per_page": 100, "page": page})


def list_folders(session: OAuth1Session, username: str) -> list[dict]:
    url = f"{API_BASE}/users/{username}/collection/folders"
    data = _get(session, url)
    return data.get("folders", [])


def resolve_folder(
    folder_arg: str | None, session: OAuth1Session, username: str
) -> tuple[int, str]:
    """Return (folder_id, display_name). None/empty → folder 0 (All)."""
    if not folder_arg:
        return 0, "All"
    if folder_arg.isdigit():
        return int(folder_arg), f"id={folder_arg}"
    folders = list_folders(session, username)
    needle = folder_arg.strip().lower()
    for f in folders:
        if (f.get("name") or "").strip().lower() == needle:
            return int(f["id"]), f["name"]
    names = ", ".join(repr(f.get("name")) for f in folders)
    raise SystemExit(f"Folder {folder_arg!r} not found. Available folders: {names}")


def is_cached(conn, user_id: int, release_id: int) -> bool:
    """A release counts as cached once we've stored its tracklist for that user."""
    row = conn.execute(
        "SELECT 1 FROM releases "
        "WHERE user_id = ? AND id = ? AND raw_tracklist IS NOT NULL",
        (user_id, release_id),
    ).fetchone()
    return row is not None


def upsert_release_detail(
    conn,
    user_id: int,
    release_id: int,
    basic: dict,
    tracklist: list,
    notes: str | None,
) -> None:
    """Persist a release's raw API payload + normalized fields + tracks. Idempotent."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO releases (user_id, id, basic_information, raw_tracklist, notes, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id, id) DO UPDATE SET
            basic_information = excluded.basic_information,
            raw_tracklist     = excluded.raw_tracklist,
            notes             = excluded.notes,
            fetched_at        = excluded.fetched_at
        """,
        (
            user_id,
            release_id,
            json.dumps(basic or {}, ensure_ascii=False),
            json.dumps(tracklist or [], ensure_ascii=False),
            notes,
            now,
        ),
    )
    normalize_release(conn, user_id, release_id, basic or {}, tracklist or [])


def fetch_release_detail(
    session: OAuth1Session, release_id: int, gap_s: float = REQUEST_GAP_S
) -> dict:
    """Fetch a release detail from the Discogs API. Caller checks the DB cache."""
    url = f"{API_BASE}/releases/{release_id}"
    detail = _get(session, url)
    time.sleep(gap_s)
    return detail


def load_release_ids_from_csv(
    csv_path: Path, folder_filter: str | None
) -> list[int]:
    """Read release_ids from a Discogs CSV export, optionally filtered by folder.

    `folder_filter` is matched case-insensitively against the `CollectionFolder`
    column. Pass None (or the literal "all"/"0") to keep every row.
    """
    keep_all = folder_filter is None or folder_filter.strip().lower() in {"all", "0", ""}
    needle = None if keep_all else folder_filter.strip().lower()

    ids: list[int] = []
    seen: set[int] = set()
    available_folders: set[str] = set()
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if "release_id" not in (reader.fieldnames or []):
            raise SystemExit(
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
        raise SystemExit(
            f"Folder {folder_filter!r} not found in {csv_path}. "
            f"Available folders: {names}"
        )
    return ids


def basic_from_detail(detail: dict) -> dict:
    """Shape a release-detail JSON to match the collection-API basic_information."""
    return {
        "id": detail.get("id"),
        "master_id": detail.get("master_id"),
        "master_url": detail.get("master_url"),
        "resource_url": detail.get("resource_url"),
        "thumb": detail.get("thumb"),
        "cover_image": (detail.get("images") or [{}])[0].get("uri") if detail.get("images") else None,
        "title": detail.get("title"),
        "year": detail.get("year"),
        "formats": detail.get("formats") or [],
        "labels": detail.get("labels") or [],
        "artists": detail.get("artists") or [],
        "genres": detail.get("genres") or [],
        "styles": detail.get("styles") or [],
    }


def _require_oauth_env() -> tuple[int, str, str, str]:
    """Read the four per-user env vars set by the web JobRunner (or by the
    developer in .env for standalone CLI use). Bails with a clear error if
    any are missing."""
    load_dotenv(ROOT / ".env")
    uid_raw = os.environ.get("DISCOGS_USER_ID")
    username = os.environ.get("DISCOGS_USERNAME")
    token = os.environ.get("DISCOGS_OAUTH_TOKEN")
    token_secret = os.environ.get("DISCOGS_OAUTH_TOKEN_SECRET")
    if not (uid_raw and username and token and token_secret):
        raise SystemExit(
            "ERROR: DISCOGS_USER_ID, DISCOGS_USERNAME, DISCOGS_OAUTH_TOKEN and "
            "DISCOGS_OAUTH_TOKEN_SECRET must all be set. Log in via the web UI "
            "to populate them automatically, or extract them from your session "
            "for CLI use."
        )
    try:
        uid = int(uid_raw)
    except ValueError:
        raise SystemExit(f"ERROR: DISCOGS_USER_ID must be an integer, got {uid_raw!r}")
    return uid, username, token, token_secret


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--folder",
        type=str,
        default=None,
        help="Discogs folder name or id. Default: whole collection (folder 'All', id 0).",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=None,
        help="Path to a Discogs CSV export. When set, release_ids come from the "
        "CSV and the collection-listing API is skipped; per-release tracklists "
        "are still fetched (cache-aware). With --folder, filters on the CSV's "
        "CollectionFolder column.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after N releases")
    args = parser.parse_args(argv)

    user_id, username, token, token_secret = _require_oauth_env()
    session = make_oauth_session(token, token_secret)

    with dbmod.session() as conn:
        if args.csv:
            return _run_csv_path(conn, args, session, user_id)
        return _run_api_path(conn, args, session, user_id, username)


def _run_csv_path(conn, args, session: OAuth1Session, user_id: int) -> int:
    csv_path = Path(args.csv).expanduser()
    if not csv_path.is_absolute():
        csv_path = (ROOT / csv_path).resolve()
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}", file=sys.stderr)
        return 2
    if args.folder and args.folder.isdigit():
        print(
            "ERROR: --csv expects a folder name (the CSV has no folder ids); "
            f"got {args.folder!r}.",
            file=sys.stderr,
        )
        return 2

    release_ids = load_release_ids_from_csv(csv_path, args.folder)
    if args.limit:
        release_ids = release_ids[: args.limit]
    folder_label = args.folder if args.folder else "All"
    print(
        f"Loaded {len(release_ids)} release ids from {csv_path.name} "
        f"(folder {folder_label!r})"
    )

    missing = [rid for rid in release_ids if not is_cached(conn, user_id, rid)]
    if missing:
        eta_min = len(missing) * REQUEST_GAP_S / 60
        print(
            f"  {len(missing)} releases need a tracklist fetch "
            f"({REQUEST_GAP_S:.1f}s gap, ~{eta_min:.1f} min); rest served from cache."
        )
    else:
        print("  all releases already cached; no API calls needed.")

    fetched = 0
    for i, rid in enumerate(release_ids, start=1):
        if is_cached(conn, user_id, rid):
            continue
        try:
            detail = fetch_release_detail(session, rid)
        except Exception as e:
            print(f"  [{i}/{len(release_ids)}] release {rid}: ERROR {e}", file=sys.stderr)
            continue
        upsert_release_detail(
            conn,
            user_id,
            rid,
            basic_from_detail(detail),
            detail.get("tracklist") or [],
            detail.get("notes"),
        )
        fetched += 1
        if fetched % 25 == 0:
            conn.commit()
            print(f"  [{i}/{len(release_ids)}] cached (fetched {fetched} this run)")
    conn.commit()

    total_cached = conn.execute(
        "SELECT COUNT(*) AS n FROM releases "
        "WHERE user_id = ? AND raw_tracklist IS NOT NULL",
        (user_id,),
    ).fetchone()["n"]
    print(
        f"Stored {len(release_ids)} releases in {dbmod.db_path().name} "
        f"(fetched {fetched} this run, total cached: {total_cached})."
    )
    return 0


def _run_api_path(
    conn, args, session: OAuth1Session, user_id: int, username: str
) -> int:
    folder_id, folder_name = resolve_folder(args.folder, session, username)
    print(f"Fetching collection for {username}, folder {folder_name!r} (id={folder_id})...")
    first = fetch_collection_page(session, username, folder_id, 1)
    pages = first.get("pagination", {}).get("pages", 1)
    total = first.get("pagination", {}).get("items", 0)
    print(f"  pagination: {pages} pages, {total} items")

    basics: list[dict] = []
    basics.extend(first.get("releases", []))
    for page in range(2, pages + 1):
        time.sleep(REQUEST_GAP_S)
        data = fetch_collection_page(session, username, folder_id, page)
        basics.extend(data.get("releases", []))
        print(f"  page {page}/{pages}: {len(basics)} releases so far")
        if args.limit and len(basics) >= args.limit:
            basics = basics[: args.limit]
            break

    if args.limit:
        basics = basics[: args.limit]
    print(f"Fetched {len(basics)} release rows; fetching tracklists...")

    fetched = 0
    for i, row in enumerate(basics, start=1):
        basic = row.get("basic_information", {})
        release_id = basic.get("id")
        if not release_id:
            continue
        if is_cached(conn, user_id, release_id):
            continue
        try:
            detail = fetch_release_detail(session, release_id)
        except Exception as e:
            print(f"  [{i}/{len(basics)}] release {release_id}: ERROR {e}", file=sys.stderr)
            continue
        upsert_release_detail(
            conn,
            user_id,
            release_id,
            basic,
            detail.get("tracklist") or [],
            detail.get("notes"),
        )
        fetched += 1
        if fetched % 25 == 0:
            conn.commit()
            print(f"  [{i}/{len(basics)}] cached (fetched {fetched} this run)")
    conn.commit()

    total_cached = conn.execute(
        "SELECT COUNT(*) AS n FROM releases "
        "WHERE user_id = ? AND raw_tracklist IS NOT NULL",
        (user_id,),
    ).fetchone()["n"]
    print(
        f"Stored {len(basics)} releases in {dbmod.db_path().name} "
        f"(fetched {fetched} this run, total cached: {total_cached})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
