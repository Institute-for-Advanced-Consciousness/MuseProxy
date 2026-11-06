"""Tests for muse_proxy.stream_health.

The status-derivation and windowing logic is driven through a FakePoller
so it is fully deterministic. One integration test exercises the real
LslPoller against a live pylsl outlet.
"""

from __future__ import annotations

import time

import pylsl
import pytest

from muse_proxy.stream_health import (
    LslPoller,
    PollResult,
    StreamHealthMonitor,
    StreamSpec,
    StreamStatus,
    all_alive,
    block_health_summary,
    specs_from_stream_refs,
)

# ---------------------------------------------------------------------------
# Test fixtures / fakes
# ---------------------------------------------------------------------------

EEG = StreamSpec(key="eeg", name="Muse", type="EEG", description="Muse EEG")
PPG = StreamSpec(key="ppg", name="Muse", type="PPG", description="Muse PPG")
MARK = StreamSpec(
    key="mark", name="MuseProxyMarkers", type="Markers",
    description="markers", expect_continuous=False,
)


class FakePoller:
    """Replays a scripted list of poll results; repeats the last forever."""

    def __init__(self, script: list[dict[str, PollResult]]) -> None:
        self._script = list(script)
        self._i = 0
        self.closed = False

    def poll(self) -> dict[str, PollResult]:
        if not self._script:
            return {}
        idx = min(self._i, len(self._script) - 1)
        self._i += 1
        return dict(self._script[idx])

    def close(self) -> None:
        self.closed = True


def _alive(n: int = 10) -> PollResult:
    return PollResult(present=True, new_samples=n)


def _silent() -> PollResult:
    return PollResult(present=True, new_samples=0)


def _gone() -> PollResult:
    return PollResult(present=False)


# ---------------------------------------------------------------------------
# Status derivation
# ---------------------------------------------------------------------------


def test_present_with_samples_is_alive() -> None:
    mon = StreamHealthMonitor([EEG], FakePoller([{"eeg": _alive()}]))
    mon.poll_once()
    assert mon.snapshot()[0].status is StreamStatus.ALIVE
    assert mon.all_alive()


def test_absent_stream_is_missing() -> None:
    mon = StreamHealthMonitor([EEG], FakePoller([{"eeg": _gone()}]))
    mon.poll_once()
    assert mon.snapshot()[0].status is StreamStatus.MISSING
    assert not mon.all_alive()


def test_silent_only_after_threshold_polls() -> None:
    script = [{"eeg": _alive()}] + [{"eeg": _silent()}] * 3
    mon = StreamHealthMonitor([EEG], FakePoller(script), silent_polls=3)
    mon.poll_once()  # has delivered data once
    # Silent polls 1 and 2: still within grace -> ALIVE.
    mon.poll_once()
    mon.poll_once()
    assert mon.snapshot()[0].status is StreamStatus.ALIVE
    # Poll 3: crosses the threshold -> SILENT.
    mon.poll_once()
    assert mon.snapshot()[0].status is StreamStatus.SILENT


def test_visible_but_never_sampled_stream_is_silent_immediately() -> None:
    """An outlet that exists but has never delivered data gets no grace."""
    mon = StreamHealthMonitor([EEG], FakePoller([{"eeg": _silent()}]), silent_polls=3)
    mon.poll_once()
    assert mon.snapshot()[0].status is StreamStatus.SILENT
    assert not mon.all_alive()


def test_samples_reset_the_silent_counter() -> None:
    script = [{"eeg": _silent()}, {"eeg": _silent()}, {"eeg": _alive()}, {"eeg": _silent()}]
    mon = StreamHealthMonitor([EEG], FakePoller(script), silent_polls=3)
    for _ in range(4):
        mon.poll_once()
    # The alive poll reset the counter, so one trailing silent poll is grace.
    assert mon.snapshot()[0].status is StreamStatus.ALIVE


