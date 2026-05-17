"""Look up BPM per track via a 5-source cascade.

Sources, in order:
  1. songbpm.com  — direct HTML scrape of canonical detail pages (no auth).
  2. Deezer       — public JSON API at api.deezer.com (no auth).
  3. ReccoBeats   — drop-in replacement for the deprecated Spotify audio-features
                    endpoint. Requires a Spotify Client Credentials app for
                    name→ID translation (free, no user OAuth).
  4. Beatport v4  — editorial BPM from labels themselves. OAuth via the public
                    Swagger client_id; refresh tokens stored in the DB
                    (set up via sleeve_notes/beatport_auth.py).
  5. AcousticBrainz — open dataset, looked up via MusicBrainz recording IDs.
                    Frozen since 2022, but excellent coverage for older
                    electronic releases.

All five are validated against the queried artist+title via fuzzy match, and
results are cached in the ``bpm_cache`` + ``bpm_source_hits`` tables. Each
cache entry tracks which sources have already been tried, so re-runs only
hit the sources that haven't exhausted yet. Sources without configured
credentials raise SourceUnavailable and are skipped without being marked
as tried — configuring them later automatically extends previously-skipped
entries on the next run.
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
from datetime import datetime, timezone
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
    from sleeve_notes import project_root
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sleeve_notes import project_root

from sleeve_notes import db as dbmod

ROOT = project_root()

# All sources in the cascade. Order here is informational; the cascade fires
# them in parallel per track, then `consense()` picks the canonical value.
ALL_SOURCES = ("songbpm", "deezer", "reccobeats", "beatport", "acousticbrainz")

# Tie-breaker order when sources disagree (no consensus, raw or octave-aware).
# Order is empirically driven by per-source outlier rates measured against the
# rest of the cascade: SongBPM disagrees least often when it returns a value;
# Beatport disagrees most often (frequently a wrong-track match or a halftime
# reading for older DJ catalog entries). Beatport's "labels supply the BPM"
# argument didn't hold up in practice on this collection.
SOURCE_PRIORITY = ("songbpm", "reccobeats", "deezer", "acousticbrainz", "beatport")

# --- songbpm.com scrape -------------------------------------------------------
SONGBPM_BASE = "https://songbpm.com"
SONGBPM_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
SONGBPM_RATE_S = 0.6
SONGBPM_JITTER_S = 0.3

# --- Deezer + MusicBrainz API -------------------------------------------------
API_USER_AGENT = "sleeve-notes/0.3 (+https://github.com/GijsHendrickx/sleeve-notes)"
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
BEATPORT_CLIENT_ID = "0GIvkCltVIuPkkwSJHp6NDb3s0potTjLBQr388Dd"
BEATPORT_BASE = "https://api.beatport.com/v4"
BEATPORT_TOKEN_URL = f"{BEATPORT_BASE}/auth/o/token/"
BEATPORT_RATE_S = 0.5

# --- Validation thresholds ----------------------------------------------------
ARTIST_TITLE_MIN_FUZZ = 70.0
BPM_MIN, BPM_MAX = 50, 250

TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)
BPM_RE = re.compile(r"\b(\d{2,3})\s*BPM\b", re.IGNORECASE)
TITLE_PARSE_RE = re.compile(r"BPM and key for (.+?) by (.+?) \|", re.IGNORECASE)
# songbpm.com renders key in several places; we try a few patterns and take
# the first parseable hit.
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
    cam = re.match(r"^\s*(1[0-2]|[1-9])([AB])\s*$", s, re.IGNORECASE)
    if cam:
        return f"{cam.group(1)}{cam.group(2).upper()}"
    if "/" in s:
        head, _, tail = s.partition("/")
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

_SESSION = requests.Session()
_SESSION.mount(
    "https://",
    requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=40),
)
_SESSION.mount(
    "http://",
    requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=40),
)


@retry(
    retry=retry_if_exception_type(RetryableHTTPError),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    stop=stop_after_attempt(2),
    reraise=True,
)
def http_get_html(url: str) -> tuple[int, str]:
    resp = _SESSION.get(
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
    wait=wait_exponential(multiplier=1, min=1, max=5),
    stop=stop_after_attempt(2),
    reraise=True,
)
def http_get_json(url: str, params: dict | None = None) -> tuple[int, dict | None]:
    resp = _SESSION.get(
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
    # Cap at 2 URLs: best-guess (primary artist + parenthesis-stripped title)
    # and broadest fallback (full artist + raw title). The previous 4–6
    # permutations rarely produced extra hits beyond these two and burned
    # 0.6–0.9 s of rate-limit time per miss.
    title_clean = strip_parentheses(title)
    artist_full = artist or ""
    artist_first = primary_artist(artist or "")

    pairs: list[tuple[str, str]] = []
    if artist_first and title_clean:
        pairs.append((slugify(artist_first), slugify(title_clean)))
    if artist_full and title:
        pairs.append((slugify(artist_full), slugify(title)))

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
        "score": round(score, 1),
        "url": detail.get("link") or f"https://www.deezer.com/track/{tid}",
    }


# ============================================================================
# Source 3: ReccoBeats (via Spotify Client Credentials for name → ID lookup)
# ============================================================================

_spotify_token: dict | None = None
_spotify_token_lock = threading.Lock()


def _get_spotify_token() -> str:
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
        resp = _SESSION.post(
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
            resp = _SESSION.get(
                SPOTIFY_SEARCH_URL,
                params={"q": q, "type": "track", "limit": 5},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except Exception:
            continue
        if resp.status_code == 401:
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
            break
    return best


def reccobeats_lookup(artist: str, title: str, rl: RateLimiter) -> dict | None:
    spotify_hit = _spotify_search_best(artist, title, rl)
    if not spotify_hit:
        return None
    spot_score, spot_id, spot_title, spot_artist = spotify_hit

    rl.wait("reccobeats", RECCOBEATS_RATE_S)
    try:
        _, data = http_get_json(f"{RECCOBEATS_BASE}/track", params={"ids": spot_id})
    except Exception:
        return None
    if not data:
        return None
    content = data.get("content") or []
    if not content:
        return None
    track = content[0]
    rb_id = track.get("id")
    if not rb_id:
        return None
    rb_artist = " ".join(a.get("name", "") for a in track.get("artists", []) if isinstance(a, dict))
    rb_title = track.get("trackTitle", "")
    score = fuzz_score(artist, title, rb_artist or spot_artist, rb_title or spot_title)
    if score < ARTIST_TITLE_MIN_FUZZ:
        return None

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
        "spotify_track_id": spot_id,
    }


# ============================================================================
# Source 4: Beatport v4 (OAuth refresh; tokens stored in kv['beatport_tokens'])
# ============================================================================

_beatport_token_lock = threading.Lock()


def _load_beatport_tokens() -> dict | None:
    with dbmod.session() as conn:
        raw = dbmod.get_kv(conn, "beatport_tokens")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _save_beatport_tokens(tokens: dict) -> None:
    with dbmod.session() as conn:
        dbmod.set_kv(conn, "beatport_tokens", json.dumps(tokens))


def _refresh_beatport_access(refresh_token: str) -> dict:
    resp = _SESSION.post(
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
    """Re-authenticate from scratch via username/password. Returns None if
    BEATPORT_USERNAME/PASSWORD aren't in the env."""
    username = os.environ.get("BEATPORT_USERNAME")
    password = os.environ.get("BEATPORT_PASSWORD")
    if not username or not password:
        return None
    from sleeve_notes.beatport_auth import authenticate
    return authenticate(username, password)


