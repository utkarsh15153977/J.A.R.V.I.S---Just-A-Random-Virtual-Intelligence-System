"""Tests for voice enrolment storage and capture.

No microphone, no model files and no network. Samples and the embedding
backend are faked, so the enrolment logic is fully deterministic.
"""

import os
import tempfile
import unittest

import numpy as np

from config import SpeakerVerificationConfig
from voice.enrollment import (
    EnrollmentError,
    EnrollmentStore,
    default_data_dir,
    enroll_speaker,
)
from voice.speaker_verification import (
    Enrollment,
    SpeakerVerificationError,
    normalize_embedding,
)


class FakeModel:
    def __init__(self, embedding, model_id="fake"):
        self.embedding = np.asarray(embedding, dtype=np.float32)
        self.model_id = model_id
        self.closed = False

    def embed(self, audio, sample_rate):
        return self.embedding

    def close(self):
        self.closed = True


class ExplodingModel(FakeModel):
    def embed(self, audio, sample_rate):
        raise SpeakerVerificationError("backend broke")


def make_config(**overrides):
    base = {
        "enabled": True,
        "sample_rate": 16000,
        "min_sample_seconds": 1.0,
        "min_rms": 20.0,
        "enrollment_samples": 3,
        "min_enrollment_samples": 2,
    }
    base.update(overrides)
    return SpeakerVerificationConfig(**base)


def loud_audio(seconds=1.0):
    return np.full(round(seconds * 16000), 1000, dtype=np.int16)


def quiet_audio(seconds=1.0):
    return np.zeros(round(seconds * 16000), dtype=np.int16)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = make_config()
        self.path = os.path.join(self._tmp.name, "speaker_enrollment.npz")
        self.store = EnrollmentStore(self.config, path=self.path)


class TestDefaultDataDir(unittest.TestCase):
    def test_returns_a_string_under_jarvis(self):
        value = default_data_dir()
        self.assertIsInstance(value, str)
        self.assertIn("Jarvis", value)


class TestEnrollmentStore(StoreTestCase):
    def test_missing_enrollment_loads_none(self):
        self.assertIsNone(self.store.load())

    def test_round_trip_preserves_fields(self):
        embedding = normalize_embedding([1.0, 2.0, 3.0])
        self.store.save(
            Enrollment(
                embedding=embedding,
                sample_count=3,
                model_id="fake",
                created_at=123.0,
            )
        )
        loaded = self.store.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.sample_count, 3)
        self.assertEqual(loaded.model_id, "fake")
        self.assertEqual(loaded.created_at, 123.0)
        np.testing.assert_allclose(
            loaded.embedding, embedding, rtol=1e-6, atol=1e-6
        )

    def test_exists_tracks_saved_state(self):
        self.assertFalse(self.store.exists())
        self.store.save(
            Enrollment(
                embedding=normalize_embedding([1.0, 0.0]),
                sample_count=1,
                model_id="fake",
                created_at=0.0,
            )
        )
        self.assertTrue(self.store.exists())

    def test_model_mismatch_is_rejected(self):
        self.store.save(
            Enrollment(
                embedding=normalize_embedding([1.0, 0.0]),
                sample_count=1,
                model_id="other",
                created_at=0.0,
            )
        )
        with self.assertRaises(EnrollmentError):
            self.store.load(expected_model_id="fake")

    def test_corrupt_file_is_rejected(self):
        with open(self.path, "wb") as handle:
            handle.write(b"not an npz file")
        with self.assertRaises(EnrollmentError):
            self.store.load()

    def test_malformed_embedding_is_rejected(self):
        with open(self.path, "wb") as handle:
            np.savez(handle, embedding=np.zeros((2, 2)), sample_count=1,
                     model_id="fake", created_at=0.0, version=1)
        with self.assertRaises(EnrollmentError):
            self.store.load()

    def test_non_finite_embedding_is_rejected(self):
        with open(self.path, "wb") as handle:
            np.savez(handle, embedding=np.array([np.nan]), sample_count=1,
                     model_id="fake", created_at=0.0, version=1)
        with self.assertRaises(EnrollmentError):
            self.store.load()

    def test_zero_samples_is_rejected(self):
        with open(self.path, "wb") as handle:
            np.savez(handle, embedding=np.array([1.0]), sample_count=0,
                     model_id="fake", created_at=0.0, version=1)
        with self.assertRaises(EnrollmentError):
            self.store.load()

    def test_clear_reports_whether_something_was_removed(self):
        self.assertFalse(self.store.clear())
        self.store.save(
            Enrollment(
                embedding=normalize_embedding([1.0, 0.0]),
                sample_count=1,
                model_id="fake",
                created_at=0.0,
            )
        )
        self.assertTrue(self.store.clear())
        self.assertFalse(self.store.exists())

    def test_saved_file_stores_no_raw_audio(self):
        self.store.save(
            Enrollment(
                embedding=normalize_embedding([1.0, 0.0]),
                sample_count=1,
                model_id="fake",
                created_at=0.0,
            )
        )
        with np.load(self.path, allow_pickle=False) as data:
            keys = set(data.files)
        self.assertEqual(
            keys,
            {"embedding", "sample_count", "model_id", "created_at",
             "version"},
        )


