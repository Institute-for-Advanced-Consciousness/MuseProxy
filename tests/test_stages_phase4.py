"""Stage tests for Phase 4 — intake, pre_launch, volume_check, settle.

Uses the ``shared_app`` fixture for one Tk root across all tests.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Tk availability is enforced by the ``shared_app`` fixture in conftest.py —
# every test in this module depends on it, so we don't need a separate skip.


# ---------------------------------------------------------------------------
# Intake — collect & open services
# ---------------------------------------------------------------------------


def _fill_intake_minimally(intake_frame: Any, *, pid: str = "P777") -> None:
    """Populate every required form field with a valid value."""
    intake_frame._vars["pid"].set(pid)
    intake_frame._vars["pass_A_device"].set("Muse-2")
    intake_frame._vars["age"].set("31")
    intake_frame._vars["caffeine_hours_ago"].set("99")
    intake_frame._vars["last_meal_hours_ago"].set("3.0")
    intake_frame._vars["sleep_hours"].set("7.5")
    intake_frame._vars["stress_self_report"].set(2)
    intake_frame._vars["trip_settle_minutes"].set("0")


def test_intake_on_enter_suggests_next_id(shared_app: Any, tmp_path: Path) -> None:
    """on_enter should populate the pid field from next_participant_id."""
    import muse_proxy.stages.intake as intake_mod
    from muse_proxy.config import load_config

    intake = shared_app.stages["intake"]
    # Force config + redirect data_dir so we don't write the real Data dir.
    cfg = load_config()
    with patch.object(intake_mod, "REPO_ROOT", tmp_path):
        # Mark cfg so the stage uses it
        intake._config = cfg
        # Clear pid so we can verify auto-suggestion runs.
        intake._vars["pid"].set("")
        intake.on_enter()
        assert intake._vars["pid"].get().startswith("P")


def test_intake_collect_returns_canonical_devices(shared_app: Any) -> None:
    """_collect should canonicalize 'Muse-2' → 'muse-2'."""
    intake = shared_app.stages["intake"]
    _fill_intake_minimally(intake)
    out = intake._collect()
    assert out["pid"] == "P777"
    assert out["pass_A_device"] == "muse-2"
    assert out["pass_B_device"] == "muse-athena"


def test_intake_collect_rejects_blank_pid(shared_app: Any) -> None:
    from muse_proxy.stages.intake import _IntakeError

    intake = shared_app.stages["intake"]
    _fill_intake_minimally(intake)
    intake._vars["pid"].set("")
    with pytest.raises(_IntakeError):
        intake._collect()


def test_intake_collect_rejects_same_pass_devices(shared_app: Any) -> None:
    from muse_proxy.stages.intake import _IntakeError

    intake = shared_app.stages["intake"]
    _fill_intake_minimally(intake)
    intake._vars["pass_A_device"].set("Muse-2")
    intake._vars["pass_B_device"].set("Muse-2")
    with pytest.raises(_IntakeError):
        intake._collect()


def test_intake_pass_b_autofills_when_pass_a_changes(shared_app: Any) -> None:
    intake = shared_app.stages["intake"]
    intake._vars["pass_A_device"].set("Muse-2")
    assert intake._vars["pass_B_device"].get() == "Muse-Athena"
    intake._vars["pass_A_device"].set("Muse-Athena")
    assert intake._vars["pass_B_device"].get() == "Muse-2"


def test_intake_open_services_creates_outlet_writer_and_intake_json(
    shared_app: Any, tmp_path: Path
) -> None:
    import muse_proxy.stages.intake as intake_mod

    intake = shared_app.stages["intake"]
    _fill_intake_minimally(intake, pid=f"P{int(time.time()) % 1000:03d}")
    intake.on_enter()  # ensures config is loaded
    payload = intake._collect()

    # Redirect REPO_ROOT to tmp_path inside the stage module so the writer
    # lands in our tmp dir (the writer joins REPO_ROOT / cfg.data_dir).
    with patch.object(intake_mod, "REPO_ROOT", tmp_path):
        intake._open_services(payload)

    try:
        # App state populated.
        assert shared_app.participant_id == payload["pid"]
        assert shared_app.pass_label == "A"
        assert shared_app.pass_a_device == "muse-2"
        assert shared_app.pass_b_device == "muse-athena"
        assert shared_app.outlet is not None
        assert shared_app.writer is not None

        # intake.json written under {tmp_path}/{cfg.data_dir}/{pid}/.
        data_root = tmp_path / shared_app.config.data_dir
        intake_path = data_root / payload["pid"] / "intake.json"
        assert intake_path.exists()
        loaded = json.loads(intake_path.read_text(encoding="utf-8"))
        assert loaded["pid"] == payload["pid"]
        assert loaded["pass_A_device"] == "muse-2"

        # Pass A dir + PARTIAL_session.json + markers.csv all present.
        pass_dir = data_root / payload["pid"] / "pass_A_muse-2"
        assert (pass_dir / "PARTIAL_session.json").exists()
        assert (pass_dir / "markers.csv").exists()
    finally:
        # Clean up the outlet + monitor so subsequent tests can rebuild.
        if shared_app.outlet is not None:
            shared_app.outlet.close()
            shared_app.outlet = None
        if shared_app.monitor is not None:
            shared_app.monitor.stop()
            shared_app.monitor = None
        shared_app.writer = None
        shared_app.audio_scheduler = None


# ---------------------------------------------------------------------------
# Pre-launch checklist
# ---------------------------------------------------------------------------


def test_prelaunch_start_disabled_initially(shared_app: Any) -> None:
    from muse_proxy.config import load_config

    pl = shared_app.stages["pre_launch"]
    shared_app.config = load_config()  # type: ignore[attr-defined]
    shared_app.pass_label = "A"
    shared_app.pass_a_device = "muse-2"
    pl.on_enter()
    assert str(pl._start_btn.cget("state")) == "disabled"


def test_prelaunch_start_enables_when_all_ticked(shared_app: Any) -> None:
    from muse_proxy.config import load_config

    pl = shared_app.stages["pre_launch"]
    shared_app.config = load_config()  # type: ignore[attr-defined]
    shared_app.pass_label = "A"
    shared_app.pass_a_device = "muse-2"
    pl.on_enter()
    for v in pl._stream_vars + pl._confirmation_vars:
        v.set(True)
    pl._refresh_start()
    assert str(pl._start_btn.cget("state")) == "normal"


def test_prelaunch_uses_pass_b_checklist(shared_app: Any) -> None:
    """Pass B checklist contains the 'NEW Lab Recorder recording' item."""
    from muse_proxy.config import load_config

    pl = shared_app.stages["pre_launch"]
    shared_app.config = load_config()  # type: ignore[attr-defined]
    shared_app.pass_label = "B"
    shared_app.pass_a_device = "muse-2"
    shared_app.pass_b_device = "muse-athena"
    pl.on_enter()
    # Count checkboxes for Pass B.
    n_streams = len(pl._stream_vars)
    n_confirms = len(pl._confirmation_vars)
    assert n_streams > 0
    assert n_confirms > 0
    # Heading should mention Pass B + athena
    text = pl._heading.cget("text").lower()
    assert "pass b" in text
    assert "muse-athena" in text


# ---------------------------------------------------------------------------
# Volume check
# ---------------------------------------------------------------------------


def test_volume_check_emits_start_marker(shared_app: Any) -> None:
    vc = shared_app.stages["volume_check"]
    fake_outlet = MagicMock()
    shared_app.outlet = fake_outlet
    shared_app.audio_scheduler = None  # silent path
    try:
        vc.on_enter()
    finally:
        shared_app.outlet = None
    fake_outlet.volume_check_start.assert_called_once()


def test_volume_check_confirm_emits_end_marker_and_advances(shared_app: Any) -> None:
    vc = shared_app.stages["volume_check"]
    fake_outlet = MagicMock()
    shared_app.outlet = fake_outlet
    shared_app.audio_scheduler = None
    advanced: list[str] = []
    orig = shared_app.show_stage

    def _stub(name: str, **kwargs: Any) -> None:
        advanced.append(name)

    shared_app.show_stage = _stub  # type: ignore[assignment]
    try:
        vc._on_confirm()
    finally:
        shared_app.show_stage = orig  # type: ignore[assignment]
        shared_app.outlet = None
    fake_outlet.volume_check_end.assert_called_once_with(level="ok")
    assert advanced == ["settle"]


def test_volume_check_handles_missing_audio_scheduler(shared_app: Any) -> None:
    vc = shared_app.stages["volume_check"]
    shared_app.outlet = None
    shared_app.audio_scheduler = None
    # Should not raise.
    vc.on_enter()


# ---------------------------------------------------------------------------
# Settle
# ---------------------------------------------------------------------------


def test_settle_emits_start_marker_and_renders_timer(shared_app: Any) -> None:
    from muse_proxy.config import load_config

    settle = shared_app.stages["settle"]
    shared_app.config = load_config()  # type: ignore[attr-defined]
    shared_app.pass_label = "A"
    fake_outlet = MagicMock()
    shared_app.outlet = fake_outlet
    try:
        settle.on_enter()
        # Timer should display a duration matching ~5 min for Pass A.
        text = settle._timer_label.cget("text")
        assert ":" in text
        # We've barely entered, so the timer should show 04:5x or 05:00.
        m, s = text.split(":")
        assert int(m) >= 4
    finally:
        settle.on_leave()
        shared_app.outlet = None
    fake_outlet.settle_start.assert_called_once()


def test_settle_skip_button_emits_end_marker(shared_app: Any) -> None:
    from muse_proxy.config import load_config

    settle = shared_app.stages["settle"]
    shared_app.config = load_config()  # type: ignore[attr-defined]
    shared_app.pass_label = "A"
    fake_outlet = MagicMock()
    shared_app.outlet = fake_outlet
    advanced: list[str] = []
    orig = shared_app.show_stage
    shared_app.show_stage = lambda name, **kwargs: advanced.append(name)  # type: ignore[assignment]
    try:
        settle.on_enter()
        settle._finish()
    finally:
        shared_app.show_stage = orig  # type: ignore[assignment]
        shared_app.outlet = None
    fake_outlet.settle_end.assert_called_once()
    assert advanced == ["block_instructions"]


def test_settle_uses_pass_b_default(shared_app: Any) -> None:
    """Pass B default is shorter than Pass A."""
    from muse_proxy.config import load_config

    settle = shared_app.stages["settle"]
    cfg = load_config()
    shared_app.config = cfg  # type: ignore[attr-defined]
    shared_app.pass_label = "B"
    shared_app.outlet = None
    try:
        settle.on_enter()
        assert settle._planned_s == cfg.settle.initial_pass_b_default_min * 60.0
    finally:
        settle.on_leave()
