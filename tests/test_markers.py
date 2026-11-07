"""Tests for muse_proxy.markers — format_marker + outlet/inlet round-trip."""

from __future__ import annotations

import time
from pathlib import Path

import pylsl
import pytest

from muse_proxy.markers import (
    MARKER_RESERVED_CHARS,
    MuseProxyMarkerOutlet,
    build_source_id,
    format_marker,
)

# ---------------------------------------------------------------------------
# format_marker — happy path
# ---------------------------------------------------------------------------


def test_format_marker_no_payload() -> None:
    assert format_marker("session_start") == "session_start"


def test_format_marker_with_payload() -> None:
    s = format_marker("breath:inhale", {"block": 2, "cycle": 1, "pass": "A"})
    assert s == "breath:inhale:block=2:cycle=1:pass=A"


def test_format_marker_bool_value() -> None:
    s = format_marker("intake", {"glasses": True, "dental_work": False})
    assert s == "intake:glasses=Y:dental_work=N"


def test_format_marker_float_strips_trailing_zeros() -> None:
    s = format_marker("settle:end", {"duration_elapsed_s": 287.0})
    assert s == "settle:end:duration_elapsed_s=287"


def test_format_marker_float_preserves_significant_decimals() -> None:
    s = format_marker("x", {"rate_hz": 0.1})
    assert s == "x:rate_hz=0.1"


# ---------------------------------------------------------------------------
# format_marker — guards
# ---------------------------------------------------------------------------


def test_format_marker_empty_event_rejected() -> None:
    with pytest.raises(ValueError):
        format_marker("")


def test_format_marker_event_with_equals_rejected() -> None:
    with pytest.raises(ValueError):
        format_marker("k=v")


@pytest.mark.parametrize("bad", MARKER_RESERVED_CHARS)
def test_format_marker_reserved_char_in_value_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        format_marker("x", {"k": f"some{bad}thing"})


def test_format_marker_reserved_char_in_key_rejected() -> None:
    with pytest.raises(ValueError):
        format_marker("x", {"k:bad": "v"})


# ---------------------------------------------------------------------------
# build_source_id
# ---------------------------------------------------------------------------


def test_build_source_id_pass_a() -> None:
    assert (
        build_source_id("MuseProxy", "A", "P001", "20260512")
        == "MuseProxy_passA_P001_20260512"
    )


def test_build_source_id_pass_b() -> None:
    assert (
        build_source_id("MuseProxy", "B", "P001", "20260512")
        == "MuseProxy_passB_P001_20260512"
    )


def test_build_source_id_bad_pass_label() -> None:
    with pytest.raises(ValueError):
        build_source_id("MuseProxy", "C", "P001", "20260512")


# ---------------------------------------------------------------------------
# Outlet → inlet round-trip
# ---------------------------------------------------------------------------


def _resolve_inlet(source_id: str, timeout_s: float = 5.0) -> pylsl.StreamInlet:
    streams = pylsl.resolve_byprop("source_id", source_id, 1, timeout_s)
    assert streams, f"no LSL stream resolved by source_id={source_id!r}"
    inlet = pylsl.StreamInlet(streams[0], recover=False)
    # Force the inlet to fully connect before the caller pushes any
    # samples — open_stream() blocks until the connection is established,
    # avoiding the race where early pushes are dropped.
    inlet.open_stream(timeout=2.0)
    # A small additional settle prevents pylsl from missing the very
    # first sample on Windows.
    time.sleep(0.2)
    return inlet


def _drain(inlet: pylsl.StreamInlet, expected: int, timeout_s: float = 3.0) -> list[tuple[str, float]]:
    """Pull until we've seen ``expected`` samples or time out."""
    deadline = time.time() + timeout_s
    out: list[tuple[str, float]] = []
    while len(out) < expected and time.time() < deadline:
        sample, ts = inlet.pull_sample(timeout=0.1)
        if sample is None:
            continue
        out.append((sample[0], ts))
    return out


