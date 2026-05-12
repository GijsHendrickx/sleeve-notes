"""Read-only aggregations for the dashboard + collection screens.

Computations that need the BPM consensus go via ``derive_track_result`` so
the numbers always match what the renderer would print.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from sleeve_notes.fetch_bpm import derive_track_result, load_overrides


@dataclass(frozen=True)
class CoverageStats:
    total: int
    with_bpm: int
    high_confidence: int
    single_source: int
    disputed: int
    continuous_mix: int
    missing: int


def collection_coverage(conn: sqlite3.Connection) -> CoverageStats:
    """Per-track BPM coverage across every kept track. Drives the dashboard."""
    by_rp, by_tk, _ = load_overrides(conn)
    rows = conn.execute(
        "SELECT t.release_id, t.position, t.artist, t.title, t.duration_s, r.artist AS r_artist "
        "FROM tracks t JOIN releases r ON r.id = t.release_id "
        "WHERE r.is_dj_release = 1"
    ).fetchall()
    total = len(rows)
    with_bpm = high = single = disputed = mix = 0
    for r in rows:
        artist = r["artist"] or r["r_artist"] or "V/A"
        result = derive_track_result(
            conn, r["release_id"], r["position"] or "",
            artist, r["title"] or "", r["duration_s"], by_rp, by_tk,
        )
        if result.get("reason") == "continuous_mix":
            mix += 1
            continue
        if result.get("bpm"):
            with_bpm += 1
            conf = result.get("bpm_confidence")
            if conf in ("high", "manual"):
                high += 1
            elif conf == "single":
                single += 1
            elif conf == "disputed":
                disputed += 1
    return CoverageStats(
        total=total,
        with_bpm=with_bpm,
        high_confidence=high,
        single_source=single,
        disputed=disputed,
        continuous_mix=mix,
        missing=total - with_bpm - mix,
    )


def new_since_last_print(conn: sqlite3.Connection) -> tuple[int, str | None]:
    """Count of kept releases that aren't in any past print_run + last-print timestamp."""
    last_ts_row = conn.execute(
        "SELECT timestamp FROM print_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_ts = last_ts_row["timestamp"] if last_ts_row else None
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM releases r "
        "WHERE r.is_dj_release = 1 "
        "AND NOT EXISTS (SELECT 1 FROM print_run_releases prr WHERE prr.release_id = r.id)"
    ).fetchone()["n"]
    return n, last_ts


def collection_totals(conn: sqlite3.Connection) -> dict:
    """Cheap counts: total releases, kept, skipped, never-filtered."""
    row = conn.execute(
        "SELECT "
        "  COUNT(*) AS total, "
        "  SUM(CASE WHEN is_dj_release = 1 THEN 1 ELSE 0 END) AS kept, "
        "  SUM(CASE WHEN is_dj_release = 0 THEN 1 ELSE 0 END) AS skipped, "
        "  SUM(CASE WHEN is_dj_release IS NULL THEN 1 ELSE 0 END) AS unfiltered "
        "FROM releases"
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "kept": row["kept"] or 0,
        "skipped": row["skipped"] or 0,
        "unfiltered": row["unfiltered"] or 0,
    }
