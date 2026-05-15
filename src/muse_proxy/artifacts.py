"""Artifact-block cue schedule builder.

Pure helpers that turn ``config.artifact_block.cues`` into a list of
``(onset_sample, wav_path, label, window_s, is_state)`` records suitable
for ``audio.AudioScheduler.schedule_artifact_block``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import ArtifactCueConfig

__all__ = [
    "ArtifactCue",
    "ArtifactSchedule",
    "build_artifact_schedule",
    "wav_path_for_cue",
]


@dataclass(frozen=True)
class ArtifactCue:
    label: str
    onset_sample: int
    onset_s: float
    end_sample: int  # onset_sample + window_s in samples
    end_s: float
    window_s: float
    is_state: bool
    wav_path: Path


@dataclass(frozen=True)
class ArtifactSchedule:
    cues: tuple[ArtifactCue, ...]
    total_samples: int  # length of the contiguous buffer at sample_rate
    sample_rate: int

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(c.label for c in self.cues)


def wav_path_for_cue(cue_wav_dir: str | Path, label: str) -> Path:
    """Resolve the WAV path for an artifact cue label.

    Convention: ``{cue_wav_dir}/artifact_{label}.wav`` (matches the
    output of ``scripts/generate_audio.py``).
    """
    return Path(cue_wav_dir) / f"artifact_{label}.wav"


def build_artifact_schedule(
    cues: list[ArtifactCueConfig] | tuple[ArtifactCueConfig, ...],
    *,
    cue_wav_dir: str | Path,
    sample_rate: int,
    require_wav_files: bool = True,
) -> ArtifactSchedule:
    """Turn config-validated cues into an ArtifactSchedule with onsets.

    ``require_wav_files`` controls whether missing WAVs raise. The tests
    use ``False`` since the WAVs are rendered at install time.
    """
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be positive; got {sample_rate!r}")
    if not cues:
        raise ValueError("cues is empty")

    out: list[ArtifactCue] = []
    for c in cues:
        wav = wav_path_for_cue(cue_wav_dir, c.label)
        if require_wav_files and not wav.exists():
            raise FileNotFoundError(f"WAV not found for cue {c.label!r}: {wav}")
        onset_sample = int(round(c.start_s * sample_rate))
        end_s = c.start_s + c.window_s
        end_sample = int(round(end_s * sample_rate))
        out.append(
            ArtifactCue(
                label=c.label,
                onset_sample=onset_sample,
                onset_s=c.start_s,
                end_sample=end_sample,
                end_s=end_s,
                window_s=c.window_s,
                is_state=c.state,
                wav_path=wav,
            )
        )

    # Ensure monotonic onsets (config validator already enforces this,
    # but assert defensively for callers passing hand-built lists).
    for prev, nxt in zip(out[:-1], out[1:], strict=True):
        if nxt.onset_sample < prev.onset_sample:
            raise ValueError(
                f"artifact cues out of order: {prev.label}@{prev.onset_sample}, "
                f"{nxt.label}@{nxt.onset_sample}"
            )

    # Total buffer length spans through the last cue's window.
    total_samples = max((c.end_sample for c in out), default=0)

    return ArtifactSchedule(
        cues=tuple(out), total_samples=total_samples, sample_rate=sample_rate
    )