@pytest.fixture
def outlet(tmp_path: Path) -> MuseProxyMarkerOutlet:
    """Per-test outlet with a unique source_id to avoid cross-test pollution."""
    src = f"MuseProxy_TEST_{int(time.time() * 1e6)}"
    o = MuseProxyMarkerOutlet(
        outlet_name="MuseProxyMarkers",
        outlet_type="Markers",
        source_id=src,
        csv_path=tmp_path / "markers.csv",
    )
    yield o
    o.close()


def test_round_trip_simple_marker(outlet: MuseProxyMarkerOutlet) -> None:
    inlet = _resolve_inlet(outlet.source_id)
    try:
        outlet.push("session_start")
        outlet.push("session_end")
        samples = _drain(inlet, 2)
        labels = [s[0] for s in samples]
        assert labels == ["session_start", "session_end"]
    finally:
        inlet.close_stream()


def test_round_trip_breath_marker_with_supplied_timestamp(
    outlet: MuseProxyMarkerOutlet,
) -> None:
    inlet = _resolve_inlet(outlet.source_id)
    try:
        custom_ts = pylsl.local_clock() + 0.123
        outlet.breath(
            "inhale", block_id=2, cycle=1, pass_label="A", lsl_timestamp=custom_ts
        )
        samples = _drain(inlet, 1)
        assert samples
        label, ts = samples[0]
        assert label == "breath:inhale:block=2:cycle=1:pass=A"
        # Timestamp should be propagated verbatim.
        assert ts == pytest.approx(custom_ts, abs=1e-3)
    finally:
        inlet.close_stream()


def test_round_trip_every_marker_family(outlet: MuseProxyMarkerOutlet) -> None:
    """Every marker family in the schema round-trips through pylsl."""
    inlet = _resolve_inlet(outlet.source_id)
    try:
        outlet.session_start(pid="P001", date_yyyymmdd="20260512")
        outlet.intake(
            age=31,
            sex="F",
            handedness="R",
            glasses=False,
            dental_work=False,
            facial_pain=False,
            caffeine_hours_ago=2.5,
            last_meal_hours_ago=3.0,
            sleep_hours=7.5,
            recent_exercise=False,
            stress_self_report=3,
            trip_stress=False,
            hydration=True,
            warm=True,
        )
        outlet.device_marker(pass_label="A", device="muse-2")
        outlet.device_marker(pass_label="B", device="muse-athena")
        outlet.pass_start(pass_label="A", device="muse-2")
        outlet.volume_check_start()
        outlet.volume_check_end(level="ok")
        outlet.settle_start(pass_label="A", duration_planned_s=300.0)
        outlet.settle_end(pass_label="A", duration_elapsed_s=287.0)
        outlet.block_start(block_id=1, label="baseline", pass_label="A", duration_planned_s=300.0)
        outlet.block_end(block_id=1, label="baseline", pass_label="A", duration_actual_s=300.5)
        outlet.between_block_settle_start(after_block=1)
        outlet.between_block_settle_end(after_block=1, duration_s=15.5)
        outlet.tts(clip="close_eyes", kind="start", lsl_timestamp=pylsl.local_clock())
        outlet.tts(clip="close_eyes", kind="end", lsl_timestamp=pylsl.local_clock())
        outlet.breath("inhale", block_id=2, cycle=1, pass_label="A", lsl_timestamp=pylsl.local_clock())
        outlet.breath("exhale", block_id=2, cycle=1, pass_label="A", lsl_timestamp=pylsl.local_clock())
        outlet.artifact(label="blink_slow", kind="start", pass_label="A", lsl_timestamp=pylsl.local_clock())
        outlet.artifact(label="blink_slow", kind="end", pass_label="A", lsl_timestamp=pylsl.local_clock())
        outlet.pass_end(pass_label="A")
        outlet.session_end(pid="P001")

        samples = _drain(inlet, 21, timeout_s=5.0)
        labels = [s[0] for s in samples]
        # Sanity: every family is represented.
        assert any(s.startswith("session_start:") for s in labels)
        assert any(s.startswith("intake:") for s in labels)
        assert any(s.startswith("pass_start:") for s in labels)
        assert any(s.startswith("volume_check:start") for s in labels)
        assert any(s.startswith("settle:start:") for s in labels)
        assert any(s.startswith("block_start:") for s in labels)
        assert any(s.startswith("between_block_settle:start:") for s in labels)
        assert any(s.startswith("tts:close_eyes:start") for s in labels)
        assert any(s.startswith("breath:inhale:") for s in labels)
        assert any(s.startswith("artifact:blink_slow:start:") for s in labels)
        assert any(s.startswith("pass_end:") for s in labels)
        assert any(s.startswith("session_end:") for s in labels)
    finally:
        inlet.close_stream()