def _get_beatport_token() -> str:
    """Return a valid Beatport access token.

    Strategy: live token from DB → refresh via refresh_token → full
    re-auth via BEATPORT_USERNAME/PASSWORD. Raises SourceUnavailable only
    if no tokens AND no credentials.
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
                pass
        fresh = _full_reauth_beatport()
        if fresh:
            _save_beatport_tokens(fresh)
            return fresh["access_token"]
        raise SourceUnavailable(
            "Beatport tokens missing/expired and no BEATPORT_USERNAME/PASSWORD in .env — "
            "run `sleeve-notes auth-beatport` to authorize"
        )


def _beatport_track_artist_title(t: dict) -> tuple[str, str]:
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
            resp = _SESSION.get(
                f"{BEATPORT_BASE}/catalog/search/",
                params={"q": q, "type": "tracks", "per_page": 10},
                headers=headers,
                timeout=20,
            )
        except Exception:
            continue
        if resp.status_code == 401:
            return None
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
        except ValueError:
            continue
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
        return None
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
    """Fire every untried source in parallel, return a merged cache entry."""
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
                new_unavailable.append(src)
                continue
            except Exception as e:
                new_errored.append(f"{src}: {e}")
                continue
            tried.add(src)
            if hit:
                sources[src] = hit

    out = {
        "sources": sources,
        "sources_tried": sorted(tried),
        "sources_unavailable": sorted(new_unavailable),
        "sources_errored": sorted(new_errored),
    }
    spot_id = (sources.get("reccobeats") or {}).get("spotify_track_id")
    if spot_id:
        out["spotify_track_id"] = spot_id
    return out


def consense(entry: dict) -> dict:
    """Derive canonical (bpm, key, confidence) from per-source results."""
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
    by_val = sorted(hits, key=lambda x: x[1])
    clusters: list[list[tuple[str, int]]] = [[by_val[0]]]
    for name, val in by_val[1:]:
        if val - clusters[-1][-1][1] <= tolerance:
            clusters[-1].append((name, val))
        else:
            clusters.append([(name, val)])
    clusters.sort(key=lambda c: -len(c))
    strict_winner = clusters[0]
    strict_size = len(strict_winner)

    # Always compute octave-aware consensus in parallel. Half/double-time
    # confusion is the dominant failure mode of algorithmic BPM detection: two
    # sources can strict-cluster on a doubled reading while three other sources
    # agree on the real tempo. We can't catch that by only running octave after
    # strict fails.
    octave = _octave_consensus(hits, tolerance=2)
    octave_size = len(octave[1]) if octave else 0

    # Trust strict consensus when it's already strong (≥3 sources within ±1
    # BPM). Don't downgrade those to "octave" confidence just because some
    # other source happens to fold into the same canonical band.
    if strict_size >= 3:
        sorted_vals = sorted(v for _, v in strict_winner)
        median = sorted_vals[len(sorted_vals) // 2]
        return median, "high", sorted(n for n, _ in strict_winner)

    # Strict pair vs octave: octave wins if it gathers strictly more sources
    # (e.g. Toto – Rosanna: strict {rb,dz}=166 size 2 vs octave {sb=82, rb→83,
    # dz→83} size 3 — the doubled reading loses).
    if octave_size > strict_size:
        bpm, sources = octave
        return bpm, "octave", sources

    if strict_size >= 2:
        sorted_vals = sorted(v for _, v in strict_winner)
        median = sorted_vals[len(sorted_vals) // 2]
        return median, "high", sorted(n for n, _ in strict_winner)

    priority = {s: i for i, s in enumerate(SOURCE_PRIORITY)}
    by_priority = sorted(hits, key=lambda x: priority.get(x[0], 99))
    return by_priority[0][1], "disputed", [by_priority[0][0]]


def _canonical_octave(v: int, lo: int = 80, hi: int = 160) -> int:
    """Halve/double a BPM into a canonical [lo, hi] range.

    Range chosen so that genuinely slow ballads (Toto – Rosanna at ~83, disco
    cuts at ~85) stay in their own octave instead of being folded up. Tracks
    above 160 (DnB, hardcore) will be folded to half-time when an octave-twin
    is present — acceptable trade-off for a house/techno-leaning collection.
    """
    while v < lo:
        v *= 2
    while v > hi:
        v //= 2
    return v


def _octave_consensus(
    hits: list[tuple[str, int]], tolerance: int = 2
) -> tuple[int, list[str]] | None:
    """Cluster hits treating half/double-time readings as the same BPM.

    Two hits agree if their canonical-octave forms (folded into [90, 180]) are
    within ``tolerance`` of each other. Returns the median canonical value of
    the largest connected component, or ``None`` if no component has ≥2 hits.
    """
    n = len(hits)
    canon = [_canonical_octave(v) for _, v in hits]

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if abs(canon[i] - canon[j]) <= tolerance:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[ra] = rb

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    best = max(groups.values(), key=len)
    if len(best) < 2:
        return None
    canon_vals = sorted(canon[i] for i in best)
    median = canon_vals[len(canon_vals) // 2]
    sources = sorted(hits[i][0] for i in best)
    return median, sources


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
# Continuous-mix filter, cache I/O, and overrides
# ============================================================================

def load_overrides(conn) -> tuple[dict, dict, list[str]]:
    """Load overrides from the DB into two lookup tables.

    Returns (by_release_position, by_track_key, warnings).
      by_release_position: dict[(release_id, position), entry] — most precise
      by_track_key:        dict[cache_key(artist, title), entry] — broader
    """
    by_rp: dict[tuple[int, str], dict] = {}
    by_tk: dict[str, dict] = {}
    warnings: list[str] = []

    rows = conn.execute(
        "SELECT id, release_id, position, artist, title, bpm, key_camelot, note "
        "FROM overrides"
    ).fetchall()

    for row in rows:
        entry = {
            "id": row["id"],
            "release_id": row["release_id"],
            "position": row["position"],
            "artist": row["artist"],
            "title": row["title"],
            "bpm": row["bpm"],
            "key_camelot": row["key_camelot"],
            "note": row["note"],
        }

        if entry["bpm"] is not None:
            try:
                bpm_int = int(round(float(entry["bpm"])))
            except (TypeError, ValueError):
                warnings.append(f"override #{row['id']}: invalid bpm {entry['bpm']!r}")
                continue
            if not (BPM_MIN <= bpm_int <= BPM_MAX):
                warnings.append(
                    f"override #{row['id']}: bpm {bpm_int} outside {BPM_MIN}-{BPM_MAX}"
                )
                continue
            entry["bpm"] = bpm_int

        raw_key = entry.get("key_camelot")
        if raw_key is not None:
            normalized = parse_key_to_camelot(str(raw_key))
            if normalized is None:
                warnings.append(
                    f"override #{row['id']}: unrecognised key_camelot {raw_key!r}"
                )
                continue
            entry["key_camelot"] = normalized

        if entry["release_id"] is not None and entry["position"] is not None:
            by_rp[(int(entry["release_id"]), str(entry["position"]))] = entry
        elif entry["artist"] and entry["title"]:
            by_tk[cache_key(str(entry["artist"]), str(entry["title"]))] = entry
        else:
            warnings.append(
                f"override #{row['id']}: must have (release_id, position) or (artist, title)"
            )

    return by_rp, by_tk, warnings


def override_to_result(entry: dict) -> dict:
    """Convert a validated override entry into a final-result dict."""
    base = {
        "source": "manual",
        "bpm_confidence": "manual",
        "key_confidence": "manual" if entry.get("key_camelot") else None,
        "bpm_sources": ["manual"],
        "key_sources": ["manual"] if entry.get("key_camelot") else [],
        "source_url": None,
    }
    bpm = entry.get("bpm")
    key_cam = parse_key_to_camelot(entry.get("key_camelot")) if entry.get("key_camelot") else None
    if bpm is None and key_cam is None:
        return {**base, "bpm": None, "key_camelot": None, "reason": "manual_no_bpm"}
    return {**base, "bpm": bpm, "key_camelot": key_cam, "reason": "manual"}


# Threshold above which a Beatport/Deezer/etc. URL shared across distinct
# cache_keys is read as a "remix collapse" (the matcher returned the base
# track for every remix variant we queried). 3+ is the sweet spot: it lets
# legitimate vocal/instrumental pairs (which always share a single recording)
# pass through, while catching the multi-remix patterns (Salt-N-Pepa "Push It
# (Again)" → 9 cache_keys, Klubbheads "Kickin' Hard" → 5, etc.).
URL_SHARE_SUSPECT_THRESHOLD = 3

_URL_SHARE_CACHE: dict[int, dict[tuple[str, str], int]] = {}


def url_share_counts(conn) -> dict[tuple[str, str], int]:
    """How many distinct cache_keys each (source, url) pair appears under.

    Cached per-connection. Call ``invalidate_url_share_counts`` after writing
    to ``bpm_source_hits`` so the next read reflects the new data.
    """
    cid = id(conn)
    cached = _URL_SHARE_CACHE.get(cid)
    if cached is not None:
        return cached
    rows = conn.execute(
        "SELECT source, url, COUNT(DISTINCT cache_key) AS n "
        "FROM bpm_source_hits WHERE url IS NOT NULL AND url != '' "
        "GROUP BY source, url"
    ).fetchall()
    counts: dict[tuple[str, str], int] = {(r[0], r[1]): r[2] for r in rows}
    _URL_SHARE_CACHE[cid] = counts
    return counts


def invalidate_url_share_counts(conn=None) -> None:
    if conn is None:
        _URL_SHARE_CACHE.clear()
    else:
        _URL_SHARE_CACHE.pop(id(conn), None)


def load_cache_entry(conn, ck: str) -> dict | None:
    """Reconstruct the v2-style cache entry shape from DB rows."""
    row = conn.execute(
        "SELECT sources_tried FROM bpm_cache WHERE cache_key = ?", (ck,)
    ).fetchone()
    if not row:
        return None
    sources: dict[str, dict] = {}
    for sr in conn.execute(
        "SELECT source, bpm, key_camelot, score, url, mbid "
        "FROM bpm_source_hits WHERE cache_key = ?",
        (ck,),
    ):
        hit = {
            "bpm": sr["bpm"],
            "key_camelot": sr["key_camelot"],
            "score": sr["score"],
            "url": sr["url"],
        }
        if sr["mbid"]:
            hit["mbid"] = sr["mbid"]
        sources[sr["source"]] = {k: v for k, v in hit.items() if v is not None}
    try:
        tried = json.loads(row["sources_tried"] or "[]")
    except json.JSONDecodeError:
        tried = []
    return {"sources": sources, "sources_tried": tried}


def save_cache_entry(conn, ck: str, artist: str, title: str, entry: dict) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO bpm_cache (cache_key, artist, title, sources_tried, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (cache_key) DO UPDATE SET
            artist = COALESCE(bpm_cache.artist, excluded.artist),
            title  = COALESCE(bpm_cache.title,  excluded.title),
            sources_tried = excluded.sources_tried,
            updated_at = excluded.updated_at
        """,
        (
            ck,
            artist,
            title,
            json.dumps(entry.get("sources_tried") or []),
            now,
        ),
    )
    for src, hit in (entry.get("sources") or {}).items():
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
                ck,
                src,
                hit.get("bpm"),
                hit.get("key_camelot"),
                hit.get("score"),
                hit.get("url"),
                hit.get("mbid"),
            ),
        )


