"""Tests for muse_proxy.audio — pure helpers + buffer math.

Tests that touch ``sounddevice`` mock the OutputStream so they run in
CI. A separate manual loopback procedure (``tests/test_audio_timing``)
documents the real timing verification.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf

from muse_proxy.artifacts import build_artifact_schedule
from muse_proxy.audio import (
    AudioScheduler,
    build_artifact_buffer,
    build_paced_buffer,
    load_wav_mono,
    synth_volume_check_tone,
)
from muse_proxy.breathing import build_paced_schedule
from muse_proxy.config import ArtifactCueConfig

# ---------------------------------------------------------------------------
# synth_volume_check_tone
# ---------------------------------------------------------------------------


def test_volume_check_tone_shape_and_dtype() -> None:
    tone = synth_volume_check_tone(
        frequency_hz=1000, duration_s=3.0, sample_rate=44100, amplitude=0.3, ramp_s=0.05
    )
    assert tone.dtype == np.float32
    assert tone.shape == (3 * 44100,)


def test_volume_check_tone_respects_amplitude() -> None:
    tone = synth_volume_check_tone(
        frequency_hz=1000, duration_s=1.0, sample_rate=44100, amplitude=0.2, ramp_s=0.05
    )
    assert np.max(np.abs(tone)) <= 0.2 + 1e-3


def test_volume_check_tone_starts_and_ends_near_zero() -> None:
    tone = synth_volume_check_tone(
        frequency_hz=1000, duration_s=1.0, sample_rate=44100, amplitude=0.3, ramp_s=0.05
    )
    assert abs(tone[0]) < 1e-3
    assert abs(tone[-1]) < 1e-3


# ---------------------------------------------------------------------------
# load_wav_mono
# ---------------------------------------------------------------------------


def _make_wav(path: Path, sr: int = 44100, freq: float = 440.0, dur: float = 0.1) -> None:
    t = np.arange(int(sr * dur)) / sr
    data = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    sf.write(str(path), data, sr, subtype="FLOAT")


def test_load_wav_mono_matches_sample_rate(tmp_path: Path) -> None:
    p = tmp_path / "tone.wav"
    _make_wav(p, sr=44100)
    audio = load_wav_mono(p, 44100)
    assert audio.dtype == np.float32
    assert audio.shape == (int(44100 * 0.1),)


def test_load_wav_mono_sample_rate_mismatch_raises(tmp_path: Path) -> None:
    p = tmp_path / "tone.wav"
    _make_wav(p, sr=22050)
    with pytest.raises(ValueError):
        load_wav_mono(p, 44100)


def test_load_wav_mono_collapses_stereo(tmp_path: Path) -> None:
    p = tmp_path / "stereo.wav"
    sr = 44100
    n = int(0.1 * sr)
    data = np.zeros((n, 2), dtype=np.float32)
    data[:, 0] = 0.5  # L = 0.5
    data[:, 1] = -0.5  # R = -0.5
    sf.write(str(p), data, sr, subtype="FLOAT")
    audio = load_wav_mono(p, sr)
    assert audio.shape == (n,)
    # Mean of (0.5, -0.5) = 0
    assert np.allclose(audio, 0.0)


# ---------------------------------------------------------------------------
# build_paced_buffer
# ---------------------------------------------------------------------------


def test_build_paced_buffer_length_matches_schedule_total_samples() -> None:
    sr = 44100
    schedule = build_paced_schedule(
        rate_hz=0.1, inhale_duration_s=5.0, exhale_duration_s=5.0,
        duration_s=20.0, sample_rate=sr,
    )
    inh = np.full(int(0.3 * sr), 0.5, dtype=np.float32)
    exh = np.full(int(0.3 * sr), -0.5, dtype=np.float32)
    buf = build_paced_buffer(schedule, inhale_audio=inh, exhale_audio=exh, sample_rate=sr)
    assert buf.shape == (schedule.total_samples,)


def test_build_paced_buffer_places_inhale_at_zero() -> None:
    sr = 44100
    schedule = build_paced_schedule(
        rate_hz=0.1, inhale_duration_s=5.0, exhale_duration_s=5.0,
        duration_s=10.0, sample_rate=sr,
    )
    inh = np.full(int(0.3 * sr), 0.7, dtype=np.float32)
    exh = np.full(int(0.3 * sr), -0.7, dtype=np.float32)
    buf = build_paced_buffer(schedule, inhale_audio=inh, exhale_audio=exh, sample_rate=sr)
    # First inhale should start at sample 0.
    assert np.allclose(buf[:len(inh)], 0.7)


def test_build_paced_buffer_places_exhale_at_inhale_duration() -> None:
    sr = 44100
    schedule = build_paced_schedule(
        rate_hz=0.1, inhale_duration_s=5.0, exhale_duration_s=5.0,
        duration_s=10.0, sample_rate=sr,
    )
    inh = np.full(int(0.3 * sr), 0.7, dtype=np.float32)
    exh = np.full(int(0.3 * sr), -0.7, dtype=np.float32)
    buf = build_paced_buffer(schedule, inhale_audio=inh, exhale_audio=exh, sample_rate=sr)
    exh_start = int(5.0 * sr)
    assert np.allclose(buf[exh_start:exh_start + len(exh)], -0.7)


# ---------------------------------------------------------------------------
# build_artifact_buffer
# ---------------------------------------------------------------------------


def test_build_artifact_buffer_places_cues(tmp_path: Path) -> None:
    sr = 44100
    cues_cfg = [
        ArtifactCueConfig(start_s=0.0, label="a", spoken="x", window_s=2.0, state=True),
        ArtifactCueConfig(start_s=2.0, label="b", spoken="x", window_s=2.0),
    ]
    schedule = build_artifact_schedule(
        cues_cfg, cue_wav_dir=tmp_path, sample_rate=sr, require_wav_files=False
    )

    def audio_for_cue(label: str) -> np.ndarray:
        return np.full(int(0.2 * sr), 0.5 if label == "a" else -0.5, dtype=np.float32)

    buf = build_artifact_buffer(schedule, sample_rate=sr, audio_for_cue=audio_for_cue)
    assert buf.shape == (schedule.total_samples,)
    assert np.allclose(buf[:int(0.2 * sr)], 0.5)
    assert np.allclose(buf[2 * sr:2 * sr + int(0.2 * sr)], -0.5)


# ---------------------------------------------------------------------------
# AudioScheduler.play_buffer — mock sounddevice
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal sounddevice.OutputStream stand-in.

    Calling ``start()`` immediately drives the callback exactly once with
    a frame that includes the first cue, captures the t0_lsl, then calls
    the finished callback.
    """

    def __init__(self, samplerate, channels, dtype, blocksize, device, callback, finished_callback):
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.callback = callback
        self.finished_callback = finished_callback

    def start(self) -> None:
        # Pretend a callback fires. We're not actually playing audio.
        # Provide a time_info object the callback can read.
        time_info = MagicMock()
        time_info.currentTime = 100.0
        time_info.outputBufferDacTime = 100.05
        out = np.zeros((self.blocksize, 1), dtype=np.float32)
        try:
            self.callback(out, self.blocksize, time_info, None)
        except Exception:
            pass
        # Then signal "finished".
        self.finished_callback()

    def stop(self, ignore_errors: bool = False) -> None:
        return None

    def close(self, ignore_errors: bool = False) -> None:
        return None


