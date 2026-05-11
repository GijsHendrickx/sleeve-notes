"""Fetch a Discogs collection folder + per-release tracklists into .tmp/."""

from __future__ import annotations

import argparse
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

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"
RELEASE_CACHE = TMP / "release_cache"
COLLECTION_OUT = TMP / "collection.json"

API_BASE = "https://api.discogs.com"
USER_AGENT = "discogs-dj-stickers/0.1"
REQUEST_GAP_S = 1.1


class RetryableHTTPError(Exception):
    pass


def auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Discogs token={token}",
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


def fetch_release_detail(release_id: int, headers: dict[str, str]) -> dict:
    cache_path = RELEASE_CACHE / f"{release_id}.json"
    if cache_path.exists():
        with cache_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    url = f"{API_BASE}/releases/{release_id}"
    detail = _get(url, headers)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
    time.sleep(REQUEST_GAP_S)
    return detail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--folder",
        type=str,
        default=None,
        help="Discogs folder name or id. Default: hele collectie (folder 'All', id 0).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after N releases")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    token = os.environ.get("DISCOGS_TOKEN")
    username = os.environ.get("DISCOGS_USERNAME")
    if not token or not username:
        print("ERROR: set DISCOGS_TOKEN and DISCOGS_USERNAME in .env", file=sys.stderr)
        return 2

    TMP.mkdir(parents=True, exist_ok=True)
    RELEASE_CACHE.mkdir(parents=True, exist_ok=True)
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

    merged: list[dict] = []
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
