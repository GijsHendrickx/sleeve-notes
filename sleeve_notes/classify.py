"""Derive ``type`` (musical category) and ``format`` (physical medium) for a release.

Pure functions over a Discogs ``basic_information`` dict — no DB, no network.
Called from ``sleeve_notes.ingest.normalize_release`` (fresh fetches) and from
the connect-time backfill in ``sleeve_notes.db`` (legacy rows).

Taxonomies:
    type   = Album | EP | Single | Compilation | Other
    format = 12" | 10" | 7" | Other  (non-vinyl falls into Other)
"""

from __future__ import annotations


TYPES = ("Album", "EP", "Single", "Compilation", "Other")
FORMATS = ('12"', '10"', '7"', "Other")


def _all_descriptions(formats: list[dict]) -> list[str]:
    out: list[str] = []
    for f in formats or []:
        for d in f.get("descriptions") or []:
            out.append(d.strip())
    return out


def _any_vinyl(formats: list[dict]) -> bool:
    return any((f.get("name") or "").strip().lower() == "vinyl" for f in formats or [])


def classify_format(formats: list[dict]) -> str:
    """Bucket a release into a vinyl size, else ``Other``.

    Discogs encodes size in the format ``descriptions`` array (e.g. ``"12\""``).
    Multi-format bundles take the largest vinyl size present; a release with no
    vinyl entries at all (CD, digital, cassette, …) returns ``Other``.
    """
    if not _any_vinyl(formats):
        return "Other"
    descs = _all_descriptions(formats)
    if '12"' in descs or "LP" in descs:
        return '12"'
    if '10"' in descs:
        return '10"'
    if '7"' in descs:
        return '7"'
    return "Other"


def classify_type(basic: dict, track_count: int | None = None) -> str:
    """Bucket a release into a musical category.

    Precedence: Compilation (Various-artists flag) → explicit Discogs description
    (EP / Single / Album, more specific labels win) → track-count fallback for
    releases where Discogs only tagged the format size. Many DJ 12-inch records
    omit Album/EP/Single tags entirely, so track count is the only signal.

    Thresholds (when track_count is provided):
        1-3 tracks → Single,  4-6 → EP,  7+ → Album.
    """
    release_artists = basic.get("artists") or []
    if any((a.get("name") or "").strip().lower() == "various" for a in release_artists):
        return "Compilation"
    descs = _all_descriptions(basic.get("formats") or [])
    desc_set = {d for d in descs}
    if "EP" in desc_set or "Mini-Album" in desc_set:
        return "EP"
    if "Single" in desc_set or "Maxi-Single" in desc_set:
        return "Single"
    if "Album" in desc_set or "LP" in desc_set:
        return "Album"
    if track_count is not None and track_count > 0:
        if track_count <= 3:
            return "Single"
        if track_count <= 6:
            return "EP"
        return "Album"
    return "Other"


def classify_from_basic(basic: dict, track_count: int | None = None) -> tuple[str, str]:
    """Return ``(type, format)`` for a Discogs ``basic_information`` dict."""
    return classify_type(basic, track_count), classify_format(basic.get("formats") or [])
