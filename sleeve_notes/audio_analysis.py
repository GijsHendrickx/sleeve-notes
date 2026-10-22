"""Measure BPM from a strictly matched YouTube recording."""

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
MIN_MATCH_SCORE = 90.0
MIN_BEAT_COUNT = 16
MIN_BEAT_STABILITY = 0.75
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
        candidate = {"id": m.group(1), "title": v.get("title") or "", "score": score, "origin": "discogs"}
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


def _to_analysis_wav(source: Path, target: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise AudioAnalysisUnavailable("ffmpeg is not installed")
    command = [
        "ffmpeg", "-y", "-loglevel", "error", "-ss", str(ANALYSIS_START_S),
        "-i", str(source), "-t", str(ANALYSIS_DURATION_S), "-ac", "1", "-ar", "44100",
        str(target),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioAnalysisUnavailable(f"audio conversion failed: {exc}") from exc
    if result.returncode != 0:
        raise AudioAnalysisUnavailable("ffmpeg could not decode the YouTube audio")


def measure_bpm(path: Path) -> tuple[float, float, int]:
    try:
        import librosa
        import numpy as np
    except ImportError as exc:
        raise AudioAnalysisUnavailable("librosa is not installed") from exc

    try:
        audio, sample_rate = librosa.load(path, sr=44100, mono=True)
        onset = librosa.onset.onset_strength(y=audio, sr=sample_rate, aggregate=np.median)
        tempo, beats = librosa.beat.beat_track(
            onset_envelope=onset,
            sr=sample_rate,
            trim=False,
            units="time",
        )
        bpm = float(np.atleast_1d(tempo)[0])
        intervals = np.diff(beats)
        if len(intervals) == 0:
            return bpm, 0.0, len(beats)
        median = float(np.median(intervals))
        stable = intervals[np.abs(intervals - median) <= 0.12 * median]
        stability = float(len(stable) / len(intervals))
        if len(stable) >= MIN_BEAT_COUNT - 1:
            bpm = float(60.0 / np.mean(stable))
    except Exception as exc:
        raise AudioAnalysisUnavailable(f"audio analysis failed: {exc}") from exc
    return bpm, stability, len(beats)


def youtube_audio_lookup(
    artist: str, title: str, *,
    discogs_videos: list[dict] | None = None, duration_s: int | None = None,
) -> dict | None:
    if os.environ.get("AUDIO_ANALYSIS_ENABLED", "").lower() not in ("1", "true", "yes"):
        raise AudioAnalysisUnavailable("audio analysis is disabled")

    video = find_discogs_video(title, discogs_videos, duration_s)
    if video is None:
        video = find_youtube_video(artist, title)
    if video is None:
        return None
    with tempfile.TemporaryDirectory(prefix="sleeve-notes-audio-") as tmp:
        tmp_path = Path(tmp)
        source = _download_audio(video["id"], tmp_path)
        wav = tmp_path / "analysis.wav"
        _to_analysis_wav(source, wav)
        bpm, stability, beat_count = measure_bpm(wav)

    if stability < MIN_BEAT_STABILITY or beat_count < MIN_BEAT_COUNT or not (50 <= bpm <= 250):
        return None
    return {
        "bpm": int(round(bpm)),
        "score": round(video["score"], 1),
        "analysis_confidence": round(stability, 2),
        "beat_count": beat_count,
        "url": f"https://www.youtube.com/watch?v={video['id']}",
        "video_id": video["id"],
        "video_title": video["title"],
        "video_origin": video.get("origin", "search"),
    }
