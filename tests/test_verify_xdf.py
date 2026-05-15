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
