"""YouTube Data API v3 — top-hit video resolver for the listen icon.

Called lazily from the web layer on the first click of a track that has
neither a cached Spotify track ID nor a cached YouTube video ID. Returns
the videoId of the top ``search.list`` match so the route can 302 to
``youtube.com/watch?v=<id>`` and the caller can persist the ID for
future clicks.

A single ``search.list`` call costs 100 quota units; the free tier is
10k units/day. We catch everything broadly and return ``None`` on
failure so the route can soft-fall-back to a YouTube search URL.
"""

from __future__ import annotations

import os

import requests


_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


def resolve_video_id(artist: str, title: str) -> str | None:
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        return None
    q = f"{artist} {title}".strip()
    if not q:
        return None
    try:
        resp = requests.get(
            _SEARCH_URL,
            params={
                "q": q,
                "type": "video",
                "maxResults": 1,
                "part": "id",
                "key": api_key,
            },
            timeout=10,
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        items = resp.json().get("items") or []
    except ValueError:
        return None
    if not items:
        return None
    vid = items[0].get("id") or {}
    return vid.get("videoId") or None
