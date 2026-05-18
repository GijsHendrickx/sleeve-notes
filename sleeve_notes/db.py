"""SQLite store for sleeve-notes.

A single DB file at ``<project_root>/data/sleeve_notes.db`` holds all
state for the app. Tables:

  users                one row per Discogs user that has signed in via
                       OAuth. ``id`` is the Discogs user_id; every other
                       per-user table foreign-keys back to it.
  releases             one row per (user, Discogs release). Holds the raw
                       API payload (``basic_information``,
                       ``raw_tracklist``, ``notes``) and the normalized
                       columns derived from it (``artist``, ``title``,
                       ``rpm``, ``type``, ``format``, ...). All
                       normalization happens at fetch time via
                       ``sleeve_notes.ingest.normalize_release``.
  tracks               normalized track rows. Populated by fetch
                       alongside the parent release row.
  bpm_cache            one row per (user, artist, title) hash. Tracks
                       which sources have been queried for that user.
  bpm_source_hits      one row per (user, cache_key, source) — the
                       per-source BPM/key result. Reconstructed into the
                       cache-entry shape by ``load_cache_entry``.
  overrides            manual BPM/key overrides.
  print_runs +
  print_run_releases   print history.
  kv                   small key/value table — Beatport OAuth tokens,
                       schema version, anything that doesn't fit a table.
                       App-wide, NOT per-user.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

try:
    from sleeve_notes import project_root
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sleeve_notes import project_root


SCHEMA_VERSION = 2
DB_FILENAME = "sleeve_notes.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
  user_id INTEGER NOT NULL,
  id INTEGER NOT NULL,
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
  type TEXT,
  format TEXT,
  fetched_at TEXT,
  PRIMARY KEY (user_id, id)
);

CREATE INDEX IF NOT EXISTS idx_releases_user_artist ON releases(user_id, artist);
CREATE INDEX IF NOT EXISTS idx_releases_user_type ON releases(user_id, type);
CREATE INDEX IF NOT EXISTS idx_releases_user_format ON releases(user_id, format);

CREATE TABLE IF NOT EXISTS tracks (
  user_id INTEGER NOT NULL,
  release_id INTEGER NOT NULL,
  position TEXT NOT NULL,
  side TEXT,
  artist TEXT,
  title TEXT,
  duration TEXT,
  duration_s INTEGER,
  spotify_track_id TEXT,
  youtube_video_id TEXT,
  PRIMARY KEY (user_id, release_id, position)
);

CREATE TABLE IF NOT EXISTS bpm_cache (
  user_id INTEGER NOT NULL,
  cache_key TEXT NOT NULL,
  artist TEXT,
  title TEXT,
  sources_tried TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT,
  PRIMARY KEY (user_id, cache_key)
);

CREATE INDEX IF NOT EXISTS idx_bpm_cache_user_artist_title ON bpm_cache(user_id, artist, title);

CREATE TABLE IF NOT EXISTS bpm_source_hits (
  user_id INTEGER NOT NULL,
  cache_key TEXT NOT NULL,
  source TEXT NOT NULL,
  bpm INTEGER,
  key_camelot TEXT,
  score REAL,
  url TEXT,
  mbid TEXT,
  PRIMARY KEY (user_id, cache_key, source)
);

CREATE TABLE IF NOT EXISTS overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  release_id INTEGER,
  position TEXT,
  artist TEXT,
  title TEXT,
  bpm INTEGER,
  key_camelot TEXT,
  note TEXT,
  created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_overrides_user_rp ON overrides(user_id, release_id, position);
CREATE INDEX IF NOT EXISTS idx_overrides_user_at ON overrides(user_id, artist, title);

CREATE TABLE IF NOT EXISTS print_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  timestamp TEXT NOT NULL,
  name TEXT,
  settings_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_print_runs_user ON print_runs(user_id);

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


def connect() -> sqlite3.Connection:
    """Open (or create) the DB."""
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    set_kv(conn, "schema_version", str(SCHEMA_VERSION))
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
