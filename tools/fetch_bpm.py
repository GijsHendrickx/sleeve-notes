"""Look up BPM per track via a 5-source cascade.

Sources, in order:
  1. songbpm.com  — direct HTML scrape of canonical detail pages (no auth).
  2. Deezer       — public JSON API at api.deezer.com (no auth).
  3. ReccoBeats   — drop-in replacement for the deprecated Spotify audio-features
                    endpoint. Requires a Spotify Client Credentials app for
                    name→ID translation (free, no user OAuth).
  4. Beatport v4  — editorial BPM from labels themselves. OAuth via the public
                    Swagger client_id; refresh tokens stored in
                    .tmp/beatport_tokens.json (set up via tools/beatport_auth.py).
  5. AcousticBrainz — open dataset, looked up via MusicBrainz recording IDs.
                    Frozen since 2022, but excellent coverage for older
                    electronic releases.

All five are validated against the queried artist+title via fuzzy match, and
results are cached in .tmp/bpm_cache.json. Each cache entry tracks which sources
have already been tried, so re-runs only hit the sources that haven't exhausted
yet. Sources without configured credentials raise SourceUnavailable and are
skipped without being marked as tried — configuring them later automatically
extends previously-skipped entries on the next run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv
from rapidfuzz import fuzz
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

load_dotenv()

try:
    from tools import project_root
except ImportError:
    # Script-mode invocation (`python tools/fetch_bpm.py`) — Python adds
    # tools/ to sys.path but not its parent. Add the parent so the package
    # import works, then retry.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import project_root
ROOT = project_root()
TMP = ROOT / ".tmp"
DJ_IN = TMP / "dj_releases.json"
BPM_OUT = TMP / "bpm_results.json"
BPM_CACHE = TMP / "bpm_cache.json"
BEATPORT_TOKENS_FILE = TMP / "beatport_tokens.json"
OVERRIDES_FILE = ROOT / "overrides.json"

# All sources in the cascade. Order here is informational; the cascade fires
# them in parallel per track, then `consense()` picks the canonical value.
ALL_SOURCES = ("songbpm", "deezer", "reccobeats", "beatport", "acousticbrainz")

# Tie-breaker order when sources disagree (no 2+ consensus). Beatport is
# editorial — labels supply the BPM themselves — so it wins for the DJ-relevant
# genres. SongBPM is broad coverage from analysis. AcousticBrainz is last
# because the dataset is frozen since 2022.
SOURCE_PRIORITY = ("beatport", "songbpm", "reccobeats", "deezer", "acousticbrainz")

# --- songbpm.com scrape -------------------------------------------------------
SONGBPM_BASE = "https://songbpm.com"
SONGBPM_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
SONGBPM_RATE_S = 0.6
SONGBPM_JITTER_S = 0.3

# --- Deezer + MusicBrainz API -------------------------------------------------
API_USER_AGENT = "discogs-dj-stickers/1.0 (+https://github.com/local; contact via repo)"
DEEZER_RATE_S = 0.25
MB_RATE_S = 1.05  # MusicBrainz is strict: 1 req/sec. Leave headroom.
AB_RATE_S = 0.5

# --- ReccoBeats (via Spotify Client Credentials) ------------------------------
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SEARCH_URL = "https://api.spotify.com/v1/search"
RECCOBEATS_BASE = "https://api.reccobeats.com/v1"
SPOTIFY_RATE_S = 0.2
RECCOBEATS_RATE_S = 0.15

# --- Beatport v4 --------------------------------------------------------------
# Public client_id scraped from the Beatport Swagger UI JS bundle. Stable since
# 2023; if Beatport ever rotates it, re-extract from
# api.beatport.com/v4/docs/ → /static/btprt/*.js (grep for API_CLIENT_ID).
# Auth uses the same authorization_code dance as beets-beatport4: POST creds
# to /auth/login/ for a session cookie, GET /auth/o/authorize/ to grab the
# code from the 302 Location header, POST /auth/o/token/ to exchange.
BEATPORT_CLIENT_ID = "0GIvkCltVIuPkkwSJHp6NDb3s0potTjLBQr388Dd"
BEATPORT_BASE = "https://api.beatport.com/v4"
BEATPORT_TOKEN_URL = f"{BEATPORT_BASE}/auth/o/token/"
BEATPORT_RATE_S = 0.5

# --- Validation thresholds ----------------------------------------------------
ARTIST_TITLE_MIN_FUZZ = 70.0
BPM_MIN, BPM_MAX = 50, 250
CONTINUOUS_MIX_SECONDS = 12 * 60

TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)
BPM_RE = re.compile(r"\b(\d{2,3})\s*BPM\b", re.IGNORECASE)
TITLE_PARSE_RE = re.compile(r"BPM and key for (.+?) by (.+?) \|", re.IGNORECASE)
# songbpm.com renders key in several places; we try a few patterns and take
# the first parseable hit. Order matters: prefer explicit Camelot, then
# JSON-LD-ish, then loose "Key: X" text.
SONGBPM_CAMELOT_RE = re.compile(r"\b(1[0-2]|[1-9])([AB])\b\s*[—\-–]?\s*camelot", re.IGNORECASE)
SONGBPM_KEY_JSON_RE = re.compile(r'"key"\s*:\s*"([^"]{1,30})"', re.IGNORECASE)
SONGBPM_KEY_TEXT_RE = re.compile(
    r"Key</[^>]+>\s*<[^>]+>\s*([A-G][#b♭♯]?\s*[A-Za-z]+)",
    re.IGNORECASE,
)


class RetryableHTTPError(Exception):
    pass


class SourceUnavailable(Exception):
    """Source can't be tried (e.g. credentials missing). Skip without
    marking as tried — configuring it later will retry on next run."""


# ============================================================================
# Text normalization
# ============================================================================

def slugify(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", text)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[''`]", "", s)
    s = re.sub(r"&", "and", s)
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s


def normalize_for_fuzz(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", text)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def strip_parentheses(text: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*", " ", text or "").strip()


def primary_artist(artist: str) -> str:
    head = re.split(r"[,&]| feat\.| ft\.| with ", artist or "", maxsplit=1, flags=re.IGNORECASE)[0]
    head = re.sub(r"\s*\(\d+\)\s*$", "", head).strip()
    for prefix in ("dj ", "the ", "mc "):
        if head.lower().startswith(prefix):
            head = head[len(prefix):]
            break
    return head.strip()


def fuzz_score(q_artist: str, q_title: str, c_artist: str, c_title: str) -> float:
    art = fuzz.token_set_ratio(normalize_for_fuzz(q_artist), normalize_for_fuzz(c_artist))
    tit = fuzz.token_set_ratio(
        normalize_for_fuzz(strip_parentheses(q_title)),
        normalize_for_fuzz(strip_parentheses(c_title)),
    )
    return 0.6 * art + 0.4 * tit


def cache_key(artist: str, title: str) -> str:
    return hashlib.sha1(
        f"{normalize_for_fuzz(artist)}|{normalize_for_fuzz(title)}".encode("utf-8")
    ).hexdigest()


# ============================================================================
# Musical key → Camelot wheel
# ============================================================================
# Camelot wheel mapping. Letter = mode (B=major, A=minor), number = position.
# Used by rekordbox, Serato, Mixed In Key, etc. Adjacent positions and same
# numbers across A/B = compatible mixes.

_NOTE_TO_PITCH_CLASS = {
    "C": 0, "C#": 1, "Db": 1,
    "D": 2, "D#": 3, "Eb": 3,
    "E": 4,
    "F": 5, "F#": 6, "Gb": 6,
    "G": 7, "G#": 8, "Ab": 8,
    "A": 9, "A#": 10, "Bb": 10,
    "B": 11,
}

_PC_MAJOR_CAMELOT = {
    0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
    6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B",
}
_PC_MINOR_CAMELOT = {
    0: "5A", 1: "12A", 2: "7A", 3: "2A", 4: "9A", 5: "4A",
    6: "11A", 7: "6A", 8: "1A", 9: "8A", 10: "3A", 11: "10A",
}

_KEY_PARSE_RE = re.compile(
    r"^\s*([A-G])\s*([#b♭♯])?\s*(.*)$",
    re.IGNORECASE,
)


def pc_mode_to_camelot(pc: int | None, mode_int: int | None) -> str | None:
    """ReccoBeats/Spotify-style: pitch class (0=C..11=B), mode (0=minor, 1=major)."""
    if pc is None or pc < 0 or pc > 11:
        return None
    if mode_int == 1:
        return _PC_MAJOR_CAMELOT[pc]
    if mode_int == 0:
        return _PC_MINOR_CAMELOT[pc]
    return None


def parse_key_to_camelot(s: str | None) -> str | None:
    """Parse free-form key strings to Camelot. Accepts:

    - "A minor", "C# major", "Eb major"  (note + word)
    - "Am", "C#m"                         (lead-sheet shorthand)
    - "A", "C#"                           (bare note → assume major)
    - "F♯/G♭ Major"                       (slash-form, takes first note)
    - Pre-Camelot strings like "8A"       (passed through)
    """
    if not s:
        return None
    s = s.strip().replace("♭", "b").replace("♯", "#")
    # Pass-through if already Camelot
    cam = re.match(r"^\s*(1[0-2]|[1-9])([AB])\s*$", s, re.IGNORECASE)
    if cam:
        return f"{cam.group(1)}{cam.group(2).upper()}"
    # Slash form: take the first note (e.g. "F#/Gb major" -> "F# major")
    if "/" in s:
        head, _, tail = s.partition("/")
        # Strip trailing tail note, keep the mode word that follows
        s = head + re.sub(r"^\s*[A-Ga-g][#b]?", "", tail)
    m = _KEY_PARSE_RE.match(s)
    if not m:
        return None
    note_letter = m.group(1).upper()
    accidental = (m.group(2) or "").replace("♭", "b").replace("♯", "#")
    suffix = (m.group(3) or "").strip().lower()
    note = note_letter + accidental
    pc = _NOTE_TO_PITCH_CLASS.get(note)
    if pc is None:
        return None
    if suffix in ("minor", "min", "m"):
        return _PC_MINOR_CAMELOT[pc]
    if suffix in ("major", "maj", ""):
        return _PC_MAJOR_CAMELOT[pc]
    return None


# ============================================================================
# HTTP helpers
# ============================================================================

@retry(
    retry=retry_if_exception_type(RetryableHTTPError),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)
def http_get_html(url: str) -> tuple[int, str]:
    resp = requests.get(
        url,
        headers={"User-Agent": SONGBPM_USER_AGENT, "Accept": "text/html"},
        timeout=20,
        allow_redirects=True,
    )
    if resp.status_code == 404:
        return 404, ""
    if resp.status_code in (429, 503) or resp.status_code >= 500:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                time.sleep(float(ra))
            except ValueError:
                pass
        raise RetryableHTTPError(f"{resp.status_code} on {url}")
    resp.raise_for_status()
    return resp.status_code, resp.text


@retry(
    retry=retry_if_exception_type(RetryableHTTPError),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    stop=stop_after_attempt(3),
    reraise=True,
)
def http_get_json(url: str, params: dict | None = None) -> tuple[int, dict | None]:
    resp = requests.get(
        url,
        params=params,
        headers={"User-Agent": API_USER_AGENT, "Accept": "application/json"},
        timeout=20,
    )
    if resp.status_code in (429, 503) or resp.status_code >= 500:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                time.sleep(float(ra))
            except ValueError:
                pass
        raise RetryableHTTPError(f"{resp.status_code} on {url}")
    if resp.status_code == 404:
        return 404, None
    resp.raise_for_status()
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


class RateLimiter:
    """Per-host minimum interval between requests, thread-safe.

    Each host has its own lock so different sources can fire truly in parallel,
    but requests to the same host are serialized to respect the rate limit.
    Without per-host locking, two threads racing on the same host would both
    pass the gap check and fire requests within nanoseconds of each other.
    """
    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._global_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}

    def _lock_for(self, host: str) -> threading.Lock:
        with self._global_lock:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[host] = lock
            return lock

    def wait(self, host: str, min_interval_s: float) -> None:
        with self._lock_for(host):
            elapsed = time.time() - self._last.get(host, 0.0)
            if elapsed < min_interval_s:
                time.sleep(min_interval_s - elapsed)
            self._last[host] = time.time()


# ============================================================================
# Source 1: songbpm.com
# ============================================================================

def songbpm_candidate_urls(artist: str, title: str) -> list[str]:
    title_clean = strip_parentheses(title)
    title_alt = re.sub(r"\s+", " ", re.sub(r"[''`]", "", title or "")).strip()
    artist_full = artist or ""
    artist_first = primary_artist(artist or "")

    pairs: list[tuple[str, str]] = []
    for a in (artist_first, artist_full):
        for t in (title_clean, title_alt, title or ""):
            if not a or not t:
                continue
            pairs.append((slugify(a), slugify(t)))

    seen: set[tuple[str, str]] = set()
    urls: list[str] = []
    for a_slug, t_slug in pairs:
        if not a_slug or not t_slug or (a_slug, t_slug) in seen:
            continue
        seen.add((a_slug, t_slug))
        urls.append(f"{SONGBPM_BASE}/@{a_slug}/{t_slug}")
    return urls


def songbpm_extract_key(html: str) -> str | None:
    """Try several patterns to pull a key out of the page HTML. Best-effort."""
    m = SONGBPM_CAMELOT_RE.search(html)
    if m:
        return f"{m.group(1)}{m.group(2).upper()}"
    m = SONGBPM_KEY_JSON_RE.search(html)
    if m:
        cam = parse_key_to_camelot(m.group(1))
        if cam:
            return cam
    m = SONGBPM_KEY_TEXT_RE.search(html)
    if m:
        cam = parse_key_to_camelot(m.group(1))
        if cam:
            return cam
    return None


def songbpm_validate(html: str, q_artist: str, q_title: str) -> tuple[int | None, str | None, float]:
    bpm_match = BPM_RE.search(html)
    if not bpm_match:
        return None, None, 0.0
    bpm = int(bpm_match.group(1))
    if not (BPM_MIN <= bpm <= BPM_MAX):
        return None, None, 0.0

    key_cam = songbpm_extract_key(html)

    title_tag = TITLE_RE.search(html)
    if not title_tag:
        return bpm, key_cam, 60.0
    m = TITLE_PARSE_RE.search(title_tag.group(1))
    if not m:
        return bpm, key_cam, 60.0
    page_track, page_artist = m.group(1), m.group(2)
    score = fuzz_score(q_artist, q_title, page_artist, page_track)
    if score < ARTIST_TITLE_MIN_FUZZ:
        return None, None, score
    return bpm, key_cam, score


def songbpm_lookup(artist: str, title: str, rl: RateLimiter) -> dict | None:
    urls = songbpm_candidate_urls(artist, title)
    if not urls:
        return None
    for url in urls:
        rl.wait("songbpm", SONGBPM_RATE_S + random.uniform(0, SONGBPM_JITTER_S))
        try:
            status, html = http_get_html(url)
        except Exception:
            return None
        if status == 404:
            continue
        bpm, key_cam, score = songbpm_validate(html, artist, title)
        if bpm:
            return {"bpm": bpm, "key_camelot": key_cam, "score": round(score, 1), "url": url}
    return None


# ============================================================================
# Source 2: Deezer
# ============================================================================

def deezer_search(artist: str, title: str, rl: RateLimiter) -> list[dict]:
    queries = [
        f'artist:"{artist}" track:"{title}"',
        f'artist:"{primary_artist(artist)}" track:"{strip_parentheses(title)}"',
        f"{artist} {title}",
    ]
    seen_ids: set[int] = set()
    out: list[dict] = []
    for q in queries:
        rl.wait("deezer", DEEZER_RATE_S)
        try:
            _, data = http_get_json("https://api.deezer.com/search", params={"q": q})
        except Exception:
            continue
        if not data:
            continue
        for item in data.get("data", [])[:8]:
            tid = item.get("id")
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)
            out.append(item)
        if out:
            break
    return out


def deezer_lookup(artist: str, title: str, rl: RateLimiter) -> dict | None:
    candidates = deezer_search(artist, title, rl)
    best: tuple[float, dict] | None = None
    for c in candidates[:5]:
        score = fuzz_score(artist, title, c.get("artist", {}).get("name", ""), c.get("title", ""))
        if score < ARTIST_TITLE_MIN_FUZZ:
            continue
        if best is None or score > best[0]:
            best = (score, c)
    if best is None:
        return None
    score, hit = best
    tid = hit["id"]
    rl.wait("deezer", DEEZER_RATE_S)
    try:
        _, detail = http_get_json(f"https://api.deezer.com/track/{tid}")
    except Exception:
        return None
    if not detail:
        return None
    bpm_raw = detail.get("bpm")
    if not bpm_raw or bpm_raw <= 0:
        return None
    bpm = int(round(float(bpm_raw)))
    if not (BPM_MIN <= bpm <= BPM_MAX):
        return None
    return {
        "bpm": bpm,
        # Deezer's public track endpoint exposes BPM but not key.
        "score": round(score, 1),
        "url": detail.get("link") or f"https://www.deezer.com/track/{tid}",
    }


# ============================================================================
# Source 3: ReccoBeats (via Spotify Client Credentials for name → ID lookup)
# ============================================================================

_spotify_token: dict | None = None  # {access_token, expires_at}
_spotify_token_lock = threading.Lock()


def _get_spotify_token() -> str:
    """Return a valid Spotify access token, refreshing if needed.

    Raises SourceUnavailable if SPOTIFY_CLIENT_ID/SECRET are not configured.
    Thread-safe: concurrent callers serialize on _spotify_token_lock so only
    one fetch happens per expiration window.
    """
    global _spotify_token
    with _spotify_token_lock:
        now = time.time()
        if _spotify_token and _spotify_token["expires_at"] > now + 30:
            return _spotify_token["access_token"]
        cid = os.environ.get("SPOTIFY_CLIENT_ID")
        cs = os.environ.get("SPOTIFY_CLIENT_SECRET")
        if not cid or not cs:
            raise SourceUnavailable("missing SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET in .env")
        auth = base64.b64encode(f"{cid}:{cs}".encode()).decode()
        resp = requests.post(
            SPOTIFY_TOKEN_URL,
            headers={"Authorization": f"Basic {auth}"},
            data={"grant_type": "client_credentials"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Spotify token request failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        _spotify_token = {
            "access_token": data["access_token"],
            "expires_at": now + int(data.get("expires_in", 3600)),
        }
        return _spotify_token["access_token"]


def _spotify_search_best(artist: str, title: str, rl: RateLimiter) -> tuple[float, str, str, str] | None:
    """Return (score, spotify_id, returned_title, returned_artist) for the best match."""
    token = _get_spotify_token()
    queries = [
        f'track:"{strip_parentheses(title)}" artist:"{primary_artist(artist)}"',
        f'"{title}" "{artist}"',
        f"{artist} {title}",
    ]
    best: tuple[float, str, str, str] | None = None
    for q in queries:
        rl.wait("spotify", SPOTIFY_RATE_S)
        try:
            resp = requests.get(
                SPOTIFY_SEARCH_URL,
                params={"q": q, "type": "track", "limit": 5},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except Exception:
            continue
        if resp.status_code == 401:
            # Token expired mid-run; force refresh next call.
            global _spotify_token
            _spotify_token = None
            return None
        if resp.status_code != 200:
            continue
        items = resp.json().get("tracks", {}).get("items", []) or []
        for it in items:
            sid = it.get("id")
            if not sid:
                continue
            cand_title = it.get("name", "")
            cand_artist = " ".join(a.get("name", "") for a in it.get("artists", []) if isinstance(a, dict))
            score = fuzz_score(artist, title, cand_artist, cand_title)
            if score < ARTIST_TITLE_MIN_FUZZ:
                continue
            if best is None or score > best[0]:
                best = (score, sid, cand_title, cand_artist)
        if best:
            break  # first query that yields a good match wins
    return best


def reccobeats_lookup(artist: str, title: str, rl: RateLimiter) -> dict | None:
    spotify_hit = _spotify_search_best(artist, title, rl)
    if not spotify_hit:
        return None
    spot_score, spot_id, spot_title, spot_artist = spotify_hit

    # Translate Spotify ID → ReccoBeats UUID via multi-fetch endpoint.
    rl.wait("reccobeats", RECCOBEATS_RATE_S)
    try:
        _, data = http_get_json(f"{RECCOBEATS_BASE}/track", params={"ids": spot_id})
    except Exception:
        return None
    if not data:
        return None
    content = data.get("content") or []
    if not content:
        return None  # Spotify has it, ReccoBeats doesn't — coverage gap, fall through.
    track = content[0]
    rb_id = track.get("id")
    if not rb_id:
        return None
    rb_artist = " ".join(a.get("name", "") for a in track.get("artists", []) if isinstance(a, dict))
    rb_title = track.get("trackTitle", "")
    score = fuzz_score(artist, title, rb_artist or spot_artist, rb_title or spot_title)
    if score < ARTIST_TITLE_MIN_FUZZ:
        return None

    # Fetch audio features for the ReccoBeats UUID.
    rl.wait("reccobeats", RECCOBEATS_RATE_S)
    try:
        _, features = http_get_json(f"{RECCOBEATS_BASE}/track/{rb_id}/audio-features")
    except Exception:
        return None
    if not features:
        return None
    tempo = features.get("tempo")
    if not tempo or tempo <= 0:
        return None
    bpm = int(round(float(tempo)))
    if not (BPM_MIN <= bpm <= BPM_MAX):
        return None
    # ReccoBeats audio-features uses the Spotify pitch-class + mode convention:
    # key 0=C..11=B (or -1 = no key detected), mode 0=minor / 1=major.
    key_pc = features.get("key")
    mode_int = features.get("mode")
    key_cam = pc_mode_to_camelot(
        key_pc if isinstance(key_pc, int) and key_pc >= 0 else None,
        mode_int if isinstance(mode_int, int) else None,
    )
    return {
        "bpm": bpm,
        "key_camelot": key_cam,
        "score": round(score, 1),
        "url": track.get("href") or f"https://open.spotify.com/track/{spot_id}",
    }


# ============================================================================
# Source 4: Beatport v4 (OAuth refresh; one-time setup via beatport_auth.py)
# ============================================================================

def _load_beatport_tokens() -> dict | None:
    if not BEATPORT_TOKENS_FILE.exists():
        return None
    try:
        with BEATPORT_TOKENS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_beatport_tokens(tokens: dict) -> None:
    BEATPORT_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BEATPORT_TOKENS_FILE.with_suffix(BEATPORT_TOKENS_FILE.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp, BEATPORT_TOKENS_FILE)


def _refresh_beatport_access(refresh_token: str) -> dict:
    resp = requests.post(
        BEATPORT_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": BEATPORT_CLIENT_ID,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Beatport refresh failed: {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", refresh_token),
        "expires_at": time.time() + int(data.get("expires_in", 3600)),
    }


def _full_reauth_beatport() -> dict | None:
    """Re-authenticate from scratch via the username/password authorization
    flow (delegating to tools/beatport_auth.py's `authenticate`). Returns None
    if BEATPORT_USERNAME/PASSWORD aren't in the env."""
    username = os.environ.get("BEATPORT_USERNAME")
    password = os.environ.get("BEATPORT_PASSWORD")
    if not username or not password:
        return None
    # Lazy import so fetch_bpm.py doesn't depend on the auth module unless
    # we actually need to bootstrap tokens.
    from tools.beatport_auth import authenticate

    return authenticate(username, password)


_beatport_token_lock = threading.Lock()


def _get_beatport_token() -> str:
    """Return a valid Beatport access token.

    Strategy, in order:
      1. Live access_token from .tmp/beatport_tokens.json (if not yet expired).
      2. Refresh via refresh_token from disk.
      3. Full re-auth via BEATPORT_USERNAME/PASSWORD (also bootstraps the
         tokens file if it doesn't exist yet).

    Raises SourceUnavailable only if no tokens file exists AND no credentials
    are configured. Thread-safe: concurrent callers serialize on the lock to
    prevent duplicate refreshes.
    """
    with _beatport_token_lock:
        tokens = _load_beatport_tokens()
        now = time.time()
        if tokens and tokens.get("expires_at", 0) > now + 60:
            return tokens["access_token"]
        if tokens and tokens.get("refresh_token"):
            try:
                fresh = _refresh_beatport_access(tokens["refresh_token"])
                _save_beatport_tokens(fresh)
                return fresh["access_token"]
            except Exception:
                # Refresh token expired/revoked — fall through to full re-auth.
                pass
        fresh = _full_reauth_beatport()
        if fresh:
            _save_beatport_tokens(fresh)
            return fresh["access_token"]
        raise SourceUnavailable(
            "Beatport tokens missing/expired and no BEATPORT_USERNAME/PASSWORD in .env — "
            "run `python tools/beatport_auth.py` to authorize"
        )


def _beatport_track_artist_title(t: dict) -> tuple[str, str]:
    """Extract artist + full title (with mix suffix) from a Beatport track dict."""
    name = t.get("name") or t.get("title") or ""
    mix = t.get("mix_name") or t.get("mix") or ""
    full_title = f"{name} ({mix})" if mix and mix.lower() not in name.lower() else name
    artists = t.get("artists") or []
    artist = " ".join(a.get("name", "") for a in artists if isinstance(a, dict))
    return artist, full_title


def _beatport_extract_key(t: dict) -> str | None:
    """Beatport v4 returns key as a nested object with camelot_number+camelot_letter.

    Older v3-style responses surface a string ('A Minor') in track['key'], so
    handle both shapes defensively.
    """
    k = t.get("key")
    if isinstance(k, dict):
        num = k.get("camelot_number")
        letter = k.get("camelot_letter")
        if num and letter:
            return f"{num}{str(letter).upper()}"
        name = k.get("name") or ""
        if name:
            return parse_key_to_camelot(name)
    if isinstance(k, str) and k:
        return parse_key_to_camelot(k)
    return None


def beatport_lookup(artist: str, title: str, rl: RateLimiter) -> dict | None:
    token = _get_beatport_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": API_USER_AGENT,
    }
    queries = [
        f"{primary_artist(artist)} {strip_parentheses(title)}",
        f"{artist} {title}",
    ]
    seen: set[int] = set()
    candidates: list[dict] = []
    for q in queries:
        rl.wait("beatport", BEATPORT_RATE_S)
        try:
            resp = requests.get(
                f"{BEATPORT_BASE}/catalog/search/",
                params={"q": q, "type": "tracks", "per_page": 10},
                headers=headers,
                timeout=20,
            )
        except Exception:
            continue
        if resp.status_code == 401:
            return None  # token rejected mid-run; let next run re-refresh.
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
        except ValueError:
            continue
        # Search response may nest tracks under "tracks" or "results" depending
        # on whether type= filter narrowed it. Handle both shapes defensively.
        tracks = data.get("tracks")
        if tracks is None:
            tracks = data.get("results") or []
        for t in tracks[:10]:
            tid = t.get("id")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            candidates.append(t)
        if candidates:
            break

    best: tuple[float, dict] | None = None
    for c in candidates:
        c_artist, c_title = _beatport_track_artist_title(c)
        score = fuzz_score(artist, title, c_artist, c_title)
        if score < ARTIST_TITLE_MIN_FUZZ:
            continue
        if best is None or score > best[0]:
            best = (score, c)
    if best is None:
        return None
    score, hit = best
    bpm_raw = hit.get("bpm")
    try:
        bpm = int(round(float(bpm_raw))) if bpm_raw else None
    except (TypeError, ValueError):
        bpm = None
    if bpm is not None and not (BPM_MIN <= bpm <= BPM_MAX):
        bpm = None
    key_cam = _beatport_extract_key(hit)
    if bpm is None and key_cam is None:
        return None  # Beatport had a match candidate but no useful fields.
    tid = hit.get("id")
    slug = hit.get("slug") or "_"
    return {
        "bpm": bpm,
        "key_camelot": key_cam,
        "score": round(score, 1),
        "url": f"https://www.beatport.com/track/{slug}/{tid}" if tid else None,
    }


