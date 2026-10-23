"""Measure BPM from a strictly matched recording of the track.

Where the audio comes from, in order (measured Oct 2026 on the 227 tracks of
a real collection that reach audio analysis, i.e. have at most one metadata
BPM): the release's own Discogs videos (76% of them), then an iTunes preview
(+35 tracks), then YouTube search. Deezer previews, Hard Wax snippets and
Bandcamp added two tracks between them, Bandcamp with wrong-version matches,
so they're not in the chain. See roadmap.md, BPM-bronnen onderzoek.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
from rapidfuzz import fuzz


YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
# Apple allows roughly 20 search calls a minute.
ITUNES_RATE_S = 3.2
MAX_PREVIEW_BYTES = 5 * 1024 * 1024
MIN_MATCH_SCORE = 90.0
MIN_BEAT_COUNT = 16
MIN_ESSENTIA_CONFIDENCE = 1.5
MAX_VIDEO_DURATION_S = 15 * 60
ANALYSIS_START_S = 30
ANALYSIS_DURATION_S = 120


class AudioAnalysisUnavailable(Exception):
    """The source could not run; callers should retry on a later cascade."""


def _normalized(value: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in value.lower()).split())


def youtube_match_score(artist: str, title: str, video_title: str) -> float:
    """Require both artist and title to occur strongly in the video title."""
    candidate = _normalized(video_title)
    artist_score = fuzz.partial_ratio(_normalized(artist), candidate)
    title_score = fuzz.partial_ratio(_normalized(title), candidate)
    return min(float(artist_score), float(title_score))


DURATION_TOLERANCE = 0.10
_YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})")


def find_discogs_video(
    title: str, videos: list[dict] | None, duration_s: int | None = None,
) -> dict | None:
    """Pick the community-linked Discogs video that is this track.

    Discogs videos are attached to the exact release, so the artist check of
    the search route is dropped, but the title must still match strongly and,
    when both durations are known, the lengths must agree: a release often
    carries a full-EP upload or a live set next to the track's own video.
    """
    needle = _normalized(title)
    if not needle:
        return None
    best: tuple[float, dict] | None = None
    for v in videos or []:
        m = _YOUTUBE_ID_RE.search(v.get("uri") or "")
        if not m:
            continue
        score = float(fuzz.partial_ratio(needle, _normalized(v.get("title") or "")))
        if score < MIN_MATCH_SCORE:
            continue
        vid_dur = v.get("duration")
        if duration_s and vid_dur and abs(vid_dur - duration_s) > DURATION_TOLERANCE * duration_s:
            continue
        candidate = {"id": m.group(1), "title": v.get("title") or "", "score": score, "origin": "discogs_video"}
        if best is None or score > best[0]:
            best = (score, candidate)
    return best[1] if best else None


def find_youtube_video(artist: str, title: str) -> dict | None:
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise AudioAnalysisUnavailable("missing YOUTUBE_API_KEY")

    try:
        response = requests.get(
            YOUTUBE_SEARCH_URL,
            params={
                "q": f'"{artist}" "{title}" audio',
                "type": "video",
                "maxResults": 5,
                "part": "id,snippet",
                "videoCategoryId": "10",
                "key": api_key,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise AudioAnalysisUnavailable(f"YouTube search failed: {exc}") from exc

    if response.status_code in (429, 500, 502, 503, 504):
        raise AudioAnalysisUnavailable(f"YouTube search returned {response.status_code}")
    if response.status_code != 200:
        raise AudioAnalysisUnavailable(f"YouTube search returned {response.status_code}")

    best: tuple[float, dict] | None = None
    for item in response.json().get("items") or []:
        video_id = (item.get("id") or {}).get("videoId")
        video_title = (item.get("snippet") or {}).get("title") or ""
        if not video_id:
            continue
        score = youtube_match_score(artist, title, video_title)
        if score < MIN_MATCH_SCORE:
            continue
        candidate = {"id": video_id, "title": video_title, "score": score}
        if best is None or score > best[0]:
            best = (score, candidate)
    return best[1] if best else None


def find_itunes_preview(
    artist: str, title: str, duration_s: int | None = None, rl=None,
) -> dict | None:
    """A keyless 30-second preview, matched on artist and title separately.

    token_set_ratio per field rather than partial_ratio on a joined string:
    iTunes keeps artist and track name apart, and comparing them apart keeps
    "Threee (Deetron Mix)" from matching "Threee (Deetron Beeetz)".
    """
    if rl is not None:
        rl.wait("itunes", ITUNES_RATE_S)
    try:
        response = requests.get(
            ITUNES_SEARCH_URL,
            params={"term": f"{artist} {title}", "entity": "song", "limit": 10},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise AudioAnalysisUnavailable(f"iTunes search failed: {exc}") from exc
    if response.status_code != 200:
        raise AudioAnalysisUnavailable(f"iTunes search returned {response.status_code}")

    best: tuple[float, dict] | None = None
    for item in response.json().get("results") or []:
        preview_url = item.get("previewUrl")
        if not preview_url:
            continue
        score = min(
            float(fuzz.token_set_ratio(_normalized(artist), _normalized(item.get("artistName") or ""))),
            float(fuzz.token_set_ratio(_normalized(title), _normalized(item.get("trackName") or ""))),
        )
        if score < MIN_MATCH_SCORE:
            continue
        length = (item.get("trackTimeMillis") or 0) / 1000
        if duration_s and length and abs(length - duration_s) > DURATION_TOLERANCE * duration_s:
            continue
        candidate = {
            "preview_url": preview_url,
            "url": item.get("trackViewUrl") or preview_url,
            "title": f"{item.get('artistName')} - {item.get('trackName')}",
            "score": score,
            "origin": "itunes_preview",
        }
        if best is None or score > best[0]:
            best = (score, candidate)
    return best[1] if best else None


def _download_preview(url: str, target_dir: Path) -> Path:
    target = target_dir / "source.m4a"
    try:
        with requests.get(url, stream=True, timeout=30) as response:
            if response.status_code != 200:
                raise AudioAnalysisUnavailable(f"preview download returned {response.status_code}")
            size = 0
            with open(target, "wb") as fh:
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_PREVIEW_BYTES:
                        raise AudioAnalysisUnavailable("preview larger than expected")
                    fh.write(chunk)
    except requests.RequestException as exc:
        raise AudioAnalysisUnavailable(f"preview download failed: {exc}") from exc
    return target


def _download_audio(video_id: str, target_dir: Path) -> Path:
    output = target_dir / "source.%(ext)s"
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--quiet",
        "--no-warnings",
        "--no-playlist",
        "--max-filesize",
        "30M",
        "--match-filter",
        f"duration <= {MAX_VIDEO_DURATION_S}",
        "-f",
        "bestaudio[filesize<30M]/bestaudio[filesize_approx<30M]/worstaudio",
        "-o",
        str(output),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioAnalysisUnavailable(f"YouTube audio download failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        message = detail[-1] if detail else "unknown yt-dlp error"
        raise AudioAnalysisUnavailable(f"YouTube audio download failed: {message}")
    files = [p for p in target_dir.glob("source.*") if p.is_file()]
    if not files:
        raise AudioAnalysisUnavailable("YouTube audio download produced no file")
    return files[0]


def _to_analysis_wav(source: Path, target: Path, start_s: int = ANALYSIS_START_S) -> None:
    if not shutil.which("ffmpeg"):
        raise AudioAnalysisUnavailable("ffmpeg is not installed")
    command = [
        "ffmpeg", "-y", "-loglevel", "error", "-ss", str(start_s),
        "-i", str(source), "-t", str(ANALYSIS_DURATION_S), "-ac", "1", "-ar", "44100",
        str(target),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioAnalysisUnavailable(f"audio conversion failed: {exc}") from exc
    if result.returncode != 0:
        raise AudioAnalysisUnavailable("ffmpeg could not decode the downloaded audio")


def measure_bpm(path: Path) -> tuple[float, float, int]:
    """Return (bpm, confidence, beat_count) from Essentia's RhythmExtractor2013.

    Multifeature is the method AcousticBrainz ran on. Its confidence runs 0 to
    ~5.3; in the Sep 2026 benchmark every correct reading scored above 1.7 and
    both wrong ones below 0.6. It does not catch a confident 3:2 misreading
    (Robert Hood "Minus": 92.5 at 3.13 against a real 139) — that is left to the
    cascade's consensus with the metadata sources.
    """
    try:
        import essentia.standard as es
    except ImportError as exc:
        raise AudioAnalysisUnavailable("essentia is not installed") from exc

    try:
        audio = es.MonoLoader(filename=str(path), sampleRate=44100)()
        bpm, beats, confidence, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
    except Exception as exc:
        raise AudioAnalysisUnavailable(f"audio analysis failed: {exc}") from exc
    return float(bpm), float(confidence), len(beats)


def audio_lookup(
    artist: str, title: str, *,
    discogs_videos: list[dict] | None = None, duration_s: int | None = None, rl=None,
) -> dict | None:
    if os.environ.get("AUDIO_ANALYSIS_ENABLED", "").lower() not in ("1", "true", "yes"):
        raise AudioAnalysisUnavailable("audio analysis is disabled")

    match = find_discogs_video(title, discogs_videos, duration_s)
    if match is None:
        match = find_itunes_preview(artist, title, duration_s, rl)
    if match is None:
        match = find_youtube_video(artist, title)
    if match is None:
        return None
    with tempfile.TemporaryDirectory(prefix="sleeve-notes-audio-") as tmp:
        tmp_path = Path(tmp)
        wav = tmp_path / "analysis.wav"
        if match.get("preview_url"):
            # A preview is only 30 s long, so the usual skip past the intro
            # would leave nothing to analyse.
            _to_analysis_wav(_download_preview(match["preview_url"], tmp_path), wav, start_s=0)
        else:
            _to_analysis_wav(_download_audio(match["id"], tmp_path), wav)
        bpm, confidence, beat_count = measure_bpm(wav)

    if confidence < MIN_ESSENTIA_CONFIDENCE or beat_count < MIN_BEAT_COUNT or not (50 <= bpm <= 250):
        return None
    return {
        "bpm": int(round(bpm)),
        "score": round(match["score"], 1),
        "analysis_confidence": round(confidence, 2),
        "beat_count": beat_count,
        "url": match.get("url") or f"https://www.youtube.com/watch?v={match['id']}",
        "matched_title": match["title"],
        "origin": match.get("origin", "youtube_search"),
    }
