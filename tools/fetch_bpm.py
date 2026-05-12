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
import time
import unicodedata
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

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"
DJ_IN = TMP / "dj_releases.json"
BPM_OUT = TMP / "bpm_results.json"
BPM_CACHE = TMP / "bpm_cache.json"
BEATPORT_TOKENS_FILE = TMP / "beatport_tokens.json"
OVERRIDES_FILE = ROOT / "overrides.json"

# Sources, in the order they are tried.
ALL_SOURCES = ("songbpm", "deezer", "reccobeats", "beatport", "acousticbrainz")

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
    """Per-host minimum interval between requests."""
    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def wait(self, host: str, min_interval_s: float) -> None:
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


def songbpm_validate(html: str, q_artist: str, q_title: str) -> tuple[int | None, float]:
    bpm_match = BPM_RE.search(html)
    if not bpm_match:
        return None, 0.0
    bpm = int(bpm_match.group(1))
    if not (BPM_MIN <= bpm <= BPM_MAX):
        return None, 0.0

    title_tag = TITLE_RE.search(html)
    if not title_tag:
        return bpm, 60.0
    m = TITLE_PARSE_RE.search(title_tag.group(1))
    if not m:
        return bpm, 60.0
    page_track, page_artist = m.group(1), m.group(2)
    score = fuzz_score(q_artist, q_title, page_artist, page_track)
    if score < ARTIST_TITLE_MIN_FUZZ:
        return None, score
    return bpm, score


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
        bpm, score = songbpm_validate(html, artist, title)
        if bpm:
            return {
                "bpm": bpm,
                "source": "songbpm",
                "source_url": url,
                "confidence": "ok" if score >= 82 else "low",
                "score": round(score, 1),
                "reason": "match",
            }
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
        "source": "deezer",
        "source_url": detail.get("link") or f"https://www.deezer.com/track/{tid}",
        "confidence": "ok" if score >= 82 else "low",
        "score": round(score, 1),
        "reason": "match",
    }


# ============================================================================
# Source 3: ReccoBeats (via Spotify Client Credentials for name → ID lookup)
# ============================================================================

_spotify_token: dict | None = None  # {access_token, expires_at}


