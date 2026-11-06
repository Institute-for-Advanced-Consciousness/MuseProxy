"""Post-session XDF auditor.

Usage:
    uv run python scripts/verify_xdf.py path\\to\\P001_passA_muse2.xdf

Loads the XDF and reports:
  * Coverage table — every expected marker family vs. observed count.
  * Per breathing block — cycle count vs. expected, mean inter-inhale
    interval vs. target (1 / rate_hz), SD. Re-run attempts are audited
    separately (a block may legitimately appear more than once).
  * Artifact block — every expected cue label vs. presence, mean
    cue-window duration.
  * Stream coverage — for every block window, whether each continuous
    data stream (EEG / PPG / ECG / ...) actually has samples spanning
    the block. This is what catches a Muse PPG dropout that the marker
    audit alone cannot see.
  * Pass detection — single ``pass=A|B`` label across markers (sanity
    check that we didn't mix passes in one file).

Exit 0 = clean; exit 1 = at least one FAIL worth investigating.
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

# A continuous data stream must start within this many seconds of a block's
# start (and end no earlier than this before the block's end) to count as
# covering it. Slack absorbs LSL clock jitter and start/stop ordering.
COVERAGE_TOLERANCE_S = 1.0
# An internal gap longer than this inside a block window is flagged.
GAP_WARN_S = 2.0
# Streams slower than this are not coverage-audited (battery, markers).
MIN_CONTINUOUS_SRATE_HZ = 16.0


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


def _attempt_of(kv: dict[str, str]) -> int:
    """Return the re-run attempt number from a marker payload (default 1)."""
    try:
        return int(kv.get("attempt", "1"))
    except (TypeError, ValueError):
        return 1


# ---------------------------------------------------------------------------
# XDF loading
# ---------------------------------------------------------------------------


def _load_xdf(xdf_path: Path) -> list[dict[str, Any]]:
    import pyxdf

    # dejitter_timestamps=False: audit the timestamps exactly as recorded.
    # pyxdf's default dejitter fits a line through them, which hides
    # backwards/irregular stamps (the 2026 muselsl ACC/GYRO problem).
    streams, _header = pyxdf.load_xdf(str(xdf_path), dejitter_timestamps=False)
    return list(streams)


def _markers_from_streams(
    streams: list[dict[str, Any]], stream_name: str
) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for s in streams:
        info = s.get("info", {}) or {}
        names = info.get("name", []) or []
        if not names or names[0] != stream_name:
            continue
        # NOTE: ``x or []`` is wrong here — time_series/time_stamps come back
        # as numpy arrays, and a numpy array in a boolean context raises
        # "ambiguous truth value". Test for None explicitly.
        samples = s.get("time_series")
        if samples is None:
            samples = []
        ts = s.get("time_stamps")
        if ts is None:
            ts = []
        for sample, t in zip(samples, ts):
            if isinstance(sample, str):
                m = sample
            elif hasattr(sample, "__len__"):
                m = str(sample[0]) if len(sample) else ""
            else:
                m = str(sample)
            out.append((m, float(t)))
    return out


def load_marker_stream(
    xdf_path: Path, stream_name: str = "MuseProxyMarkers"
) -> list[tuple[str, float]]:
    """Load the named string-marker stream as a list of (marker, timestamp)."""
    return _markers_from_streams(_load_xdf(Path(xdf_path)), stream_name)


def load_data_streams(
    streams_or_path: list[dict[str, Any]] | Path | str,
    *,
    min_srate: float = MIN_CONTINUOUS_SRATE_HZ,
) -> list[dict[str, Any]]:
    """Return the continuous (non-marker) data streams worth coverage-auditing.

    Each entry: ``{name, type, nominal_srate, time_stamps}``. Streams
    slower than ``min_srate`` (battery, markers) are skipped.
    """
    if isinstance(streams_or_path, (str, Path)):
        streams = _load_xdf(Path(streams_or_path))
    else:
        streams = streams_or_path
    out: list[dict[str, Any]] = []
    for s in streams:
        info = s.get("info", {}) or {}
        names = info.get("name", []) or []
        types = info.get("type", []) or []
        name = names[0] if names else "?"
        stype = types[0] if types else "?"
        if str(stype).lower() in ("markers", "marker"):
            continue
        srate_raw = (info.get("nominal_srate", ["0"]) or ["0"])[0]
        try:
            srate = float(srate_raw)
        except (TypeError, ValueError):
            srate = 0.0
        if srate < min_srate:
            continue
        ts = s.get("time_stamps")
        if ts is None:
            ts = []
        out.append(
            {"name": name, "type": stype, "nominal_srate": srate, "time_stamps": ts,
             "time_series": s.get("time_series")}
        )
    return out


# ---------------------------------------------------------------------------
# Audits — required device streams + signal integrity
# ---------------------------------------------------------------------------


def pass_device(markers: list[tuple[str, float]]) -> str | None:
    """The device this XDF's pass ran on, from its ``pass_start`` marker."""
    for m, _t in markers:
        prefix, kv = parse_payload(m)
        if prefix == "pass_start" and kv.get("device"):
            return kv["device"]
    # Fallback: pass_<x>_device:<device> for the pass this file carries.
    labels = {kv.get("pass") for m, _ in markers for _p, kv in [parse_payload(m)]}
    labels.discard(None)
    if len(labels) == 1:
        want = f"pass_{next(iter(labels)).lower()}_device:"
        for m, _t in markers:
            if m.startswith(want):
                return m[len(want):].split(":")[0] or None
    return None