class TestEnrollSpeaker(StoreTestCase):
    def test_successful_enrolment_is_saved(self):
        model = FakeModel([1.0, 0.0])
        saved = enroll_speaker(
            self.config,
            model,
            self.store,
            capture=lambda seconds: loud_audio(),
            confirm=lambda prompt: True,
            output=lambda line: None,
        )
        self.assertTrue(saved)
        enrollment = self.store.load(expected_model_id="fake")
        self.assertEqual(enrollment.sample_count, 3)
        np.testing.assert_allclose(
            enrollment.embedding, [1.0, 0.0], atol=1e-6
        )

    def test_existing_enrolment_is_not_replaced_without_confirming(self):
        self.store.save(
            Enrollment(
                embedding=normalize_embedding([0.0, 1.0]),
                sample_count=1,
                model_id="fake",
                created_at=0.0,
            )
        )
        model = FakeModel([1.0, 0.0])
        saved = enroll_speaker(
            self.config,
            model,
            self.store,
            capture=lambda seconds: loud_audio(),
            confirm=lambda prompt: False,
            output=lambda line: None,
        )
        self.assertFalse(saved)
        enrollment = self.store.load()
        self.assertEqual(enrollment.sample_count, 1)

    def test_unusable_samples_are_rejected_and_recaptured(self):
        model = FakeModel([1.0, 0.0])
        calls = {"n": 0}

        def capture(seconds):
            calls["n"] += 1
            if calls["n"] <= 2:
                return quiet_audio()  # silent: unusable
            return loud_audio()

        saved = enroll_speaker(
            self.config,
            model,
            self.store,
            capture=capture,
            confirm=lambda prompt: True,
            output=lambda line: None,
        )
        self.assertTrue(saved)
        self.assertEqual(self.store.load().sample_count, 3)

    def test_too_few_usable_samples_is_not_saved(self):
        model = FakeModel([1.0, 0.0])
        saved = enroll_speaker(
            self.config,
            model,
            self.store,
            capture=lambda seconds: quiet_audio(),
            confirm=lambda prompt: True,
            output=lambda line: None,
        )
        self.assertFalse(saved)
        self.assertFalse(self.store.exists())

    def test_embedding_failure_aborts_enrolment(self):
        model = ExplodingModel([1.0, 0.0])
        saved = enroll_speaker(
            self.config,
            model,
            self.store,
            capture=lambda seconds: loud_audio(),
            confirm=lambda prompt: True,
            output=lambda line: None,
        )
        self.assertFalse(saved)
        self.assertFalse(self.store.exists())


if __name__ == "__main__":
    unittest.main()
