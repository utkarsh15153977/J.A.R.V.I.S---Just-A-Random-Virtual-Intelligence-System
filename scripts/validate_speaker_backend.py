"""Validate the real speaker-verification backend on this machine.

This is deliberately **not** part of the unit test suite: it loads the real
SpeechBrain ECAPA model (downloading weights on first use), runs real
embeddings, writes a temporary enrolment and deletes everything afterwards.
Unit tests stay offline and deterministic; this script answers the one
question they cannot -- "does the real backend work here?".

Run it from the repository root after installing the optional backend
(README, "Speaker verification"):

    python scripts/validate_speaker_backend.py --audio-file voice.wav
    python scripts/validate_speaker_backend.py --record 4 --consent

``--audio-file`` takes a 16-bit PCM WAV you are allowed to use; ``--record
4`` records four seconds from the microphone and requires ``--consent`` to
acknowledge the sample is your own voice. Audio is read only from that
source, kept in a temporary directory that is removed on exit, and never
logged: the report prints shapes, scores, booleans and PASS/FAIL only.

Exit status: 0 when every step passes, 1 when a step fails or the run is
refused, 3 when a step is blocked (for example the optional package is
not installed).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

# The script is run as a file, so the repository root (not scripts/) is
# what has to be importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import SpeakerVerificationConfig  # noqa: E402
from voice.enrollment import (  # noqa: E402
    EnrollmentError,
    EnrollmentStore,
    enroll_speaker,
    microphone_capture,
)
from voice.speaker_backend import load_speechbrain_ecapa  # noqa: E402
from voice.speaker_verification import (  # noqa: E402
    SpeakerVerificationError,
    SpeakerVerifier,
    UnusableAudio,
    build_embedding_model,
    embed_audio,
)

_STATUS_PASS = "PASS"
_STATUS_FAIL = "FAIL"
_STATUS_BLOCKED = "BLOCKED"

# Two samples per run, so the audio must hold two usable halves.
_MIN_RECORD_SECONDS = 1.5


class _Blocked(RuntimeError):
    """A prerequisite is missing, so this step cannot run at all."""


def _brief(exc: BaseException) -> str:
    """One short line for ``exc``; long messages are truncated."""
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return text[:300]


def _run_step(
    results: list,
    name: str,
    needs: tuple,
    action,
) -> None:
    """Run one step (after checking prerequisites) and record the status."""
    passed = {n for n, s, _ in results if s == _STATUS_PASS}
    missing = [n for n in needs if n not in passed]
    if missing:
        status = _STATUS_BLOCKED
        detail = f"needs {', '.join(missing)} first"
    else:
        try:
            detail = action()
        except _Blocked as exc:
            status, detail = _STATUS_BLOCKED, str(exc)
        except Exception as exc:
            status, detail = _STATUS_FAIL, _brief(exc)
        else:
            status = _STATUS_PASS
            detail = detail or ""
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))


def _summary(results: list) -> int:
    """Print totals and return the process exit code."""
    passed = sum(1 for _, s, _ in results if s == _STATUS_PASS)
    failed = sum(1 for _, s, _ in results if s == _STATUS_FAIL)
    blocked = sum(1 for _, s, _ in results if s == _STATUS_BLOCKED)
    print()
    print(f"RESULT: {passed} passed, {failed} failed, {blocked} blocked")
    if failed:
        print("The backend did not validate on this machine.")
        return 1
    if blocked:
        print("Some steps could not run; the results above are partial.")
        return 3
    print("The real speaker-verification backend validated here.")
    return 0


def _load_wav(path: str, target_rate: int) -> np.ndarray:
    """Return mono int16 audio at ``target_rate`` from a PCM WAV file."""
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError("only 16-bit PCM WAV files are supported")
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float64)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != target_rate and rate > 0:
        # Linear resample: enough to exercise the real signal path here.
        seconds = audio.size / float(rate)
        src = np.arange(audio.size) / float(rate)
        dst = np.arange(round(seconds * target_rate)) / float(target_rate)
        audio = np.interp(dst, src, audio)
    clipped = np.clip(audio, -32768.0, 32767.0)
    return np.round(clipped).astype(np.int16)


def _record(seconds: float, config: SpeakerVerificationConfig):
    """Record ``seconds`` of consented microphone audio, or report why not."""
    print(
        f"Recording {seconds} seconds from the microphone "
        "(used only for this run)."
    )
    try:
        audio = microphone_capture(config, seconds)
    except EnrollmentError as exc:
        raise _Blocked(f"microphone unavailable ({exc})") from exc
    if audio is None or np.asarray(audio).size == 0:
        raise _Blocked("the microphone returned no audio")
    return np.asarray(audio)


def _parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the real speaker-verification backend."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--audio-file",
        metavar="WAV",
        help="16-bit PCM WAV file you are allowed to use",
    )
    source.add_argument(
        "--record",
        type=float,
        metavar="SECONDS",
        help="record that many seconds from the microphone",
    )
    parser.add_argument(
        "--consent",
        action="store_true",
        help="confirm the --record sample is your own voice "
        "(required with --record)",
    )
    return parser.parse_args(argv)


def main(argv: list) -> int:
    args = _parse_args(argv)
    if args.record is not None and not args.consent:
        print(
            "Refusing to record: pass --consent to confirm the sample "
            "is your own voice."
        )
        return 1
    if args.record is not None and args.record < _MIN_RECORD_SECONDS:
        print(
            "Refusing to record: pass at least "
            f"{_MIN_RECORD_SECONDS} seconds so two samples can be "
            "compared."
        )
        return 1

    results: list = []
    segments = None
    model = None
    store = None
    enrollment = None

    with tempfile.TemporaryDirectory(prefix="jarvis-validate-") as tmp:
        config = SpeakerVerificationConfig(
            enabled=True,
            data_dir=tmp,
        )
        print("Speaker-verification backend validation (real backend).")
        print(f"Temporary data directory: {tmp}")
        print()

        def step_environment() -> str:
            try:
                import speechbrain
                import torch
            except Exception as exc:
                raise _Blocked(
                    "optional backend missing ("
                    f"{type(exc).__name__}); install it first "
                    "(see README, 'Speaker verification')"
                ) from exc
            return (
                f"speechbrain {speechbrain.__version__}, "
                f"torch {torch.__version__}"
            )

        def step_audio_source() -> str:
            nonlocal segments
            if args.audio_file:
                audio = _load_wav(args.audio_file, config.sample_rate)
                origin = f"file {Path(args.audio_file).name}"
            else:
                audio = _record(args.record, config)
                origin = "microphone"
            midpoint = audio.size // 2
            need = int(config.min_sample_seconds * config.sample_rate)
            if midpoint < need or audio.size - midpoint < need:
                raise ValueError(
                    "audio is too short for two comparable samples "
                    f"(need at least {2 * need} samples)"
                )
            segments = (audio[:midpoint], audio[midpoint:])
            seconds = audio.size / float(config.sample_rate)
            return f"{origin}: {seconds:.2f} s at {config.sample_rate} Hz"

        def step_load_model() -> str:
            nonlocal model
            model = load_speechbrain_ecapa(config)
            return f"model_id={model.model_id}"

        def step_embed() -> str:
            embedding = embed_audio(
                model,
                segments[0],
                sample_rate=config.sample_rate,
                min_seconds=config.min_sample_seconds,
                min_rms=config.min_rms,
            )
            if embedding.ndim != 1 or embedding.size == 0:
                raise AssertionError("embedding is not a non-empty vector")
            if not bool(np.isfinite(embedding).all()):
                raise AssertionError("embedding contains non-finite values")
            return f"shape={embedding.shape}, finite=True"

        def step_enrol() -> str:
            nonlocal enrollment, store
            store = EnrollmentStore(config)
            first, second = segments
            choices = (first, second, first)
            position = {"n": 0}

            def capture(_seconds):
                pick = choices[position["n"] % len(choices)]
                position["n"] += 1
                return pick

            saved = enroll_speaker(
                config,
                model,
                store,
                capture=capture,
                confirm=lambda _prompt: False,
                output=lambda line: print(f"      {line}"),
            )
            if not saved:
                raise AssertionError("enrolment was not saved")
            enrollment = store.load()
            if enrollment is None:
                raise AssertionError("stored enrolment was not reloaded")
            return f"samples={enrollment.sample_count}"

        def step_reload() -> str:
            nonlocal enrollment
            enrollment = store.load(expected_model_id=model.model_id)
            if enrollment is None:
                raise AssertionError("stored enrolment is missing")
            if not bool(np.isfinite(enrollment.embedding).all()):
                raise AssertionError("stored embedding is non-finite")
            if not np.isclose(np.linalg.norm(enrollment.embedding), 1.0):
                raise AssertionError("stored embedding is not unit length")
            return f"model_id={enrollment.model_id}, unit-length=True"

        def step_verify() -> str:
            verifier = SpeakerVerifier(
                model,
                enrollment,
                threshold=config.threshold,
                sample_rate=config.sample_rate,
                min_sample_seconds=config.min_sample_seconds,
                min_rms=config.min_rms,
            )
            result = verifier.verify(segments[1])
            score = "n/a" if result.score is None else f"{result.score:.3f}"
            if not result.accepted:
                raise AssertionError(
                    "same-speaker sample rejected "
                    f"(score={score}, reason={result.reason})"
                )
            return f"score={score} (threshold {config.threshold})"

        def step_wrong_rate() -> str:
            loud = np.full(config.sample_rate, 1000, dtype=np.int16)
            try:
                model.embed(loud, 8000)
            except SpeakerVerificationError:
                return "16 kHz guard raised"
            raise AssertionError("16 kHz guard did not fire")

        def step_unusable_audio() -> str:
            try:
                embed_audio(
                    model,
                    np.zeros(1000, dtype=np.int16),
                    sample_rate=config.sample_rate,
                    min_seconds=config.min_sample_seconds,
                    min_rms=config.min_rms,
                )
            except UnusableAudio:
                return "short silent audio rejected"
            raise AssertionError("unusable audio was accepted")

        def step_missing_model_files() -> str:
            empty = Path(tmp) / "empty-model-directory"
            empty.mkdir(exist_ok=True)
            broken = SpeakerVerificationConfig(
                model_path=str(empty),
                data_dir=str(Path(tmp) / "broken"),
            )
            try:
                load_speechbrain_ecapa(broken)
            except SpeakerVerificationError:
                return "load failure reported"
            raise AssertionError("missing model files were not reported")

        def step_unknown_backend() -> str:
            bogus = SpeakerVerificationConfig(model="no-such-backend")
            try:
                build_embedding_model(bogus)
            except SpeakerVerificationError:
                return "unknown backend reported"
            raise AssertionError("unknown backend was accepted")

        def step_corrupt_enrolment() -> str:
            corrupt = Path(tmp) / "corrupt-enrolment.npz"
            corrupt.write_bytes(b"this is not an npz archive")
            broken_store = EnrollmentStore(config, path=corrupt)
            try:
                broken_store.load()
            except EnrollmentError:
                return "corrupt file rejected"
            raise AssertionError("corrupt enrolment was accepted")

        steps = (
            ("environment", (), step_environment),
            ("audio-source", (), step_audio_source),
            ("load-model", ("environment",), step_load_model),
            ("embed", ("load-model", "audio-source"), step_embed),
            ("enrol", ("embed", "load-model"), step_enrol),
            ("reload", ("enrol", "load-model"), step_reload),
            ("verify", ("reload", "load-model"), step_verify),
            ("wrong-sample-rate", ("load-model",), step_wrong_rate),
            ("unusable-audio", ("load-model",), step_unusable_audio),
            (
                "missing-model-files",
                ("environment",),
                step_missing_model_files,
            ),
            ("unknown-backend", (), step_unknown_backend),
            ("corrupt-enrolment", (), step_corrupt_enrolment),
        )
        for name, needs, action in steps:
            _run_step(results, name, needs, action)
        if model is not None:
            model.close()

    def step_cleanup() -> str:
        if Path(tmp).exists():
            raise AssertionError("temporary directory still exists")
        return "temporary directory removed"

    _run_step(results, "cleanup", (), step_cleanup)
    print()
    return _summary(results)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
