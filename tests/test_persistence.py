"""Tests for muse_proxy.persistence — atomic writes, two-pass layout, abort behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from muse_proxy.persistence import (
    BlockRecord,
    IntakeWriter,
    MarkerCsvAppender,
    PassWriter,
    atomic_write_json,
    atomic_write_text,
    pass_dir,
)

# ---------------------------------------------------------------------------
# atomic writes
# ---------------------------------------------------------------------------


def test_atomic_write_text_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "subdir" / "out.txt"
    atomic_write_text(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"


def test_atomic_write_json_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "data.json"
    atomic_write_json(p, {"a": 1, "b": [1, 2, 3]})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "b": [1, 2, 3]}


def test_atomic_write_no_residual_tmp(tmp_path: Path) -> None:
    p = tmp_path / "out.txt"
    atomic_write_text(p, "x")
    # The temp file should be gone.
    leftovers = [c for c in tmp_path.iterdir() if c.name.startswith(".out.txt")]
    assert leftovers == []


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    p = tmp_path / "out.txt"
    p.write_text("old", encoding="utf-8")
    atomic_write_text(p, "new")
    assert p.read_text(encoding="utf-8") == "new"


# ---------------------------------------------------------------------------
# pass_dir
# ---------------------------------------------------------------------------


def test_pass_dir_layout(tmp_path: Path) -> None:
    d = pass_dir(tmp_path, "P007", "A", "muse-2")
    assert d == tmp_path / "P007" / "pass_A_muse-2"


def test_pass_dir_invalid_label(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        pass_dir(tmp_path, "P001", "C", "muse-2")


# ---------------------------------------------------------------------------
# IntakeWriter
# ---------------------------------------------------------------------------


def test_intake_writer_writes_under_pid_dir(tmp_path: Path) -> None:
    intake = {"pid": "P001", "age": 31, "ra_notes": "no notes"}
    w = IntakeWriter(tmp_path, "P001")
    p = w.write(intake)
    assert p == tmp_path / "P001" / "intake.json"
    assert json.loads(p.read_text(encoding="utf-8")) == intake


def test_intake_writer_idempotent_with_same_content(tmp_path: Path) -> None:
    intake = {"pid": "P001", "age": 31}
    w = IntakeWriter(tmp_path, "P001")
    w.write(intake)
    w.write(intake)
    assert json.loads((tmp_path / "P001" / "intake.json").read_text(encoding="utf-8")) == intake


def test_intake_writer_rejects_divergent_overwrite(tmp_path: Path) -> None:
    w = IntakeWriter(tmp_path, "P001")
    w.write({"pid": "P001", "age": 31})
    with pytest.raises(FileExistsError):
        w.write({"pid": "P001", "age": 32})


# ---------------------------------------------------------------------------
# PassWriter — happy path
# ---------------------------------------------------------------------------


def test_pass_writer_creates_dir_and_partial(tmp_path: Path) -> None:
    w = PassWriter(tmp_path, "P001", "A", "muse-2", config_snapshot={"k": "v"})
    assert w.dir == tmp_path / "P001" / "pass_A_muse-2"
    w.start_pass(start_lsl_ts=100.0)
    partial = w.dir / "PARTIAL_session.json"
    assert partial.exists()
    doc = json.loads(partial.read_text(encoding="utf-8"))
    assert doc["pid"] == "P001"
    assert doc["pass_label"] == "A"
    assert doc["device"] == "muse-2"
    assert doc["status"] == "in_progress"


def test_pass_writer_records_block_lifecycle(tmp_path: Path) -> None:
    w = PassWriter(tmp_path, "P001", "A", "muse-2")
    w.start_pass(start_lsl_ts=1000.0)
    rec = w.start_block(1, "baseline", 300.0, start_lsl=1001.0)
    assert isinstance(rec, BlockRecord)
    assert rec.status == "in_progress"
    w.end_block(rec, end_lsl=1301.0, events=[{"label": "block_start", "kind": "marker", "lsl_ts": 1001.0}], status="completed")
    assert rec.status == "completed"
    assert rec.duration_actual_s == pytest.approx(300.0)

    block_csv = w.dir / "block_01_baseline.csv"
    assert block_csv.exists()
    content = block_csv.read_text(encoding="utf-8")
    assert "block_start" in content
    assert "completed" in content


def test_pass_writer_finalize_completed_removes_partial(tmp_path: Path) -> None:
    w = PassWriter(tmp_path, "P001", "A", "muse-2")
    w.start_pass(start_lsl_ts=0.0)
    rec = w.start_block(1, "baseline", 5.0, start_lsl=0.0)
    w.end_block(rec, end_lsl=5.0)
    path = w.finalize(status="completed")
    assert path.name == "session.json"
    assert path.exists()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["status"] == "completed"
    assert not (w.dir / "PARTIAL_session.json").exists()


def test_pass_writer_finalize_aborted_retains_partial(tmp_path: Path) -> None:
    w = PassWriter(tmp_path, "P001", "A", "muse-2")
    w.start_pass(start_lsl_ts=0.0)
    w.start_block(1, "baseline", 5.0, start_lsl=0.0)
    # never call end_block - block stays in_progress
    w.mark_pending_blocks_aborted()
    w.finalize(status="aborted")
    assert (w.dir / "session.json").exists()
    assert (w.dir / "PARTIAL_session.json").exists()
    doc = json.loads((w.dir / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "aborted"
    assert doc["blocks"][0]["status"] == "aborted"


def test_mark_pending_blocks_aborted_writes_block_csv(tmp_path: Path) -> None:
    w = PassWriter(tmp_path, "P001", "A", "muse-2")
    w.start_pass(start_lsl_ts=0.0)
    w.start_block(1, "baseline", 5.0, start_lsl=0.0)
    n = w.mark_pending_blocks_aborted()
    assert n == 1
    block_csv = w.dir / "block_01_baseline.csv"
    assert block_csv.exists()
    assert "aborted" in block_csv.read_text(encoding="utf-8")


def test_mark_pending_blocks_aborted_skips_completed(tmp_path: Path) -> None:
    w = PassWriter(tmp_path, "P001", "A", "muse-2")
    w.start_pass(start_lsl_ts=0.0)
    rec = w.start_block(1, "baseline", 5.0, start_lsl=0.0)
    w.end_block(rec, end_lsl=5.0)
    rec2 = w.start_block(2, "paced_0p1Hz", 5.0, start_lsl=10.0)
    n = w.mark_pending_blocks_aborted()
    assert n == 1  # only block 2
    assert rec.status == "completed"
    assert rec2.status == "aborted"


def test_pass_writer_finalize_bad_status_raises(tmp_path: Path) -> None:
    w = PassWriter(tmp_path, "P001", "A", "muse-2")
    w.start_pass(start_lsl_ts=0.0)
    with pytest.raises(ValueError):
        w.finalize(status="kinda done")


def test_end_block_bad_status_raises(tmp_path: Path) -> None:
    w = PassWriter(tmp_path, "P001", "A", "muse-2")
    w.start_pass(start_lsl_ts=0.0)
    rec = w.start_block(1, "baseline", 5.0, start_lsl=0.0)
    with pytest.raises(ValueError):
        w.end_block(rec, end_lsl=5.0, status="kinda done")


# ---------------------------------------------------------------------------
# Two-pass directory layout
# ---------------------------------------------------------------------------


def test_two_pass_separate_dirs(tmp_path: Path) -> None:
    """The two passes write to distinct dirs; intake.json is shared."""
    # Pass A
    w_a = PassWriter(tmp_path, "P001", "A", "muse-2", config_snapshot={"pass": "A"})
    w_a.start_pass(start_lsl_ts=0.0)
    rec = w_a.start_block(1, "baseline", 5.0, start_lsl=0.0)
    w_a.end_block(rec, end_lsl=5.0)
    w_a.finalize(status="completed")

    # Intake shared (writer can be re-used; same dict)
    intake = {"pid": "P001", "age": 31}
    IntakeWriter(tmp_path, "P001").write(intake)

    # Pass B
    w_b = PassWriter(tmp_path, "P001", "B", "muse-athena", config_snapshot={"pass": "B"})
    w_b.start_pass(start_lsl_ts=1000.0)
    rec_b = w_b.start_block(1, "baseline", 5.0, start_lsl=1000.0)
    w_b.end_block(rec_b, end_lsl=1005.0)
    w_b.finalize(status="completed")

    assert (tmp_path / "P001" / "intake.json").exists()
    assert (tmp_path / "P001" / "pass_A_muse-2" / "session.json").exists()
    assert (tmp_path / "P001" / "pass_B_muse-athena" / "session.json").exists()
    # No cross-pollution: each session.json names its own pass.
    a = json.loads((tmp_path / "P001" / "pass_A_muse-2" / "session.json").read_text())
    b = json.loads((tmp_path / "P001" / "pass_B_muse-athena" / "session.json").read_text())
    assert a["pass_label"] == "A" and a["device"] == "muse-2"
    assert b["pass_label"] == "B" and b["device"] == "muse-athena"
    assert a["config_snapshot"] == {"pass": "A"}
    assert b["config_snapshot"] == {"pass": "B"}


def test_pass_b_abort_does_not_touch_pass_a(tmp_path: Path) -> None:
    """If Pass B aborts, Pass A's session.json must remain intact."""
    w_a = PassWriter(tmp_path, "P001", "A", "muse-2")
    w_a.start_pass(start_lsl_ts=0.0)
    rec = w_a.start_block(1, "baseline", 5.0, start_lsl=0.0)
    w_a.end_block(rec, end_lsl=5.0)
    w_a.finalize(status="completed")
    pass_a_doc_before = (tmp_path / "P001" / "pass_A_muse-2" / "session.json").read_text()

    w_b = PassWriter(tmp_path, "P001", "B", "muse-athena")
    w_b.start_pass(start_lsl_ts=1000.0)
    w_b.start_block(1, "baseline", 5.0, start_lsl=1000.0)
    w_b.mark_pending_blocks_aborted()
    w_b.finalize(status="aborted")

    pass_a_doc_after = (tmp_path / "P001" / "pass_A_muse-2" / "session.json").read_text()
    assert pass_a_doc_before == pass_a_doc_after


