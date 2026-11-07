"""Per-pass JSON + per-block CSV writers, with incremental partial-save.

Visit-level: ``Data/P{NNN}/intake.json`` (written once at intake submit).
Per-pass:
  Data/P{NNN}/pass_{A|B}_{device}/session.json
  Data/P{NNN}/pass_{A|B}_{device}/PARTIAL_session.json   (retained on abort)
  Data/P{NNN}/pass_{A|B}_{device}/markers.csv            (CSV mirror)
  Data/P{NNN}/pass_{A|B}_{device}/block_{NN}_{label}.csv (per-block)

The writer ALWAYS writes both PARTIAL and the per-block CSV at the end
of every block, so a Windows hard-crash leaves the most recent state on
disk.

Public surface:
  next_participant_id is in config.py.
  IntakeWriter(data_root, pid).write(intake_dict) -> Path
  pass_dir(data_root, pid, pass_label, device) -> Path
  PassWriter(data_root, pid, pass_label, device, config_snapshot)
    .start_pass(start_lsl_ts, start_iso) -> None
    .start_block(idx, label, duration_s, start_lsl) -> BlockRecord
    .end_block(record, end_lsl, events, status='completed') -> None
    .mark_pending_blocks_aborted() -> int
    .finalize(status='completed'|'aborted') -> Path
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "IntakeWriter",
    "PassWriter",
    "BlockRecord",
    "pass_dir",
    "atomic_write_text",
    "atomic_write_json",
]


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` via tmp + rename.

    On Windows ``os.replace`` is atomic on the same volume. We never write
    a partially-flushed file to the final name.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use a NamedTemporaryFile in the same dir so rename is on the same volume.
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any, *, indent: int = 2) -> None:
    """``json.dumps`` + ``atomic_write_text``."""
    atomic_write_text(path, json.dumps(obj, indent=indent, sort_keys=False, default=str))


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def pass_dir(data_root: str | Path, pid: str, pass_label: str, device: str) -> Path:
    """``data_root/P{NNN}/pass_{A|B}_{device}/``. Created lazily by callers."""
    if pass_label not in ("A", "B"):
        raise ValueError(f"pass_label must be 'A' or 'B', got {pass_label!r}")
    return Path(data_root) / pid / f"pass_{pass_label}_{device}"


def visit_dir(data_root: str | Path, pid: str) -> Path:
    return Path(data_root) / pid


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------


class IntakeWriter:
    """Writes the visit-level ``intake.json`` once.

    The intake doc is shared by both passes — Pass B does NOT overwrite
    Pass A's intake. ``write()`` is idempotent for the same intake dict;
    if called twice with different contents, the existing file is left
    intact and a ``FileExistsError`` is raised so divergence is loud.
    """

    def __init__(self, data_root: str | Path, pid: str) -> None:
        self.data_root = Path(data_root)
        self.pid = pid
        self.path = visit_dir(self.data_root, pid) / "intake.json"

    def write(self, intake: dict[str, Any]) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            if existing == intake:
                return self.path
            raise FileExistsError(
                f"intake.json already exists at {self.path} with different "
                f"contents; refusing to overwrite."
            )
        atomic_write_json(self.path, intake)
        return self.path


# ---------------------------------------------------------------------------
# Per-pass writer
# ---------------------------------------------------------------------------


@dataclass
class BlockRecord:
    """Mutable record for a single block in progress.

    The PassWriter creates one of these per block. Field naming mirrors
    the YAML so the JSON dump reads naturally.
    """

    block_id: int
    label: str
    duration_planned_s: float
    start_lsl: float
    start_iso: str
    end_lsl: float | None = None
    end_iso: str | None = None
    duration_actual_s: float | None = None
    status: str = "in_progress"  # 'in_progress' | 'completed' | 'aborted'
    events: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    # Re-run attempt: 1 for the first run of this block; 2+ if the RA
    # re-ran it after a stream dropout. Earlier attempts are kept, not
    # overwritten — the analyst picks the attempt with full coverage.
    attempt: int = 1
    # Worst per-stream health observed during the block (set by the
    # block stage from the StreamHealthMonitor's window report).
    stream_health: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "attempt": self.attempt,
            "label": self.label,
            "duration_planned_s": self.duration_planned_s,
            "start_lsl": self.start_lsl,
            "start_iso": self.start_iso,
            "end_lsl": self.end_lsl,
            "end_iso": self.end_iso,
            "duration_actual_s": self.duration_actual_s,
            "status": self.status,
            "stream_health": self.stream_health,
            "events": list(self.events),
            "notes": self.notes,
        }


class PassWriter:
    """Owns all on-disk artifacts for one pass of one visit.

    Lifecycle:
      writer = PassWriter(root, pid, "A", "muse-2", config_snapshot=cfg.model_dump())
      writer.start_pass(lsl_ts, iso)
      rec = writer.start_block(1, "baseline", 300.0, start_lsl)
      ...
      writer.end_block(rec, end_lsl, events, status="completed")
      ...
      writer.finalize(status="completed")    # or "aborted"
    """

    def __init__(
        self,
        data_root: str | Path,
        pid: str,
        pass_label: str,
        device: str,
        *,
        config_snapshot: dict[str, Any] | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.pid = pid
        self.pass_label = pass_label
        self.device = device
        self.dir = pass_dir(self.data_root, pid, pass_label, device)
        self.dir.mkdir(parents=True, exist_ok=True)

        self._session_path = self.dir / "session.json"
        self._partial_path = self.dir / "PARTIAL_session.json"

        self.config_snapshot = config_snapshot or {}
        self.start_lsl: float | None = None
        self.start_iso: str | None = None
        self.end_lsl: float | None = None
        self.end_iso: str | None = None
        self.status: str = "pending"
        self.blocks: list[BlockRecord] = []

    # ----- pass lifecycle -------------------------------------------------

    def start_pass(self, start_lsl_ts: float, start_iso: str | None = None) -> None:
        self.start_lsl = start_lsl_ts
        self.start_iso = start_iso or _utc_now_iso()
        self.status = "in_progress"
        self._refresh_partial()

    # ----- block lifecycle ------------------------------------------------

    def start_block(
        self,
        block_id: int,
        label: str,
        duration_planned_s: float,
        start_lsl: float,
        *,
        start_iso: str | None = None,
        attempt: int = 1,
    ) -> BlockRecord:
        rec = BlockRecord(
            block_id=block_id,
            label=label,
            duration_planned_s=duration_planned_s,
            start_lsl=start_lsl,
            start_iso=start_iso or _utc_now_iso(),
            attempt=attempt,
        )
        self.blocks.append(rec)
        self._refresh_partial()
        return rec

    def end_block(
        self,
        rec: BlockRecord,
        end_lsl: float,
        events: list[dict[str, Any]] | None = None,
        status: str = "completed",
        *,
        end_iso: str | None = None,
    ) -> None:
        if status not in ("completed", "aborted"):
            raise ValueError(f"bad block status: {status}")
        rec.end_lsl = end_lsl
        rec.end_iso = end_iso or _utc_now_iso()
        rec.duration_actual_s = max(0.0, end_lsl - rec.start_lsl)
        if events is not None:
            rec.events = list(events)
        rec.status = status
        self._write_block_csv(rec)
        self._refresh_partial()

    def mark_pending_blocks_aborted(self) -> int:
        n = 0
        for rec in self.blocks:
            if rec.status == "in_progress":
                rec.status = "aborted"
                self._write_block_csv(rec)
                n += 1
        if n:
            self._refresh_partial()
        return n

    # ----- finalize -------------------------------------------------------

    def finalize(self, status: str = "completed") -> Path:
        if status not in ("completed", "aborted"):
            raise ValueError(f"bad pass status: {status}")
        self.status = status
        self.end_iso = _utc_now_iso()
        atomic_write_json(self._session_path, self._session_dict())
        if status == "completed":
            try:
                self._partial_path.unlink()
            except FileNotFoundError:
                pass
        # On abort: leave PARTIAL_session.json on disk as forensic snapshot.
        return self._session_path

    # ----- markers CSV mirror --------------------------------------------

    @property
    def markers_csv_path(self) -> Path:
        return self.dir / "markers.csv"

    def open_markers_csv(self) -> MarkerCsvAppender:
        """Open (or create+header) the markers CSV for streaming appends.

        The PassWriter doesn't own marker emission — markers.py does — but
        we expose the path + a thin appender here so the marker mirror
        and the per-block CSVs live in the same directory.
        """
        return MarkerCsvAppender(self.markers_csv_path)

    # ----- internal -------------------------------------------------------

    def _session_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "pass_label": self.pass_label,
            "device": self.device,
            "start_lsl": self.start_lsl,
            "start_iso": self.start_iso,
            "end_iso": self.end_iso,
            "status": self.status,
            "blocks": [b.to_dict() for b in self.blocks],
            "config_snapshot": self.config_snapshot,
        }

    def _refresh_partial(self) -> None:
        atomic_write_json(self._partial_path, self._session_dict())

    def _write_block_csv(self, rec: BlockRecord) -> None:
        # Re-runs get an ``_attemptN`` suffix so a re-run never overwrites
        # the first attempt's CSV.
        suffix = f"_attempt{rec.attempt}" if rec.attempt > 1 else ""
        path = self.dir / f"block_{rec.block_id:02d}_{rec.label}{suffix}.csv"
        rows: list[dict[str, Any]] = []
        # First row: block summary.
        rows.append(
            {
                "kind": "block",
                "block_id": rec.block_id,
                "attempt": rec.attempt,
                "label": rec.label,
                "duration_planned_s": rec.duration_planned_s,
                "start_lsl": rec.start_lsl,
                "end_lsl": rec.end_lsl if rec.end_lsl is not None else "",
                "duration_actual_s": (
                    rec.duration_actual_s if rec.duration_actual_s is not None else ""
                ),
                "status": rec.status,
                "event_label": "",
                "event_kind": "",
                "event_lsl": "",
                "event_payload": "",
            }
        )
        for ev in rec.events:
            rows.append(
                {
                    "kind": "event",
                    "block_id": rec.block_id,
                    "attempt": rec.attempt,
                    "label": rec.label,
                    "duration_planned_s": "",
                    "start_lsl": "",
                    "end_lsl": "",
                    "duration_actual_s": "",
                    "status": "",
                    "event_label": ev.get("label", ""),
                    "event_kind": ev.get("kind", ""),
                    "event_lsl": ev.get("lsl_ts", ""),
                    "event_payload": json.dumps(ev.get("payload", {}), default=str),
                }
            )

        fieldnames = [
            "kind",
            "block_id",
            "attempt",
            "label",
            "duration_planned_s",
            "start_lsl",
            "end_lsl",
            "duration_actual_s",
            "status",
            "event_label",
            "event_kind",
            "event_lsl",
            "event_payload",
        ]

        # csv.DictWriter doesn't support atomic writes directly; build the
        # text in memory then atomic-write.
        import io

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        atomic_write_text(path, buf.getvalue())


# ---------------------------------------------------------------------------
# Markers CSV appender
# ---------------------------------------------------------------------------


class MarkerCsvAppender:
    """Streaming CSV writer for the LSL-marker mirror.

    Header columns: ``timestamp_iso, timestamp_lsl, marker_string``.
    Appended line-by-line; no atomic semantics — the CSV is forensic
    redundancy. The authoritative copy is the XDF.
    """

    HEADER = ("timestamp_iso", "timestamp_lsl", "marker_string")

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0
        self._fp = self.path.open("a", encoding="utf-8", newline="")
        self._csv = csv.writer(self._fp)
        if new:
            self._csv.writerow(self.HEADER)
            self._fp.flush()

    def append(self, timestamp_iso: str, timestamp_lsl: float, marker_string: str) -> None:
        self._csv.writerow([timestamp_iso, f"{timestamp_lsl:.9f}", marker_string])
        self._fp.flush()

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> MarkerCsvAppender:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")