def audit_required_streams(
    streams: list[dict[str, Any]], cfg: Any, device: str | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Every stream the config expects for this pass's device must be present
    AND contain samples. (The 2026 Athena XDFs had no Athena streams at all and
    the old audit never noticed.)"""
    problems: list[str] = []
    rows: list[dict[str, Any]] = []
    if device is None:
        return rows, ["FAIL: no pass_start marker with device= — cannot check headset streams"]
    try:
        refs = cfg.streams_for_device(device)
    except ValueError as e:
        return rows, [f"FAIL: {e}"]
    for ref in refs:
        matches = []
        for s in streams:
            info = s.get("info", {}) or {}
            name = (info.get("name") or ["?"])[0]
            stype = (info.get("type") or ["?"])[0]
            if ref.name.lower() in str(name).lower() and (ref.type is None or stype == ref.type):
                ts = s.get("time_stamps")
                matches.append((name, 0 if ts is None else len(ts)))
        n = sum(c for _, c in matches)
        rows.append({"expected": f"{ref.name}|{ref.type or '*'}",
                     "matched": [m for m, _ in matches], "samples": n})
        if not matches:
            problems.append(f"FAIL: expected stream {ref.name!r} ({ref.type}) is not in the XDF")
        elif n == 0:
            problems.append(f"FAIL: expected stream {ref.name!r} ({ref.type}) has no samples")
        if len(matches) > 1:
            problems.append(
                f"WARN: {len(matches)} streams match {ref.name!r} ({ref.type}): "
                + ", ".join(m for m, _ in matches)
            )
    return rows, problems


def _zero_run_fraction(x: Any, min_run: int) -> float:
    """Share of samples sitting in runs of >= ``min_run`` exact zeros."""
    import numpy as np

    z = np.asarray(x) == 0
    if z.size == 0 or not z.any():
        return 0.0
    edges = np.diff(np.concatenate([[0], z.astype(np.int8), [0]]))
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
    lens = ends - starts
    return float(lens[lens >= min_run].sum()) / z.size


def audit_signal_integrity(
    data_streams: list[dict[str, Any]],
    *,
    zero_run_min: int = 6,
    zero_frac_warn: float = 0.01,
    nan_frac_warn: float = 0.05,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Per stream: backwards timestamps, NaN share (bridge-marked packet loss),
    and runs of exact zeros (muselsl's zero-filled packet loss)."""
    import numpy as np

    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    for ds in data_streams:
        name, stype = ds["name"], ds["type"]
        ts = np.asarray(ds["time_stamps"], dtype=float)
        backwards = int((np.diff(ts) < 0).sum()) if ts.size > 1 else 0
        row: dict[str, Any] = {"stream": name, "type": stype, "n": int(ts.size),
                               "backwards_ts": backwards}
        x = ds.get("time_series")
        if (
            isinstance(x, np.ndarray)
            and x.ndim == 2
            and np.issubdtype(x.dtype, np.number)
            and x.shape[0]
        ):
            nan_frac = float(np.isnan(x).any(axis=1).mean())
            # Entirely-zero columns are unused inputs (e.g. unconnected CGX
            # channels), not packet loss — listed separately.
            flat = [c for c in range(x.shape[1]) if not np.any(x[:, c])]
            zero = {f"ch{c}": round(_zero_run_fraction(x[:, c], zero_run_min), 4)
                    for c in range(x.shape[1]) if c not in flat}
            row["flat_channels"] = flat
            row.update(nan_row_frac=round(nan_frac, 4), zero_run_frac=zero)
            worst = max(zero.values()) if zero else 0.0
            if worst > zero_frac_warn:
                problems.append(
                    f"WARN: stream {name!r} ({stype}) — up to {worst:.1%} of samples are in "
                    f"runs of >= {zero_run_min} exact zeros (zero-filled packet loss?)"
                )
            if nan_frac > nan_frac_warn:
                problems.append(
                    f"WARN: stream {name!r} ({stype}) — {nan_frac:.1%} of samples contain NaN "
                    f"(dropped BLE packets marked by the bridge)"
                )
        if backwards:
            problems.append(
                f"WARN: stream {name!r} ({stype}) — {backwards} timestamps go backwards"
            )
        rows.append(row)
    return rows, problems


# ---------------------------------------------------------------------------
# Audits — markers
# ---------------------------------------------------------------------------


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
    """For each paced block (and each re-run attempt), verify cycle count + ISI."""
    summaries: list[dict[str, Any]] = []
    problems: list[str] = []

    # Group inhale markers by (block, attempt).
    per_block: dict[tuple[int, int], list[float]] = defaultdict(list)
    for m, t in markers:
        if not m.startswith("breath:inhale"):
            continue
        _, kv = parse_payload(m)
        try:
            block_id = int(kv.get("block", "-1"))
        except ValueError:
            continue
        per_block[(block_id, _attempt_of(kv))].append(t)

    for block in cfg.blocks:
        if not block.paced:
            continue
        attempts = sorted({a for (bid, a) in per_block if bid == block.id}) or [1]
        for attempt in attempts:
            inhale_ts = sorted(per_block.get((block.id, attempt), []))
            expected_n = int(round(block.duration_s * (block.rate_hz or 0)))
            observed_n = len(inhale_ts)
            tag = f"block {block.id}" + (f" attempt {attempt}" if attempt > 1 else "")
            if observed_n != expected_n:
                problems.append(
                    f"FAIL: {tag} inhale count {observed_n} != expected {expected_n}"
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
                    "attempt": attempt,
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
                        f"WARN: {tag} mean ISI {mean_isi:.3f}s differs "
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
# Audits — data-stream coverage
# ---------------------------------------------------------------------------


def block_windows(markers: list[tuple[str, float]]) -> list[dict[str, Any]]:
    """Pair block_start / block_end markers into per-(block, attempt) windows."""
    starts: dict[tuple[int, int], tuple[str, float]] = {}
    ends: dict[tuple[int, int], float] = {}
    for m, t in markers:
        pre, kv = parse_payload(m)
        if pre not in ("block_start", "block_end"):
            continue
        try:
            bid = int(kv.get("block", "-1"))
        except ValueError:
            continue
        key = (bid, _attempt_of(kv))
        if pre == "block_start":
            starts[key] = (kv.get("label", "?"), t)
        else:
            ends[key] = t
    windows: list[dict[str, Any]] = []
    for key in sorted(starts):
        label, t0 = starts[key]
        windows.append(
            {
                "block_id": key[0],
                "attempt": key[1],
                "label": label,
                "t_start": t0,
                "t_end": ends.get(key),
            }
        )
    return windows


def audit_stream_coverage(
    data_streams: list[dict[str, Any]],
    markers: list[tuple[str, float]],
    *,
    tolerance_s: float = COVERAGE_TOLERANCE_S,
    gap_warn_s: float = GAP_WARN_S,
) -> tuple[dict[str, Any], list[str]]:
    """For every block window, check each data stream actually covers it.

    Catches the failure the marker audit cannot see: a headset stream
    that stopped partway through (e.g. Muse PPG/optics dropping out).
    """
    import numpy as np

    problems: list[str] = []
    windows = block_windows(markers)
    rows: list[dict[str, Any]] = []

    for win in windows:
        bid, attempt, label = win["block_id"], win["attempt"], win["label"]
        t0, t1 = win["t_start"], win["t_end"]
        tag = f"block {bid}" + (f" attempt {attempt}" if attempt > 1 else "")
        if t1 is None:
            problems.append(f"FAIL: {tag} ({label}) has no block_end marker")
            continue
        for ds in data_streams:
            name, stype = ds["name"], ds["type"]
            ts = np.asarray(ds["time_stamps"], dtype=float)
            row: dict[str, Any] = {
                "block_id": bid,
                "attempt": attempt,
                "label": label,
                "stream": name,
                "type": stype,
            }
            if ts.size == 0:
                problems.append(
                    f"FAIL: {tag} ({label}) — stream {name!r} ({stype}) has no samples"
                )
                row.update(covered=False, missing_start_s=t1 - t0, missing_end_s=0.0,
                           max_gap_s=None)
                rows.append(row)
                continue
            first, last = float(ts[0]), float(ts[-1])
            missing_start = max(0.0, first - t0)
            missing_end = max(0.0, t1 - last)
            # Largest internal gap inside the window.
            if ts.size > 1 and (np.diff(ts) < 0).any():
                ts = np.sort(ts)  # searchsorted needs order; reported separately
                first, last = float(ts[0]), float(ts[-1])
                missing_start = max(0.0, first - t0)
                missing_end = max(0.0, t1 - last)
            lo = int(np.searchsorted(ts, t0))
            hi = int(np.searchsorted(ts, t1))
            seg = ts[max(0, lo - 1) : hi + 1]
            max_gap = float(np.max(np.diff(seg))) if seg.size >= 2 else (t1 - t0)
            covered = missing_start <= tolerance_s and missing_end <= tolerance_s
            row.update(
                covered=covered,
                missing_start_s=missing_start,
                missing_end_s=missing_end,
                max_gap_s=max_gap,
            )
            rows.append(row)
            if missing_start > tolerance_s:
                problems.append(
                    f"FAIL: {tag} ({label}) — stream {name!r} ({stype}) "
                    f"starts {missing_start:.1f}s into the block"
                )
            if missing_end > tolerance_s:
                problems.append(
                    f"FAIL: {tag} ({label}) — stream {name!r} ({stype}) "
                    f"ends {missing_end:.1f}s before the block finished"
                )
            if covered and max_gap > gap_warn_s:
                problems.append(
                    f"WARN: {tag} ({label}) — stream {name!r} ({stype}) "
                    f"has a {max_gap:.1f}s internal gap"
                )

    n_fail = sum(1 for r in rows if not r["covered"])
    return {
        "n_windows": len(windows),
        "n_streams": len(data_streams),
        "n_checks": len(rows),
        "n_uncovered": n_fail,
        "windows": windows,
        "rows": rows,
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
    try:
        streams = _load_xdf(xdf_path)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: could not load XDF: {e}", file=sys.stderr)
        return 1
    markers = _markers_from_streams(streams, args.stream_name)
    if not markers:
        print(f"FAIL: no markers found in stream {args.stream_name!r}", file=sys.stderr)
        return 1
    data_streams = load_data_streams(streams)

    pass_label, pass_problems = audit_pass_purity(markers)
    coverage = audit_coverage(markers)
    breathing, breathing_problems = audit_breathing(markers, cfg)
    artifact, artifact_problems = audit_artifact_block(markers, cfg)
    stream_cov, stream_cov_problems = audit_stream_coverage(data_streams, markers)
    device = pass_device(markers)
    required, required_problems = audit_required_streams(streams, cfg, device)
    integrity, integrity_problems = audit_signal_integrity(data_streams)

    all_problems = (
        required_problems
        + integrity_problems
        + pass_problems
        + breathing_problems
        + artifact_problems
        + stream_cov_problems
    )

    if args.json:
        out = {
            "xdf_path": str(xdf_path),
            "n_markers": len(markers),
            "pass_label": pass_label,
            "coverage": dict(coverage),
            "breathing": breathing,
            "artifact": artifact,
            "stream_coverage": stream_cov,
            "device": device,
            "required_streams": required,
            "signal_integrity": integrity,
            "problems": all_problems,
        }
        print(json.dumps(out, indent=2, default=str))
        return 0 if not any(p.startswith("FAIL") for p in all_problems) else 1

    print()
    print(f"XDF: {xdf_path}")
    print(f"  {len(markers)} markers in stream {args.stream_name!r}")
    print(f"  pass label: {pass_label or '(none / mixed)'}")
    print(f"  device:     {device or '(unknown)'}")
    print()
    print("Required headset streams")
    for r in required:
        print(f"  {r['expected']:<28} samples={r['samples']:<9} matched={r['matched']}")
    print()
    print("Signal integrity")
    for r in integrity:
        extra = ""
        if "nan_row_frac" in r:
            worst = max(r["zero_run_frac"].values()) if r["zero_run_frac"] else 0.0
            extra = f" NaN rows {r['nan_row_frac']:.2%}  worst zero-run share {worst:.2%}"
        print(f"  {r['stream']:<34} n={r['n']:<9} backwards_ts={r['backwards_ts']}{extra}")
    print()
    print("Coverage")
    print(f"  {'family':<36}{'count'}")
    for fam in EXPECTED_FAMILIES:
        print(f"  {fam:<36}{coverage.get(fam, 0)}")
    print()
    print("Breathing blocks")
    for b in breathing:
        tag = f"block {b['block_id']}" + (
            f" attempt {b['attempt']}" if b["attempt"] > 1 else ""
        )
        print(
            f"  {tag} ({b['label']}): "
            f"{b['observed_cycles']}/{b['expected_cycles']} cycles, "
            f"mean ISI {b['mean_isi_s']:.3f}s (target {b['target_isi_s']:.3f}s), "
            f"SD {b['sd_isi_s']:.3f}s"
        )
    print()
    print("Artifact block")
    print(f"  expected labels:         {len(artifact['expected_labels'])}")
    print(f"  observed :start markers: {len(artifact['observed_starts'])}")
    print(f"  observed :end markers:   {len(artifact['observed_ends'])}")
    print(f"  mean window duration:    {artifact['mean_window_s']:.3f}s")
    print()
    print("Stream coverage")
    print(
        f"  {stream_cov['n_checks']} block×stream checks, "
        f"{stream_cov['n_uncovered']} uncovered "
        f"({stream_cov['n_streams']} data streams, "
        f"{stream_cov['n_windows']} block windows)"
    )
    for r in stream_cov["rows"]:
        if r["covered"]:
            continue
        tag = f"block {r['block_id']}" + (
            f" attempt {r['attempt']}" if r["attempt"] > 1 else ""
        )
        print(
            f"  UNCOVERED  {tag} ({r['label']})  {r['stream']} ({r['type']})  "
            f"missing {r['missing_start_s']:.0f}s@start / "
            f"{r['missing_end_s']:.0f}s@end"
        )

    print()
    if all_problems:
        for p in all_problems:
            print(p)
    else:
        print("OK — no violations")

    return 0 if not any(p.startswith("FAIL") for p in all_problems) else 1


if __name__ == "__main__":
    raise SystemExit(main())
