"""Microphone level diagnostics for tuning speech detection.

Run with:

    python -m voice.test_voice --diagnose

This mode only MEASURES. It never loads Whisper, never changes the
production threshold logic, and never classifies anything.

Pure helpers (summaries, formatting, interpretation) are kept separate
from the hardware loop so they can be unit tested without a microphone.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

import sounddevice as sd

from config import VoiceConfig
from voice.detection import compute_energy_threshold, rms

# Labels shown to the user (also used as result keys).
PHASE_AMBIENT = "Ambient"
PHASE_NORMAL = "Normal speech"
PHASE_LOUD = "Loud speech"

# (label, instruction, seconds)
PHASES: Sequence[tuple] = (
    (PHASE_AMBIENT, "remain completely silent", 5.0),
    (PHASE_NORMAL, "speak normally, as you would to Jarvis", 10.0),
    (PHASE_LOUD, "speak loudly", 10.0),
)


# ----------------------------------------------------------------------
# Pure helpers (no hardware, unit tested)
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class LevelSummary:
    """Statistics of one measured phase."""

    count: int
    minimum: float
    median: float
    mean: float
    maximum: float


def wants_diagnostics(argv: Iterable[str]) -> bool:
    """True when the --diagnose flag is present."""
    return "--diagnose" in list(argv)


def summarize_levels(levels: Iterable[float]) -> LevelSummary:
    """Compute min/median/mean/max over measured block levels."""
    values = [float(value) for value in levels]
    if not values:
        raise ValueError("levels must contain at least one value")
    return LevelSummary(
        count=len(values),
        minimum=min(values),
        median=statistics.median(values),
        mean=statistics.fmean(values),
        maximum=max(values),
    )


def format_summary(label: str, summary: LevelSummary) -> str:
    """Human-readable block for one measured phase."""
    return (
        f"{label}:\n"
        f"  min={summary.minimum:.0f}\n"
        f"  median={summary.median:.0f}\n"
        f"  mean={summary.mean:.0f}\n"
        f"  max={summary.maximum:.0f}\n"
        f"  blocks={summary.count}"
    )


def describe_input_device(device: dict, configured_rate: int) -> List[str]:
    """Describe the active input device for the report."""
    rate = device.get("default_samplerate")
    lines = [
        f"Input device: {device.get('name', '<unknown>')}",
        f"  max input channels: {device.get('max_input_channels', '<unknown>')}",
        f"  device default sample rate: {rate} Hz",
        f"  configured sample rate: {configured_rate} Hz",
    ]
    if isinstance(rate, (int, float)) and abs(float(rate) - configured_rate) > 1:
        lines.append(
            "  note: rates differ; PortAudio performs the conversion."
        )
    return lines


def interpret(
    ambient: LevelSummary,
    normal: LevelSummary,
    loud: LevelSummary,
    threshold: float,
) -> List[str]:
    """Explain, from measured numbers alone, why detection does/doesn't work.

    Every line is a pure function of the summaries and the threshold.
    """
    lines: List[str] = []

    if ambient.maximum >= threshold:
        lines.append(
            f"PROBLEM: ambient noise reaches the threshold "
            f"(max {ambient.maximum:.0f} >= {threshold:.0f}) -> "
            f"false speech triggers are likely."
        )
    else:
        lines.append(
            f"Ambient stays below threshold "
            f"(max {ambient.maximum:.0f} < {threshold:.0f}) -> "
            f"no false triggers from background noise."
        )

    if normal.maximum < threshold:
        lines.append(
            f"PROBLEM: normal speech stays BELOW threshold "
            f"(max {normal.maximum:.0f} < {threshold:.0f}) -> "
            f"speech will not be detected."
        )
    else:
        lines.append(
            f"Normal speech reaches the threshold "
            f"(max {normal.maximum:.0f} >= {threshold:.0f}) -> "
            f"detection should trigger."
        )

    if loud.maximum >= threshold:
        lines.append(
            f"Loud speech reaches the threshold "
            f"(max {loud.maximum:.0f} >= {threshold:.0f})."
        )
    else:
        lines.append(
            f"PROBLEM: even loud speech stays below threshold "
            f"(max {loud.maximum:.0f} < {threshold:.0f}) -> "
            f"the threshold cannot be satisfied by voice at this gain."
        )

    if ambient.median > 0:
        ratio = normal.median / ambient.median
        lines.append(
            f"Speech/ambient median ratio: {ratio:.1f}x "
            f"(a healthy microphone usually shows >= 2x)."
        )
    elif normal.median > 0:
        lines.append("Speech/ambient median ratio: infinite (ambient measured 0).")
    else:
        lines.append("Speech/ambient median ratio: 0.0x (no speech level measured).")

    if normal.median <= ambient.median:
        lines.append(
            "PROBLEM: normal speech is not louder than ambient -> "
            "check the input device, gain, or mute switch."
        )

    return lines


# ----------------------------------------------------------------------
# Hardware loop
# ----------------------------------------------------------------------
def measure_phase(config: VoiceConfig, seconds: float) -> List[float]:
    """Read RMS levels for `seconds`, printing one live line per second."""
    blocks = max(1, round(seconds / config.block_duration))
    blocks_per_second = max(1, round(1.0 / config.block_duration))
    levels: List[float] = []

    with sd.InputStream(
        samplerate=config.sample_rate,
        channels=1,
        dtype="int16",
        blocksize=config.block_size,
    ) as stream:
        for index in range(blocks):
            data, overflowed = stream.read(config.block_size)
            if overflowed:
                print("  warning: audio buffer overflow")
            levels.append(rms(data[:, 0]))
            done = index + 1
            if done % blocks_per_second == 0 or done == blocks:
                elapsed = done * config.block_duration
                print(f"  {elapsed:4.1f}/{seconds:.0f}s  rms={levels[-1]:.0f}")

    return levels


def run_diagnostics(config: VoiceConfig) -> int:
    """Interactive microphone measurement. Returns a process exit code."""
    print("=== Jarvis microphone diagnostics ===")
    print("Whisper is NOT used in this mode; no thresholds are changed.\n")

    try:
        device = sd.query_devices(kind="input")
        for line in describe_input_device(device, config.sample_rate):
            print(line)
        print(f"  default device pair (in/out): {sd.default.device}")
    except (sd.PortAudioError, OSError) as exc:
        print(f"Microphone error: {exc}")
        return 1

    levels_by_phase: Dict[str, List[float]] = {}

    try:
        for label, instruction, seconds in PHASES:
            try:
                input(
                    f"\nPress Enter to start '{label}' ({seconds:.0f} s), "
                    f"{instruction}... "
                )
            except EOFError:
                print(
                    "\nNo interactive console detected. "
                    "Run this command in a terminal and press Enter."
                )
                return 1

            print(f"Measuring {label}...")
            levels_by_phase[label] = measure_phase(config, seconds)
            print(format_summary(label, summarize_levels(levels_by_phase[label])))
    except KeyboardInterrupt:
        print("\nDiagnostics cancelled.")
        return 0
    except (sd.PortAudioError, OSError) as exc:
        print(f"Microphone error: {exc}")
        return 1

    ambient_levels = levels_by_phase[PHASE_AMBIENT]
    threshold = compute_energy_threshold(
        ambient_levels,
        multiplier=config.threshold_multiplier,
        floor=config.threshold_floor,
        ceiling=config.threshold_ceiling,
    )
    raw = statistics.median(ambient_levels) * config.threshold_multiplier
    clamped = " (clamped)" if abs(raw - threshold) > 0.5 else ""
    print(
        f"\nThreshold production calibration would use: {threshold:.0f}{clamped} "
        f"(raw {raw:.0f}, floor {config.threshold_floor:.0f}, "
        f"ceiling {config.threshold_ceiling:.0f})"
    )

    summaries = {
        label: summarize_levels(values) for label, values in levels_by_phase.items()
    }
    print("\nDiagnosis:")
    for line in interpret(
        summaries[PHASE_AMBIENT],
        summaries[PHASE_NORMAL],
        summaries[PHASE_LOUD],
        threshold,
    ):
        print(f"  - {line}")

    print("\nNo production settings were changed by this mode.")
    return 0
