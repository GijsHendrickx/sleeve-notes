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
        self.assertEqual(video["origin"], "discogs")

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