def test_marker_stream_is_alive_on_presence_alone() -> None:
    mon = StreamHealthMonitor(
        [MARK], FakePoller([{"mark": _silent()}]), silent_polls=1
    )
    for _ in range(5):
        mon.poll_once()
    # expect_continuous=False — never goes SILENT just from no samples.
    assert mon.snapshot()[0].status is StreamStatus.ALIVE


def test_stream_recovers_from_missing_to_alive() -> None:
    script = [{"eeg": _gone()}, {"eeg": _gone()}, {"eeg": _alive()}]
    mon = StreamHealthMonitor([EEG], FakePoller(script))
    mon.poll_once()
    assert mon.snapshot()[0].status is StreamStatus.MISSING
    mon.poll_once()
    mon.poll_once()
    assert mon.snapshot()[0].status is StreamStatus.ALIVE


# ---------------------------------------------------------------------------
# Block-window reports
# ---------------------------------------------------------------------------


def test_window_report_flags_a_mid_window_drop() -> None:
    script = [
        {"eeg": _alive(), "ppg": _alive()},   # before the window
        {"eeg": _alive(), "ppg": _alive()},   # window: ok
        {"eeg": _alive(), "ppg": _gone()},    # window: ppg drops
        {"eeg": _alive(), "ppg": _gone()},    # window: still gone
    ]
    mon = StreamHealthMonitor([EEG, PPG], FakePoller(script))
    mon.poll_once()  # pre-window
    token = mon.begin_window()
    time.sleep(0.01)
    mon.poll_once()
    mon.poll_once()
    mon.poll_once()
    report = mon.window_report(token)
    assert report["eeg"] is StreamStatus.ALIVE
    assert report["ppg"] is StreamStatus.MISSING


def test_window_report_clean_when_nothing_dropped() -> None:
    mon = StreamHealthMonitor(
        [EEG, PPG], FakePoller([{"eeg": _alive(), "ppg": _alive()}])
    )
    token = mon.begin_window()
    time.sleep(0.01)
    for _ in range(4):
        mon.poll_once()
    report = mon.window_report(token)
    assert report["eeg"] is StreamStatus.ALIVE
    assert report["ppg"] is StreamStatus.ALIVE


def test_window_report_excludes_pre_window_drops() -> None:
    script = [
        {"eeg": _gone()},   # pre-window drop — must NOT count
        {"eeg": _alive()},
        {"eeg": _alive()},
    ]
    mon = StreamHealthMonitor([EEG], FakePoller(script))
    mon.poll_once()  # the drop, before begin_window
    token = mon.begin_window()
    time.sleep(0.01)
    mon.poll_once()
    mon.poll_once()
    assert mon.window_report(token)["eeg"] is StreamStatus.ALIVE


def test_window_report_falls_back_to_current_when_no_polls_in_window() -> None:
    mon = StreamHealthMonitor([EEG], FakePoller([{"eeg": _alive()}]))
    mon.poll_once()
    token = mon.begin_window()  # no poll after this
    report = mon.window_report(token)
    assert report["eeg"] is StreamStatus.ALIVE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Ref:
    def __init__(self, name: str, type: str | None, description: str) -> None:
        self.name = name
        self.type = type
        self.description = description


def test_specs_from_stream_refs_keys_and_continuity() -> None:
    refs = [
        _Ref("Muse", "EEG", "Muse EEG"),
        _Ref("Muse", "PPG", "Muse PPG"),
        _Ref("MuseProxyMarkers", "Markers", "markers"),
        _Ref("AIM", None, "CGX"),
    ]
    specs = specs_from_stream_refs(refs)
    keys = [s.key for s in specs]
    assert keys == ["Muse|EEG", "Muse|PPG", "MuseProxyMarkers|Markers", "AIM|*"]
    by_key = {s.key: s for s in specs}
    assert by_key["Muse|EEG"].expect_continuous is True
    assert by_key["MuseProxyMarkers|Markers"].expect_continuous is False
    # type=None -> presence-only.
    assert by_key["AIM|*"].expect_continuous is False


def test_all_alive_helper() -> None:
    mon = StreamHealthMonitor(
        [EEG, PPG], FakePoller([{"eeg": _alive(), "ppg": _gone()}])
    )
    mon.poll_once()
    assert not all_alive(mon.snapshot())


