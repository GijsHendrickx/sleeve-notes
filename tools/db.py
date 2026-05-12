"""SQLite store for bpm-stickers.

A single DB file at ``<project_root>/bpm_stickers.db`` replaces every JSON
file that used to live in ``.tmp/`` (plus ``overrides.json`` at root). The
schema is intentionally close to the old JSONs so the cascade/consensus
logic in ``fetch_bpm.py`` stays unchanged in shape; we just persist via
rows instead of nested dicts.

Tables:

  releases             one row per Discogs release. Holds the raw API
                       payload (``basic_information``, ``raw_tracklist``,
                       ``notes``) and the filter-derived columns
                       (``artist``, ``title``, ``rpm``, ...). ``is_dj_release``
                       is NULL until the filter has run, then 0/1.
  tracks               normalized track rows for releases that passed
                       the filter (is_dj_release=1).
  bpm_cache            one row per (artist, title) hash. Tracks which
                       sources have been queried.
  bpm_source_hits      one row per (cache_key, source) — the per-source
                       BPM/key result. Reconstructed into the old v2
                       cache-entry shape by ``load_cache_entry``.
  overrides            manual BPM/key overrides (replaces overrides.json).
  print_runs +
  print_run_releases   print history (replaces printed.json).
  kv                   small key/value table — Beatport OAuth tokens,
                       schema version, anything that doesn't fit a table.

On first init ``migrate_from_json`` imports whatever legacy files it
finds, then renames the originals to ``*.bak`` so they don't get
re-imported. The migration runs inside the same transaction as the
schema setup: if anything raises, the half-written DB is deleted so
the next call re-tries cleanly.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

try:
    from tools import project_root
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import project_root


SCHEMA_VERSION = 1
DB_FILENAME = "bpm_stickers.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
  id INTEGER PRIMARY KEY,
  artist TEXT,
  title TEXT,
  year INTEGER,
  compilation INTEGER NOT NULL DEFAULT 0,
  labels TEXT,
  genres TEXT,
  styles TEXT,
  rpm TEXT,
  notes TEXT,
  basic_information TEXT,
  raw_tracklist TEXT,
  is_dj_release INTEGER,
  skip_reasons TEXT,
  fetched_at TEXT,
  filtered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_releases_dj ON releases(is_dj_release);
CREATE INDEX IF NOT EXISTS idx_releases_artist ON releases(artist);

CREATE TABLE IF NOT EXISTS tracks (
  release_id INTEGER NOT NULL,
  position TEXT NOT NULL,
  side TEXT,
  artist TEXT,
  title TEXT,
  duration TEXT,
  duration_s INTEGER,
  PRIMARY KEY (release_id, position)
);

CREATE INDEX IF NOT EXISTS idx_tracks_release ON tracks(release_id);

CREATE TABLE IF NOT EXISTS bpm_cache (
  cache_key TEXT PRIMARY KEY,
  artist TEXT,
  title TEXT,
  sources_tried TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_bpm_cache_artist_title ON bpm_cache(artist, title);

CREATE TABLE IF NOT EXISTS bpm_source_hits (
  cache_key TEXT NOT NULL,
  source TEXT NOT NULL,
  bpm INTEGER,
  key_camelot TEXT,
  score REAL,
  url TEXT,
  mbid TEXT,
  PRIMARY KEY (cache_key, source)
);

CREATE TABLE IF NOT EXISTS overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  release_id INTEGER,
  position TEXT,
  artist TEXT,
  title TEXT,
  bpm INTEGER,
  key_camelot TEXT,
  continuous_mix INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_overrides_rp ON overrides(release_id, position);
CREATE INDEX IF NOT EXISTS idx_overrides_at ON overrides(artist, title);

CREATE TABLE IF NOT EXISTS print_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS print_run_releases (
  print_run_id INTEGER NOT NULL,
  release_id INTEGER NOT NULL,
  PRIMARY KEY (print_run_id, release_id)
);

CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


DATA_DIRNAME = "data"


def data_dir() -> Path:
    return project_root() / DATA_DIRNAME


def db_path() -> Path:
    return data_dir() / DB_FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _move_legacy_db_to_data_dir(p: Path) -> None:
    """One-shot relocation of a pre-data/ DB.

    Prior versions kept ``bpm_stickers.db`` directly at the project root.
    If the new ``data/`` path is empty but the legacy one exists, move it
    (plus its WAL/SHM sidecars) so existing users don't lose their data
    when they upgrade.
    """
    legacy = project_root() / DB_FILENAME
    if not legacy.exists() or p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    legacy.rename(p)
    for suffix in ("-wal", "-shm"):
        side = legacy.with_name(legacy.name + suffix)
        if side.exists():
            side.rename(p.with_name(p.name + suffix))
    print(
        f"  bpm-stickers: moved legacy {legacy.name} → {p.relative_to(project_root())}.",
        file=sys.stderr,
    )


def connect() -> sqlite3.Connection:
    """Open (or create) the DB. On first creation, runs the JSON migration."""
    p = db_path()
    _move_legacy_db_to_data_dir(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    fresh = not p.exists()
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    if fresh:
        try:
            files_to_move = _migrate_from_json(conn)
            set_kv(conn, "schema_version", str(SCHEMA_VERSION))
            conn.commit()
        except Exception:
            conn.close()
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            raise
        # Only rename source files after the DB commit succeeds. If the rename
        # itself fails (perm error, etc.) we've still got the data persisted.
        for src in files_to_move:
            _rename_to_bak(src)
        if files_to_move:
            print(
                f"  bpm-stickers: migrated {len(files_to_move)} legacy file(s) "
                f"into {p.name}; originals renamed to .bak.",
                file=sys.stderr,
            )
    else:
        conn.commit()
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tiny key/value helpers
# ---------------------------------------------------------------------------

def get_kv(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def delete_kv(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM kv WHERE key = ?", (key,))


# ---------------------------------------------------------------------------
# JSON migration
# ---------------------------------------------------------------------------

def _rename_to_bak(p: Path) -> None:
    if not p.exists():
        return
    suffix = p.suffix if p.suffix else ""
    base = p.with_suffix(suffix + ".bak") if suffix else Path(str(p) + ".bak")
    i = 1
    while base.exists():
        base = (
            p.with_suffix(suffix + f".bak{i}")
            if suffix else Path(str(p) + f".bak{i}")
        )
        i += 1
    try:
        p.rename(base)
    except OSError as e:
        print(f"  bpm-stickers: could not rename {p.name} → {base.name}: {e}", file=sys.stderr)


_V1_URL_TO_SOURCE = (
    ("songbpm.com", "songbpm"),
    ("deezer.com", "deezer"),
    ("spotify.com", "reccobeats"),  # v1 stored Spotify track URLs for ReccoBeats hits
    ("reccobeats.com", "reccobeats"),
    ("beatport.com", "beatport"),
    ("acousticbrainz.org", "acousticbrainz"),
)


def _v1_entry_to_v2(entry: dict) -> tuple[dict, list[str]]:
    """Best-effort mapping of a v1 single-source cache entry onto v2 shape.

    Returns (sources, sources_tried). Drops entries with no usable URL or
    no BPM/key — they'll be re-queried by the v2 cascade.
    """
    bpm = entry.get("bpm")
    key_cam = entry.get("key_camelot")
    if not bpm and not key_cam:
        return {}, []
    url = entry.get("source_url") or ""
    src: str | None = None
    for needle, name in _V1_URL_TO_SOURCE:
        if needle in url:
            src = name
            break
    if not src:
        return {}, []
    hit: dict = {}
    if bpm is not None:
        hit["bpm"] = bpm
    if key_cam:
        hit["key_camelot"] = key_cam
    if entry.get("score") is not None:
        hit["score"] = entry["score"]
    if url:
        hit["url"] = url
    return {src: hit}, [src]


def _migrate_from_json(conn: sqlite3.Connection) -> list[Path]:
    """Best-effort one-shot import of any legacy JSON state.

    Returns the list of file paths to rename to ``.bak`` after the commit.
    """
    root = project_root()
    tmp = root / ".tmp"
    to_move: list[Path] = []
    now = _now_iso()

    # collection.json → releases (raw fields)
    coll = tmp / "collection.json"
    if coll.exists():
        try:
            with coll.open("r", encoding="utf-8") as f:
                entries = json.load(f)
        except Exception:
            entries = []
        for e in entries or []:
            rid = e.get("id")
            if not rid:
                continue
            conn.execute(
                """
                INSERT INTO releases (id, basic_information, raw_tracklist, notes, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    basic_information = excluded.basic_information,
                    raw_tracklist     = excluded.raw_tracklist,
                    notes             = excluded.notes,
                    fetched_at        = COALESCE(releases.fetched_at, excluded.fetched_at)
                """,
                (
                    rid,
                    json.dumps(e.get("basic_information") or {}, ensure_ascii=False),
                    json.dumps(e.get("tracklist") or [], ensure_ascii=False),
                    e.get("notes"),
                    now,
                ),
            )
        to_move.append(coll)

    # release_cache/*.json → fill releases rows that collection.json didn't
    rc_dir = tmp / "release_cache"
    if rc_dir.exists() and rc_dir.is_dir():
        from tools.fetch_discogs_collection import basic_from_detail
        for p in rc_dir.glob("*.json"):
            try:
                rid = int(p.stem)
            except ValueError:
                continue
            try:
                with p.open("r", encoding="utf-8") as f:
                    detail = json.load(f)
            except Exception:
                continue
            row = conn.execute(
                "SELECT raw_tracklist FROM releases WHERE id = ?", (rid,)
            ).fetchone()
            if row and row["raw_tracklist"]:
                continue
            basic = basic_from_detail(detail)
            conn.execute(
                """
                INSERT INTO releases (id, basic_information, raw_tracklist, notes, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    basic_information = excluded.basic_information,
                    raw_tracklist     = excluded.raw_tracklist,
                    notes             = excluded.notes,
                    fetched_at        = COALESCE(releases.fetched_at, excluded.fetched_at)
                """,
                (
                    rid,
                    json.dumps(basic, ensure_ascii=False),
                    json.dumps(detail.get("tracklist") or [], ensure_ascii=False),
                    detail.get("notes"),
                    now,
                ),
            )
        to_move.append(rc_dir)

    # dj_releases.json → extracted columns + tracks
    dj = tmp / "dj_releases.json"
    if dj.exists():
        try:
            with dj.open("r", encoding="utf-8") as f:
                kept = json.load(f)
        except Exception:
            kept = []
        for r in kept or []:
            rid = r.get("id")
            if not rid:
                continue
            conn.execute(
                """
                INSERT INTO releases (
                    id, artist, title, year, compilation, labels, genres, styles,
                    rpm, is_dj_release, filtered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT (id) DO UPDATE SET
                    artist = excluded.artist,
                    title  = excluded.title,
                    year   = excluded.year,
                    compilation = excluded.compilation,
                    labels = excluded.labels,
                    genres = excluded.genres,
                    styles = excluded.styles,
                    rpm    = excluded.rpm,
                    is_dj_release = 1,
                    skip_reasons  = NULL,
                    filtered_at   = excluded.filtered_at
                """,
                (
                    rid,
                    r.get("artist"),
                    r.get("title"),
                    r.get("year"),
                    1 if r.get("compilation") else 0,
                    json.dumps(r.get("labels") or [], ensure_ascii=False),
                    json.dumps(r.get("genres") or [], ensure_ascii=False),
                    json.dumps(r.get("styles") or [], ensure_ascii=False),
                    json.dumps(r.get("rpm") or [], ensure_ascii=False),
                    now,
                ),
            )
            conn.execute("DELETE FROM tracks WHERE release_id = ?", (rid,))
            for t in r.get("tracks") or []:
                conn.execute(
                    """
                    INSERT INTO tracks (release_id, position, side, artist, title, duration, duration_s)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rid,
                        t.get("position"),
                        t.get("side"),
                        t.get("artist"),
                        t.get("title"),
                        t.get("duration"),
                        t.get("duration_s"),
                    ),
                )
        to_move.append(dj)

    # skipped.json → mark releases is_dj_release=0
    skipped = tmp / "skipped.json"
    if skipped.exists():
        try:
            with skipped.open("r", encoding="utf-8") as f:
                sk = json.load(f)
        except Exception:
            sk = []
        for entry in sk or []:
            rid = entry.get("id")
            if not rid:
                continue
            conn.execute(
                """
                INSERT INTO releases (id, artist, title, is_dj_release, skip_reasons, filtered_at)
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    is_dj_release = 0,
                    skip_reasons  = excluded.skip_reasons,
                    filtered_at   = excluded.filtered_at,
                    artist        = COALESCE(releases.artist, excluded.artist),
                    title         = COALESCE(releases.title,  excluded.title)
                """,
                (
                    rid,
                    entry.get("artist"),
                    entry.get("title"),
                    json.dumps(entry.get("reasons") or [], ensure_ascii=False),
                    now,
                ),
            )
        to_move.append(skipped)

    # bpm_cache.json → bpm_cache + bpm_source_hits. Handles both shapes:
    #   v2: {"_meta":…, "tracks": {ck: {"sources":…, "sources_tried":[…]}}}
    #   v1: {ck: {"bpm":…, "source_url":…, "reason":…}}  (flat, single-source)
    bpm_cache_file = tmp / "bpm_cache.json"
    if bpm_cache_file.exists():
        try:
            with bpm_cache_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = None
        if isinstance(data, dict):
            tracks_cache = data.get("tracks") if "_meta" in data else data
            tracks_cache = tracks_cache or {}
            migrated = 0
            dropped = 0
            for ck, entry in tracks_cache.items():
                if not isinstance(entry, dict):
                    continue
                if "sources" in entry or "sources_tried" in entry:
                    # v2 shape — pass through.
                    sources = entry.get("sources") or {}
                    sources_tried = entry.get("sources_tried") or []
                else:
                    # v1 shape — lossy migration: only entries with a known
                    # source URL get imported, mapped onto that single source.
                    sources, sources_tried = _v1_entry_to_v2(entry)
                    if not sources_tried:
                        dropped += 1
                        continue
                conn.execute(
                    """
                    INSERT INTO bpm_cache (cache_key, sources_tried, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        sources_tried = excluded.sources_tried,
                        updated_at    = excluded.updated_at
                    """,
                    (ck, json.dumps(sources_tried), now),
                )
                for src, hit in sources.items():
                    if not isinstance(hit, dict):
                        continue
                    conn.execute(
                        """
                        INSERT INTO bpm_source_hits (cache_key, source, bpm, key_camelot, score, url, mbid)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (cache_key, source) DO UPDATE SET
                            bpm = excluded.bpm,
                            key_camelot = excluded.key_camelot,
                            score = excluded.score,
                            url = excluded.url,
                            mbid = excluded.mbid
                        """,
                        (
                            ck, src,
                            hit.get("bpm"), hit.get("key_camelot"),
                            hit.get("score"), hit.get("url"), hit.get("mbid"),
                        ),
                    )
                migrated += 1
            if dropped:
                print(
                    f"  bpm-stickers: bpm_cache.json was pre-consensus schema; "
                    f"kept {migrated} entries with a known source URL, "
                    f"dropped {dropped} unmappable entries (will be re-queried).",
                    file=sys.stderr,
                )
        to_move.append(bpm_cache_file)

    # bpm_results.json: derivable from cache, don't import — just retire.
    bpm_results_file = tmp / "bpm_results.json"
    if bpm_results_file.exists():
        to_move.append(bpm_results_file)

    # printed.json → print_runs + print_run_releases
    printed = tmp / "printed.json"
    if printed.exists():
        try:
            with printed.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = None
        if isinstance(data, dict):
            for entry in data.get("prints") or []:
                if not isinstance(entry, dict):
                    continue
                ts = entry.get("timestamp") or now
                cur = conn.execute("INSERT INTO print_runs (timestamp) VALUES (?)", (ts,))
                pr_id = cur.lastrowid
                for rid in entry.get("release_ids") or []:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO print_run_releases (print_run_id, release_id) VALUES (?, ?)",
                            (pr_id, int(rid)),
                        )
                    except (TypeError, ValueError):
                        continue
        to_move.append(printed)

    # overrides.json → overrides table
    overrides_file = root / "overrides.json"
    if overrides_file.exists():
        try:
            with overrides_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = None
        if isinstance(data, list):
            for e in data:
                if not isinstance(e, dict):
                    continue
                conn.execute(
                    """
                    INSERT INTO overrides (
                        release_id, position, artist, title, bpm, key_camelot,
                        continuous_mix, note, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        e.get("release_id"),
                        e.get("position"),
                        e.get("artist"),
                        e.get("title"),
                        e.get("bpm"),
                        e.get("key_camelot"),
                        1 if e.get("continuous_mix") else 0,
                        e.get("note"),
                        now,
                    ),
                )
        to_move.append(overrides_file)

    # beatport_tokens.json → kv['beatport_tokens']
    btf = tmp / "beatport_tokens.json"
    if btf.exists():
        try:
            with btf.open("r", encoding="utf-8") as f:
                tokens = json.load(f)
        except Exception:
            tokens = None
        if isinstance(tokens, dict):
            set_kv(conn, "beatport_tokens", json.dumps(tokens))
        to_move.append(btf)

    return to_move