# ============================================================================
# Source 5: MusicBrainz + AcousticBrainz
# ============================================================================

def _mb_escape(s: str) -> str:
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s or "")


def mb_search(artist: str, title: str, rl: RateLimiter) -> list[tuple[str, float]]:
    title_clean = strip_parentheses(title)
    art = primary_artist(artist)
    query = f'recording:"{_mb_escape(title_clean)}" AND artist:"{_mb_escape(art)}"'
    rl.wait("musicbrainz", MB_RATE_S)
    try:
        _, data = http_get_json(
            "https://musicbrainz.org/ws/2/recording",
            params={"query": query, "fmt": "json", "limit": "10"},
        )
    except Exception:
        return []
    if not data:
        return []
    out: list[tuple[str, float]] = []
    for rec in data.get("recordings", [])[:10]:
        mbid = rec.get("id")
        rec_artist = " ".join(a.get("name", "") for a in rec.get("artist-credit", []) if isinstance(a, dict))
        score = fuzz_score(artist, title, rec_artist, rec.get("title", ""))
        if score < ARTIST_TITLE_MIN_FUZZ:
            continue
        out.append((mbid, score))
    out.sort(key=lambda x: -x[1])
    return out


def ab_lookup(artist: str, title: str, rl: RateLimiter) -> dict | None:
    candidates = mb_search(artist, title, rl)
    if not candidates:
        return None
    for mbid, score in candidates[:5]:
        rl.wait("acousticbrainz", AB_RATE_S)
        try:
            status, data = http_get_json(f"https://acousticbrainz.org/api/v1/{mbid}/low-level")
        except Exception:
            continue
        if status == 404 or not data:
            continue
        bpm_raw = data.get("rhythm", {}).get("bpm")
        bpm = None
        if bpm_raw:
            bpm_int = int(round(float(bpm_raw)))
            if BPM_MIN <= bpm_int <= BPM_MAX:
                bpm = bpm_int
        # AcousticBrainz tonal: key_key like "Ab", key_scale "minor"/"major".
        tonal = data.get("tonal") or {}
        key_cam = parse_key_to_camelot(
            f"{tonal.get('key_key', '')} {tonal.get('key_scale', '')}".strip()
        )
        if bpm is None and key_cam is None:
            continue
        return {
            "bpm": bpm,
            "key_camelot": key_cam,
            "score": round(score, 1),
            "url": f"https://acousticbrainz.org/{mbid}",
            "mbid": mbid,
        }
    return None


