"""Normalize a Discogs release payload into our ``releases`` + ``tracks`` shape.

Pure transformations + DB writes; no network. Called from ``fetch`` immediately
after the API response is stored, and from the connect-time backfill in
:mod:`sleeve_notes.db` for legacy rows that pre-date this code.
"""

from __future__ import annotations

import json
import re
import sqlite3

from sleeve_notes.classify import classify_from_basic


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


def normalize_release(
    conn: sqlite3.Connection,
    release_id: int,
    basic: dict,
    tracklist: list[dict],
) -> None:
    """Extract release fields, classify, and (re)write the tracks rows.

    Idempotent: existing track rows for the release are replaced. Raw
    ``basic_information`` / ``raw_tracklist`` must already be in place
    (the caller's INSERT runs first).
    """
    release_artists = basic.get("artists") or []
    release_artist = join_artists(release_artists)
    compilation = any(
        (a.get("name") or "").strip().lower() == "various"
        for a in release_artists
    )
    title = (basic.get("title") or "").strip()
    formats = basic.get("formats") or []
    tracks = transform_tracks(tracklist, release_artist or "V/A")
    type_, fmt = classify_from_basic(basic, len(tracks) or None)

    conn.execute(
        """
        UPDATE releases SET
            artist = ?,
            title = ?,
            year = ?,
            compilation = ?,
            labels = ?,
            genres = ?,
            styles = ?,
            rpm = ?,
            type = ?,
            format = ?
        WHERE id = ?
        """,
        (
            release_artist or "V/A",
            title,
            basic.get("year"),
            1 if compilation else 0,
            json.dumps(
                [(l.get("name") or "").strip() for l in basic.get("labels") or []],
                ensure_ascii=False,
            ),
            json.dumps(basic.get("genres") or [], ensure_ascii=False),
            json.dumps(basic.get("styles") or [], ensure_ascii=False),
            json.dumps(extract_rpms(formats), ensure_ascii=False),
            type_,
            fmt,
            release_id,
        ),
    )

    conn.execute("DELETE FROM tracks WHERE release_id = ?", (release_id,))
    for t in tracks:
        conn.execute(
            """
            INSERT INTO tracks (release_id, position, side, artist, title, duration, duration_s)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                release_id,
                t["position"],
                t["side"],
                t["artist"],
                t["title"],
                t["duration"],
                t["duration_s"],
            ),
        )
