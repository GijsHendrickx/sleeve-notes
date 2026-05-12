"""Filter Discogs releases down to DJ-ready 12" electronic vinyl.

Reads from the ``releases`` table (rows with ``raw_tracklist`` populated by
``fetch_discogs_collection``), applies the format/tracklist filter, and
writes back:
  - The extracted columns (``artist``, ``title``, ``rpm``, ...).
  - ``is_dj_release`` (1 or 0) plus ``skip_reasons`` for rejected releases.
  - Normalized rows in the ``tracks`` table (kept releases only).

Pure transformation, no network. Idempotent.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from tools import project_root
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import project_root

from tools import db as dbmod

ROOT = project_root()

VINYL_DESCRIPTIONS = {'12"', "LP"}

# Discogs format descriptions encode RPM as "45 RPM" / "33 ⅓ RPM". Extract
# the numeric prefix (with the unicode fraction if present) for display on
# the sticker — wrong-RPM playback is the failure mode this catches.
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


SIDE_RE = re.compile(r"^([A-Z]+)")


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


def check_format(formats: list[dict]) -> bool:
    """True for 12" / LP vinyl. Filters out 7", 10", CD, cassette, digital."""
    desc_set: set[str] = set()
    for f in formats:
        for d in f.get("descriptions") or []:
            desc_set.add(d)
    return bool(desc_set & VINYL_DESCRIPTIONS)


def extract_rpms(formats: list[dict]) -> list[str]:
    """Pull unique RPM values from format descriptions, normalized for display.

    Returns e.g. ["33⅓"], ["45"], ["33⅓", "45"] for the rare mixed-RPM release,
    or [] when Discogs doesn't list an RPM (the sticker simply omits it).
    """
    found: list[str] = []
    for f in formats:
        for d in f.get("descriptions") or []:
            m = RPM_RE.search(d)
            if not m:
                continue
            normalized = re.sub(r"\s+", "", m.group(1))
            if normalized not in found:
                found.append(normalized)
    return found


def is_real_track(t: dict) -> bool:
    type_ = (t.get("type_") or "track").lower()
    return type_ == "track" and bool(t.get("position"))


def transform_tracks(tracklist: list[dict], release_artist: str) -> list[dict]:
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


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with dbmod.session() as conn:
        rows = conn.execute(
            "SELECT id, basic_information, raw_tracklist FROM releases "
            "WHERE raw_tracklist IS NOT NULL"
        ).fetchall()

        if not rows:
            print(
                "ERROR: no fetched releases found. Run `bpm-stickers fetch` first.",
                file=sys.stderr,
            )
            return 2

        kept = 0
        skipped = 0

        for row in rows:
            rid = row["id"]
            try:
                basic = json.loads(row["basic_information"] or "{}")
            except json.JSONDecodeError:
                basic = {}
            try:
                tracklist = json.loads(row["raw_tracklist"] or "[]")
            except json.JSONDecodeError:
                tracklist = []

            formats = basic.get("formats") or []
            genres = basic.get("genres") or []
            title = (basic.get("title") or "").strip()
            release_artists = basic.get("artists") or []
            release_artist = join_artists(release_artists)
            compilation = any(
                (a.get("name") or "").strip().lower() == "various"
                for a in release_artists
            )

            reasons: list[str] = []
            if not check_format(formats):
                reasons.append("not_12inch_or_lp")
            if not tracklist:
                reasons.append("no_tracklist")

            if reasons:
                conn.execute(
                    """
                    UPDATE releases SET
                        artist = ?,
                        title = ?,
                        is_dj_release = 0,
                        skip_reasons = ?,
                        filtered_at = ?
                    WHERE id = ?
                    """,
                    (
                        release_artist or "V/A",
                        title,
                        json.dumps(reasons, ensure_ascii=False),
                        now,
                        rid,
                    ),
                )
                conn.execute("DELETE FROM tracks WHERE release_id = ?", (rid,))
                skipped += 1
                continue

            tracks = transform_tracks(tracklist, release_artist or "V/A")
            if not tracks:
                conn.execute(
                    """
                    UPDATE releases SET
                        artist = ?, title = ?,
                        is_dj_release = 0,
                        skip_reasons = ?,
                        filtered_at = ?
                    WHERE id = ?
                    """,
                    (
                        release_artist or "V/A",
                        title,
                        json.dumps(["no_real_tracks"], ensure_ascii=False),
                        now,
                        rid,
                    ),
                )
                conn.execute("DELETE FROM tracks WHERE release_id = ?", (rid,))
                skipped += 1
                continue

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
                    is_dj_release = 1,
                    skip_reasons = NULL,
                    filtered_at = ?
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
                    json.dumps(genres, ensure_ascii=False),
                    json.dumps(basic.get("styles") or [], ensure_ascii=False),
                    json.dumps(extract_rpms(formats), ensure_ascii=False),
                    now,
                    rid,
                ),
            )
            conn.execute("DELETE FROM tracks WHERE release_id = ?", (rid,))
            for t in tracks:
                conn.execute(
                    """
                    INSERT INTO tracks (release_id, position, side, artist, title, duration, duration_s)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rid,
                        t["position"],
                        t["side"],
                        t["artist"],
                        t["title"],
                        t["duration"],
                        t["duration_s"],
                    ),
                )
            kept += 1

    print(
        f"Kept {kept} release(s) (is_dj_release=1), "
        f"skipped {skipped} (is_dj_release=0) → {dbmod.db_path().name}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
