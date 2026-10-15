# Voice Pipeline

Phase 1 of Jarvis: **reliable microphone → speech detection → recording
→ transcription**.

## Flow of one capture attempt

```
microphone (16 kHz, int16, 100 ms blocks)
    │
    ▼
noise calibration (once, ~1 s) ──► dynamic energy threshold
    │
    ▼
wait for speech (pre-roll buffer keeps last 3 blocks)
    │  block RMS >= threshold for 0.4 s  ──► speech starts
    ▼
record (pre-roll prepended so the first word is not clipped)
    │  RMS < threshold for 1.0 s  ──► speech ends
    │  recording reaches 10 s     ──► forced stop
    │  no speech within 30 s      ──► attempt abandoned
    ▼
reject capture if < 0.6 s or peak level below threshold
    │
    ▼
faster-whisper (base / cpu / int8, English, beam 1, no VAD)
    │
    ▼
clean lower-cased text   ("" when nothing was heard — never a crash)
```

## Design decisions

### Threshold comes from measurement, not a constant

The old implementation used a hard-coded `energy_threshold = 600`.
Ambient noise on this machine measures ~2000 RMS, which made the old
threshold trigger constantly and produced garbage transcriptions.

On startup the listener records ~1 s of ambient noise and sets:

```
threshold = clamp(median(noise_levels) * 4.0, floor=50, ceiling=4000)
```

The median ignores single loud clicks during calibration. The floor and
ceiling protect against broken microphones, and hitting either limit
logs a warning (saturated mic / muted mic).

### Speech start requires sustained energy

A block above threshold is not enough. The level must stay above the
threshold for `min_speech_seconds` (0.4 s); any gap resets progress.
This rejects clicks, keyboard clatter and other short spikes.

### Silence detection uses audio time, not wall clock

The segmenter accumulates block durations, so timing is deterministic
and unit testable. A brief dip (< `silence_stop_seconds`) does not end
the recording; speech resuming resets the silence counter.

### No temp files

faster-whisper accepts a float32 NumPy array directly, so the recorded
int16 audio is converted in memory (`/ 32768.0`) instead of being
written to a temporary WAV file.

### Whisper VAD is off

Our own recorder already segments speech, so Whisper's VAD filter is
disabled. Revisit only if measurements show a benefit.

## Configuration

All values live in `config.py` (`VoiceConfig`) and are validated on
startup (`ConfigError` collects every problem in one message):

| Setting | Default | Meaning |
|---|---|---|
| `sample_rate` | 16000 | microphone sample rate |
| `block_duration` | 0.1 s | audio block length |
| `calibration_seconds` | 1.0 | ambient noise measurement |
| `threshold_multiplier` | 4.0 | threshold = baseline × this |
| `threshold_floor` / `threshold_ceiling` | 50 / 4000 | clamp range |
| `min_speech_seconds` | 0.4 | sustained energy to start |
| `min_recording_seconds` | 0.6 | reject shorter recordings |
| `silence_stop_seconds` | 1.0 | silence that ends recording |
| `max_recording_seconds` | 10.0 | hard cap per utterance |
| `max_wait_seconds` | 30.0 | give up waiting for speech |
| `pre_roll_blocks` | 3 | audio kept before speech start |

## Speaker verification (optional)

When enabled, voice-triggered activation is gated by a local speaker
check. It sits **between** wake-word detection and the command-listening
session:

```
wake phrase detected
    │  (the wake detector keeps ~2 s of the just-fired audio in memory)
    ▼
speaker verification: embed the captured audio, cosine-compare to the
enrolled centroid, accept when score >= threshold
    │  accepted ──────────────────────► LISTENING (capture the command)
    │  rejected / unusable / error ────► stay IDLE, wait for another wake
    ▼
command still goes through router → guarded ToolExecutor (unchanged)
```

Speaker verification only decides whether to *listen*. It never
authorises a command: a match cannot approve a confirmation, pick a
permission level or mint a ticket. The wake phrase and the speaker check
are independent of each other and of tool permissions; none substitutes
for another.

The wake audio is read from the same detection session (no second
microphone stream) and the buffer is cleared at the start of every
session, so stale audio is never reused.

Activation is one of three explicit modes decided at startup — `MANUAL`,
`WAKE` (`--wake-word`) or `WAKE_VERIFIED` (`--wake-word
--verify-speaker`). `WAKE_VERIFIED` **fails closed**: if the wake engine,
the enrolment or the embedding model is missing, or
`--verify-speaker` is given without `--wake-word`, Jarvis reports the
failing gate and exits (`2` for the configuration error, `1` when a gate
could not start) rather than downgrading to manual activation. The
enrolment is loaded before the model, so a missing profile never causes a
download.

Optional backend (install once per machine):

