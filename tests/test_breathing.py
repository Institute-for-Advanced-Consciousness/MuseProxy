"""Tests for muse_proxy.breathing — paced cue schedule generation."""

from __future__ import annotations

import pytest

from muse_proxy.breathing import PacedSchedule, build_paced_schedule


def test_0p1Hz_5min_yields_30_cycles() -> None:
    s = build_paced_schedule(
        rate_hz=0.1,
        inhale_duration_s=5.0,
        exhale_duration_s=5.0,
        duration_s=300.0,
        sample_rate=44100,
    )
    assert s.n_cycles == 30
    assert len(s.inhales) == 30
    assert len(s.exhales) == 30


def test_0p2Hz_5min_yields_60_cycles() -> None:
    s = build_paced_schedule(
        rate_hz=0.2,
        inhale_duration_s=2.5,
        exhale_duration_s=2.5,
        duration_s=300.0,
        sample_rate=44100,
    )
    assert s.n_cycles == 60
    assert len(s.inhales) == 60
    assert len(s.exhales) == 60


def test_inhale_onsets_at_zero_then_cycle_period() -> None:
    s = build_paced_schedule(
        rate_hz=0.1,
        inhale_duration_s=5.0,
        exhale_duration_s=5.0,
        duration_s=300.0,
        sample_rate=44100,
    )
    inhales = s.inhales
    assert inhales[0].onset_s == pytest.approx(0.0)
    assert inhales[1].onset_s == pytest.approx(10.0)
    assert inhales[-1].onset_s == pytest.approx(290.0)


def test_exhale_onsets_offset_by_inhale_duration() -> None:
    s = build_paced_schedule(
        rate_hz=0.1,
        inhale_duration_s=5.0,
        exhale_duration_s=5.0,
        duration_s=300.0,
        sample_rate=44100,
    )
    for inh, exh in zip(s.inhales, s.exhales, strict=True):
        assert exh.onset_s - inh.onset_s == pytest.approx(5.0)


def test_onset_sample_matches_onset_s_at_sr() -> None:
    sr = 44100
    s = build_paced_schedule(
        rate_hz=0.1,
        inhale_duration_s=5.0,
        exhale_duration_s=5.0,
        duration_s=300.0,
        sample_rate=sr,
    )
    for cue in s.cues:
        assert cue.onset_sample == int(round(cue.onset_s * sr))


def test_total_samples_matches_duration() -> None:
    sr = 44100
    s = build_paced_schedule(
        rate_hz=0.1,
        inhale_duration_s=5.0,
        exhale_duration_s=5.0,
        duration_s=300.0,
        sample_rate=sr,
    )
    assert s.total_samples == 300 * sr


def test_cycles_are_1_indexed() -> None:
    s = build_paced_schedule(
        rate_hz=0.1,
        inhale_duration_s=5.0,
        exhale_duration_s=5.0,
        duration_s=10.0,  # 1 cycle
        sample_rate=44100,
    )
    assert s.cues[0].cycle == 1
    assert s.cues[1].cycle == 1


def test_cues_paired_inhale_then_exhale_per_cycle() -> None:
    s = build_paced_schedule(
        rate_hz=0.1,
        inhale_duration_s=5.0,
        exhale_duration_s=5.0,
        duration_s=20.0,  # 2 cycles
        sample_rate=44100,
    )
    kinds = [c.kind for c in s.cues]
    cycles = [c.cycle for c in s.cues]
    assert kinds == ["inhale", "exhale", "inhale", "exhale"]
    assert cycles == [1, 1, 2, 2]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_rate_must_be_positive() -> None:
    with pytest.raises(ValueError):
        build_paced_schedule(
            rate_hz=0.0, inhale_duration_s=5.0, exhale_duration_s=5.0,
            duration_s=300.0, sample_rate=44100,
        )


def test_durations_must_be_positive() -> None:
    with pytest.raises(ValueError):
        build_paced_schedule(
            rate_hz=0.1, inhale_duration_s=0.0, exhale_duration_s=10.0,
            duration_s=300.0, sample_rate=44100,
        )


def test_cycle_inconsistent_with_rate_rejected() -> None:
    with pytest.raises(ValueError):
        build_paced_schedule(
            rate_hz=0.1, inhale_duration_s=4.0, exhale_duration_s=5.0,
            duration_s=300.0, sample_rate=44100,
        )


def test_zero_duration_rejected() -> None:
    with pytest.raises(ValueError):
        build_paced_schedule(
            rate_hz=0.1, inhale_duration_s=5.0, exhale_duration_s=5.0,
            duration_s=0.0, sample_rate=44100,
        )


def test_sub_cycle_duration_rejected() -> None:
    with pytest.raises(ValueError):
        # 0.1 Hz * 5 s = 0.5 cycles → rounds to 0
        build_paced_schedule(
            rate_hz=0.1, inhale_duration_s=5.0, exhale_duration_s=5.0,
            duration_s=5.0, sample_rate=44100,
        )


def test_sample_rate_must_be_positive() -> None:
    with pytest.raises(ValueError):
        build_paced_schedule(
            rate_hz=0.1, inhale_duration_s=5.0, exhale_duration_s=5.0,
            duration_s=300.0, sample_rate=0,
        )


def test_schedule_is_dataclass_with_useful_repr() -> None:
    s = build_paced_schedule(
        rate_hz=0.1, inhale_duration_s=5.0, exhale_duration_s=5.0,
        duration_s=10.0, sample_rate=44100,
    )
    assert isinstance(s, PacedSchedule)
    r = repr(s)
    assert "rate_hz=0.1" in r
