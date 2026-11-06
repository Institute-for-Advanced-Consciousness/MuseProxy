"""Tests for the salvage loop — the pre-block gate, the per-block health
window routing, and the RA-facing stream_recovery stage.

Stages are driven directly with a minimal fake monitor so the routing is
deterministic and needs no live LSL streams.
"""

from __future__ import annotations

from typing import Any

import pytest

from muse_proxy.config import load_config
from muse_proxy.stages.stream_recovery import advance_to_next, block_window_report
from muse_proxy.stream_health import StreamHealth, StreamSpec, StreamStatus

ALIVE = StreamStatus.ALIVE
MISSING = StreamStatus.MISSING
SILENT = StreamStatus.SILENT


class _FakeMonitor:
    """Minimal StreamHealthMonitor stand-in for stage tests."""

    def __init__(self, statuses: dict[str, StreamStatus]) -> None:
        self._statuses = dict(statuses)
        self._specs = [
            StreamSpec(
                key=k, name=k.split("|")[0], type=k.split("|")[-1], description=k
            )
            for k in statuses
        ]

    def set(self, statuses: dict[str, StreamStatus]) -> None:
        self._statuses = dict(statuses)

    def snapshot(self) -> list[StreamHealth]:
        return [
            StreamHealth(spec=s, status=self._statuses[s.key]) for s in self._specs
        ]

    def all_alive(self) -> bool:
        return all(v is ALIVE for v in self._statuses.values())

    def window_report(self, token: float) -> dict[str, StreamStatus]:
        return dict(self._statuses)

    def window_report_values(self, token: float) -> dict[str, str]:
        return {k: v.value for k, v in self._statuses.items()}

    @property
    def specs(self) -> tuple[StreamSpec, ...]:
        return tuple(self._specs)

    def spec_by_key(self, key: str) -> StreamSpec | None:
        return next((s for s in self._specs if s.key == key), None)

    def begin_window(self) -> float:
        return 0.0

    def stop(self) -> None:
        pass


def _capture_show_stage(app: Any) -> tuple[list[tuple[str, dict]], Any]:
    captured: list[tuple[str, dict]] = []
    orig = app.show_stage
    app.show_stage = lambda name, **kw: captured.append((name, kw))  # type: ignore[assignment]
    return captured, orig


# ---------------------------------------------------------------------------
# advance_to_next
# ---------------------------------------------------------------------------


def test_advance_to_next_routes_to_following_block(shared_app: Any) -> None:
    captured, orig = _capture_show_stage(shared_app)
    try:
        advance_to_next(shared_app, 2)
    finally:
        shared_app.show_stage = orig
    assert captured == [("block_instructions", {"block_id": 3})]


def test_advance_to_next_pass_a_block4_goes_to_pass_switch(shared_app: Any) -> None:
    shared_app.pass_label = "A"
    captured, orig = _capture_show_stage(shared_app)
    try:
        advance_to_next(shared_app, 4)
    finally:
        shared_app.show_stage = orig
    assert captured == [("pass_switch", {})]


def test_advance_to_next_pass_b_block4_goes_to_completion(shared_app: Any) -> None:
    shared_app.pass_label = "B"
    captured, orig = _capture_show_stage(shared_app)
    try:
        advance_to_next(shared_app, 4)
    finally:
        shared_app.show_stage = orig
        shared_app.pass_label = "A"
    assert captured == [("completion", {})]


# ---------------------------------------------------------------------------
# block_window_report
# ---------------------------------------------------------------------------


def test_block_window_report_empty_without_monitor(shared_app: Any) -> None:
    shared_app.monitor = None
    assert block_window_report(shared_app, 123.0) == {}


def test_block_window_report_empty_without_token(shared_app: Any) -> None:
    shared_app.monitor = _FakeMonitor({"Muse|EEG": ALIVE})
    try:
        assert block_window_report(shared_app, None) == {}
    finally:
        shared_app.monitor = None