def _get_spotify_token() -> str:
    """Return a valid Spotify access token, refreshing if needed.

    Raises SourceUnavailable if SPOTIFY_CLIENT_ID/SECRET are not configured.
    """
    global _spotify_token
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
    return {
        "bpm": bpm,
        "source": "reccobeats",
        "source_url": track.get("href") or f"https://open.spotify.com/track/{spot_id}",
        "confidence": "ok" if score >= 82 else "low",
        "score": round(score, 1),
        "reason": "match",
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
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from beatport_auth import authenticate  # type: ignore[import-not-found]

    return authenticate(username, password)


def _get_beatport_token() -> str:
    """Return a valid Beatport access token.

    Strategy, in order:
      1. Live access_token from .tmp/beatport_tokens.json (if not yet expired).
      2. Refresh via refresh_token from disk.
      3. Full re-auth via BEATPORT_USERNAME/PASSWORD (also bootstraps the
         tokens file if it doesn't exist yet).

    Raises SourceUnavailable only if no tokens file exists AND no credentials
    are configured.
    """
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
    if not bpm_raw:
        return None  # Beatport knows the track but doesn't have a BPM (rare).
    try:
        bpm = int(round(float(bpm_raw)))
    except (TypeError, ValueError):
        return None
    if not (BPM_MIN <= bpm <= BPM_MAX):
        return None
    tid = hit.get("id")
    slug = hit.get("slug") or "_"
    return {
        "bpm": bpm,
        "source": "beatport",
        "source_url": f"https://www.beatport.com/track/{slug}/{tid}" if tid else None,
        "confidence": "ok" if score >= 82 else "low",
        "score": round(score, 1),
        "reason": "match",
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
        if not bpm_raw:
            continue
        bpm = int(round(float(bpm_raw)))
        if not (BPM_MIN <= bpm <= BPM_MAX):
            continue
        return {
            "bpm": bpm,
            "source": "acousticbrainz",
            "source_url": f"https://acousticbrainz.org/{mbid}",
            "confidence": "ok" if score >= 82 else "low",
            "score": round(score, 1),
            "reason": "match",
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


def previously_tried(cached: dict | None) -> set[str]:
    """Return the set of sources already attempted for a cached entry.

    Back-compat: cache entries written before the cascade existed have no
    `sources_tried` field. They were produced by the songbpm-only version of
    this script, so we assume {"songbpm"} for them.
    """
    if not cached:
        return set()
    if "sources_tried" in cached:
        return set(cached["sources_tried"])
    if cached.get("source") in ALL_SOURCES:
        return {cached["source"]}
    return {"songbpm"}


def cascade_lookup(artist: str, title: str, cached: dict | None, rl: RateLimiter) -> dict:
    tried = previously_tried(cached)
    to_try = [s for s in ALL_SOURCES if s not in tried]
    skipped: list[str] = []

    for source in to_try:
        fn = _LOOKUP_FNS[source]
        try:
            result = fn(artist, title, rl)
        except SourceUnavailable:
            # Credentials missing: skip without marking as tried. Configuring
            # the source later will retry on the next run.
            skipped.append(source)
            continue
        except Exception as e:
            # Network/parse failure: don't mark as tried, so we retry next run.
            return {
                "bpm": None,
                "source": None,
                "source_url": None,
                "confidence": None,
                "reason": f"error:{source}:{e}",
                "sources_tried": sorted(tried),
            }
        tried.add(source)
        if result:
            return {**result, "sources_tried": sorted(tried)}

    # Only mark exhausted if every source was actually tried (none skipped).
    if skipped:
        return {
            "bpm": None,
            "source": None,
            "source_url": None,
            "confidence": None,
            "reason": "skipped_unavailable:" + ",".join(skipped),
            "sources_tried": sorted(tried),
        }
    return {
        "bpm": None,
        "source": None,
        "source_url": None,
        "confidence": None,
        "reason": "exhausted",
        "sources_tried": sorted(tried),
        "exhausted": True,
    }


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
    """Convert a validated override entry into a cascade-shaped result dict.

    Uses source="manual" so the BPM is traceable in bpm_results.json. The
    PDF generator only inspects `bpm` and `reason` so manual hits render
    identically to source-supplied BPMs.
    """
    if entry.get("continuous_mix"):
        return {
            "bpm": None,
            "source": "manual",
            "source_url": None,
            "confidence": "manual",
            "reason": "continuous_mix",
        }
    bpm = entry.get("bpm")
    if bpm is None:
        return {
            "bpm": None,
            "source": "manual",
            "source_url": None,
            "confidence": "manual",
            "reason": "manual_no_bpm",
        }
    return {
        "bpm": bpm,
        "source": "manual",
        "source_url": None,
        "confidence": "manual",
        "score": None,
        "reason": "manual",
    }


def load_cache() -> dict:
    if BPM_CACHE.exists():
        with BPM_CACHE.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict) -> None:
    tmp = BPM_CACHE.with_suffix(BPM_CACHE.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BPM_CACHE)


def main() -> int:
    if not DJ_IN.exists():
        print(f"ERROR: {DJ_IN} not found. Run filter_dj_releases.py first.", file=sys.stderr)
        return 2

    with DJ_IN.open("r", encoding="utf-8") as f:
        releases = json.load(f)

    TMP.mkdir(parents=True, exist_ok=True)
    cache = load_cache()
    rl = RateLimiter()
    results: list[dict] = []

    by_rp, by_tk, override_warnings = load_overrides()
    for w in override_warnings:
        print(f"  override warning: {w}", file=sys.stderr)
    if by_rp or by_tk:
        print(f"Loaded {len(by_rp) + len(by_tk)} override(s) from {OVERRIDES_FILE.name}.")
    used_overrides: set[tuple] = set()

    total_tracks = sum(len(r["tracks"]) for r in releases)
    print(f"Looking up BPM for {total_tracks} tracks across {len(releases)} releases...")
    done = 0
    cached_hits = 0
    override_hits = 0
    new_hits_by_source: dict[str, int] = {s: 0 for s in ALL_SOURCES}

    for release in releases:
        track_results: list[dict] = []
        for track in release["tracks"]:
            done += 1
            artist = track.get("artist") or release["artist"]
            title = track.get("title", "")
            position = track.get("position", "")
            duration_s = track.get("duration_s")

            # Manual overrides win over everything (cache, continuous-mix
            # detection, the cascade). (release_id, position) is the precise
            # match; artist+title is the fallback.
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
                result = override_to_result(override)
                track_results.append({
                    "position": position, "artist": artist, "title": title, **result,
                })
                continue

            if is_continuous_mix(position, duration_s):
                track_results.append({
                    "position": position, "artist": artist, "title": title,
                    "bpm": None, "confidence": None, "reason": "continuous_mix", "source_url": None,
                })
                continue

            key = cache_key(artist, title)
            cached = cache.get(key)

            # Serve from cache on a hit, or on an exhausted miss IF every current
            # source has actually been tried. When new sources are added (e.g.
            # we extended the cascade from 3→5), old `exhausted: True` entries
            # fall through and cascade tries only the new ones.
            if cached and cached.get("bpm"):
                cached_hits += 1
                track_results.append({"position": position, "artist": artist, "title": title, **cached})
                continue
            if cached and cached.get("exhausted") and set(ALL_SOURCES).issubset(previously_tried(cached)):
                track_results.append({"position": position, "artist": artist, "title": title, **cached})
                continue

            # Cascade through whatever sources are left.
            result = cascade_lookup(artist, title, cached, rl)
            cache[key] = result
            save_cache(cache)
            if result.get("bpm"):
                src = result.get("source") or "?"
                new_hits_by_source[src] = new_hits_by_source.get(src, 0) + 1
                print(f"  [{done}/{total_tracks}] {src}: {artist} - {title} -> {result['bpm']} (score {result.get('score')})")
            track_results.append({"position": position, "artist": artist, "title": title, **result})

            if done % 25 == 0 or done == total_tracks:
                hits_str = " ".join(f"{s}={new_hits_by_source[s]}" for s in ALL_SOURCES)
                print(f"  [{done}/{total_tracks}] progress: cached={cached_hits} {hits_str}")

        results.append({"id": release["id"], "artist": release["artist"], "title": release["title"], "tracks": track_results})

    with BPM_OUT.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    found = sum(1 for r in results for t in r["tracks"] if t.get("bpm"))
    missing = total_tracks - found
    hits_str = ", ".join(f"{s}={new_hits_by_source[s]}" for s in ALL_SOURCES)
    print(f"Wrote {BPM_OUT}: {found}/{total_tracks} BPMs found, {missing} missing.")
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
