from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from sleeve_notes.audio_analysis import youtube_match_score
from sleeve_notes.fetch_bpm import (
    RateLimiter,
    SourceUnavailable,
    _align_audio_bpm,
    build_track_result,
    cascade_all,
    sources_required_for_cache,
)

# Create your tests here.


class AudioAnalysisSourceTests(SimpleTestCase):
    def test_audio_result_exposes_frontend_label_and_audit_details(self):
        result = build_track_result(
            "A1",
            "Artist",
            "Title",
            {
                "sources": {
                    "audio_analysis": {
                        "bpm": 128,
                        "analysis_confidence": 0.97,
                        "url": "https://www.youtube.com/watch?v=video",
                    }
                },
                "sources_tried": ["audio_analysis"],
            },
        )

        self.assertEqual(result["bpm_source_labels"], ["YouTube audio analysis"])
        self.assertEqual(result["audio_analysis"]["analysis_confidence"], 0.97)

    @patch.dict("os.environ", {"AUDIO_ANALYSIS_ENABLED": "false"})
    def test_disabled_audio_source_is_not_required_for_a_complete_cache(self):
        self.assertNotIn("audio_analysis", sources_required_for_cache())

    @patch.dict("os.environ", {"AUDIO_ANALYSIS_ENABLED": "true"})
    def test_enabling_audio_source_makes_existing_cache_eligible_for_backfill(self):
        self.assertIn("audio_analysis", sources_required_for_cache())

    def test_audio_bpm_aligns_three_to_two_meter_against_single_source(self):
        result = _align_audio_bpm(
            {"bpm": 93, "analysis_confidence": 3.1},
            {"acousticbrainz": {"bpm": 139}},
        )

        self.assertEqual(result["bpm"], 140)
        self.assertEqual(result["measured_bpm"], 93.0)
        self.assertEqual(result["meter_adjustment"], 1.5)

    def test_audio_bpm_is_not_forced_to_unrelated_source(self):
        result = _align_audio_bpm(
            {"bpm": 123, "analysis_confidence": 3.1},
            {"songbpm": {"bpm": 118}},
        )

        self.assertEqual(result["bpm"], 123)
        self.assertNotIn("meter_adjustment", result)

    def test_youtube_match_requires_artist_and_title(self):
        self.assertEqual(
            youtube_match_score(
                "Robert Hood", "Minus", "Robert Hood - Minus [Official Audio]"
            ),
            100.0,
        )
        self.assertLess(
            youtube_match_score("Levon Vincent", "Man Or Mistress", "Central - Six Five Two"),
            90.0,
        )

    @patch("sleeve_notes.fetch_bpm._audio_analysis_lookup")
    @patch.dict("sleeve_notes.fetch_bpm._LOOKUP_FNS", {})
    def test_audio_analysis_is_skipped_with_two_existing_bpm_hits(self, audio_lookup):
        previous = {
            "sources": {"songbpm": {"bpm": 128}, "deezer": {"bpm": 128}},
            "sources_tried": [
                "songbpm", "deezer", "reccobeats", "beatport", "acousticbrainz"
            ],
        }

        result = cascade_all("Artist", "Title", previous, RateLimiter())

        audio_lookup.assert_not_called()
        self.assertNotIn("audio_analysis", result["sources_tried"])

    @patch("sleeve_notes.fetch_bpm._audio_analysis_lookup")
    @patch.dict("sleeve_notes.fetch_bpm._LOOKUP_FNS", {})
    def test_unavailable_audio_analysis_remains_retryable(self, audio_lookup):
        audio_lookup.side_effect = SourceUnavailable("temporary YouTube failure")
        previous = {
            "sources": {"songbpm": {"bpm": 128}},
            "sources_tried": [
                "songbpm", "deezer", "reccobeats", "beatport", "acousticbrainz"
            ],
        }

        result = cascade_all("Artist", "Title", previous, RateLimiter())

        self.assertNotIn("audio_analysis", result["sources_tried"])
        self.assertIn("audio_analysis", result["sources_unavailable"])

    @patch("sleeve_notes.fetch_bpm._audio_analysis_lookup")
    @patch.dict("sleeve_notes.fetch_bpm._LOOKUP_FNS", {})
    def test_successful_audio_analysis_becomes_a_source(self, audio_lookup):
        audio_lookup.return_value = {
            "bpm": 128,
            "analysis_confidence": 3.1,
            "url": "https://www.youtube.com/watch?v=video",
        }
        previous = {
            "sources": {},
            "sources_tried": [
                "songbpm", "deezer", "reccobeats", "beatport", "acousticbrainz"
            ],
        }

        result = cascade_all("Artist", "Title", previous, RateLimiter())

        self.assertEqual(result["sources"]["audio_analysis"]["bpm"], 128)
        self.assertIn("audio_analysis", result["sources_tried"])
