"""Actionable, privacy-safe messages for audio / microphone failures.

Pure helpers only: this module imports no audio library, opens no device
and touches no hardware, so it is fully unit testable and safe to import
anywhere. The application uses it to turn a low-level PortAudio / OS error
into a message that names the *likely* cause **and** still shows the
original detail, so a Windows user can act on it instead of reading a raw
``PaErrorCode``.

Classification is deliberately conservative and best-effort: matching is
case-insensitive on the error text, and anything unrecognised falls back
to a generic audio-error explanation rather than guessing a specific
cause. It never claims a device or model works; it only explains a
failure that already happened. No audio content or embedding data is ever
part of these messages.
"""

from __future__ import annotations

# Ordered (trigger substrings, actionable explanation). The first match
# wins, so more specific causes are listed before broader ones. Matching is
# done on the lower-cased error text.
_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "access denied",
            "access is denied",
            "permission denied",
            "not permitted",
            "0x80070005",
        ),
        "microphone access was denied; allow it in Windows Settings > "
        "Privacy & security > Microphone (and check the app's permissions)",
    ),
    (
        (
            "no default input device",
            "no default device",
            "no input device",
            "no such device",
            "error querying device",
            "invalid device",
            "device not found",
        ),
        "no microphone/input device was found; connect one and make sure it "
        "is enabled in Windows Sound settings",
    ),
    (
        (
            "device unavailable",
            "device is busy",
            "device busy",
            "already in use",
            "exclusive",
            "unavailable",
            "in use",
        ),
        "the microphone is unavailable or busy; close other applications "
        "using it (and Jarvis) and try again",
    ),
    (
        (
            "sample rate",
            "samplerate",
            "sample-rate",
            "sampling rate",
            "invalid sample",
            "unsupported format",
            "incompatible",
        ),
        "the device rejected the requested sample rate; check the device's "
        "supported rates (Jarvis uses 16 kHz mono)",
    ),
    (
        (
            "disconnect",
            "device removal",
            "device removed",
            "unplugged",
            "hot unplug",
        ),
        "the audio device was disconnected; reconnect it and try again",
    ),
)

_GENERIC = "the audio subsystem reported an error"

# Keep the echoed original text from flooding the console; it is never
# secret, only potentially long.
_MAX_DETAIL = 300


def classify_audio_error(exc: BaseException) -> str:
    """Return an actionable explanation for a low-level audio error.

    Matches on the (case-insensitive) error text. Unknown text yields a
    generic explanation, so this never fabricates a specific cause.
    """
    text = str(exc).strip().lower()
    for needles, explanation in _RULES:
        if any(needle in text for needle in needles):
            return explanation
    return _GENERIC


def describe_audio_error(exc: BaseException, *, context: str) -> str:
    """Return ``"<context>: <explanation> (<original text>)"``.

    The original text is preserved to aid diagnosis but truncated to
    ``_MAX_DETAIL`` characters. An empty message falls back to the
    exception's type name so the line is never blank.
    """
    detail = str(exc).strip() or type(exc).__name__
    if len(detail) > _MAX_DETAIL:
        detail = detail[:_MAX_DETAIL] + "..."
    return f"{context}: {classify_audio_error(exc)} ({detail})"