def test_status_rank_ordering() -> None:
    assert StreamStatus.ALIVE.rank < StreamStatus.SILENT.rank < StreamStatus.MISSING.rank
    assert StreamStatus.ALIVE.ok and not StreamStatus.MISSING.ok


def test_monitor_with_no_specs_start_stop_is_safe() -> None:
    mon = StreamHealthMonitor([], FakePoller([]))
    mon.start()  # no specs — no thread
    mon.stop()
    mon.stop()  # idempotent


def test_stop_closes_the_poller() -> None:
    poller = FakePoller([{"eeg": _alive()}])
    mon = StreamHealthMonitor([EEG], poller)
    mon.stop()
    assert poller.closed


# ---------------------------------------------------------------------------
# Integration — real pylsl outlet
# ---------------------------------------------------------------------------


def test_lsl_poller_sees_a_live_outlet() -> None:
    """LslPoller resolves a real outlet and reports it ALIVE."""
    name = f"MuseProxyHealthTest_{int(time.time() * 1e6)}"
    info = pylsl.StreamInfo(
        name=name, type="EEG", channel_count=4,
        nominal_srate=256, channel_format=pylsl.cf_float32,
        source_id=name,
    )
    outlet = pylsl.StreamOutlet(info)
    spec = StreamSpec(key="k", name=name, type="EEG", description="test")
    mon = StreamHealthMonitor([spec], LslPoller([spec]))
    try:
        deadline = time.time() + 8.0
        status = StreamStatus.MISSING
        while time.time() < deadline:
            for _ in range(64):
                outlet.push_sample([0.0, 0.0, 0.0, 0.0])
            mon.poll_once()
            status = mon.snapshot()[0].status
            if status is StreamStatus.ALIVE:
                break
            time.sleep(0.1)
        assert status is StreamStatus.ALIVE
    finally:
        mon.stop()
        del outlet


def test_lsl_poller_reports_missing_when_absent() -> None:
    spec = StreamSpec(
        key="k", name=f"NoSuchStream_{int(time.time() * 1e6)}",
        type="EEG", description="absent",
    )
    mon = StreamHealthMonitor([spec], LslPoller([spec]))
    try:
        mon.poll_once()
        assert mon.snapshot()[0].status is StreamStatus.MISSING
    finally:
        mon.stop()


# ---------------------------------------------------------------------------
# block_health_summary
# ---------------------------------------------------------------------------


class _Block:
    """Duck-typed stand-in for persistence.BlockRecord."""

    def __init__(self, block_id, attempt, label, stream_health) -> None:
        self.block_id = block_id
        self.attempt = attempt
        self.label = label
        self.stream_health = stream_health


def test_block_health_summary_clean_blocks() -> None:
    blocks = [
        _Block(1, 1, "baseline", {"Muse|EEG": "alive"}),
        _Block(2, 1, "paced_0p1Hz", None),
    ]
    lines = block_health_summary(blocks)
    assert len(lines) == 2
    assert all("OK" in line for line in lines)


def test_block_health_summary_flags_degraded_stream() -> None:
    blocks = [_Block(3, 1, "paced_0p2Hz", {"Muse|PPG": "missing", "Muse|EEG": "alive"})]
    lines = block_health_summary(blocks)
    assert "DEGRADED" in lines[0]
    assert "Muse PPG" in lines[0]
    assert "Muse EEG" not in lines[0]


def test_block_health_summary_latest_attempt_wins() -> None:
    # Block 3 ran twice — attempt 1 lost PPG, attempt 2 was clean.
    blocks = [
        _Block(3, 1, "paced_0p2Hz", {"Muse|PPG": "missing"}),
        _Block(3, 2, "paced_0p2Hz", {"Muse|PPG": "alive"}),
    ]
    lines = block_health_summary(blocks)
    assert len(lines) == 1
    assert "OK" in lines[0]
    assert "attempt 2" in lines[0]