# ============================================================================
# Cascade
# ============================================================================

_LOOKUP_FNS = {
    "songbpm": songbpm_lookup,
    "deezer": deezer_lookup,
    "reccobeats": reccobeats_lookup,
    "beatport": beatport_lookup,
    "acousticbrainz": ab_lookup,
}


def cascade_all(
    artist: str,
    title: str,
    prev: dict | None,
    rl: RateLimiter,
) -> dict:
    """Fire every untried source in parallel, return a merged v2 cache entry.

    `prev` is a v2 entry from the cache (or None). Sources already in
    `prev['sources_tried']` are skipped — same incremental behaviour as the
    serial cascade, but per-source instead of stop-on-first-hit. We always
    query every source we can so the consensus has enough data to evaluate.

    Result shape (v2 cache entry):
        {
            "sources": {<source>: {bpm, key_camelot, score, url, ...}, ...},
            "sources_tried":       [<source>, ...],   # completed (hit or miss)
            "sources_unavailable": [<source>, ...],   # this run, e.g. no creds
            "sources_errored":     [<source>, ...],   # this run, retry next time
        }
    """
    sources: dict[str, dict] = dict((prev or {}).get("sources") or {})
    tried: set[str] = set((prev or {}).get("sources_tried") or [])
    to_run = [s for s in ALL_SOURCES if s not in tried]
    new_unavailable: list[str] = []
    new_errored: list[str] = []

    if not to_run:
        return {
            "sources": sources,
            "sources_tried": sorted(tried),
            "sources_unavailable": [],
            "sources_errored": [],
        }

    with ThreadPoolExecutor(max_workers=len(to_run)) as ex:
        futures = {ex.submit(_LOOKUP_FNS[s], artist, title, rl): s for s in to_run}
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                hit = fut.result()
            except SourceUnavailable:
                # Credentials missing: don't mark as tried, configure later and retry.
                new_unavailable.append(src)
                continue
            except Exception as e:
                # Network/parse error: don't mark as tried, retry next run.
                new_errored.append(f"{src}: {e}")
                continue
            tried.add(src)
            if hit:
                sources[src] = hit

    return {
        "sources": sources,
        "sources_tried": sorted(tried),
        "sources_unavailable": sorted(new_unavailable),
        "sources_errored": sorted(new_errored),
    }