def build_track_result(
    position: str, artist: str, title: str, entry: dict,
    *, share_counts: dict[tuple[str, str], int] | None = None,
) -> dict:
    """Combine a cache entry with derived consensus into a per-track result dict.

    When ``share_counts`` is provided (mapping ``(source, url) → cache_key
    count``), tracks where every winning consensus source has a URL shared
    across ≥``URL_SHARE_SUSPECT_THRESHOLD`` cache_keys are flagged as
    ``"shared"`` confidence. This catches the remix-collapse pattern where
    multiple distinct remixes were matched to the same base-track BPM.
    """
    derived = consense(entry)
    sources = entry.get("sources") or {}
    bpm = derived["bpm"]
    if bpm is None:
        all_tried = set(ALL_SOURCES).issubset(set(entry.get("sources_tried") or []))
        reason = "exhausted" if all_tried else "incomplete"
    else:
        reason = "match"

    bpm_confidence = derived["bpm_confidence"]
    shared_siblings = 0
    if share_counts and derived["bpm_sources"] and bpm_confidence != "manual":
        share_per_src = [
            share_counts.get((s, (sources.get(s) or {}).get("url") or ""), 1)
            for s in derived["bpm_sources"]
        ]
        if share_per_src and min(share_per_src) >= URL_SHARE_SUSPECT_THRESHOLD:
            shared_siblings = max(share_per_src)
            bpm_confidence = "shared"

    return {
        "position": position,
        "artist": artist,
        "title": title,
        "bpm": bpm,
        "key_camelot": derived["key_camelot"],
        "bpm_confidence": bpm_confidence,
        "key_confidence": derived["key_confidence"],
        "bpm_sources": derived["bpm_sources"],
        "key_sources": derived["key_sources"],
        "source": derived["bpm_sources"][0] if derived["bpm_sources"] else None,
        "source_url": (sources.get(derived["bpm_sources"][0]) or {}).get("url")
            if derived["bpm_sources"] else None,
        "shared_siblings": shared_siblings,
        "reason": reason,
    }


