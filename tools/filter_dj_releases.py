"""Filter Discogs collection down to DJ-ready 12" electronic releases."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    from tools import project_root
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import project_root
ROOT = project_root()
TMP = ROOT / ".tmp"
COLLECTION_IN = TMP / "collection.json"
DJ_OUT = TMP / "dj_releases.json"
SKIPPED_OUT = TMP / "skipped.json"

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
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    if not COLLECTION_IN.exists():
        print(f"ERROR: {COLLECTION_IN} not found. Run fetch_discogs_collection.py first.", file=sys.stderr)
        return 2

    with COLLECTION_IN.open("r", encoding="utf-8") as f:
        collection = json.load(f)

    kept: list[dict] = []
    skipped: list[dict] = []

    for entry in collection:
        basic = entry.get("basic_information", {})
        rid = entry.get("id")
        formats = basic.get("formats") or []
        genres = basic.get("genres") or []
        tracklist = entry.get("tracklist") or []

        title = (basic.get("title") or "").strip()
        release_artists = basic.get("artists") or []
        release_artist = join_artists(release_artists)
        compilation = any((a.get("name") or "").strip().lower() == "various" for a in release_artists)

        is_target_vinyl = check_format(formats)

        reasons: list[str] = []
        if not is_target_vinyl:
            reasons.append("not_12inch_or_lp")
        if not tracklist:
            reasons.append("no_tracklist")

        if reasons:
            skipped.append(
                {
                    "id": rid,
                    "artist": release_artist or "V/A",
                    "title": title,
                    "reasons": reasons,
                }
            )
            continue

        tracks = transform_tracks(tracklist, release_artist or "V/A")
        if not tracks:
            skipped.append(
                {"id": rid, "artist": release_artist or "V/A", "title": title, "reasons": ["no_real_tracks"]}
            )
            continue

        kept.append(
            {
                "id": rid,
                "artist": release_artist or "V/A",
                "title": title,
                "year": basic.get("year"),
                "labels": [(l.get("name") or "").strip() for l in basic.get("labels") or []],
                "genres": genres,
                "styles": basic.get("styles") or [],
                "compilation": compilation,
                "rpm": extract_rpms(formats),
                "tracks": tracks,
            }
        )

    TMP.mkdir(parents=True, exist_ok=True)
    with DJ_OUT.open("w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=2)
    with SKIPPED_OUT.open("w", encoding="utf-8") as f:
        json.dump(skipped, f, ensure_ascii=False, indent=2)

    print(f"Kept {len(kept)} releases → {DJ_OUT}")
    print(f"Skipped {len(skipped)} releases → {SKIPPED_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
