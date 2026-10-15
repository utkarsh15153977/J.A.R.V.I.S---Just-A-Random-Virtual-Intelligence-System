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

## Failure handling

| Condition | Behaviour |
|---|---|
| Microphone missing / PortAudio error | `MicrophoneError` raised; manual test prints it and exits with code 1 |
| Whisper raises | logged with traceback, returns `""` |
| Empty transcription | logged, returns `""` |
| Short / noise-only capture | discarded before Whisper, returns `""` |
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
validation, transcript joining, and the manual-test shutdown paths.

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
