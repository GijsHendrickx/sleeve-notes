"""Fetch a Discogs collection folder + per-release tracklists into .tmp/.

Two sources are supported for the release listing:
- Default: paginated Discogs collection API (needs DISCOGS_USERNAME).
- `--csv PATH`: a Discogs CSV export (Collection → Export). The CSV provides
  release_ids (and `CollectionFolder` for folder filtering); tracklists are
  still fetched from the per-release endpoint (cached in `.tmp/release_cache/`).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

try:
    from tools import project_root
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import project_root
ROOT = project_root()
TMP = ROOT / ".tmp"
RELEASE_CACHE = TMP / "release_cache"
COLLECTION_OUT = TMP / "collection.json"

API_BASE = "https://api.discogs.com"
USER_AGENT = "discogs-dj-stickers/0.1"
REQUEST_GAP_S = 1.1            # authenticated: 60 req/min
REQUEST_GAP_PUBLIC_S = 2.5     # unauthenticated: 25 req/min


class RetryableHTTPError(Exception):
    pass


def auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Discogs token={token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def public_headers() -> dict[str, str]:
    """Unauthenticated headers — works for `/releases/{id}` at 25 req/min."""
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


@retry(
    retry=retry_if_exception_type(RetryableHTTPError),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    stop=stop_after_attempt(6),
    reraise=True,
)
def _get(url: str, headers: dict[str, str], params: dict | None = None) -> dict:
    resp = requests.get(url, headers=headers, params=params, timeout=30)
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
    username: str, folder: int, page: int, headers: dict[str, str]
) -> dict:
    url = f"{API_BASE}/users/{username}/collection/folders/{folder}/releases"
    return _get(url, headers, params={"per_page": 100, "page": page})


def list_folders(username: str, headers: dict[str, str]) -> list[dict]:
    url = f"{API_BASE}/users/{username}/collection/folders"
    data = _get(url, headers)
    return data.get("folders", [])


def resolve_folder(folder_arg: str | None, username: str, headers: dict[str, str]) -> tuple[int, str]:
    """Return (folder_id, display_name). None/empty → folder 0 (All)."""
    if not folder_arg:
        return 0, "All"
    if folder_arg.isdigit():
        return int(folder_arg), f"id={folder_arg}"
    folders = list_folders(username, headers)
    needle = folder_arg.strip().lower()
    for f in folders:
        if (f.get("name") or "").strip().lower() == needle:
            return int(f["id"]), f["name"]
    names = ", ".join(repr(f.get("name")) for f in folders)
    raise SystemExit(f"Folder {folder_arg!r} not found. Available folders: {names}")


def fetch_release_detail(
    release_id: int, headers: dict[str, str], gap_s: float = REQUEST_GAP_S
) -> dict:
    cache_path = RELEASE_CACHE / f"{release_id}.json"
    if cache_path.exists():
        with cache_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    url = f"{API_BASE}/releases/{release_id}"
    detail = _get(url, headers)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
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

    load_dotenv(ROOT / ".env")
    token = os.environ.get("DISCOGS_TOKEN")
    username = os.environ.get("DISCOGS_USERNAME")

    TMP.mkdir(parents=True, exist_ok=True)
    RELEASE_CACHE.mkdir(parents=True, exist_ok=True)

    if args.csv:
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

        missing = [rid for rid in release_ids if not (RELEASE_CACHE / f"{rid}.json").exists()]
        headers: dict[str, str] = {}
        gap_s = REQUEST_GAP_S
        if missing:
            if token:
                headers = auth_headers(token)
                gap_s = REQUEST_GAP_S
                eta_min = len(missing) * gap_s / 60
                print(
                    f"  {len(missing)} releases need a tracklist fetch "
                    f"(authenticated, {gap_s:.1f}s gap, ~{eta_min:.1f} min); "
                    "rest served from cache."
                )
            else:
                headers = public_headers()
                gap_s = REQUEST_GAP_PUBLIC_S
                eta_min = len(missing) * gap_s / 60
                print(
                    f"  {len(missing)} releases need a tracklist fetch "
                    f"(unauthenticated, {gap_s:.1f}s gap, ~{eta_min:.1f} min); "
                    "rest served from cache."
                )
                print(
                    "  TIP: set DISCOGS_TOKEN in .env for ~2× faster fetches (60 req/min)."
                )
        else:
            print("  all releases already cached; no API calls needed.")

        merged: list[dict] = []
        for i, release_id in enumerate(release_ids, start=1):
            try:
                detail = fetch_release_detail(release_id, headers, gap_s)
            except Exception as e:
                print(f"  [{i}/{len(release_ids)}] release {release_id}: ERROR {e}", file=sys.stderr)
                continue
            merged.append(
                {
                    "id": release_id,
                    "basic_information": basic_from_detail(detail),
                    "tracklist": detail.get("tracklist", []),
                    "notes": detail.get("notes"),
                }
            )
            if i % 25 == 0 or i == len(release_ids):
                print(f"  [{i}/{len(release_ids)}] cached")

        with COLLECTION_OUT.open("w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        print(f"Wrote {COLLECTION_OUT} ({len(merged)} releases)")
        return 0

    if not token or not username:
        print("ERROR: set DISCOGS_TOKEN and DISCOGS_USERNAME in .env", file=sys.stderr)
        return 2

    headers = auth_headers(token)

    folder_id, folder_name = resolve_folder(args.folder, username, headers)
    print(f"Fetching collection for {username}, folder {folder_name!r} (id={folder_id})...")
    first = fetch_collection_page(username, folder_id, 1, headers)
    pages = first.get("pagination", {}).get("pages", 1)
    total = first.get("pagination", {}).get("items", 0)
    print(f"  pagination: {pages} pages, {total} items")

    basics: list[dict] = []
    basics.extend(first.get("releases", []))
    for page in range(2, pages + 1):
        time.sleep(REQUEST_GAP_S)
        data = fetch_collection_page(username, folder_id, page, headers)
        basics.extend(data.get("releases", []))
        print(f"  page {page}/{pages}: {len(basics)} releases so far")
        if args.limit and len(basics) >= args.limit:
            basics = basics[: args.limit]
            break

    if args.limit:
        basics = basics[: args.limit]
    print(f"Fetched {len(basics)} release rows; fetching tracklists...")

    merged = []
    for i, row in enumerate(basics, start=1):
        basic = row.get("basic_information", {})
        release_id = basic.get("id")
        if not release_id:
            continue
        try:
            detail = fetch_release_detail(release_id, headers)
        except Exception as e:
            print(f"  [{i}/{len(basics)}] release {release_id}: ERROR {e}", file=sys.stderr)
            continue
        merged.append(
            {
                "id": release_id,
                "basic_information": basic,
                "tracklist": detail.get("tracklist", []),
                "notes": detail.get("notes"),
            }
        )
        if i % 25 == 0 or i == len(basics):
            print(f"  [{i}/{len(basics)}] cached")

    with COLLECTION_OUT.open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(f"Wrote {COLLECTION_OUT} ({len(merged)} releases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