def test_csv_mirror_records_pushes(outlet: MuseProxyMarkerOutlet, tmp_path: Path) -> None:
    outlet.push("block_start", {"block": 1, "label": "baseline", "pass": "A", "duration_planned_s": 300.0})
    outlet.push("block_end", {"block": 1, "label": "baseline", "pass": "A", "duration_actual_s": 300.5})
    # CSV path was registered in fixture.
    csv_text = (tmp_path / "markers.csv").read_text(encoding="utf-8")
    assert "block_start:block=1:label=baseline:pass=A:duration_planned_s=300" in csv_text
    assert "block_end:" in csv_text
    # 1 header + 2 rows
    assert len(csv_text.strip().splitlines()) == 3


def test_close_makes_push_fail(outlet: MuseProxyMarkerOutlet) -> None:
    outlet.push("session_start")
    outlet.close()
    with pytest.raises(RuntimeError):
        outlet.push("session_end")


def test_close_is_idempotent(outlet: MuseProxyMarkerOutlet) -> None:
    outlet.close()
    outlet.close()  # second call should not raise


def test_invalid_breath_kind_raises(outlet: MuseProxyMarkerOutlet) -> None:
    with pytest.raises(ValueError):
        outlet.breath("kick", block_id=2, cycle=1, pass_label="A", lsl_timestamp=0.0)


def test_invalid_artifact_kind_raises(outlet: MuseProxyMarkerOutlet) -> None:
    with pytest.raises(ValueError):
        outlet.artifact(label="x", kind="middle", pass_label="A", lsl_timestamp=0.0)


def test_invalid_tts_kind_raises(outlet: MuseProxyMarkerOutlet) -> None:
    with pytest.raises(ValueError):
        outlet.tts(clip="x", kind="middle")


# ---------------------------------------------------------------------------
# Pass-switch — outlet open / close lifecycle (no marker leakage)
# ---------------------------------------------------------------------------


def test_pass_a_close_then_pass_b_open_yields_distinct_source_ids(
    tmp_path: Path,
) -> None:
    src_a = f"MuseProxy_passA_TEST_{int(time.time() * 1e6)}"
    src_b = f"MuseProxy_passB_TEST_{int(time.time() * 1e6)}"
    o_a = MuseProxyMarkerOutlet(
        outlet_name="MuseProxyMarkers",
        outlet_type="Markers",
        source_id=src_a,
        csv_path=tmp_path / "markers_A.csv",
    )
    o_a.push("session_start")
    o_a.close()

    o_b = MuseProxyMarkerOutlet(
        outlet_name="MuseProxyMarkers",
        outlet_type="Markers",
        source_id=src_b,
        csv_path=tmp_path / "markers_B.csv",
    )
    o_b.push("session_start")
    o_b.close()

    assert src_a != src_b
    # Each pass got its own CSV.
    assert (tmp_path / "markers_A.csv").exists()
    assert (tmp_path / "markers_B.csv").exists()
    a_csv = (tmp_path / "markers_A.csv").read_text(encoding="utf-8")
    b_csv = (tmp_path / "markers_B.csv").read_text(encoding="utf-8")
    assert "session_start" in a_csv
    assert "session_start" in b_csv
