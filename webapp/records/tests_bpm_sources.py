from unittest import mock

from django.test import SimpleTestCase

from sleeve_notes import fetch_bpm
from sleeve_notes.audio_analysis import find_discogs_video


VIDEOS = [
    {"uri": "https://www.youtube.com/watch?v=AAAAAAAAAAA", "title": "Full EP stream", "duration": 1500},
    {"uri": "https://www.youtube.com/watch?v=BBBBBBBBBBB", "title": "Actress - Hubble", "duration": 372},
    {"uri": "https://vimeo.com/123", "title": "Actress - Hubble", "duration": 372},
]


class DiscogsVideoMatchTests(SimpleTestCase):
    def test_matches_title_and_duration(self):
        video = find_discogs_video("Hubble", VIDEOS, 370)
        self.assertEqual(video["id"], "BBBBBBBBBBB")
        self.assertEqual(video["origin"], "discogs_video")

    def test_rejects_duration_mismatch(self):
        self.assertIsNone(find_discogs_video("Hubble", VIDEOS, 200))

    def test_unknown_duration_still_matches_on_title(self):
        self.assertEqual(find_discogs_video("Hubble", VIDEOS, None)["id"], "BBBBBBBBBBB")

    def test_no_title_match(self):
        self.assertIsNone(find_discogs_video("Untitled A1", VIDEOS, 372))

    def test_no_videos(self):
        self.assertIsNone(find_discogs_video("Hubble", None, 372))


class SpotifyThrottleTests(SimpleTestCase):
    def test_429_is_unavailable_not_a_miss(self):
        resp = mock.Mock(status_code=429)
        with mock.patch.object(fetch_bpm, "_get_spotify_token", return_value="t"), \
             mock.patch.object(fetch_bpm._SESSION, "get", return_value=resp):
            with self.assertRaises(fetch_bpm.SourceUnavailable):
                fetch_bpm.reccobeats_lookup("Actress", "Hubble", fetch_bpm.RateLimiter())


def _itunes_response(results, status=200):
    response = mock.Mock(status_code=status)
    response.json.return_value = {"results": results}
    return response


ITUNES_HIT = {
    "artistName": "Roots Panorama", "trackName": "Threee (Deetron Mix)",
    "trackTimeMillis": 420_000, "previewUrl": "https://audio.example/p.m4a",
    "trackViewUrl": "https://music.apple.com/track/1",
}


class ItunesPreviewMatchTests(SimpleTestCase):
    @mock.patch("sleeve_notes.audio_analysis.requests.get")
    def test_strict_match_on_artist_title_and_duration(self, get):
        from sleeve_notes.audio_analysis import find_itunes_preview

        get.return_value = _itunes_response([ITUNES_HIT])
        match = find_itunes_preview("Roots Panorama", "Threee (Deetron Mix)", 425)
        self.assertEqual(match["preview_url"], "https://audio.example/p.m4a")
        self.assertEqual(match["origin"], "itunes_preview")

    @mock.patch("sleeve_notes.audio_analysis.requests.get")
    def test_other_remix_or_length_is_not_a_match(self, get):
        from sleeve_notes.audio_analysis import find_itunes_preview

        get.return_value = _itunes_response([ITUNES_HIT])
        self.assertIsNone(find_itunes_preview("Roots Panorama", "Threee (Deetron Beeetz)", 425))
        self.assertIsNone(find_itunes_preview("Roots Panorama", "Threee (Deetron Mix)", 300))

    @mock.patch("sleeve_notes.audio_analysis.requests.get")
    def test_rate_limit_is_unavailable_not_a_miss(self, get):
        from sleeve_notes.audio_analysis import AudioAnalysisUnavailable, find_itunes_preview

        get.return_value = _itunes_response([], status=429)
        with self.assertRaises(AudioAnalysisUnavailable):
            find_itunes_preview("A", "T")


@mock.patch.dict("os.environ", {"AUDIO_ANALYSIS_ENABLED": "true"})
@mock.patch("sleeve_notes.audio_analysis.measure_bpm", return_value=(124.0, 3.0, 60))
@mock.patch("sleeve_notes.audio_analysis._to_analysis_wav")
class AudioChainOrderTests(SimpleTestCase):
    @mock.patch("sleeve_notes.audio_analysis.find_youtube_video")
    @mock.patch("sleeve_notes.audio_analysis.find_itunes_preview")
    @mock.patch("sleeve_notes.audio_analysis._download_audio")
    def test_discogs_video_wins_and_itunes_is_not_asked(self, download, itunes, youtube, convert, _measure):
        from sleeve_notes.audio_analysis import audio_lookup

        result = audio_lookup("Actress", "Hubble", discogs_videos=VIDEOS, duration_s=370)

        self.assertEqual(result["origin"], "discogs_video")
        itunes.assert_not_called()
        youtube.assert_not_called()

    @mock.patch("sleeve_notes.audio_analysis.find_youtube_video")
    @mock.patch("sleeve_notes.audio_analysis._download_preview")
    @mock.patch("sleeve_notes.audio_analysis.find_itunes_preview")
    def test_itunes_preview_before_youtube_and_analysed_from_the_start(self, itunes, download, youtube, convert, _measure):
        from sleeve_notes.audio_analysis import audio_lookup

        itunes.return_value = {"preview_url": "https://audio.example/p.m4a", "url": "https://music.apple.com/track/1",
                               "title": "A - T", "score": 100.0, "origin": "itunes_preview"}
        result = audio_lookup("A", "T", discogs_videos=[], duration_s=300)

        self.assertEqual(result["origin"], "itunes_preview")
        self.assertEqual(result["url"], "https://music.apple.com/track/1")
        youtube.assert_not_called()
        self.assertEqual(convert.call_args.kwargs.get("start_s"), 0)