```powershell
.\.venv\Scripts\pip install --index-url https://download.pytorch.org/whl/cpu torch torchaudio
.\.venv\Scripts\pip install speechbrain
```

On Windows, SpeechBrain's default fetch strategy needs symlink privileges
that a normal account lacks (`WinError 1314`); the adapter requests the
`COPY_SKIP_CACHE` strategy instead and retries without it on older
releases.

All values live in `config.py` (`SpeakerVerificationConfig`, validated the
same way):

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `False` | verification off unless opted in |
| `model` | `speechbrain-ecapa` | embedding backend identifier |
| `model_path` | `""` | local model path (default source if empty) |
| `threshold` | 0.5 | cosine-similarity acceptance (calibrate!) |
| `sample_rate` | 16000 | audio rate for embedding |
| `min_sample_seconds` | 0.6 | reject shorter captures |
| `enrollment_sample_seconds` | 2.0 | clip length per enrolment sample |
| `enrollment_samples` | 3 | samples requested |
| `min_enrollment_samples` | 2 | minimum accepted before saving |
| `min_rms` | 20.0 | reject near-silent samples (int16 scale) |
| `data_dir` | `""` | enrolment location (per-user default if empty) |

The threshold is a starting point, not a security guarantee: it must be
calibrated for the chosen embedding model and enrolment. Verification is
speaker matching, **not** liveness or anti-spoofing.

## Failure handling

| Condition | Behaviour |
|---|---|
| Microphone missing / PortAudio error | `MicrophoneError` raised; manual test prints it and exits with code 1 |
| Whisper raises | logged with traceback, returns `""` |
| Empty transcription | logged, returns `""` |
| Short / noise-only capture | discarded before Whisper, returns `""` |
| `--verify-speaker` without `--wake-word` | configuration error, exit code 2 (checked before anything is initialised) |
| Verification enabled but wake engine unavailable | reported; **refuses to start**, exit code 1 (listener never built) |
| Verification enabled but no enrolment | reported; **refuses to start**, exit code 1 (model never loaded) |
| Verification enabled but model unavailable/incompatible | reported; **refuses to start**, exit code 1 (model closed again) |
| Verification mismatch / unusable audio | activation refused (stays `IDLE`); never bypassed |
| Verification error during activation | `SpeakerVerificationError` reported, clean runtime-error exit (code 1) |
| Stored enrolment missing | "not enrolled": refuses to start if verification is required, otherwise manual mode is unaffected |
| Stored enrolment corrupt / model mismatch | `EnrollmentError`; treated as not enrolled (refuse when verification is required) |
| Enrolment sample too short / silent | rejected and re-prompted; never stored |
| Ctrl+C | caught by the entry point, clean shutdown, exit code 0 |
| Audio buffer overflow | warning logged, recording continues |

## Logging

Console: short format. File: `logs/jarvis.log` with timestamps.
Logged events: calibration baseline/threshold, speech start (with
levels), speech end (with duration), discarded captures, wait timeouts,
transcription duration, final transcription. Raw audio is never logged.

## Testing

Automated (no hardware):

```powershell
python -m unittest discover -s tests -v
```

Covers threshold calculation and clamping, speech segmenter state
machine (start/silence/reset/finish), capture acceptance gates, config
validation, transcript joining, the manual-test shutdown paths, the
activation-mode refusals, and speaker verification / enrolment against
fake embedding backends (no microphone, no model files, no network).

Real embedding backend (manual, needs the optional package installed):

```powershell
python scripts\validate_speaker_backend.py --audio-file voice.wav
python scripts\validate_speaker_backend.py --record 4 --consent
```

Loads the real ECAPA model, embeds, enrols, reloads, verifies a
same-speaker sample, exercises the error paths (wrong sample rate,
unusable audio, missing model files, unknown backend, corrupt enrolment)
and deletes its temporary data, printing `PASS`/`FAIL`/`BLOCKED` per
step. Exit `0` means every step passed.

Manual (hardware):

```powershell
python -m voice.test_voice
```

Speak after `Listening...`; verify the transcription, then exit with a
spoken `exit` or `Ctrl+C`.

## Microphone diagnostics

```powershell
python -m voice.test_voice --diagnose
```

Read-only measurement mode for debugging detection problems. It does
**not** load Whisper and does **not** change production logic. It
prints:

- active input device (name, channels, sample rates)
- per-phase live RMS (one line per second)
- `min / median / mean / max` for **Ambient** (silent), **Normal
  speech** and **Loud speech**
- the threshold calibration would derive from the ambient phase
- a diagnosis: whether ambient, normal speech and loud speech sit
  below/above that threshold, plus the speech/ambient median ratio

Use it to decide tuning changes — never to hard-code a threshold.
