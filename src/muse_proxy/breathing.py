"""Paced-breath cue schedule builder.

Pure functions — no sounddevice or pylsl imports. Used by
``audio.AudioScheduler.schedule_paced_breathing`` and tested in
``tests/test_breathing.py``.

A paced block at rate_hz with inhale_duration_s + exhale_duration_s =
cycle_period_s produces ``n_cycles = round(duration_s * rate_hz)``
cycles. Each cycle emits one ``inhale`` marker at cycle start and one
``exhale`` marker at ``cycle_start + inhale_duration_s``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "PacedCue",
    "PacedSchedule",
    "build_paced_schedule",
]


@dataclass(frozen=True)
class PacedCue:
    """One inhale or exhale cue in a paced-breathing block.

    ``onset_sample`` is the sample index in the block's contiguous audio
    buffer where the cue's WAV begins. ``onset_s`` is the same in
    seconds — convenience for tests and for the marker payload.
    """

    cycle: int  # 1-based
    kind: str  # 'inhale' | 'exhale'
    onset_sample: int
    onset_s: float


@dataclass(frozen=True)
class PacedSchedule:
    rate_hz: float
    inhale_duration_s: float
    exhale_duration_s: float
    cycle_period_s: float
    n_cycles: int
    cues: tuple[PacedCue, ...]
    total_samples: int  # length of the contiguous buffer at sample_rate

    @property
    def inhales(self) -> tuple[PacedCue, ...]:
        return tuple(c for c in self.cues if c.kind == "inhale")

    @property
    def exhales(self) -> tuple[PacedCue, ...]:
        return tuple(c for c in self.cues if c.kind == "exhale")


def build_paced_schedule(
    *,
    rate_hz: float,
    inhale_duration_s: float,
    exhale_duration_s: float,
    duration_s: float,
    sample_rate: int,
) -> PacedSchedule:
    """Build a deterministic schedule of inhale/exhale cues.

    Parameters mirror ``config.blocks[i]`` fields. Returns the full cue
    list and the total length (in samples) of the contiguous audio
    buffer the AudioScheduler will play.

    Validation: inhale + exhale must equal 1/rate_hz to within 1 ms.
    """
    if rate_hz <= 0:
        raise ValueError(f"rate_hz must be positive; got {rate_hz!r}")
    if inhale_duration_s <= 0 or exhale_duration_s <= 0:
        raise ValueError("inhale/exhale durations must be positive")
    if duration_s <= 0:
        raise ValueError(f"duration_s must be positive; got {duration_s!r}")
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be positive; got {sample_rate!r}")

    cycle_period = inhale_duration_s + exhale_duration_s
    target_period = 1.0 / rate_hz
    if abs(cycle_period - target_period) > 1e-3:
        raise ValueError(
            f"inhale+exhale={cycle_period:.6f}s but 1/rate_hz={target_period:.6f}s"
        )

    n_cycles = int(round(duration_s * rate_hz))
    if n_cycles <= 0:
        raise ValueError(
            f"duration_s={duration_s} at rate_hz={rate_hz} yields 0 cycles"
        )

    cues: list[PacedCue] = []
    for i in range(n_cycles):
        cycle_start_s = i * cycle_period
        inhale_s = cycle_start_s
        exhale_s = cycle_start_s + inhale_duration_s
        cues.append(
            PacedCue(
                cycle=i + 1,
                kind="inhale",
                onset_sample=int(round(inhale_s * sample_rate)),
                onset_s=inhale_s,
            )
        )
        cues.append(
            PacedCue(
                cycle=i + 1,
                kind="exhale",
                onset_sample=int(round(exhale_s * sample_rate)),
                onset_s=exhale_s,
            )
        )

    total_samples = int(round(n_cycles * cycle_period * sample_rate))

    return PacedSchedule(
        rate_hz=rate_hz,
        inhale_duration_s=inhale_duration_s,
        exhale_duration_s=exhale_duration_s,
        cycle_period_s=cycle_period,
        n_cycles=n_cycles,
        cues=tuple(cues),
        total_samples=total_samples,
    )
