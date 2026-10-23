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


class VersionGuardTests(SimpleTestCase):
    """Fuzzy scores pass a remix whose title contains the original's; these don't."""

    def test_version_words_must_agree(self):
        from sleeve_notes.audio_analysis import same_version

        self.assertFalse(same_version("Droogs", "Rhyw - Droogs (Paleman Remix) [TH08]"))
        self.assertFalse(same_version("God's Child (BBT Dub)", "God's Child (BBT Acappella)"))
        self.assertFalse(same_version("Stop This Dream (Club Mix)", "Stop This Dream (Alternative Club Mix)"))

    def test_spelling_variants_of_one_version_agree(self):
        from sleeve_notes.audio_analysis import same_version

        self.assertTrue(same_version("Gouryella", "Gouryella (Original Mix)"))
        self.assertTrue(same_version("Not Enough (Dub Mix)", "Not Enough (Dub)"))
        self.assertTrue(same_version("Hardstyle Revolution (Showtek Rmx)", "Hardstyle Revolution (Showtek Remix)"))
        self.assertTrue(same_version("Diapason", "Marco Carola - A1 - Diapason"))
        self.assertTrue(same_version("Infected (DJ Errick Mix)", "Barthezz - Infected (DJ Errik remix) [OFFICIAL]"))
        self.assertTrue(same_version('Freaky (The "Bar Heads" Mix', "Lou 2 - Freaky (The Bar Heads Mix)"))

    def test_length_variants_keep_the_tempo(self):
        from sleeve_notes.audio_analysis import same_version

        self.assertTrue(same_version("Hasta Luego Trinidad", "Hasta Luego Trinidad (Long Version)"))
        self.assertTrue(same_version("Damn!", "Damn! (Extended Mix)"))
        self.assertFalse(same_version("Damn!", "Damn! (Remix)"))

    def test_a_production_word_in_the_song_title_is_not_a_tag(self):
        from sleeve_notes.audio_analysis import same_version

        self.assertTrue(same_version("Her Dub Material (Guti Remix)", "Nicole Moudaber - Her Dub Material (Guti Remix) [MOOD]"))
        self.assertFalse(same_version("Her Dub Material (Guti Remix)", "Nicole Moudaber - Her Dub Material (Marino Canal Remix) [MOOD]"))
        self.assertFalse(same_version("Libera (Benjamin Fröhlich Remix)", "Libera"))

    def test_discogs_video_skips_the_remix_upload_on_the_same_release(self):
        videos = [
            {"uri": "https://www.youtube.com/watch?v=RRRRRRRRRRR", "title": "Rhyw - Droogs (Paleman Remix) [TH08]"},
            {"uri": "https://www.youtube.com/watch?v=OOOOOOOOOOO", "title": "Rhyw - Droogs [TH08]"},
        ]
        self.assertEqual(find_discogs_video("Droogs", videos)["id"], "OOOOOOOOOOO")

    @mock.patch("sleeve_notes.audio_analysis.requests.get")
    def test_itunes_subset_title_without_duration_is_rejected(self, get):
        from sleeve_notes.audio_analysis import find_itunes_preview

        remix = {**ITUNES_HIT, "trackName": "Threee (Deetron Remix)"}
        original_tagged = {**ITUNES_HIT, "trackName": "Threee (Original Mix)", "previewUrl": "https://audio.example/o.m4a"}
        get.return_value = _itunes_response([remix])
        self.assertIsNone(find_itunes_preview("Roots Panorama", "Threee"))
        get.return_value = _itunes_response([remix, original_tagged])
        self.assertEqual(find_itunes_preview("Roots Panorama", "Threee")["preview_url"], "https://audio.example/o.m4a")

    @mock.patch("sleeve_notes.audio_analysis.requests.get")
    def test_itunes_length_variant_is_the_same_recording_when_durations_agree(self, get):
        from sleeve_notes.audio_analysis import find_itunes_preview

        radio = {**ITUNES_HIT, "artistName": "Magazine 60", "trackName": "Hasta Luego Trinidad",
                 "trackTimeMillis": 214_000, "previewUrl": "https://audio.example/radio.m4a"}
        long = {**radio, "trackName": "Hasta Luego Trinidad (Long Version)",
                "trackTimeMillis": 288_000, "previewUrl": "https://audio.example/long.m4a"}
        get.return_value = _itunes_response([radio, long])
        match = find_itunes_preview("Magazine 60", "Hasta Luego Trinidad", 287)
        self.assertEqual(match["preview_url"], "https://audio.example/long.m4a")