def test_block_window_report_returns_string_values(shared_app: Any) -> None:
    shared_app.monitor = _FakeMonitor({"Muse|EEG": ALIVE, "Muse|PPG": MISSING})
    try:
        report = block_window_report(shared_app, 1.0)
    finally:
        shared_app.monitor = None
    assert report == {"Muse|EEG": "alive", "Muse|PPG": "missing"}


# ---------------------------------------------------------------------------
# between_block_settle routing
# ---------------------------------------------------------------------------


def test_between_settle_routes_to_recovery_on_degraded_stream(shared_app: Any) -> None:
    shared_app.outlet = None
    shared_app.audio_scheduler = None
    bbs = shared_app.stages["between_block_settle"]
    bbs.on_enter(
        after_block=3, attempt=1, health_report={"Muse|PPG": "missing"}
    )
    captured, orig = _capture_show_stage(shared_app)
    try:
        bbs._on_space()
    finally:
        shared_app.show_stage = orig
    assert captured[-1][0] == "stream_recovery"
    assert captured[-1][1]["block_id"] == 3
    assert captured[-1][1]["report"] == {"Muse|PPG": "missing"}


def test_between_settle_normal_route_when_streams_clean(shared_app: Any) -> None:
    shared_app.outlet = None
    shared_app.audio_scheduler = None
    bbs = shared_app.stages["between_block_settle"]
    bbs.on_enter(
        after_block=2, attempt=1, health_report={"Muse|EEG": "alive"}
    )
    captured, orig = _capture_show_stage(shared_app)
    try:
        bbs._on_space()
    finally:
        shared_app.show_stage = orig
    assert captured[-1] == ("block_instructions", {"block_id": 3})


def test_between_settle_no_report_routes_normally(shared_app: Any) -> None:
    shared_app.outlet = None
    shared_app.audio_scheduler = None
    bbs = shared_app.stages["between_block_settle"]
    bbs.on_enter(after_block=1)  # no health_report at all
    captured, orig = _capture_show_stage(shared_app)
    try:
        bbs._on_space()
    finally:
        shared_app.show_stage = orig
    assert captured[-1] == ("block_instructions", {"block_id": 2})


# ---------------------------------------------------------------------------
# block_instructions pre-block gate
# ---------------------------------------------------------------------------


def test_gate_blocks_space_while_a_stream_is_down(shared_app: Any) -> None:
    shared_app.config = load_config()
    shared_app.monitor = _FakeMonitor({"Muse|EEG": ALIVE, "Muse|PPG": MISSING})
    bi = shared_app.stages["block_instructions"]
    captured, orig = _capture_show_stage(shared_app)
    try:
        bi.on_enter(block_id=2)
        assert bi._streams_ok is False
        bi._on_space()
        assert captured == []  # gated — SPACE did nothing
    finally:
        shared_app.show_stage = orig
        bi.on_leave()
        shared_app.monitor = None


def test_gate_opens_when_every_stream_is_alive(shared_app: Any) -> None:
    shared_app.config = load_config()
    shared_app.monitor = _FakeMonitor({"Muse|EEG": ALIVE, "Muse|PPG": ALIVE})
    bi = shared_app.stages["block_instructions"]
    captured, orig = _capture_show_stage(shared_app)
    try:
        bi.on_enter(block_id=2)
        assert bi._streams_ok is True
        bi._on_space()
        assert captured[-1] == ("block_breathing", {"block_id": 2, "attempt": 1})
    finally:
        shared_app.show_stage = orig
        bi.on_leave()
        shared_app.monitor = None


def test_ra_override_starts_block_despite_down_stream(shared_app: Any) -> None:
    shared_app.config = load_config()
    shared_app.monitor = _FakeMonitor({"Muse|EEG": MISSING})
    bi = shared_app.stages["block_instructions"]
    captured, orig = _capture_show_stage(shared_app)
    try:
        bi.on_enter(block_id=3)
        assert bi._streams_ok is False
        bi._on_ra_override()
        assert captured[-1] == ("block_breathing", {"block_id": 3, "attempt": 1})
    finally:
        shared_app.show_stage = orig
        bi.on_leave()
        shared_app.monitor = None


