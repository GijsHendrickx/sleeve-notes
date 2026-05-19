"""Normalize a Discogs release payload into our ``Release`` + ``Track`` shape.

Pure transformations only; no network, no DB. Called from
``webapp/records/services/discogs_sync.py`` right after the Discogs API
response lands, before the rows are written via the ORM.
"""

from __future__ import annotations

import re


SIDE_RE = re.compile(r"^([A-Z]+)")
# Discogs format descriptions encode RPM as "45 RPM" / "33 ⅓ RPM".
RPM_RE = re.compile(r"(\d{2,3}(?:\s*[⅓⅔½])?)\s*RPM", re.IGNORECASE)


def parse_duration_to_seconds(duration: str) -> int | None:
    if not duration:
        return None
    parts = duration.strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def derive_side(position: str) -> str | None:
    if not position:
        return None
    m = SIDE_RE.match(position.strip())
    return m.group(1) if m else None


def join_artists(artists: list[dict]) -> str:
    names: list[str] = []
    for a in artists or []:
        name = (a.get("name") or "").strip()
        if not name:
            continue
        clean = re.sub(r"\s*\(\d+\)$", "", name)
        join = (a.get("join") or "").strip()
        names.append(clean)
        if join and join not in (",",):
            names.append(join)
    out = " ".join(names).strip()
    out = re.sub(r"\s+,", ",", out)
    return re.sub(r"\s{2,}", " ", out)


def is_real_track(t: dict) -> bool:
    type_ = (t.get("type_") or "track").lower()
    return type_ == "track" and bool(t.get("position"))


def transform_tracks(tracklist: list[dict], release_artist: str) -> list[dict]:
    """Flatten a Discogs tracklist into the row shape stored in ``tracks``."""
    out: list[dict] = []
    for t in tracklist or []:
        if not is_real_track(t):
            continue
        position = (t.get("position") or "").strip()
        title = (t.get("title") or "").strip()
        duration = (t.get("duration") or "").strip()
        track_artists = t.get("artists") or []
        track_artist = join_artists(track_artists) if track_artists else release_artist
        out.append(
            {
                "position": position,
                "side": derive_side(position),
                "artist": track_artist or release_artist,
                "title": title,
                "duration": duration,
                "duration_s": parse_duration_to_seconds(duration),
            }
        )
    return out


def extract_rpms(formats: list[dict]) -> list[str]:
    """Pull unique RPM values from format descriptions, normalized for display."""
    found: list[str] = []
    for f in formats or []:
        for d in f.get("descriptions") or []:
            m = RPM_RE.search(d)
            if not m:
                continue
            normalized = re.sub(r"\s+", "", m.group(1))
            if normalized not in found:
                found.append(normalized)
    return found


