from importlib.util import find_spec
from unittest import skipUnless
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from sleeve_notes.audio_analysis import youtube_match_score
from sleeve_notes.fetch_bpm import (
    RateLimiter,
    SourceUnavailable,
    cache_entry_complete,
    consense,
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

        self.assertEqual(result["bpm_source_labels"], ["Audio analysis"])
        self.assertEqual(result["audio_analysis"]["analysis_confidence"], 0.97)

    @patch.dict("os.environ", {"AUDIO_ANALYSIS_ENABLED": "false"})
    def test_disabled_audio_source_is_not_required_for_a_complete_cache(self):
        self.assertNotIn("audio_analysis", sources_required_for_cache())

    @patch.dict("os.environ", {"AUDIO_ANALYSIS_ENABLED": "true"})
    def test_enabling_audio_source_makes_existing_cache_eligible_for_backfill(self):
        self.assertIn("audio_analysis", sources_required_for_cache())

    def _confidence(self, audio_bpm, other):
        return consense({"sources": {"audio_analysis": {"bpm": audio_bpm}, **other}})["bpm_confidence"]

    @patch("sleeve_notes.fetch_bpm._audio_analysis_lookup")
    @patch.dict("sleeve_notes.fetch_bpm._LOOKUP_FNS", {})
    def test_three_to_two_audio_reading_never_earns_the_trust_dot(self, audio_lookup):
        # songbpm wrong at 100, audio at 67: scaling 67 × 1.5 towards it would
        # make them "agree" and print 100 with the trust dot.
        audio_lookup.return_value = {"bpm": 67, "analysis_confidence": 3.1}
        previous = {
            "sources": {"songbpm": {"bpm": 100}},
            "sources_tried": ["songbpm", "deezer", "reccobeats", "beatport", "acousticbrainz"],
        }

        result = cascade_all("Artist", "Title", previous, RateLimiter())

        self.assertEqual(result["sources"]["audio_analysis"]["bpm"], 67)
        self.assertNotIn(consense(result)["bpm_confidence"], ("high", "manual"))

    def test_half_time_audio_reading_is_octave_not_high(self):
        self.assertEqual(self._confidence(64, {"songbpm": {"bpm": 128}}), "octave")

    def test_genuine_audio_agreement_still_counts(self):
        self.assertEqual(self._confidence(128, {"songbpm": {"bpm": 127}}), "high")

    @patch.dict("os.environ", {"AUDIO_ANALYSIS_ENABLED": "false"})
    def test_cache_complete_without_beatport(self):
        entry = {"sources": {}, "sources_tried": ["songbpm", "deezer", "reccobeats", "acousticbrainz"]}
        self.assertTrue(cache_entry_complete(entry))

    @patch.dict("os.environ", {"AUDIO_ANALYSIS_ENABLED": "true"})
    def test_cache_complete_when_audio_was_skipped_for_enough_hits(self):
        tried = ["songbpm", "deezer", "reccobeats", "acousticbrainz"]
        two_hits = {"songbpm": {"bpm": 124}, "deezer": {"bpm": 124}}
        one_hit = {"songbpm": {"bpm": 124}}
        self.assertTrue(cache_entry_complete({"sources": two_hits, "sources_tried": tried}))
        self.assertFalse(cache_entry_complete({"sources": one_hit, "sources_tried": tried}))

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


def _write_wav(path, signal, sr=44100):
    import wave

    import numpy as np

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes())


def _click_track(bpm, seconds=40, sr=44100):
    import numpy as np

    signal = np.zeros(sr * seconds)
    for beat in np.arange(0, seconds, 60 / bpm):
        i = int(beat * sr)
        n = np.arange(min(2000, len(signal) - i))
        signal[i:i + len(n)] += np.sin(2 * np.pi * 1000 * n / sr) * np.exp(-n / 300)
    return signal * 0.8


@skipUnless(find_spec("essentia"), "needs the [audio] extra; CI installs it")
class EssentiaMeasureTests(SimpleTestCase):
    """Runs the real engine on synthetic audio, so no network or fixtures."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_click_track_measures_its_tempo_confidently(self):
        from sleeve_notes.audio_analysis import MIN_ESSENTIA_CONFIDENCE, measure_bpm

        _write_wav(self.dir / "click.wav", _click_track(128))
        bpm, confidence, beats = measure_bpm(self.dir / "click.wav")

        self.assertAlmostEqual(bpm, 128, delta=1)
        self.assertGreaterEqual(confidence, MIN_ESSENTIA_CONFIDENCE)

    def test_noise_falls_below_the_confidence_gate(self):
        import numpy as np

        from sleeve_notes.audio_analysis import MIN_ESSENTIA_CONFIDENCE, measure_bpm

        noise = np.random.default_rng(0).normal(0, 0.3, 44100 * 40)
        _write_wav(self.dir / "noise.wav", noise)
        _, confidence, _ = measure_bpm(self.dir / "noise.wav")

        self.assertLess(confidence, MIN_ESSENTIA_CONFIDENCE)

    @patch.dict("os.environ", {"AUDIO_ANALYSIS_ENABLED": "true"})
    @patch("sleeve_notes.audio_analysis._to_analysis_wav")
    @patch("sleeve_notes.audio_analysis._download_audio")
    @patch("sleeve_notes.audio_analysis.measure_bpm")
    def test_low_confidence_reading_is_not_returned_as_a_bpm(self, measure, _download, _convert):
        from sleeve_notes.audio_analysis import audio_lookup

        measure.return_value = (143.3, 0.21, 68)
        video = {"id": "abcdefghijk", "title": "Artist - Title", "score": 95.0, "origin": "discogs"}
        with patch("sleeve_notes.audio_analysis.find_discogs_video", return_value=video):
            self.assertIsNone(audio_lookup("Artist", "Title", discogs_videos=[]))
