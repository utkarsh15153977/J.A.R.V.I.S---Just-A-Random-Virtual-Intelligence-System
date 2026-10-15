"""Tests for the optional SpeechBrain ECAPA backend.

The tests never import the real library or touch its weights: loaders are
injected, so nothing is downloaded and no network access happens. Real
backend validation lives in ``scripts/validate_speaker_backend.py``.
"""

import importlib.util
import unittest
from unittest import mock

import numpy as np

from config import SpeakerVerificationConfig
from voice import speaker_backend
from voice.speaker_backend import (
    DEFAULT_ECAPA_SOURCE,
    SpeechBrainECAPAModel,
    load_speechbrain_ecapa,
)
from voice.speaker_verification import SpeakerVerificationError


def _speechbrain_installed() -> bool:
    return importlib.util.find_spec("speechbrain") is not None


def make_config(**overrides):
    return SpeakerVerificationConfig(**overrides)


def loud_audio():
    return np.full(16000, 1000, dtype=np.int16)


class TestLoadFailure(unittest.TestCase):
    def test_missing_package_raises_unavailable(self):
        def loader():
            raise ImportError("no speechbrain")

        with self.assertRaises(SpeakerVerificationError):
            load_speechbrain_ecapa(make_config(), loader=loader)

    def test_model_load_failure_raises_unavailable(self):
        class Classifier:
            @classmethod
            def from_hparams(cls, **kwargs):
                raise RuntimeError("weights not found")

        with self.assertRaises(SpeakerVerificationError):
            load_speechbrain_ecapa(make_config(), loader=lambda: Classifier)


class TestLoadSuccess(unittest.TestCase):
    def test_returns_a_model_with_a_stable_id(self):
        class Classifier:
            @classmethod
            def from_hparams(cls, **kwargs):
                return cls()

        model = load_speechbrain_ecapa(make_config(), loader=lambda: Classifier)
        self.assertEqual(model.model_id, f"speechbrain-ecapa:{DEFAULT_ECAPA_SOURCE}")

    def test_passes_model_path_and_savedir_to_the_loader(self):
        seen = {}

        class Classifier:
            @classmethod
            def from_hparams(cls, **kwargs):
                seen.update(kwargs)
                return cls()

        config = make_config(model_path="/models/ecapa", data_dir="/data")
        load_speechbrain_ecapa(config, loader=lambda: Classifier)
        self.assertEqual(seen["source"], "/models/ecapa")
        self.assertIn("models", seen["savedir"])
        self.assertEqual(seen["run_opts"], {"device": "cpu"})

    def test_close_releases_the_classifier(self):
        class Classifier:
            @classmethod
            def from_hparams(cls, **kwargs):
                return cls()

        model = load_speechbrain_ecapa(make_config(), loader=lambda: Classifier)
        model.close()


class TestFetchStrategy(unittest.TestCase):
    """Model loading must not depend on symlink privileges on Windows."""

    def test_copy_strategy_when_speechbrain_supports_it(self):
        if not _speechbrain_installed():
            self.skipTest("speechbrain not installed; validated separately")
        strategy = speaker_backend._copy_strategy()
        self.assertIsNotNone(strategy)
        self.assertEqual(strategy.name, "COPY_SKIP_CACHE")

    def test_no_strategy_when_speechbrain_is_missing(self):
        if _speechbrain_installed():
            self.skipTest("speechbrain is installed in this environment")
        self.assertIsNone(speaker_backend._copy_strategy())

    def test_strategy_is_passed_to_the_loader(self):
        seen = {}

        class Classifier:
            @classmethod
            def from_hparams(cls, **kwargs):
                seen.update(kwargs)
                return cls()

        with mock.patch.object(
            speaker_backend,
            "_copy_strategy",
            return_value="COPY_SKIP_CACHE",
        ):
            load_speechbrain_ecapa(make_config(), loader=lambda: Classifier)
        self.assertEqual(seen.get("local_strategy"), "COPY_SKIP_CACHE")

    def test_older_speechbrain_without_the_keyword_is_retried(self):
        attempts = []

        class Classifier:
            @classmethod
            def from_hparams(cls, **kwargs):
                attempts.append(sorted(kwargs))
                if "local_strategy" in kwargs:
                    raise TypeError(
                        "unexpected keyword argument 'local_strategy'"
                    )
                return cls()

        with mock.patch.object(
            speaker_backend,
            "_copy_strategy",
            return_value="COPY_SKIP_CACHE",
        ):
            model = load_speechbrain_ecapa(
                make_config(), loader=lambda: Classifier
            )
        self.assertIsInstance(model, SpeechBrainECAPAModel)
        self.assertEqual(len(attempts), 2)
        self.assertIn("local_strategy", attempts[0])
        self.assertNotIn("local_strategy", attempts[1])

    def test_unrelated_type_error_is_not_retried(self):
        attempts = []

        class Classifier:
            @classmethod
            def from_hparams(cls, **kwargs):
                attempts.append(kwargs)
                raise TypeError("something else broke")

        with mock.patch.object(
            speaker_backend,
            "_copy_strategy",
            return_value="COPY_SKIP_CACHE",
        ):
            with self.assertRaises(SpeakerVerificationError):
                load_speechbrain_ecapa(
                    make_config(), loader=lambda: Classifier
                )
        self.assertEqual(len(attempts), 1)


class TestEmbedGuards(unittest.TestCase):
    def test_wrong_sample_rate_is_rejected_before_import(self):
        model = SpeechBrainECAPAModel(object(), model_id="x")
        with self.assertRaises(SpeakerVerificationError):
            model.embed(loud_audio(), 8000)

    def test_missing_torch_raises_unavailable(self):
        try:  # pragma: no cover - depends on the environment
            import torch  # noqa: F401
        except Exception:
            model = SpeechBrainECAPAModel(object(), model_id="x")
            with self.assertRaises(SpeakerVerificationError):
                model.embed(loud_audio(), 16000)
        else:
            self.skipTest("torch is installed; missing-torch path not tested")


if __name__ == "__main__":
    unittest.main()
