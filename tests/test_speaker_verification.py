"""Tests for the speaker-verification interface.

No microphone, no model files and no network. The embedding backend is a
fake, and the optional SpeechBrain adapter is exercised through injected
loaders, so these tests are fully deterministic.
"""

import math
import unittest
from unittest import mock

import numpy as np

from config import SpeakerVerificationConfig
from voice import speaker_backend
from voice.speaker_verification import (
    SpeakerVerificationError,
    SpeakerVerifier,
    VerificationResult,
    build_embedding_model,
    cosine_similarity,
    embed_audio,
    is_usable_sample,
    normalize_embedding,
    to_float_audio,
    to_int16_scale,
)


class FakeModel:
    """Returns one fixed embedding for every sample."""

    def __init__(self, embedding, model_id="fake"):
        self.embedding = np.asarray(embedding, dtype=np.float32)
        self._model_id = model_id
        self.calls = []
        self.closed = False

    @property
    def model_id(self):
        return self._model_id

    def embed(self, audio, sample_rate):
        self.calls.append((np.asarray(audio).shape, sample_rate))
        return self.embedding

    def close(self):
        self.closed = True


class ExplodingModel(FakeModel):
    def embed(self, audio, sample_rate):
        raise SpeakerVerificationError("backend broke")


class RaisingModel(FakeModel):
    def embed(self, audio, sample_rate):
        raise RuntimeError("unexpected")


def audio(seconds=1.0, rate=16000, level=1000):
    """A loud, non-silent int16 clip long enough to be usable."""
    size = round(seconds * rate)
    return np.full(size, level, dtype=np.int16)


def enrollment_for(embedding, model_id="fake"):
    from voice.speaker_verification import Enrollment

    return Enrollment(
        embedding=normalize_embedding(embedding),
        sample_count=3,
        model_id=model_id,
        created_at=0.0,
    )


def make_verifier(model, enrollment, threshold=0.5):
    return SpeakerVerifier(
        model,
        enrollment,
        threshold=threshold,
        sample_rate=16000,
        min_sample_seconds=1.0,
        min_rms=20.0,
    )


class TestAudioHelpers(unittest.TestCase):
    def test_to_int16_scale_passes_int16_through(self):
        values = np.array([-32768, 0, 32767], dtype=np.int16)
        self.assertEqual(to_int16_scale(values).tolist(), [-32768, 0, 32767])

    def test_to_int16_scale_rescales_float(self):
        scaled = to_int16_scale(np.array([-1.0, 0.0, 1.0]))
        np.testing.assert_allclose(scaled, [-32768.0, 0.0, 32768.0])

    def test_to_float_audio_converts_and_clips(self):
        values = to_float_audio(np.array([-32768, 32767], dtype=np.int16))
        self.assertTrue(np.isclose(values[0], -1.0))
        self.assertTrue(np.isclose(values[1], 32767 / 32768.0))

    def test_to_float_audio_keeps_float_input(self):
        values = to_float_audio(np.array([-0.5, 0.5]))
        np.testing.assert_allclose(values, [-0.5, 0.5])


class TestUsableSample(unittest.TestCase):
    def test_valid_sample_is_usable(self):
        self.assertTrue(
            is_usable_sample(
                audio(), sample_rate=16000, min_seconds=1.0, min_rms=20.0
            )
        )

    def test_none_is_not_usable(self):
        self.assertFalse(
            is_usable_sample(
                None, sample_rate=16000, min_seconds=1.0, min_rms=20.0
            )
        )

    def test_empty_is_not_usable(self):
        self.assertFalse(
            is_usable_sample(
                np.array([], dtype=np.int16),
                sample_rate=16000,
                min_seconds=1.0,
                min_rms=20.0,
            )
        )

    def test_two_dimensional_audio_is_not_usable(self):
        data = np.zeros((100, 1), dtype=np.int16)
        self.assertFalse(
            is_usable_sample(
                data, sample_rate=16000, min_seconds=1.0, min_rms=20.0
            )
        )

    def test_short_sample_is_not_usable(self):
        self.assertFalse(
            is_usable_sample(
                audio(seconds=0.5),
                sample_rate=16000,
                min_seconds=1.0,
                min_rms=20.0,
            )
        )

    def test_silent_sample_is_not_usable(self):
        silent = np.zeros(16000, dtype=np.int16)
        self.assertFalse(
            is_usable_sample(
                silent, sample_rate=16000, min_seconds=1.0, min_rms=20.0
            )
        )

    def test_non_finite_sample_is_not_usable(self):
        data = np.array([np.nan, np.inf] + [0.0] * 16000)
        self.assertFalse(
            is_usable_sample(
                data, sample_rate=16000, min_seconds=1.0, min_rms=20.0
            )
        )