# ---------------------------------------------------------------------------
# Markers CSV appender
# ---------------------------------------------------------------------------


def test_markers_csv_appender_writes_header_once(tmp_path: Path) -> None:
    p = tmp_path / "markers.csv"
    with MarkerCsvAppender(p) as a:
        a.append("2026-05-13T10:00:00", 12345.0, "session_start")
        a.append("2026-05-13T10:00:01", 12346.0, "block_start:block=1")
    text = p.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "timestamp_iso,timestamp_lsl,marker_string"
    assert "session_start" in text
    assert "block_start:block=1" in text


def test_markers_csv_appender_appends_without_re_header(tmp_path: Path) -> None:
    p = tmp_path / "markers.csv"
    a = MarkerCsvAppender(p)
    a.append("2026-05-13T10:00:00", 1.0, "first")
    a.close()
    a2 = MarkerCsvAppender(p)
    a2.append("2026-05-13T10:00:01", 2.0, "second")
    a2.close()
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    # 1 header + 2 rows
    assert len(lines) == 3
    assert lines[0].startswith("timestamp_iso")


def test_pass_writer_open_markers_csv_writes_to_pass_dir(tmp_path: Path) -> None:
    w = PassWriter(tmp_path, "P001", "A", "muse-2")
    with w.open_markers_csv() as a:
        a.append("2026-05-13T10:00:00", 1.0, "session_start")
    assert (w.dir / "markers.csv").exists()


# ---------------------------------------------------------------------------
# Block CSV — events
# ---------------------------------------------------------------------------


def test_block_csv_contains_event_rows(tmp_path: Path) -> None:
    w = PassWriter(tmp_path, "P001", "A", "muse-2")
    w.start_pass(start_lsl_ts=0.0)
    rec = w.start_block(2, "paced_0p1Hz", 300.0, start_lsl=0.0)
    events = [
        {"label": "breath:inhale", "kind": "marker", "lsl_ts": 0.0, "payload": {"cycle": 1}},
        {"label": "breath:exhale", "kind": "marker", "lsl_ts": 5.0, "payload": {"cycle": 1}},
    ]
    w.end_block(rec, end_lsl=300.0, events=events, status="completed")
    csv_text = (w.dir / "block_02_paced_0p1Hz.csv").read_text(encoding="utf-8")
    assert "breath:inhale" in csv_text
    assert "breath:exhale" in csv_text
    # JSON payload is quoted inside the CSV; check for the escaped form.
    assert '""cycle"": 1' in csv_text or '"cycle": 1' in csv_text