def derive_track_result(
    conn,
    release_id: int,
    position: str,
    artist: str,
    title: str,
    overrides_rp: dict,
    overrides_tk: dict,
) -> dict:
    """Compute the per-track BPM result without running the cascade.

    Used by the PDF renderer to pull final values out of the cache + overrides
    without re-querying the network. Returns the same shape as
    ``build_track_result`` would, plus a manual-override short-circuit.
    """
    base = {"position": position, "artist": artist, "title": title}
    override = overrides_rp.get((release_id, position))
    if override is None:
        ck = cache_key(artist, title)
        if ck in overrides_tk:
            override = overrides_tk[ck]
    if override is not None:
        return {**base, **override_to_result(override)}

    entry = load_cache_entry(conn, cache_key(artist, title))
    if entry is None:
        entry = {"sources": {}, "sources_tried": []}
    return build_track_result(
        position, artist, title, entry,
        share_counts=url_share_counts(conn),
    )


# ============================================================================
# Main loop
# ============================================================================

def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers", type=int, default=8,
        help="Concurrent tracks to cascade (default: 8). The per-host "
             "RateLimiter caps real throughput at the slowest source "
             "(MusicBrainz ~1 req/s), so values above ~8 give diminishing "
             "returns.",
    )
    args = parser.parse_args(argv)

    with dbmod.session() as conn:
        rl = RateLimiter()

        by_rp, by_tk, override_warnings = load_overrides(conn)
        for w in override_warnings:
            print(f"  override warning: {w}", file=sys.stderr)
        if by_rp or by_tk:
            print(f"Loaded {len(by_rp) + len(by_tk)} override(s) from DB.")
        used_overrides: set[tuple] = set()

        releases = conn.execute(
            "SELECT r.id, r.artist, r.title FROM releases r "
            "WHERE EXISTS (SELECT 1 FROM tracks t WHERE t.release_id = r.id) "
            "ORDER BY r.id"
        ).fetchall()
        if not releases:
            print(
                "ERROR: no releases with tracks found. Run `sleeve-notes fetch` first.",
                file=sys.stderr,
            )
            return 2

        total_tracks = conn.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]

        # Pass 1: classify every track without doing any network I/O.
        # Anything that needs the cascade lands in `worklist`; everything
        # else (override / fully-cached) is accounted for straight away.
        cached_hits = 0
        override_hits = 0
        worklist: list[dict] = []
        for release in releases:
            rid = release["id"]
            release_artist = release["artist"] or "V/A"
            tracks = conn.execute(
                "SELECT position, artist, title "
                "FROM tracks WHERE release_id = ? ORDER BY position",
                (rid,),
            ).fetchall()
            for track in tracks:
                artist = track["artist"] or release_artist
                title = track["title"] or ""
                position = track["position"] or ""

                override = by_rp.get((rid, position))
                override_id: tuple | None = None
                if override is not None:
                    override_id = ("rp", rid, position)
                else:
                    tk = cache_key(artist, title)
                    if tk in by_tk:
                        override = by_tk[tk]
                        override_id = ("tk", tk)
                if override is not None:
                    used_overrides.add(override_id)
                    override_hits += 1
                    continue

                ck = cache_key(artist, title)
                cached = load_cache_entry(conn, ck)
                tried = set((cached or {}).get("sources_tried") or [])
                if cached and set(ALL_SOURCES).issubset(tried):
                    cached_hits += 1
                    continue

                worklist.append({
                    "rid": rid, "position": position,
                    "artist": artist, "title": title,
                    "cache_key": ck, "cached": cached,
                })

        print(
            f"Looking up BPM/key for {total_tracks} tracks across "
            f"{len(releases)} releases "
            f"({cached_hits} fully cached, {override_hits} overridden, "
            f"{len(worklist)} to cascade with {args.workers} workers)..."
        )

        # Pass 2: cascade in parallel. Workers do pure network work and
        # return entries; the main thread is the sole DB writer.
        hits_by_source: dict[str, int] = {s: 0 for s in ALL_SOURCES}
        done_cascade = 0

        if worklist:
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futures = {
                    ex.submit(cascade_all, w["artist"], w["title"], w["cached"], rl): w
                    for w in worklist
                }
                for fut in as_completed(futures):
                    w = futures[fut]
                    try:
                        entry = fut.result()
                    except Exception as e:
                        print(
                            f"  ERROR cascading {w['artist']} - {w['title']}: {e}",
                            file=sys.stderr,
                        )
                        continue
                    save_cache_entry(conn, w["cache_key"], w["artist"], w["title"], entry)
                    spot_id = entry.get("spotify_track_id")
                    if spot_id:
                        conn.execute(
                            "UPDATE tracks SET spotify_track_id = COALESCE(spotify_track_id, ?) "
                            "WHERE release_id = ? AND position = ?",
                            (spot_id, w["rid"], w["position"]),
                        )
                    done_cascade += 1
                    cached_src = (w["cached"] or {}).get("sources") or {}
                    for src in entry.get("sources") or {}:
                        if src not in cached_src:
                            hits_by_source[src] = hits_by_source.get(src, 0) + 1
                    if done_cascade % 5 == 0 or done_cascade == len(worklist):
                        conn.commit()
                    result = build_track_result(
                        w["position"], w["artist"], w["title"], entry
                    )
                    if result["bpm"]:
                        marker = {
                            "high": "●●", "octave": "●○",
                            "shared": "●≈",
                            "single": "○", "disputed": "?",
                        }.get(result["bpm_confidence"], "·")
                        key_str = (
                            f" key={result['key_camelot']}"
                            if result["key_camelot"] else ""
                        )
                        print(
                            f"  [{done_cascade}/{len(worklist)}] {marker} "
                            f"{w['artist']} - {w['title']} -> "
                            f"{result['bpm']}{key_str}  "
                            f"sources={result['bpm_sources']}"
                        )
                    if done_cascade % 25 == 0 or done_cascade == len(worklist):
                        hits_str = " ".join(
                            f"{s}={hits_by_source[s]}" for s in ALL_SOURCES
                        )
                        print(
                            f"  [{done_cascade}/{len(worklist)}] progress: {hits_str}"
                        )

        conn.commit()

        # Stats summary across all DJ tracks (derived after cache fully populated).
        found_bpm = 0
        found_key = 0
        high_conf = 0
        octave_conf = 0
        shared_conf = 0
        disputed = 0
        for release in releases:
            rid = release["id"]
            release_artist = release["artist"] or "V/A"
            tracks = conn.execute(
                "SELECT position, artist, title "
                "FROM tracks WHERE release_id = ?",
                (rid,),
            ).fetchall()
            for track in tracks:
                tr = derive_track_result(
                    conn, rid, track["position"],
                    track["artist"] or release_artist,
                    track["title"] or "",
                    by_rp, by_tk,
                )
                if tr.get("bpm"):
                    found_bpm += 1
                if tr.get("key_camelot"):
                    found_key += 1
                conf = tr.get("bpm_confidence")
                if conf == "high":
                    high_conf += 1
                elif conf == "octave":
                    octave_conf += 1
                elif conf == "shared":
                    shared_conf += 1
                elif conf == "disputed":
                    disputed += 1

        hits_str = ", ".join(f"{s}={hits_by_source[s]}" for s in ALL_SOURCES)
        print(
            f"Cache populated: {found_bpm}/{total_tracks} BPMs "
            f"({high_conf} multi-source consensus, {octave_conf} octave-matched, "
            f"{shared_conf} shared-URL, {disputed} disputed), "
            f"{found_key}/{total_tracks} keys."
        )
        print(
            f"  new this run by source: {hits_str}   "
            f"(cached hits: {cached_hits}, overrides: {override_hits})"
        )

        unused_rp = [k for k in by_rp if ("rp", k[0], k[1]) not in used_overrides]
        unused_tk = [k for k in by_tk if ("tk", k) not in used_overrides]
        if unused_rp or unused_tk:
            print(
                f"  WARNING: {len(unused_rp) + len(unused_tk)} override entry/entries "
                "matched nothing — check via `sleeve-notes overrides list`:",
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