class TestEmbeddingMath(unittest.TestCase):
    def test_identical_vectors_have_similarity_one(self):
        vector = normalize_embedding([1.0, 2.0, 3.0])
        self.assertAlmostEqual(cosine_similarity(vector, vector), 1.0)

    def test_orthogonal_vectors_have_similarity_zero(self):
        self.assertAlmostEqual(
            cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0
        )

    def test_normalize_embedding_is_unit_length(self):
        norm = np.linalg.norm(normalize_embedding([3.0, 4.0]))
        self.assertAlmostEqual(float(norm), 1.0)

    def test_normalize_embedding_rejects_empty(self):
        with self.assertRaises(SpeakerVerificationError):
            normalize_embedding([])

    def test_normalize_embedding_rejects_zero_vector(self):
        with self.assertRaises(SpeakerVerificationError):
            normalize_embedding([0.0, 0.0, 0.0])

    def test_normalize_embedding_rejects_non_finite(self):
        with self.assertRaises(SpeakerVerificationError):
            normalize_embedding([np.nan, 1.0])

    def test_embed_audio_rejects_unusable_input(self):
        model = FakeModel([1.0, 0.0])
        with self.assertRaises(ValueError):
            embed_audio(
                model,
                audio(seconds=0.1),
                sample_rate=16000,
                min_seconds=1.0,
                min_rms=20.0,
            )
        self.assertEqual(model.calls, [])

    def test_embed_audio_rejects_non_finite_embedding(self):
        model = FakeModel([np.nan, 0.0])
        with self.assertRaises(SpeakerVerificationError):
            embed_audio(
                model,
                audio(),
                sample_rate=16000,
                min_seconds=1.0,
                min_rms=20.0,
            )


class TestSpeakerVerifier(unittest.TestCase):
    def test_rejects_a_mismatched_speaker(self):
        model = FakeModel([0.0, 1.0])
        verifier = make_verifier(model, enrollment_for([1.0, 0.0]))
        result = verifier.verify(audio())
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "below_threshold")

    def test_accepts_the_enrolled_speaker(self):
        model = FakeModel([1.0, 0.0])
        verifier = make_verifier(model, enrollment_for([1.0, 0.0]))
        result = verifier.verify(audio())
        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "match")
        self.assertAlmostEqual(result.score, 1.0)

    def test_score_exactly_at_threshold_is_accepted(self):
        model = FakeModel([1.0, 1.0])
        threshold = 1.0 / math.sqrt(2.0)
        verifier = make_verifier(
            model, enrollment_for([1.0, 0.0]), threshold=threshold
        )
        result = verifier.verify(audio())
        self.assertTrue(result.accepted)
        self.assertAlmostEqual(result.score, threshold)

    def test_score_just_below_threshold_is_rejected(self):
        model = FakeModel([1.0, 1.0])
        verifier = make_verifier(
            model, enrollment_for([1.0, 0.0]), threshold=0.71
        )
        result = verifier.verify(audio())
        self.assertFalse(result.accepted)
        self.assertLess(result.score, 0.71)

    def test_missing_or_unusable_audio_is_rejected(self):
        model = FakeModel([1.0, 0.0])
        verifier = make_verifier(model, enrollment_for([1.0, 0.0]))
        for sample in (None, np.array([], dtype=np.int16), audio(seconds=0.1)):
            result = verifier.verify(sample)
            self.assertFalse(result.accepted)
            self.assertEqual(result.reason, "unusable_audio")

    def test_backend_error_is_rejected_not_raised(self):
        model = ExplodingModel([1.0, 0.0])
        verifier = make_verifier(model, enrollment_for([1.0, 0.0]))
        result = verifier.verify(audio())
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "model_error")

    def test_unexpected_error_is_rejected_not_raised(self):
        model = RaisingModel([1.0, 0.0])
        verifier = make_verifier(model, enrollment_for([1.0, 0.0]))
        result = verifier.verify(audio())
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "model_error")

    def test_non_finite_embedding_is_rejected(self):
        model = FakeModel([np.nan, 0.0])
        verifier = make_verifier(model, enrollment_for([1.0, 0.0]))
        result = verifier.verify(audio())
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "model_error")

    def test_closed_verifier_rejects(self):
        model = FakeModel([1.0, 0.0])
        verifier = make_verifier(model, enrollment_for([1.0, 0.0]))
        verifier.close()
        result = verifier.verify(audio())
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "closed")

    def test_close_releases_the_model_and_is_idempotent(self):
        model = FakeModel([1.0, 0.0])
        verifier = make_verifier(model, enrollment_for([1.0, 0.0]))
        verifier.close()
        verifier.close()
        self.assertTrue(model.closed)

    def test_invalid_threshold_is_rejected_at_construction(self):
        model = FakeModel([1.0, 0.0])
        with self.assertRaises(ValueError):
            SpeakerVerifier(
                model,
                enrollment_for([1.0, 0.0]),
                threshold=0.0,
                sample_rate=16000,
            )

    def test_verify_result_never_exposes_audio(self):
        model = FakeModel([1.0, 0.0])
        verifier = make_verifier(model, enrollment_for([1.0, 0.0]))
        result = verifier.verify(audio())
        self.assertIsInstance(result, VerificationResult)
        self.assertNotIsInstance(result.accepted, np.ndarray)


class TestBuildEmbeddingModel(unittest.TestCase):
    def test_unknown_backend_raises(self):
        config = SpeakerVerificationConfig(model="not-a-backend")
        with self.assertRaises(SpeakerVerificationError):
            build_embedding_model(config)

    def test_default_backend_delegates_to_speechbrain(self):
        fake = FakeModel([1.0, 0.0], model_id="speechbrain-ecapa:source")
        with mock.patch.object(
            speaker_backend,
            "load_speechbrain_ecapa",
            return_value=fake,
        ) as loader:
            config = SpeakerVerificationConfig(model="speechbrain-ecapa")
            model = build_embedding_model(config)
        self.assertIs(model, fake)
        loader.assert_called_once()


if __name__ == "__main__":
    unittest.main()
