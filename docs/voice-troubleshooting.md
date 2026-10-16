# Voice Troubleshooting (Windows)

What to do when the microphone, wake word, speaker verification or speech
output misbehaves. Jarvis never auto-fixes a problem by weakening a
security setting: every fix below is an explicit, user-visible action.

## How audio errors are reported

Low-level PortAudio / OS errors are no longer shown raw. `voice/audio_errors.py`
classifies the error text and prints **context + likely cause + original
detail**, for example:

```
Microphone unavailable: no microphone/input device was found; connect one
and make sure it is enabled in Windows Sound settings (No Default Input
Device Available)
```

The original text is preserved (truncated to 300 characters) so a support
request can include it. Classification is best-effort: an unrecognised
error says only "the audio subsystem reported an error" — it never
guesses a specific cause. Nothing about your audio content, transcript or
voice embedding is ever part of these messages.

| Message fragment | Likely cause | Action |
|---|---|---|
| "access was denied" | Windows privacy setting or app permission blocks the mic | Settings → Privacy & security → Microphone → allow desktop apps; check the terminal's permissions |
| "no microphone/input device" | No device, disabled device, or unplugged | Connect/enable a microphone; set it as the default input in Sound settings |
| "unavailable or busy" | Another app (or a previous Jarvis) holds the device | Close other apps using the mic; make sure no old Jarvis process is running |
| "sample rate" | Device does not accept 16 kHz mono | Choose a device/format that supports 16 kHz mono, or use a different microphone |
| "disconnected" | Device removed mid-stream | Reconnect the device and retry |
| "audio subsystem reported an error" | Unclassified | Include `logs/jarvis.log` when asking for help |

The same wording reaches the console for the manual test
(`python -m voice.test_voice`), the diagnostic run
(`python -m voice.test_voice --diagnostics`) and the wake-word path.

## Symptom → action

| Symptom | Likely cause | Action |
|---|---|---|
| "Wake-word mode is unavailable … Using manual activation instead." | `openwakeword` engine/model not installed | That is expected without the engine. Install it if you want wake-word mode; manual listening still works. |
| "Speaker verification is enabled, but wake-word activation is unavailable. Refusing to start…" | `--verify-speaker` needs a wake engine | Run `--wake-word --verify-speaker` only after installing the wake engine, or drop `--verify-speaker`. |
| "Speaker verification is unavailable … Refusing to start in an unverified mode." | SpeechBrain/ECAPA (or torch) missing, or the model failed to load | Install the optional backend (see `docs/voice-pipeline.md`); the enrolment is read first, so nothing is re-downloaded. |
| "Speaker verification could not start." | The enrolled voice used a different model, or the profile is unreadable | Re-run `--enroll-speaker` with the current model. |
| Replies are never spoken | `pyttsx3` unavailable or a voice failed | Replies are printed; install/fix `pyttsx3` to restore speech. This never stops the loop. |
| Every phrase is "I can't do that yet." | Tools are off | Run with `--tools` (and `--allow-apps` plus an allowlist for launching). |
| "I had a problem hearing you." then a clean shutdown | Capture/transcription provider failed unexpectedly | Re-run; if it repeats, include `logs/jarvis.log`. |
| Microphone stays busy after exit | A previous process did not release the device | Confirm no other Jarvis is running; the mic is released on every shutdown path (including Ctrl+C). |

## Diagnostics

Measure the microphone instead of guessing a threshold:

```powershell
.\.venv\Scripts\python -m voice.test_voice --diagnostics
```

It prints the active input device, ambient/normal/loud RMS levels, the
threshold calibration would derive and a diagnosis (speech below
threshold, false-trigger risk, etc.). It never changes a setting.

Run the plain manual test to see the classified microphone messages:

```powershell
.\.venv\Scripts\python -m voice.test_voice
```

## Logs

Runtime logs are written to `logs/jarvis.log` (timestamped). Transcripts
are **not** logged unless `--transcripts` is enabled; otherwise only the
captured text's length is logged. Include the relevant log lines (they
contain no raw audio) when reporting a problem.

## What the tests cover

The software paths — classification, actionable messages, exactly-once
execution, router recovery, empty transcripts, unexpected transcription
errors and clean shutdown — are covered by `tests/test_audio_errors.py`,
`tests/test_listener.py`, `tests/test_wakeword.py` and
`tests/test_pipeline.py`. No test opens a real device, model file or
process. Real hardware symptoms above must be checked by hand; see
[voice-smoke-test.md](voice-smoke-test.md).
