"""Look up BPM per track via a 3-source cascade.

Sources, in order:
  1. songbpm.com  — direct HTML scrape of canonical detail pages (no auth).
  2. Deezer       — public JSON API at api.deezer.com (no auth).
  3. AcousticBrainz — open dataset, looked up via MusicBrainz recording IDs.
                     Frozen since 2022, but excellent coverage for older
                     electronic releases.

All three are validated against the queried artist+title via fuzzy match,
and results are cached in .tmp/bpm_cache.json. Each cache entry tracks which
sources have already been tried, so re-runs only hit the sources that haven't
exhausted yet. Once all three sources have been tried without a hit, the
entry is marked `exhausted: True` and never retried.
"""

from __future__ import annotations

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
from rapidfuzz import fuzz
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"
DJ_IN = TMP / "dj_releases.json"
BPM_OUT = TMP / "bpm_results.json"
BPM_CACHE = TMP / "bpm_cache.json"

# Sources, in the order they are tried.
ALL_SOURCES = ("songbpm", "deezer", "acousticbrainz")

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

# --- Validation thresholds ----------------------------------------------------
ARTIST_TITLE_MIN_FUZZ = 70.0
BPM_MIN, BPM_MAX = 50, 250
CONTINUOUS_MIX_SECONDS = 12 * 60

TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)
BPM_RE = re.compile(r"\b(\d{2,3})\s*BPM\b", re.IGNORECASE)
TITLE_PARSE_RE = re.compile(r"BPM and key for (.+?) by (.+?) \|", re.IGNORECASE)


class RetryableHTTPError(Exception):
    pass


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
# Source 3: MusicBrainz + AcousticBrainz
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

    for source in to_try:
        fn = _LOOKUP_FNS[source]
        try:
            result = fn(artist, title, rl)
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
    if not position:
        return False
    bare_side = re.fullmatch(r"[A-Z]+", position.strip())
    if bare_side and (duration_s is None or duration_s > CONTINUOUS_MIX_SECONDS):
        return True
    if duration_s is not None and duration_s > CONTINUOUS_MIX_SECONDS * 2:
        return True
    return False


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

    total_tracks = sum(len(r["tracks"]) for r in releases)
    print(f"Looking up BPM for {total_tracks} tracks across {len(releases)} releases...")
    done = 0
    cached_hits = 0
    new_hits_by_source: dict[str, int] = {s: 0 for s in ALL_SOURCES}

    for release in releases:
        track_results: list[dict] = []
        for track in release["tracks"]:
            done += 1
            artist = track.get("artist") or release["artist"]
            title = track.get("title", "")
            position = track.get("position", "")
            duration_s = track.get("duration_s")

            if is_continuous_mix(position, duration_s):
                track_results.append({
                    "position": position, "artist": artist, "title": title,
                    "bpm": None, "confidence": None, "reason": "continuous_mix", "source_url": None,
                })
                continue

            key = cache_key(artist, title)
            cached = cache.get(key)

            # Hit or fully-exhausted miss: serve from cache.
            if cached and (cached.get("bpm") or cached.get("exhausted")):
                if cached.get("bpm"):
                    cached_hits += 1
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
    print(f"  new this run by source: {hits_str}   (cached hits: {cached_hits})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