def test_gate_open_when_no_monitor_attached(shared_app: Any) -> None:
    shared_app.config = load_config()
    shared_app.monitor = None
    bi = shared_app.stages["block_instructions"]
    captured, orig = _capture_show_stage(shared_app)
    try:
        bi.on_enter(block_id=1)
        assert bi._streams_ok is True
        bi._on_space()
        assert captured[-1] == ("block_breathing", {"block_id": 1, "attempt": 1})
    finally:
        shared_app.show_stage = orig
        bi.on_leave()


# ---------------------------------------------------------------------------
# stream_recovery stage
# ---------------------------------------------------------------------------


def test_recovery_rerun_routes_back_with_incremented_attempt(shared_app: Any) -> None:
    shared_app.config = load_config()
    shared_app.monitor = _FakeMonitor({"Muse|EEG": ALIVE, "Muse|PPG": ALIVE})
    sr = shared_app.stages["stream_recovery"]
    captured, orig = _capture_show_stage(shared_app)
    try:
        sr.on_enter(block_id=3, attempt=1, report={"Muse|PPG": "missing"})
        sr._on_rerun()
        assert captured[-1] == (
            "block_instructions", {"block_id": 3, "attempt": 2}
        )
    finally:
        shared_app.show_stage = orig
        sr.on_leave()
        shared_app.monitor = None


def test_recovery_rerun_is_gated_until_streams_recover(shared_app: Any) -> None:
    shared_app.config = load_config()
    shared_app.monitor = _FakeMonitor({"Muse|PPG": MISSING})
    sr = shared_app.stages["stream_recovery"]
    captured, orig = _capture_show_stage(shared_app)
    try:
        sr.on_enter(block_id=3, attempt=1, report={"Muse|PPG": "missing"})
        # PPG still down — the Re-run button is disabled, so _on_rerun is a no-op.
        sr._on_rerun()
        assert captured == []
    finally:
        shared_app.show_stage = orig
        sr.on_leave()
        shared_app.monitor = None


def test_recovery_continue_advances_to_next_block(shared_app: Any) -> None:
    shared_app.config = load_config()
    shared_app.monitor = _FakeMonitor({"Muse|PPG": MISSING})
    sr = shared_app.stages["stream_recovery"]
    captured, orig = _capture_show_stage(shared_app)
    try:
        sr.on_enter(block_id=3, attempt=1, report={"Muse|PPG": "missing"})
        # Continue is always available, even with PPG still down.
        sr._on_continue()
        assert captured[-1] == ("block_instructions", {"block_id": 4})
    finally:
        shared_app.show_stage = orig
        sr.on_leave()
        shared_app.monitor = None


def test_recovery_rerun_attempt_keeps_climbing(shared_app: Any) -> None:
    shared_app.config = load_config()
    shared_app.monitor = _FakeMonitor({"Muse|EEG": ALIVE})
    sr = shared_app.stages["stream_recovery"]
    captured, orig = _capture_show_stage(shared_app)
    try:
        # The block already ran twice; this recovery is after attempt 2.
        sr.on_enter(block_id=2, attempt=2, report={"Muse|EEG": "silent"})
        sr._on_rerun()
        assert captured[-1] == (
            "block_instructions", {"block_id": 2, "attempt": 3}
        )
    finally:
        shared_app.show_stage = orig
        sr.on_leave()
        shared_app.monitor = None


def test_override_marker_names_down_streams() -> None:
    from muse_proxy.stages.block_instructions import record_stream_override

    class _Outlet:
        def __init__(self):
            self.pushed = []

        def push(self, event, payload=None, **_kw):
            self.pushed.append((event, payload))
            return 0.0

    class _App:
        pass

    app = _App()
    app.outlet = _Outlet()
    app.pass_label = "B"
    app.monitor = _FakeMonitor({"Muse2-EEG|EEG": ALIVE, "Muse2-PPG|PPG": MISSING})
    record_stream_override(app, "stream_override", 3, 2)
    event, payload = app.outlet.pushed[-1]
    assert event == "stream_override"
    assert payload == {"block": 3, "attempt": 2, "pass": "B", "down": "Muse2-PPG|PPG"}
