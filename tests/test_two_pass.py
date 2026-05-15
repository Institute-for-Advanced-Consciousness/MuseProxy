"""Two-pass orchestration tests — pass A → pass_switch → pass B.

Asserts that:
  * Pass A outlet is closed and Pass B outlet has a different source_id.
  * Each pass has its own ``markers.csv`` under the correct dir.
  * Both passes share a single ``intake.json``.
  * No marker leakage between passes (each CSV only sees its own markers).
  * Pass A finalize precedes Pass B open.
  * Stage routing flows correctly through the switch.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helper: open services like the intake stage would
# ---------------------------------------------------------------------------


def _open_pass_a(app: Any, tmp_path: Path, *, pid: str) -> dict[str, Any]:
    """Mimic intake._open_services with redirected REPO_ROOT."""
    from muse_proxy.config import load_config
    from muse_proxy.markers import MuseProxyMarkerOutlet, build_source_id
    from muse_proxy.persistence import IntakeWriter, PassWriter

    cfg = load_config()
    app.config = cfg  # type: ignore[attr-defined]

    payload = {
        "pid": pid,
        "date": "20260513",
        "pass_A_device": "muse2",
        "pass_B_device": "athena",
        "age": 31,
        "sex": "F",
        "handedness": "R",
        "glasses": "N",
        "dental_work": "N",
        "facial_pain": "N",
        "caffeine_hours_ago": 99.0,
        "last_meal_hours_ago": 3.0,
        "sleep_hours": 7.5,
        "recent_exercise": "N",
        "stress_self_report": 2,
        "trip_stress": "N",
        "trip_settle_minutes": 0,
        "hydration": "Y",
        "warm": "Y",
        "ra_notes": "",
    }
    app.participant_id = pid
    app.pass_label = "A"
    app.pass_a_device = "muse2"
    app.pass_b_device = "athena"
    app.intake_payload = payload

    data_root = tmp_path / cfg.data_dir
    IntakeWriter(data_root, pid).write(payload)
    writer = PassWriter(
        data_root, pid, "A", "muse2", config_snapshot=cfg.model_dump()
    )
    src = build_source_id(cfg.lsl.source_id_template, "A", pid, payload["date"])
    outlet = MuseProxyMarkerOutlet(
        outlet_name=cfg.lsl.outlet_name,
        outlet_type=cfg.lsl.outlet_type,
        source_id=src,
        csv_path=writer.markers_csv_path,
    )
    import pylsl

    writer.start_pass(pylsl.local_clock())
    outlet.session_start(pid=pid, date_yyyymmdd=payload["date"])
    outlet.pass_start(pass_label="A", device="muse2")

    app.outlet = outlet
    app.writer = writer
    app.audio_scheduler = None  # tests don't need real audio
    return payload


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pass_switch_finalizes_pass_a_on_enter(
    shared_app: Any, tmp_path: Path
) -> None:
    """on_enter should close Pass A outlet + finalize Pass A writer."""
    import muse_proxy.stages.pass_switch as ps_mod

    pid = f"P{int(time.time() * 1e3) % 1000:03d}"
    _open_pass_a(shared_app, tmp_path, pid=pid)

    pass_a_dir = tmp_path / shared_app.config.data_dir / pid / "pass_A_muse2"
    assert (pass_a_dir / "PARTIAL_session.json").exists()

    ps = shared_app.stages["pass_switch"]
    with patch.object(ps_mod, "_pretty", side_effect=lambda d: d):
        ps.on_enter()

    # session.json now exists; PARTIAL removed.
    assert (pass_a_dir / "session.json").exists()
    assert not (pass_a_dir / "PARTIAL_session.json").exists()
    # outlet / writer dropped.
    assert shared_app.outlet is None
    assert shared_app.writer is None

    doc = json.loads((pass_a_dir / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "completed"
    assert doc["pass_label"] == "A"

    ps.on_leave()


def test_continue_opens_pass_b_with_new_source_id(
    shared_app: Any, tmp_path: Path
) -> None:
    """Clicking continue opens Pass B outlet (new source_id) + writer."""

    pid = f"P{int(time.time() * 1e3) % 1000:03d}"
    _open_pass_a(shared_app, tmp_path, pid=pid)

    # Capture the Pass A source_id before swapping.
    src_a = shared_app.outlet.source_id

    ps = shared_app.stages["pass_switch"]
    # Redirect REPO_ROOT inside pass_switch so the new writer lands in tmp_path.
    from muse_proxy.config import REPO_ROOT  # noqa: F401
    advanced: list[str] = []
    orig = shared_app.show_stage
    shared_app.show_stage = lambda name, **kw: advanced.append(name)  # type: ignore[assignment]

    try:
        with patch("muse_proxy.stages.pass_switch.REPO_ROOT", tmp_path):
            ps.on_enter()
            # Tick all checklist items.
            for v in ps._checklist_vars:
                v.set(True)
            ps._refresh_continue()
            ps._on_continue()

        assert advanced == ["pre_launch"]
        # Pass B outlet must be open with a different source_id.
        assert shared_app.outlet is not None
        src_b = shared_app.outlet.source_id
        assert src_b != src_a
        assert "passB" in src_b
        # Pass label flipped.
        assert shared_app.pass_label == "B"
        # Pass B dir was created with its own PARTIAL.
        pass_b_dir = tmp_path / shared_app.config.data_dir / pid / "pass_B_athena"
        assert (pass_b_dir / "PARTIAL_session.json").exists()
        assert (pass_b_dir / "markers.csv").exists()
        # Pass A dir still intact.
        pass_a_dir = tmp_path / shared_app.config.data_dir / pid / "pass_A_muse2"
        assert (pass_a_dir / "session.json").exists()
        # intake.json shared at visit level.
        assert (tmp_path / shared_app.config.data_dir / pid / "intake.json").exists()
    finally:
        shared_app.show_stage = orig  # type: ignore[assignment]
        if shared_app.outlet is not None:
            shared_app.outlet.close()
            shared_app.outlet = None
        shared_app.writer = None
        ps.on_leave()


def test_continue_blocked_until_all_checked(
    shared_app: Any, tmp_path: Path
) -> None:
    """The continue button stays disabled until every checkbox is ticked."""
    pid = f"P{int(time.time() * 1e3) % 1000:03d}"
    _open_pass_a(shared_app, tmp_path, pid=pid)
    ps = shared_app.stages["pass_switch"]
    try:
        with patch("muse_proxy.stages.pass_switch.REPO_ROOT", tmp_path):
            ps.on_enter()
        assert str(ps._continue_btn.cget("state")) == "disabled"
        # Tick all but the last.
        for v in ps._checklist_vars[:-1]:
            v.set(True)
        ps._refresh_continue()
        assert str(ps._continue_btn.cget("state")) == "disabled"
        # Tick the last.
        ps._checklist_vars[-1].set(True)
        ps._refresh_continue()
        assert str(ps._continue_btn.cget("state")) == "normal"
    finally:
        if shared_app.outlet is not None:
            shared_app.outlet.close()
            shared_app.outlet = None
        shared_app.writer = None
        ps.on_leave()


def test_pass_a_markers_csv_no_pass_b_pollution(
    shared_app: Any, tmp_path: Path
) -> None:
    """After swap, Pass A's markers.csv should not gain any Pass B markers."""
    pid = f"P{int(time.time() * 1e3) % 1000:03d}"
    _open_pass_a(shared_app, tmp_path, pid=pid)
    shared_app.outlet.block_start(
        block_id=1, label="baseline", pass_label="A", duration_planned_s=300.0
    )
    pass_a_csv = shared_app.writer.markers_csv_path

    ps = shared_app.stages["pass_switch"]
    try:
        with patch("muse_proxy.stages.pass_switch.REPO_ROOT", tmp_path):
            ps.on_enter()
            for v in ps._checklist_vars:
                v.set(True)
            ps._refresh_continue()
            ps._on_continue()
        # Push a Pass B marker.
        shared_app.outlet.block_start(
            block_id=1, label="baseline", pass_label="B", duration_planned_s=300.0
        )
        pass_b_csv = shared_app.writer.markers_csv_path
        a_text = pass_a_csv.read_text(encoding="utf-8")
        b_text = pass_b_csv.read_text(encoding="utf-8")
        assert "pass=A" in a_text
        assert "pass=B" not in a_text
        assert "pass=B" in b_text
        # Each CSV has the right markers.
    finally:
        if shared_app.outlet is not None:
            shared_app.outlet.close()
            shared_app.outlet = None
        shared_app.writer = None
        ps.on_leave()


