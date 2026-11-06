"""Tests for scripts/verify_xdf.py — marker parsing and audit logic.

We don't try to build a real XDF here; instead we call the pure helpers
(``parse_payload``, ``audit_coverage``, ``audit_breathing``,
``audit_artifact_block``, ``audit_pass_purity``) directly. A separate
manual smoke-test would round-trip via a real Lab Recorder recording.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load scripts/verify_xdf.py as a module.
_REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "verify_xdf", _REPO_ROOT / "scripts" / "verify_xdf.py"
)
verify_xdf = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
assert spec and spec.loader
sys.modules["verify_xdf"] = verify_xdf
spec.loader.exec_module(verify_xdf)  # type: ignore[union-attr]


from muse_proxy.config import load_config  # noqa: E402

# ---------------------------------------------------------------------------
# parse_payload
# ---------------------------------------------------------------------------


def test_parse_payload_no_kv() -> None:
    pre, kv = verify_xdf.parse_payload("volume_check:start")
    assert pre == "volume_check:start"
    assert kv == {}


def test_parse_payload_with_kv() -> None:
    pre, kv = verify_xdf.parse_payload("breath:inhale:block=2:cycle=1:pass=A")
    assert pre == "breath:inhale"
    assert kv == {"block": "2", "cycle": "1", "pass": "A"}


# ---------------------------------------------------------------------------
# audit_pass_purity
# ---------------------------------------------------------------------------


def test_pass_purity_single_pass() -> None:
    markers = [
        ("block_start:block=1:label=baseline:pass=A:duration_planned_s=300", 0.0),
        ("breath:inhale:block=2:cycle=1:pass=A", 1.0),
        ("block_end:block=1:label=baseline:pass=A:duration_actual_s=300", 300.0),
    ]
    pass_label, problems = verify_xdf.audit_pass_purity(markers)
    assert pass_label == "A"
    assert problems == []


def test_pass_purity_mixed_passes() -> None:
    markers = [
        ("block_start:block=1:label=baseline:pass=A:duration_planned_s=300", 0.0),
        ("block_start:block=1:label=baseline:pass=B:duration_planned_s=300", 100.0),
    ]
    pass_label, problems = verify_xdf.audit_pass_purity(markers)
    assert pass_label is None
    assert any("mixed passes" in p for p in problems)


def test_pass_purity_no_pass_fields() -> None:
    markers = [
        ("session_start", 0.0),
    ]
    pass_label, problems = verify_xdf.audit_pass_purity(markers)
    assert pass_label is None
    assert any("WARN" in p for p in problems)


# ---------------------------------------------------------------------------
# audit_coverage
# ---------------------------------------------------------------------------


def test_audit_coverage_counts_families() -> None:
    markers = [
        ("session_start:pid=P001:date=20260513", 0.0),
        ("pass_start:pass=A:device=muse2:source_id=X", 1.0),
        ("block_start:block=1:label=baseline:pass=A:duration_planned_s=300", 2.0),
        ("block_end:block=1:label=baseline:pass=A:duration_actual_s=300", 302.0),
        ("breath:inhale:block=2:cycle=1:pass=A", 303.0),
        ("breath:exhale:block=2:cycle=1:pass=A", 308.0),
        ("session_end:pid=P001", 1000.0),
    ]
    coverage = verify_xdf.audit_coverage(markers)
    assert coverage["session_start"] == 1
    assert coverage["pass_start"] == 1
    assert coverage["block_start"] == 1
    assert coverage["block_end"] == 1
    assert coverage["breath:inhale"] == 1
    assert coverage["breath:exhale"] == 1
    assert coverage["session_end"] == 1


# ---------------------------------------------------------------------------
# audit_breathing
# ---------------------------------------------------------------------------


def _gen_breathing_markers(*, block_id: int, n_cycles: int, period: float, base_t: float, pass_label: str = "A"):
    out = []
    for i in range(n_cycles):
        t = base_t + i * period
        out.append((f"breath:inhale:block={block_id}:cycle={i + 1}:pass={pass_label}", t))
        out.append(
            (f"breath:exhale:block={block_id}:cycle={i + 1}:pass={pass_label}", t + period / 2)
        )
    return out


def test_audit_breathing_perfect_block_2() -> None:
    cfg = load_config()
    # 30 cycles of 10 s for block 2 (0.1 Hz).
    markers = _gen_breathing_markers(block_id=2, n_cycles=30, period=10.0, base_t=100.0)
    # Add some block 3 markers too.
    markers += _gen_breathing_markers(block_id=3, n_cycles=60, period=5.0, base_t=500.0)
    summaries, problems = verify_xdf.audit_breathing(markers, cfg)
    # No problems expected.
    assert problems == []
    b2 = next(s for s in summaries if s["block_id"] == 2)
    b3 = next(s for s in summaries if s["block_id"] == 3)
    assert b2["observed_cycles"] == 30
    assert b3["observed_cycles"] == 60


def test_audit_breathing_too_few_cycles_flagged() -> None:
    cfg = load_config()
    markers = _gen_breathing_markers(block_id=2, n_cycles=10, period=10.0, base_t=0.0)
    _, problems = verify_xdf.audit_breathing(markers, cfg)
    assert any("inhale count 10 != expected 30" in p for p in problems)


def test_audit_breathing_drifted_isi_flagged() -> None:
    cfg = load_config()
    # 30 cycles but with bad ISI (12 s instead of 10).
    markers = _gen_breathing_markers(block_id=2, n_cycles=30, period=12.0, base_t=0.0)
    _, problems = verify_xdf.audit_breathing(markers, cfg)
    assert any("mean ISI" in p for p in problems)


# ---------------------------------------------------------------------------
# audit_artifact_block
# ---------------------------------------------------------------------------


def _all_artifact_markers(cfg, pass_label: str = "A"):
    out = []
    for cue in cfg.artifact_block.cues:
        out.append((f"artifact:{cue.label}:start:block=4:pass={pass_label}", cue.start_s))
        if not cue.state:
            out.append(
                (f"artifact:{cue.label}:end:block=4:pass={pass_label}",
                 cue.start_s + cue.window_s)
            )
    return out


def test_audit_artifact_all_cues_present() -> None:
    cfg = load_config()
    markers = _all_artifact_markers(cfg)
    summary, problems = verify_xdf.audit_artifact_block(markers, cfg)
    assert problems == []
    assert len(summary["observed_starts"]) == len(cfg.artifact_block.cues)


def test_audit_artifact_missing_cue_flagged() -> None:
    cfg = load_config()
    markers = _all_artifact_markers(cfg)
    # Drop one cue's start.
    bad_label = cfg.artifact_block.cues[3].label
    markers = [m for m in markers if not m[0].startswith(f"artifact:{bad_label}:")]
    _, problems = verify_xdf.audit_artifact_block(markers, cfg)
    assert any(bad_label in p for p in problems)


def test_audit_artifact_missing_end_flagged() -> None:
    cfg = load_config()
    markers = _all_artifact_markers(cfg)
    # Drop the :end marker for a window cue.
    window_cue = next(c for c in cfg.artifact_block.cues if not c.state)
    markers = [m for m in markers
               if m[0] != f"artifact:{window_cue.label}:end:block=4:pass=A"]
    _, problems = verify_xdf.audit_artifact_block(markers, cfg)
    assert any("missing :end" in p for p in problems)


# ---------------------------------------------------------------------------
# main — error paths
# ---------------------------------------------------------------------------


def test_main_missing_xdf_returns_1(tmp_path: Path) -> None:
    rc = verify_xdf.main([str(tmp_path / "does_not_exist.xdf")])
    assert rc == 1


def test_audit_uses_expected_families() -> None:
    # Smoke: every named family is also handled by _matches_family on its prefix.
    for fam in verify_xdf.EXPECTED_FAMILIES:
        assert verify_xdf._matches_family(fam, fam)
        assert verify_xdf._matches_family(f"{fam}:extra=1", fam)


# ---------------------------------------------------------------------------
# audit_breathing — re-run attempts
# ---------------------------------------------------------------------------


def test_audit_breathing_separates_attempts() -> None:
    cfg = load_config()
    # Block 3 attempt 1: a short run (10 cycles). Attempt 2: a full re-run.
    m1 = [
        (s.replace(":pass=A", ":pass=A:attempt=1"), t)
        for s, t in _gen_breathing_markers(block_id=3, n_cycles=10, period=5.0, base_t=0.0)
    ]
    m2 = [
        (s.replace(":pass=A", ":pass=A:attempt=2"), t)
        for s, t in _gen_breathing_markers(block_id=3, n_cycles=60, period=5.0, base_t=900.0)
    ]
    summaries, problems = verify_xdf.audit_breathing(m1 + m2, cfg)
    b3 = [s for s in summaries if s["block_id"] == 3]
    assert {s["attempt"] for s in b3} == {1, 2}
    assert next(s for s in b3 if s["attempt"] == 1)["observed_cycles"] == 10
    assert next(s for s in b3 if s["attempt"] == 2)["observed_cycles"] == 60
    # Attempt 1 is short — flagged. (The "attempt N" tag is omitted for
    # attempt 1 to keep the report clean, so the message reads "block 3".)
    assert any("block 3 inhale count 10" in p for p in problems)


# ---------------------------------------------------------------------------
# block_windows
# ---------------------------------------------------------------------------


def _block_markers(specs):
    """specs: list of (block_id, attempt, label, t_start, t_end)."""
    out = []
    for bid, att, label, t0, t1 in specs:
        suffix = f":attempt={att}" if att > 1 else ""
        out.append(
            (f"block_start:block={bid}:label={label}:pass=A:duration_planned_s=300{suffix}", t0)
        )
        out.append(
            (f"block_end:block={bid}:label={label}:pass=A:duration_actual_s=300{suffix}", t1)
        )
    return out


def test_block_windows_pairs_start_end() -> None:
    markers = _block_markers([(1, 1, "baseline", 0.0, 300.0), (2, 1, "paced_0p1Hz", 310.0, 610.0)])
    windows = verify_xdf.block_windows(markers)
    assert len(windows) == 2
    assert windows[0]["block_id"] == 1
    assert windows[0]["t_start"] == 0.0 and windows[0]["t_end"] == 300.0


def test_block_windows_separates_attempts() -> None:
    markers = _block_markers(
        [(3, 1, "paced_0p2Hz", 0.0, 300.0), (3, 2, "paced_0p2Hz", 400.0, 700.0)]
    )
    windows = verify_xdf.block_windows(markers)
    assert {(w["block_id"], w["attempt"]) for w in windows} == {(3, 1), (3, 2)}


def test_block_windows_missing_end_marker() -> None:
    markers = [("block_start:block=1:label=baseline:pass=A:duration_planned_s=300", 0.0)]
    windows = verify_xdf.block_windows(markers)
    assert windows[0]["t_end"] is None


# ---------------------------------------------------------------------------
# audit_stream_coverage
# ---------------------------------------------------------------------------


def _stream(name: str, stype: str, t0: float, t1: float, srate: float = 64.0):
    import numpy as np

    n = max(2, int((t1 - t0) * srate))
    return {
        "name": name,
        "type": stype,
        "nominal_srate": srate,
        "time_stamps": list(np.linspace(t0, t1, n)),
    }


def test_stream_coverage_clean_when_stream_spans_block() -> None:
    markers = _block_markers([(1, 1, "baseline", 0.0, 300.0)])
    streams = [_stream("Muse-EEG", "EEG", -5.0, 310.0)]
    report, problems = verify_xdf.audit_stream_coverage(streams, markers)
    assert problems == []
    assert report["n_uncovered"] == 0


def test_stream_coverage_flags_a_dropout() -> None:
    markers = _block_markers(
        [(1, 1, "baseline", 0.0, 300.0), (2, 1, "paced_0p1Hz", 310.0, 610.0)]
    )
    # Optics stream dies at t=400 — covers block 1 but not block 2.
    streams = [_stream("Muse-OPTICS", "PPG", -5.0, 400.0)]
    report, problems = verify_xdf.audit_stream_coverage(streams, markers)
    assert report["n_uncovered"] == 1
    assert any("before the block finished" in p and p.startswith("FAIL") for p in problems)


def test_stream_coverage_flags_late_start() -> None:
    markers = _block_markers([(1, 1, "baseline", 0.0, 300.0)])
    streams = [_stream("Muse-EEG", "EEG", 50.0, 320.0)]
    _, problems = verify_xdf.audit_stream_coverage(streams, markers)
    assert any("into the block" in p for p in problems)


def test_stream_coverage_empty_stream_flagged() -> None:
    markers = _block_markers([(1, 1, "baseline", 0.0, 300.0)])
    streams = [{"name": "Dead", "type": "EEG", "nominal_srate": 64.0, "time_stamps": []}]
    _, problems = verify_xdf.audit_stream_coverage(streams, markers)
    assert any("no samples" in p for p in problems)


def test_stream_coverage_missing_block_end_flagged() -> None:
    markers = [("block_start:block=1:label=baseline:pass=A:duration_planned_s=300", 0.0)]
    streams = [_stream("Muse-EEG", "EEG", 0.0, 300.0)]
    _, problems = verify_xdf.audit_stream_coverage(streams, markers)
    assert any("no block_end" in p for p in problems)


# ---------------------------------------------------------------------------
# load_data_streams
# ---------------------------------------------------------------------------


def test_load_data_streams_filters_slow_and_marker_streams() -> None:
    raw = [
        {"info": {"name": ["Muse-EEG"], "type": ["EEG"], "nominal_srate": ["256"]},
         "time_stamps": [1.0, 2.0]},
        {"info": {"name": ["MuseProxyMarkers"], "type": ["Markers"], "nominal_srate": ["0"]},
         "time_stamps": [1.0]},
        {"info": {"name": ["Muse-BATTERY"], "type": ["Battery"], "nominal_srate": ["1"]},
         "time_stamps": [1.0]},
    ]
    out = verify_xdf.load_data_streams(raw)
    assert [d["name"] for d in out] == ["Muse-EEG"]


# ---------------------------------------------------------------------------
# Required headset streams + signal integrity (added with the BLE bridge)
# ---------------------------------------------------------------------------


def _xdf_stream(name: str, stype: str, data, ts, srate: float = 256.0) -> dict:
    import numpy as np

    return {
        "info": {"name": [name], "type": [stype], "nominal_srate": [str(srate)]},
        "time_series": np.asarray(data, dtype=float),
        "time_stamps": np.asarray(ts, dtype=float),
    }


def test_pass_device_from_pass_start() -> None:
    markers = [("pass_start:pass=B:device=athena:source_id=x", 1.0)]
    assert verify_xdf.pass_device(markers) == "athena"


def test_pass_device_fallback_to_device_marker() -> None:
    markers = [("pass_a_device:muse2", 0.5), ("pass_b_device:athena", 0.6),
               ("block_start:block=1:pass=A", 1.0)]
    assert verify_xdf.pass_device(markers) == "muse2"


def test_required_streams_missing_athena_is_fail() -> None:
    """The 2026 failure: Athena pass XDF with CGX + markers only."""
    import numpy as np

    cfg = load_config()
    streams = [
        _xdf_stream("CGX AIM Phys. Mon. AIM-0106", "EEG", np.ones((10, 2)), np.arange(10)),
        _xdf_stream("MuseProxyMarkers", "Markers", np.zeros((1, 1)), [0.0], 0),
    ]
    _rows, problems = verify_xdf.audit_required_streams(streams, cfg, "athena")
    fails = [p for p in problems if p.startswith("FAIL")]
    assert any("Muse-EEG" in p for p in fails) and any("Muse-OPTICS" in p for p in fails)


def test_required_streams_empty_stream_is_fail() -> None:
    import numpy as np

    cfg = load_config()
    streams = [
        _xdf_stream("CGX AIM Phys. Mon. AIM-0106", "EEG", np.ones((10, 2)), np.arange(10)),
        _xdf_stream("MuseProxyMarkers", "Markers", np.zeros((1, 1)), [0.0], 0),
        _xdf_stream("Muse2-EEG (1A2B)", "EEG", np.empty((0, 4)), []),
        _xdf_stream("Muse2-PPG (1A2B)", "PPG", np.ones((5, 3)), np.arange(5), 64),
    ]
    _rows, problems = verify_xdf.audit_required_streams(streams, cfg, "muse2")
    assert problems == ["FAIL: expected stream 'Muse2-EEG' (EEG) has no samples"]


def test_signal_integrity_flags_zero_runs_backwards_ts_and_nan() -> None:
    import numpy as np

    n = 1200
    eeg = np.random.default_rng(0).normal(size=(n, 2))
    eeg[100:112, 0] = 0.0  # muselsl-style zero-filled packets: 12 samples each
    eeg[300:312, 0] = 0.0
    ts = np.arange(n) / 256.0
    ts[500] = ts[498]      # one backwards step
    ppg = np.ones((n, 3))
    ppg[::5, 1] = np.nan    # bridge-style NaN marking
    streams = [
        {"name": "Muse", "type": "EEG", "time_stamps": ts, "time_series": eeg},
        {"name": "Muse2-PPG", "type": "PPG", "time_stamps": np.arange(n) / 64,
         "time_series": ppg},
        {"name": "CGX", "type": "EEG", "time_stamps": np.arange(n),
         "time_series": np.c_[np.ones(n), np.zeros(n)]},  # unused channel: flat
    ]
    rows, problems = verify_xdf.audit_signal_integrity(streams, zero_frac_warn=0.01)
    joined = "\n".join(problems)
    assert "'Muse' (EEG) — up to 2.0%" in joined
    assert "1 timestamps go backwards" in joined
    assert "20.0% of samples contain NaN" in joined
    assert rows[2]["flat_channels"] == [1]
    assert "CGX" not in joined
