"""Tests for the optional SpeechBrain ECAPA backend.

The real library and its weights are not installed in this environment,
so the adapter is exercised through injected loaders. Nothing is
downloaded and no network access happens.
"""

import unittest

import numpy as np

from config import SpeakerVerificationConfig
from voice.speaker_backend import (
    DEFAULT_ECAPA_SOURCE,
    SpeechBrainECAPAModel,
    load_speechbrain_ecapa,
)
from voice.speaker_verification import SpeakerVerificationError


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