def test_play_buffer_invokes_cue_callbacks_with_anchored_timestamps() -> None:
    sr = 44100
    buf = np.zeros(2 * sr, dtype=np.float32)
    # 2 cues at sample 0 and sample 100.
    cue_calls: list[tuple[int, float]] = []

    sched = AudioScheduler(sample_rate=sr, blocksize=2 * sr)

    with patch("sounddevice.OutputStream", _FakeStream):
        # Mock pylsl.local_clock to return a deterministic value.
        with patch("pylsl.local_clock", return_value=1234.0):
            handle = sched.play_buffer(
                buf,
                cue_onsets=[
                    (0, lambda ts: cue_calls.append((0, ts))),
                    (100, lambda ts: cue_calls.append((100, ts))),
                ],
            )
            handle.done.wait(timeout=2.0)

    # t0_lsl = local_clock + (dac_time - currentTime) = 1234.0 + 0.05 = 1234.05
    # Cue 0: timestamp = 1234.05 + 0 / 44100 = 1234.05
    # Cue 100: timestamp = 1234.05 + 100 / 44100
    assert len(cue_calls) == 2
    assert cue_calls[0][0] == 0
    assert cue_calls[0][1] == pytest.approx(1234.05, abs=1e-6)
    assert cue_calls[1][0] == 100
    assert cue_calls[1][1] == pytest.approx(1234.05 + 100 / sr, abs=1e-6)


def test_play_buffer_invokes_on_first_callback() -> None:
    sr = 44100
    buf = np.zeros(sr, dtype=np.float32)
    sched = AudioScheduler(sample_rate=sr, blocksize=sr)

    captured: list[float] = []
    with patch("sounddevice.OutputStream", _FakeStream):
        with patch("pylsl.local_clock", return_value=10.0):
            handle = sched.play_buffer(buf, on_first_callback=lambda t: captured.append(t))
            handle.done.wait(timeout=2.0)
    assert captured == [pytest.approx(10.05, abs=1e-6)]


def test_play_buffer_invokes_on_finished() -> None:
    sr = 44100
    buf = np.zeros(sr, dtype=np.float32)
    sched = AudioScheduler(sample_rate=sr, blocksize=sr)

    finished = threading.Event()
    with patch("sounddevice.OutputStream", _FakeStream):
        with patch("pylsl.local_clock", return_value=0.0):
            handle = sched.play_buffer(buf, on_finished=finished.set)
            handle.done.wait(timeout=2.0)
    assert finished.is_set()


def test_play_buffer_rejects_2d_buffer() -> None:
    sched = AudioScheduler(sample_rate=44100)
    buf = np.zeros((44100, 2), dtype=np.float32)
    with patch("sounddevice.OutputStream", _FakeStream):
        with pytest.raises(ValueError):
            sched.play_buffer(buf)


