"""Tests for muse_proxy.artifacts — artifact-block cue schedule."""

from __future__ import annotations

from pathlib import Path

import pytest

from muse_proxy.artifacts import (
    ArtifactCue,
    build_artifact_schedule,
    wav_path_for_cue,
)
from muse_proxy.config import ArtifactCueConfig, load_config


def test_wav_path_for_cue() -> None:
    assert wav_path_for_cue("/tmp/sounds", "blink_slow") == Path("/tmp/sounds/artifact_blink_slow.wav")


def test_default_config_artifact_cues_build_schedule(tmp_path: Path) -> None:
    cfg = load_config()
    s = build_artifact_schedule(
        cfg.artifact_block.cues,
        cue_wav_dir=tmp_path,
        sample_rate=44100,
        require_wav_files=False,
    )
    # 13 cues, every label distinct, monotonic onsets.
    assert len(s.cues) == 13
    assert len(set(s.labels)) == len(s.labels)


def test_artifact_cue_end_sample_matches_window(tmp_path: Path) -> None:
    cues = [
        ArtifactCueConfig(start_s=0.0, label="eyes_open", spoken="Open", window_s=3.0, state=True),
        ArtifactCueConfig(start_s=3.0, label="blink_slow", spoken="Blink", window_s=5.0),
    ]
    sr = 44100
    s = build_artifact_schedule(cues, cue_wav_dir=tmp_path, sample_rate=sr, require_wav_files=False)
    assert s.cues[0].onset_sample == 0
    assert s.cues[0].end_sample == 3 * sr
    assert s.cues[1].onset_sample == 3 * sr
    assert s.cues[1].end_sample == 8 * sr


def test_artifact_total_samples_spans_last_window(tmp_path: Path) -> None:
    cues = [
        ArtifactCueConfig(start_s=0.0, label="eyes_open", spoken="x", window_s=3.0, state=True),
        ArtifactCueConfig(start_s=85.0, label="end_cue", spoken="x", window_s=5.0),
    ]
    sr = 44100
    s = build_artifact_schedule(cues, cue_wav_dir=tmp_path, sample_rate=sr, require_wav_files=False)
    assert s.total_samples == 90 * sr


def test_artifact_default_block_fits_in_90s(tmp_path: Path) -> None:
    cfg = load_config()
    s = build_artifact_schedule(
        cfg.artifact_block.cues,
        cue_wav_dir=tmp_path,
        sample_rate=44100,
        require_wav_files=False,
    )
    # Block 4 duration is 90 s. The schedule's last window may end at 90 s.
    assert s.total_samples <= 90 * 44100


def test_artifact_state_flag_preserved(tmp_path: Path) -> None:
    cfg = load_config()
    s = build_artifact_schedule(
        cfg.artifact_block.cues,
        cue_wav_dir=tmp_path,
        sample_rate=44100,
        require_wav_files=False,
    )
    state_labels = {c.label for c in s.cues if c.is_state}
    assert state_labels == {"eyes_open", "eyes_closed"}


def test_artifact_missing_wav_required_raises(tmp_path: Path) -> None:
    cues = [
        ArtifactCueConfig(start_s=0.0, label="blink_slow", spoken="x", window_s=5.0),
    ]
    with pytest.raises(FileNotFoundError):
        build_artifact_schedule(
            cues, cue_wav_dir=tmp_path, sample_rate=44100, require_wav_files=True
        )


def test_artifact_present_wav_passes(tmp_path: Path) -> None:
    cues = [
        ArtifactCueConfig(start_s=0.0, label="blink_slow", spoken="x", window_s=5.0),
    ]
    (tmp_path / "artifact_blink_slow.wav").write_bytes(b"")  # touch
    s = build_artifact_schedule(
        cues, cue_wav_dir=tmp_path, sample_rate=44100, require_wav_files=True
    )
    assert isinstance(s.cues[0], ArtifactCue)


def test_artifact_empty_cues_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_artifact_schedule([], cue_wav_dir=tmp_path, sample_rate=44100)


def test_artifact_bad_sample_rate_rejected(tmp_path: Path) -> None:
    cues = [
        ArtifactCueConfig(start_s=0.0, label="a", spoken="x", window_s=1.0),
    ]
    with pytest.raises(ValueError):
        build_artifact_schedule(
            cues, cue_wav_dir=tmp_path, sample_rate=0, require_wav_files=False
        )