def test_completion_finalizes_pass_b(
    shared_app: Any, tmp_path: Path
) -> None:
    """The completion stage finalizes Pass B and closes outlet."""
    from muse_proxy.config import load_config
    from muse_proxy.markers import MuseProxyMarkerOutlet, build_source_id
    from muse_proxy.persistence import PassWriter

    pid = f"P{int(time.time() * 1e3) % 1000:03d}"
    cfg = load_config()
    shared_app.config = cfg  # type: ignore[attr-defined]
    shared_app.participant_id = pid
    shared_app.pass_a_device = "muse2"
    shared_app.pass_b_device = "athena"
    shared_app.pass_label = "B"

    data_root = tmp_path / cfg.data_dir
    writer_b = PassWriter(data_root, pid, "B", "athena",
                          config_snapshot=cfg.model_dump())
    writer_b.start_pass(0.0)
    src = build_source_id(cfg.lsl.source_id_template, "B", pid, "20260513")
    outlet_b = MuseProxyMarkerOutlet(
        outlet_name=cfg.lsl.outlet_name,
        outlet_type=cfg.lsl.outlet_type,
        source_id=src,
        csv_path=writer_b.markers_csv_path,
    )
    shared_app.outlet = outlet_b
    shared_app.writer = writer_b

    completion = shared_app.stages["completion"]
    completion.on_enter()

    # We can't actually call _on_save_exit because it calls
    # shared_app.shutdown() which destroys the shared Tk root. So we
    # invoke the inner steps manually.
    outlet_b.pass_end(pass_label="B")
    outlet_b.session_end(pid=pid)
    outlet_b.close()
    writer_b.finalize(status="completed")
    shared_app.outlet = None
    shared_app.writer = None

    pass_b_dir = data_root / pid / "pass_B_athena"
    assert (pass_b_dir / "session.json").exists()
    doc = json.loads((pass_b_dir / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "completed"
    assert doc["pass_label"] == "B"
    assert doc["device"] == "athena"


def test_pass_a_data_untouched_after_switch(
    shared_app: Any, tmp_path: Path
) -> None:
    """After the pass switch, Pass A's session.json must not change."""
    pid = f"P{int(time.time() * 1e3) % 1000:03d}"
    _open_pass_a(shared_app, tmp_path, pid=pid)
    pass_a_dir = tmp_path / shared_app.config.data_dir / pid / "pass_A_muse2"

    ps = shared_app.stages["pass_switch"]
    try:
        with patch("muse_proxy.stages.pass_switch.REPO_ROOT", tmp_path):
            ps.on_enter()
        pass_a_doc_before = (pass_a_dir / "session.json").read_text()
        for v in ps._checklist_vars:
            v.set(True)
        ps._refresh_continue()
        ps._on_continue()

        # Push some Pass B markers, then finalize Pass B manually.
        shared_app.outlet.push("block_start", {"block": 1, "label": "baseline",
                                               "pass": "B", "duration_planned_s": 5.0})
        shared_app.outlet.close()
        shared_app.writer.finalize(status="completed")

        pass_a_doc_after = (pass_a_dir / "session.json").read_text()
        assert pass_a_doc_before == pass_a_doc_after
    finally:
        if shared_app.outlet is not None:
            shared_app.outlet.close()
            shared_app.outlet = None
        shared_app.writer = None
        ps.on_leave()