def consense(entry: dict) -> dict:
    """Derive canonical (bpm, key, confidence) from per-source results.

    BPM consensus: cluster source BPMs within ±1 (handles ½-BPM rounding noise);
    the largest cluster with ≥2 members wins, value = median. Otherwise:
      - 1 source returned   -> that value, confidence="single"
      - 2+ disagree         -> SOURCE_PRIORITY-ordered pick, confidence="disputed"
      - 0 sources returned  -> None, confidence=None

    Key consensus: exact Camelot string match (sources already normalised).
    Same fall-through logic (single / disputed / none).
    """
    sources: dict = entry.get("sources") or {}
    bpm_hits = [(name, int(s["bpm"])) for name, s in sources.items() if s.get("bpm")]
    key_hits = [(name, s["key_camelot"]) for name, s in sources.items() if s.get("key_camelot")]

    bpm, bpm_conf, bpm_sources = _consense_numeric(bpm_hits, tolerance=1)
    key, key_conf, key_sources = _consense_categorical(key_hits)

    return {
        "bpm": bpm,
        "bpm_confidence": bpm_conf,
        "bpm_sources": bpm_sources,
        "key_camelot": key,
        "key_confidence": key_conf,
        "key_sources": key_sources,
    }


def _consense_numeric(
    hits: list[tuple[str, int]], tolerance: int = 1
) -> tuple[int | None, str | None, list[str]]:
    if not hits:
        return None, None, []
    if len(hits) == 1:
        return hits[0][1], "single", [hits[0][0]]
    # Cluster by adjacency on a sorted axis
    by_val = sorted(hits, key=lambda x: x[1])
    clusters: list[list[tuple[str, int]]] = [[by_val[0]]]
    for name, val in by_val[1:]:
        if val - clusters[-1][-1][1] <= tolerance:
            clusters[-1].append((name, val))
        else:
            clusters.append([(name, val)])
    clusters.sort(key=lambda c: -len(c))
    winning = clusters[0]
    if len(winning) >= 2:
        sorted_vals = sorted(v for _, v in winning)
        median = sorted_vals[len(sorted_vals) // 2]
        return median, "high", sorted(n for n, _ in winning)
    # No 2+ cluster. Disputed → priority pick.
    priority = {s: i for i, s in enumerate(SOURCE_PRIORITY)}
    by_priority = sorted(hits, key=lambda x: priority.get(x[0], 99))
    return by_priority[0][1], "disputed", [by_priority[0][0]]


def _consense_categorical(
    hits: list[tuple[str, str]]
) -> tuple[str | None, str | None, list[str]]:
    if not hits:
        return None, None, []
    if len(hits) == 1:
        return hits[0][1], "single", [hits[0][0]]
    counts = Counter(v for _, v in hits)
    most, count = counts.most_common(1)[0]
    if count >= 2:
        return most, "high", sorted(n for n, v in hits if v == most)
    priority = {s: i for i, s in enumerate(SOURCE_PRIORITY)}
    by_priority = sorted(hits, key=lambda x: priority.get(x[0], 99))
    return by_priority[0][1], "disputed", [by_priority[0][0]]


# ============================================================================
# Continuous-mix filter, cache I/O, and main loop
# ============================================================================

def is_continuous_mix(position: str, duration_s: int | None) -> bool:
    """Flag a track as a continuous DJ-mix only on positive evidence.

    Requires a known duration: bare-side position (e.g. "A", "B") with
    duration > 12 min, OR any position with duration > 24 min. When the
    duration is unknown we fall back to "regular track" — Discogs often
    omits durations on single-track-per-side EPs, and treating those as
    mixes mislabels them and suppresses the BPM lookup. If the lookup
    later fails the sticker just shows the empty BPM box, which is the
    right outcome for a missing data point.
    """
    if not position or duration_s is None:
        return False
    bare_side = re.fullmatch(r"[A-Z]+", position.strip())
    if bare_side and duration_s > CONTINUOUS_MIX_SECONDS:
        return True
    if duration_s > CONTINUOUS_MIX_SECONDS * 2:
        return True
    return False


def load_overrides() -> tuple[dict, dict, list[str]]:
    """Read overrides.json (if present) into two lookup tables.

    Returns (by_release_position, by_track_key, warnings).
      by_release_position: dict[(release_id, position), entry] — most precise
      by_track_key:        dict[cache_key(artist, title), entry] — broader

    Each entry is a dict from the JSON file; only validated keys are indexed.
    Invalid entries are skipped with a warning rather than raising — we never
    want a typo in overrides.json to fail the whole run.
    """
    if not OVERRIDES_FILE.exists():
        return {}, {}, []
    try:
        with OVERRIDES_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {}, {}, [f"failed to parse {OVERRIDES_FILE.name}: {e}"]

    if not isinstance(data, list):
        return {}, {}, [f"{OVERRIDES_FILE.name}: expected a JSON list of entries"]

    by_rp: dict[tuple[int, str], dict] = {}
    by_tk: dict[str, dict] = {}
    warnings: list[str] = []

    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            warnings.append(f"entry #{i}: not a JSON object")
            continue

        bpm = entry.get("bpm")
        if bpm is not None:
            try:
                bpm_int = int(round(float(bpm)))
            except (TypeError, ValueError):
                warnings.append(f"entry #{i}: invalid bpm {bpm!r}")
                continue
            if not (BPM_MIN <= bpm_int <= BPM_MAX):
                warnings.append(
                    f"entry #{i}: bpm {bpm_int} outside {BPM_MIN}-{BPM_MAX}"
                )
                continue
            entry["bpm"] = bpm_int

        raw_key = entry.get("key_camelot")
        if raw_key is not None:
            normalized = parse_key_to_camelot(str(raw_key))
            if normalized is None:
                warnings.append(f"entry #{i}: unrecognised key_camelot {raw_key!r}")
                continue
            entry["key_camelot"] = normalized

        if "release_id" in entry and "position" in entry:
            try:
                rid = int(entry["release_id"])
            except (TypeError, ValueError):
                warnings.append(f"entry #{i}: invalid release_id {entry['release_id']!r}")
                continue
            pos = str(entry["position"])
            if (rid, pos) in by_rp:
                warnings.append(f"entry #{i}: duplicate (release_id={rid}, position={pos!r})")
            by_rp[(rid, pos)] = entry
        elif "artist" in entry and "title" in entry:
            key = cache_key(str(entry["artist"]), str(entry["title"]))
            if key in by_tk:
                warnings.append(
                    f"entry #{i}: duplicate artist+title "
                    f"({entry['artist']!r} - {entry['title']!r})"
                )
            by_tk[key] = entry
        else:
            warnings.append(
                f"entry #{i}: must have (release_id, position) or (artist, title)"
            )

    return by_rp, by_tk, warnings


def override_to_result(entry: dict) -> dict:
    """Convert a validated override entry into a final-result dict.

    Manual values bypass the cascade and the consensus entirely. The PDF
    generator inspects `bpm`, `key_camelot`, `bpm_confidence`, and `reason` —
    manual entries carry confidence="manual" so the renderer can distinguish
    them from source-derived high-confidence hits if desired.
    """
    base = {
        "source": "manual",
        "bpm_confidence": "manual",
        "key_confidence": "manual" if entry.get("key_camelot") else None,
        "bpm_sources": ["manual"],
        "key_sources": ["manual"] if entry.get("key_camelot") else [],
    }
    if entry.get("continuous_mix"):
        return {**base, "bpm": None, "key_camelot": None, "reason": "continuous_mix"}
    bpm = entry.get("bpm")
    key_cam = parse_key_to_camelot(entry.get("key_camelot")) if entry.get("key_camelot") else None
    if bpm is None and key_cam is None:
        return {**base, "bpm": None, "key_camelot": None, "reason": "manual_no_bpm"}
    return {**base, "bpm": bpm, "key_camelot": key_cam, "reason": "manual"}


CACHE_VERSION = 2


def load_cache() -> dict:
    """Load the v2 cache. On encountering an older schema, back it up and
    start fresh — the v1 schema can't be migrated without re-querying every
    source for the per-source results that didn't exist yet.

    v2 shape:
        {
            "_meta": {"version": 2},
            "tracks": {
                "<cache_key>": {
                    "sources": {...},
                    "sources_tried": [...],
                    ...
                }
            }
        }
    """
    if not BPM_CACHE.exists():
        return {"_meta": {"version": CACHE_VERSION}, "tracks": {}}
    with BPM_CACHE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    version = (data.get("_meta") or {}).get("version") if isinstance(data, dict) else None
    if version == CACHE_VERSION:
        return data
    # Pre-v2 cache (flat dict of cache_key → single-source result). Migrating
    # without re-fetching would lose the per-source data needed for consensus,
    # so we archive and start over.
    backup = BPM_CACHE.with_name("bpm_cache.v1.json.bak")
    os.replace(BPM_CACHE, backup)
    print(
        f"  notice: existing cache uses the pre-consensus schema; backed up to "
        f"{backup.name} and starting a fresh v{CACHE_VERSION} cache. "
        "Sources will be re-queried — same speed as a cold first run.",
        file=sys.stderr,
    )
    return {"_meta": {"version": CACHE_VERSION}, "tracks": {}}


def save_cache(cache: dict) -> None:
    tmp = BPM_CACHE.with_suffix(BPM_CACHE.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BPM_CACHE)


def build_track_result(position: str, artist: str, title: str, entry: dict) -> dict:
    """Combine a v2 cache entry with derived consensus into the per-track
    result that goes into bpm_results.json (and ultimately the PDF)."""
    derived = consense(entry)
    sources = entry.get("sources") or {}
    bpm = derived["bpm"]
    if bpm is None:
        # Distinguish "we tried everything" from "still incremental" so the
        # PDF knows when to lock in the empty box vs. wait for a later run.
        all_tried = set(ALL_SOURCES).issubset(set(entry.get("sources_tried") or []))
        reason = "exhausted" if all_tried else "incomplete"
    else:
        reason = "match"
    return {
        "position": position,
        "artist": artist,
        "title": title,
        "bpm": bpm,
        "key_camelot": derived["key_camelot"],
        "bpm_confidence": derived["bpm_confidence"],
        "key_confidence": derived["key_confidence"],
        "bpm_sources": derived["bpm_sources"],
        "key_sources": derived["key_sources"],
        "source": derived["bpm_sources"][0] if derived["bpm_sources"] else None,
        "source_url": (sources.get(derived["bpm_sources"][0]) or {}).get("url")
            if derived["bpm_sources"] else None,
        "reason": reason,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    if not DJ_IN.exists():
        print(f"ERROR: {DJ_IN} not found. Run filter_dj_releases.py first.", file=sys.stderr)
        return 2

    with DJ_IN.open("r", encoding="utf-8") as f:
        releases = json.load(f)

    TMP.mkdir(parents=True, exist_ok=True)
    cache = load_cache()
    tracks_cache: dict = cache.setdefault("tracks", {})
    rl = RateLimiter()
    results: list[dict] = []

    by_rp, by_tk, override_warnings = load_overrides()
    for w in override_warnings:
        print(f"  override warning: {w}", file=sys.stderr)
    if by_rp or by_tk:
        print(f"Loaded {len(by_rp) + len(by_tk)} override(s) from {OVERRIDES_FILE.name}.")
    used_overrides: set[tuple] = set()

    total_tracks = sum(len(r["tracks"]) for r in releases)
    print(f"Looking up BPM/key for {total_tracks} tracks across {len(releases)} releases...")
    done = 0
    cached_hits = 0
    override_hits = 0
    cascade_runs = 0
    hits_by_source: dict[str, int] = {s: 0 for s in ALL_SOURCES}

    for release in releases:
        track_results: list[dict] = []
        for track in release["tracks"]:
            done += 1
            artist = track.get("artist") or release["artist"]
            title = track.get("title", "")
            position = track.get("position", "")
            duration_s = track.get("duration_s")

            # Manual overrides win over everything.
            override = by_rp.get((release["id"], position))
            override_id: tuple | None = None
            if override is not None:
                override_id = ("rp", release["id"], position)
            else:
                tk = cache_key(artist, title)
                if tk in by_tk:
                    override = by_tk[tk]
                    override_id = ("tk", tk)
            if override is not None:
                used_overrides.add(override_id)
                override_hits += 1
                track_results.append({
                    "position": position, "artist": artist, "title": title,
                    **override_to_result(override),
                })
                continue

            if is_continuous_mix(position, duration_s):
                track_results.append({
                    "position": position, "artist": artist, "title": title,
                    "bpm": None, "key_camelot": None,
                    "bpm_confidence": None, "key_confidence": None,
                    "bpm_sources": [], "key_sources": [],
                    "source": None, "source_url": None,
                    "reason": "continuous_mix",
                })
                continue

            ck = cache_key(artist, title)
            cached = tracks_cache.get(ck)

            # Decide whether to call the cascade. Skip if every source has
            # already been tried for this track (hit or no-hit). Configuring
            # a new source later automatically retries on the next run because
            # untried sources fall back through to_run.
            tried = set((cached or {}).get("sources_tried") or [])
            if cached and set(ALL_SOURCES).issubset(tried):
                cached_hits += 1
                track_results.append(build_track_result(position, artist, title, cached))
                continue

            entry = cascade_all(artist, title, cached, rl)
            tracks_cache[ck] = entry
            cascade_runs += 1
            if cascade_runs % 5 == 0 or done == total_tracks:
                save_cache(cache)
            result = build_track_result(position, artist, title, entry)
            for src in entry.get("sources") or {}:
                if cached is None or src not in (cached.get("sources") or {}):
                    hits_by_source[src] = hits_by_source.get(src, 0) + 1
            if result["bpm"]:
                marker = {"high": "●●", "single": "○", "disputed": "?"}.get(
                    result["bpm_confidence"], "·"
                )
                key_str = f" key={result['key_camelot']}" if result["key_camelot"] else ""
                print(
                    f"  [{done}/{total_tracks}] {marker} {artist} - {title} -> "
                    f"{result['bpm']}{key_str}  sources={result['bpm_sources']}"
                )

            if done % 25 == 0 or done == total_tracks:
                hits_str = " ".join(f"{s}={hits_by_source[s]}" for s in ALL_SOURCES)
                print(f"  [{done}/{total_tracks}] progress: cached={cached_hits} {hits_str}")

        results.append({"id": release["id"], "artist": release["artist"], "title": release["title"], "tracks": track_results})

    save_cache(cache)
    with BPM_OUT.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    found_bpm = sum(1 for r in results for t in r["tracks"] if t.get("bpm"))
    found_key = sum(1 for r in results for t in r["tracks"] if t.get("key_camelot"))
    high_conf = sum(1 for r in results for t in r["tracks"] if t.get("bpm_confidence") == "high")
    disputed = sum(1 for r in results for t in r["tracks"] if t.get("bpm_confidence") == "disputed")
    hits_str = ", ".join(f"{s}={hits_by_source[s]}" for s in ALL_SOURCES)
    print(
        f"Wrote {BPM_OUT}: {found_bpm}/{total_tracks} BPMs ({high_conf} multi-source consensus, "
        f"{disputed} disputed), {found_key}/{total_tracks} keys."
    )
    print(f"  new this run by source: {hits_str}   (cached hits: {cached_hits}, overrides: {override_hits})")

    unused_rp = [k for k in by_rp if ("rp", k[0], k[1]) not in used_overrides]
    unused_tk = [k for k in by_tk if ("tk", k) not in used_overrides]
    if unused_rp or unused_tk:
        print(
            f"  WARNING: {len(unused_rp) + len(unused_tk)} override entry/entries "
            f"matched nothing — check for typos in {OVERRIDES_FILE.name}:",
            file=sys.stderr,
        )
        for rid, pos in unused_rp:
            print(f"    - release_id={rid} position={pos!r}", file=sys.stderr)
        for tk in unused_tk:
            e = by_tk[tk]
            print(f"    - artist={e.get('artist')!r} title={e.get('title')!r}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
