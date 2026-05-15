"""Post-session XDF auditor.

Usage:
    uv run python scripts/verify_xdf.py path\\to\\P001_passA_muse2.xdf

Loads the XDF, finds the ``MuseProxyMarkers`` stream, and prints:
  * Coverage table — every expected marker family vs. observed count.
  * Per breathing block — cycle count vs. expected, mean inter-inhale
    interval vs. target (1 / rate_hz), SD.
  * Artifact block — every expected cue label vs. presence, mean
    cue-window duration.
  * Pass detection — single ``pass=A|B`` label across markers (sanity
    check that we didn't mix passes in one file).

Exit 0 = clean; exit 1 = at least one violation worth investigating.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Add src/ to path so we can import muse_proxy.config without an editable install.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from muse_proxy.config import Config, load_config  # noqa: E402

EXPECTED_FAMILIES = (
    "session_start",
    "session_end",
    "pass_start",
    "pass_end",
    "volume_check:start",
    "volume_check:end",
    "settle:start",
    "settle:end",
    "block_start",
    "block_end",
    "between_block_settle:start",
    "between_block_settle:end",
    "intake",
    "tts:close_eyes:start",
    "tts:close_eyes:end",
    "tts:open_eyes:start",
    "tts:open_eyes:end",
    "breath:inhale",
    "breath:exhale",
)


def _matches_family(marker: str, family: str) -> bool:
    return marker == family or marker.startswith(family + ":")


def parse_payload(marker: str) -> tuple[str, dict[str, str]]:
    """Split a marker string into (event_prefix, kv_dict).

    The event_prefix is everything before the first ``key=value`` token.
    We split by ``:``, walk left-to-right, and any token without ``=`` is
    part of the event prefix. Returns the kv as plain strings.
    """
    parts = marker.split(":")
    prefix_tokens: list[str] = []
    kvs: dict[str, str] = {}
    for tok in parts:
        if "=" in tok and not kvs:
            # First k=v starts the payload.
            k, v = tok.split("=", 1)
            kvs[k] = v
        elif "=" in tok:
            k, v = tok.split("=", 1)
            kvs[k] = v
        else:
            if kvs:
                # Trailing non-kv token after kv started — keep with event.
                # Unusual but possible.
                pass
            prefix_tokens.append(tok)
    return ":".join(prefix_tokens), kvs


def load_marker_stream(xdf_path: Path, stream_name: str = "MuseProxyMarkers") -> list[tuple[str, float]]:
    """Load the named string-marker stream as a list of (marker, timestamp)."""
    import pyxdf

    streams, _header = pyxdf.load_xdf(str(xdf_path))
    out: list[tuple[str, float]] = []
    for s in streams:
        info = s.get("info", {})
        if not info:
            continue
        names = info.get("name", []) or []
        if not names or names[0] != stream_name:
            continue
        samples = s.get("time_series", []) or []
        ts = s.get("time_stamps", []) or []
        for sample, t in zip(samples, ts, strict=False):
            if isinstance(sample, list) or hasattr(sample, "__iter__"):
                m = str(sample[0])
            else:
                m = str(sample)
            out.append((m, float(t)))
    return out


def audit_pass_purity(markers: list[tuple[str, float]]) -> tuple[str | None, list[str]]:
    """Confirm every marker that carries a ``pass=`` field uses one pass.

    Returns (single_pass_label or None if no/multiple, list_of_problems).
    """
    passes: set[str] = set()
    for m, _ in markers:
        _, kv = parse_payload(m)
        if "pass" in kv:
            passes.add(kv["pass"])
    if not passes:
        return None, ["WARN: no pass= field on any marker — can't verify purity"]
    if len(passes) > 1:
        return None, [f"FAIL: mixed passes in one file: {sorted(passes)}"]
    return next(iter(passes)), []


def audit_coverage(markers: list[tuple[str, float]]) -> Counter[str]:
    """Count markers by family prefix."""
    counts: Counter[str] = Counter()
    for m, _ in markers:
        for family in EXPECTED_FAMILIES:
            if _matches_family(m, family):
                counts[family] += 1
                break
    return counts


def audit_breathing(
    markers: list[tuple[str, float]], cfg: Config
) -> tuple[list[dict[str, Any]], list[str]]:
    """For each paced block, verify cycle count and ISI."""
    summaries: list[dict[str, Any]] = []
    problems: list[str] = []

    # Group inhale markers by block.
    per_block: dict[int, list[float]] = defaultdict(list)
    for m, t in markers:
        if not m.startswith("breath:inhale"):
            continue
        _, kv = parse_payload(m)
        try:
            block_id = int(kv.get("block", "-1"))
        except ValueError:
            continue
        per_block[block_id].append(t)

    for block in cfg.blocks:
        if not block.paced:
            continue
        inhale_ts = sorted(per_block.get(block.id, []))
        expected_n = int(round(block.duration_s * (block.rate_hz or 0)))
        observed_n = len(inhale_ts)
        if observed_n != expected_n:
            problems.append(
                f"FAIL: block {block.id} inhale count {observed_n} != expected {expected_n}"
            )
        isis = (
            [b - a for a, b in zip(inhale_ts[:-1], inhale_ts[1:], strict=True)]
            if len(inhale_ts) >= 2
            else []
        )
        target_isi = 1.0 / (block.rate_hz or 1.0)
        mean_isi = statistics.fmean(isis) if isis else float("nan")
        sd_isi = statistics.stdev(isis) if len(isis) > 1 else 0.0
        summaries.append(
            {
                "block_id": block.id,
                "label": block.label,
                "rate_hz": block.rate_hz,
                "expected_cycles": expected_n,
                "observed_cycles": observed_n,
                "target_isi_s": target_isi,
                "mean_isi_s": mean_isi,
                "sd_isi_s": sd_isi,
            }
        )
        if isis:
            err = abs(mean_isi - target_isi)
            # Audio-anchored markers should be tight — < 50 ms tolerance.
            if err > 0.050:
                problems.append(
                    f"WARN: block {block.id} mean ISI {mean_isi:.3f}s differs "
                    f"from target {target_isi:.3f}s by {err * 1000:.1f}ms"
                )

    return summaries, problems


def audit_artifact_block(
    markers: list[tuple[str, float]], cfg: Config
) -> tuple[dict[str, Any], list[str]]:
    """Confirm every expected artifact cue appeared."""
    problems: list[str] = []
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    for m, t in markers:
        if not m.startswith("artifact:"):
            continue
        # Format: artifact:<label>:<start|end>[:k=v...]
        rest = m[len("artifact:") :]
        # rest = "blink_slow:start:block=4:pass=A"
        # split on ":" — label may not contain ":", we enforce that in config
        toks = rest.split(":")
        if len(toks) < 2:
            continue
        label, kind = toks[0], toks[1]
        if kind == "start":
            starts[label] = t
        elif kind == "end":
            ends[label] = t

    expected_labels = [c.label for c in cfg.artifact_block.cues]
    missing = [lab for lab in expected_labels if lab not in starts]
    if missing:
        problems.append(f"FAIL: artifact cues missing :start markers: {missing}")

    expected_window_labels = [c.label for c in cfg.artifact_block.cues if not c.state]
    missing_ends = [lab for lab in expected_window_labels if lab not in ends]
    if missing_ends:
        problems.append(f"FAIL: artifact cues missing :end markers: {missing_ends}")

    durations: dict[str, float] = {}
    for lab in expected_window_labels:
        if lab in starts and lab in ends:
            durations[lab] = ends[lab] - starts[lab]

    mean_dur = statistics.fmean(durations.values()) if durations else float("nan")

    return {
        "expected_labels": expected_labels,
        "observed_starts": sorted(starts),
        "observed_ends": sorted(ends),
        "mean_window_s": mean_dur,
        "durations": durations,
    }, problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_xdf")
    parser.add_argument("xdf_path", help="Path to the .xdf to audit.")
    parser.add_argument(
        "--config", default=None, help="Optional alternate session_defaults.yaml."
    )
    parser.add_argument(
        "--stream-name", default="MuseProxyMarkers",
        help="LSL stream name to audit (default: MuseProxyMarkers).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a single JSON document instead of a human report.",
    )
    args = parser.parse_args(argv)

    xdf_path = Path(args.xdf_path)
    if not xdf_path.exists():
        print(f"FAIL: XDF not found: {xdf_path}", file=sys.stderr)
        return 1

    cfg = load_config(args.config)
    markers = load_marker_stream(xdf_path, stream_name=args.stream_name)
    if not markers:
        print(f"FAIL: no markers found in stream {args.stream_name!r}", file=sys.stderr)
        return 1

    pass_label, pass_problems = audit_pass_purity(markers)
    coverage = audit_coverage(markers)
    breathing, breathing_problems = audit_breathing(markers, cfg)
    artifact, artifact_problems = audit_artifact_block(markers, cfg)

    all_problems = pass_problems + breathing_problems + artifact_problems

    if args.json:
        out = {
            "xdf_path": str(xdf_path),
            "n_markers": len(markers),
            "pass_label": pass_label,
            "coverage": dict(coverage),
            "breathing": breathing,
            "artifact": artifact,
            "problems": all_problems,
        }
        print(json.dumps(out, indent=2, default=str))
        return 0 if not any(p.startswith("FAIL") for p in all_problems) else 1

    print()
    print(f"XDF: {xdf_path}")
    print(f"  {len(markers)} markers in stream {args.stream_name!r}")
    print(f"  pass label: {pass_label or '(none / mixed)'}")
    print()
    print("Coverage")
    print(f"  {'family':<36}{'count'}")
    for fam in EXPECTED_FAMILIES:
        print(f"  {fam:<36}{coverage.get(fam, 0)}")
    print()
    print("Breathing blocks")
    for b in breathing:
        print(
            f"  block {b['block_id']} ({b['label']}): "
            f"{b['observed_cycles']}/{b['expected_cycles']} cycles, "
            f"mean ISI {b['mean_isi_s']:.3f}s (target {b['target_isi_s']:.3f}s), "
            f"SD {b['sd_isi_s']:.3f}s"
        )
    print()
    print("Artifact block")
    print(f"  expected labels:        {len(artifact['expected_labels'])}")
    print(f"  observed :start markers: {len(artifact['observed_starts'])}")
    print(f"  observed :end markers:   {len(artifact['observed_ends'])}")
    print(f"  mean window duration:   {artifact['mean_window_s']:.3f}s")

    print()
    if all_problems:
        for p in all_problems:
            print(p)
    else:
        print("OK — no violations")

    return 0 if not any(p.startswith("FAIL") for p in all_problems) else 1


if __name__ == "__main__":
    raise SystemExit(main())
