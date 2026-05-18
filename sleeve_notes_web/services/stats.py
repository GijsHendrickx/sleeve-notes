"""Read-only aggregations for the dashboard + collection screens.

Computations that need the BPM consensus go via ``derive_track_result`` so
the numbers always match what the renderer would print. Every helper is
scoped to one user_id — dashboards never aggregate across tenants.
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
    octave_matched: int
    shared_url: int
    single_source: int
    disputed: int
    missing: int


def collection_coverage(conn: sqlite3.Connection, user_id: int) -> CoverageStats:
    """Per-track BPM coverage across every track in the user's collection."""
    by_rp, by_tk, _ = load_overrides(conn, user_id)
    rows = conn.execute(
        "SELECT t.release_id, t.position, t.artist, t.title, r.artist AS r_artist "
        "FROM tracks t JOIN releases r "
        "  ON r.user_id = t.user_id AND r.id = t.release_id "
        "WHERE t.user_id = ?",
        (user_id,),
    ).fetchall()
    total = len(rows)
    with_bpm = high = octave = shared = single = disputed = 0
    for r in rows:
        artist = r["artist"] or r["r_artist"] or "V/A"
        result = derive_track_result(
            conn, user_id, r["release_id"], r["position"] or "",
            artist, r["title"] or "", by_rp, by_tk,
        )
        if result.get("bpm"):
            with_bpm += 1
            conf = result.get("bpm_confidence")
            if conf in ("high", "manual"):
                high += 1
            elif conf == "octave":
                octave += 1
            elif conf == "shared":
                shared += 1
            elif conf == "single":
                single += 1
            elif conf == "disputed":
                disputed += 1
    return CoverageStats(
        total=total,
        with_bpm=with_bpm,
        high_confidence=high,
        octave_matched=octave,
        shared_url=shared,
        single_source=single,
        disputed=disputed,
        missing=total - with_bpm,
    )


def new_since_last_print(
    conn: sqlite3.Connection, user_id: int
) -> tuple[int, str | None]:
    """Count of releases (with tracks) for this user that aren't in any past
    print_run + last-print timestamp."""
    last_ts_row = conn.execute(
        "SELECT timestamp FROM print_runs WHERE user_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    last_ts = last_ts_row["timestamp"] if last_ts_row else None
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM releases r "
        "WHERE r.user_id = ? "
        "AND EXISTS (SELECT 1 FROM tracks t WHERE t.user_id = r.user_id AND t.release_id = r.id) "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM print_run_releases prr "
        "  JOIN print_runs p ON p.id = prr.print_run_id "
        "  WHERE p.user_id = r.user_id AND prr.release_id = r.id"
        ")",
        (user_id,),
    ).fetchone()["n"]
    return n, last_ts


def collection_totals(conn: sqlite3.Connection, user_id: int) -> dict:
    """Total release count + breakdown by type (sorted by count desc)."""
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM releases WHERE user_id = ?",
        (user_id,),
    ).fetchone()["n"]
    by_type = [
        (r["type"] or "(unset)", r["n"])
        for r in conn.execute(
            "SELECT type, COUNT(*) AS n FROM releases "
            "WHERE user_id = ? GROUP BY type ORDER BY n DESC",
            (user_id,),
        ).fetchall()
    ]
    return {"total": total or 0, "by_type": by_type}
