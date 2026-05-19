"""LSL marker outlet wrapper + CSV mirror.

A single string outlet per pass (``nominal_srate=0``,
``channel_format=cf_string``). The outlet is closed and re-opened with a
new ``source_id`` between Pass A and Pass B so each XDF contains exactly
one pass's markers.

Marker grammar (PROJECT_PLAN.md § 9): subfields separated by ``:``,
key-value pairs use ``=``. No free-text. ``format_marker`` validates
that no payload value contains a reserved character.

CSV mirror columns: ``timestamp_iso, timestamp_lsl, marker_string``
(borrowed from Jazzo).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pylsl

from .persistence import MarkerCsvAppender

__all__ = [
    "MARKER_RESERVED_CHARS",
    "format_marker",
    "build_source_id",
    "MuseProxyMarkerOutlet",
]

MARKER_RESERVED_CHARS: tuple[str, ...] = (":", "=", "\n", "\r", "\t")


# ---------------------------------------------------------------------------
# Marker formatting
# ---------------------------------------------------------------------------


def format_marker(event: str, payload: Mapping[str, Any] | None = None) -> str:
    """Build the canonical wire format for a marker.

    The ``event`` portion may itself contain ``:`` (e.g. ``"breath:inhale"``).
    Payload values may NOT contain reserved characters.

    >>> format_marker("session_start")
    'session_start'
    >>> format_marker("breath:inhale", {"block": 2, "cycle": 1, "pass": "A"})
    'breath:inhale:block=2:cycle=1:pass=A'
    """
    if not event:
        raise ValueError("event may not be empty")
    if any(c in event for c in ("\n", "\r", "\t", "=")):
        raise ValueError(f"event contains reserved chars: {event!r}")
    parts: list[str] = [event]
    if payload:
        for key, value in payload.items():
            if not key:
                raise ValueError(f"empty key in marker payload: {payload!r}")
            if any(c in key for c in MARKER_RESERVED_CHARS):
                raise ValueError(f"reserved char in marker key: {key!r}")
            s_value = _format_value(value)
            if any(c in s_value for c in MARKER_RESERVED_CHARS):
                raise ValueError(
                    f"reserved char in marker value for key {key!r}: {value!r}"
                )
            parts.append(f"{key}={s_value}")
    return ":".join(parts)


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "Y" if value else "N"
    if isinstance(value, float):
        # Strip trailing zeros / unnecessary decimal so 0.1 -> "0.1", 1.0 -> "1"
        s = f"{value:.6f}".rstrip("0").rstrip(".")
        return s if s else "0"
    if isinstance(value, int):
        return str(value)
    if value is None:
        return ""
    return str(value)


def build_source_id(template: str, pass_label: str, pid: str, date_yyyymmdd: str) -> str:
    """Construct the LSL outlet source_id for one pass.

    >>> build_source_id("MuseProxy", "A", "P001", "20260512")
    'MuseProxy_passA_P001_20260512'
    """
    if pass_label not in ("A", "B"):
        raise ValueError(f"pass_label must be 'A' or 'B', got {pass_label!r}")
    return f"{template}_pass{pass_label}_{pid}_{date_yyyymmdd}"


# ---------------------------------------------------------------------------
# Outlet wrapper
# ---------------------------------------------------------------------------


class MuseProxyMarkerOutlet:
    """LSL string outlet + optional CSV mirror.

    One instance per pass. Construct it at the start of a pass, call
    ``push(...)`` for each marker, call ``close()`` at the end.

    The CSV mirror is an optional path. If supplied, every successful push
    is appended (timestamp_iso, timestamp_lsl, marker_string).
    """

    def __init__(
        self,
        *,
        outlet_name: str,
        outlet_type: str,
        source_id: str,
        csv_path: str | Path | None = None,
        chunk_size: int = 1,
        max_buffered: int = 60,
    ) -> None:
        self.outlet_name = outlet_name
        self.outlet_type = outlet_type
        self.source_id = source_id

        info = pylsl.StreamInfo(
            name=outlet_name,
            type=outlet_type,
            channel_count=1,
            nominal_srate=pylsl.IRREGULAR_RATE,
            channel_format=pylsl.cf_string,
            source_id=source_id,
        )
        # Embed minimal description so Lab Recorder shows useful metadata.
        desc = info.desc()
        desc.append_child_value("manufacturer", "Institute for Advanced Consciousness Studies")
        desc.append_child_value("app", "MuseProxy")
        ch = desc.append_child("channels").append_child("channel")
        ch.append_child_value("label", "marker")
        ch.append_child_value("type", "Markers")

        self._outlet: pylsl.StreamOutlet | None = pylsl.StreamOutlet(
            info, chunk_size=chunk_size, max_buffered=max_buffered
        )

        self._lock = threading.Lock()
        self._csv: MarkerCsvAppender | None = None
        if csv_path is not None:
            self._csv = MarkerCsvAppender(csv_path)

        self._closed = False

    # ----- low-level push -------------------------------------------------

    def push_raw(
        self,
        marker: str,
        *,
        lsl_timestamp: float | None = None,
        pushthrough: bool = True,
    ) -> float:
        """Push a pre-formatted marker string. Returns the LSL timestamp used.

        If ``lsl_timestamp`` is None, ``pylsl.local_clock()`` is sampled at
        push time. For sample-anchored markers (audio cues), the caller
        supplies the timestamp it has already computed from
        ``outputBufferDacTime``.
        """
        if self._closed or self._outlet is None:
            raise RuntimeError("outlet is closed; cannot push")
        ts = lsl_timestamp if lsl_timestamp is not None else pylsl.local_clock()
        with self._lock:
            self._outlet.push_sample([marker], timestamp=ts, pushthrough=pushthrough)
            if self._csv is not None:
                self._csv.append(_iso_from_lsl(ts), ts, marker)
        return ts

    def push(
        self,
        event: str,
        payload: Mapping[str, Any] | None = None,
        *,
        lsl_timestamp: float | None = None,
    ) -> float:
        """Format + push. Returns the LSL timestamp."""
        return self.push_raw(format_marker(event, payload), lsl_timestamp=lsl_timestamp)

    # ----- convenience: visit / pass / block markers ----------------------

    def session_start(self, *, pid: str, date_yyyymmdd: str) -> float:
        # The LSL timestamp on the marker is the visit start; ISO is in
        # the CSV mirror. Keep the marker payload free of reserved
        # characters (no ':' in values).
        return self.push("session_start", {"pid": pid, "date": date_yyyymmdd})

    def session_end(self, *, pid: str) -> float:
        return self.push("session_end", {"pid": pid})

    def session_abort(self, *, pass_label: str, stage: str) -> float:
        return self.push("session_abort", {"pass": pass_label, "stage": stage})

    def pass_start(self, *, pass_label: str, device: str) -> float:
        return self.push(
            "pass_start",
            {"pass": pass_label, "device": device, "source_id": self.source_id},
        )

    def pass_end(self, *, pass_label: str) -> float:
        return self.push("pass_end", {"pass": pass_label})

    def block_start(
        self,
        *,
        block_id: int,
        label: str,
        pass_label: str,
        duration_planned_s: float,
        attempt: int = 1,
    ) -> float:
        payload: dict[str, Any] = {
            "block": block_id,
            "label": label,
            "pass": pass_label,
            "duration_planned_s": duration_planned_s,
        }
        # ``attempt`` is omitted for the first run so attempt-1 markers are
        # byte-identical to pre-salvage recordings. Re-runs carry attempt=N.
        if attempt > 1:
            payload["attempt"] = attempt
        return self.push("block_start", payload)

    def block_end(
        self,
        *,
        block_id: int,
        label: str,
        pass_label: str,
        duration_actual_s: float,
        attempt: int = 1,
    ) -> float:
        payload: dict[str, Any] = {
            "block": block_id,
            "label": label,
            "pass": pass_label,
            "duration_actual_s": duration_actual_s,
        }
        if attempt > 1:
            payload["attempt"] = attempt
        return self.push("block_end", payload)

    def between_block_settle_start(self, *, after_block: int) -> float:
        return self.push("between_block_settle:start", {"after_block": after_block})

    def between_block_settle_end(self, *, after_block: int, duration_s: float) -> float:
        return self.push(
            "between_block_settle:end",
            {"after_block": after_block, "duration_s": duration_s},
        )

    def settle_start(self, *, pass_label: str, duration_planned_s: float) -> float:
        return self.push(
            "settle:start",
            {"pass": pass_label, "duration_planned_s": duration_planned_s},
        )

    def settle_end(self, *, pass_label: str, duration_elapsed_s: float) -> float:
        return self.push(
            "settle:end",
            {"pass": pass_label, "duration_elapsed_s": duration_elapsed_s},
        )

    def volume_check_start(self) -> float:
        return self.push("volume_check:start")

    def volume_check_end(self, *, level: str = "ok") -> float:
        return self.push("volume_check:end", {"level": level})

    def intake(self, **kv: Any) -> float:
        return self.push("intake", kv)

    def device_marker(self, *, pass_label: str, device: str) -> float:
        # e.g. pass_a_device:muse2  or  pass_b_device:athena
        return self.push(
            f"pass_{pass_label.lower()}_device:{device}"
        )

    def breath(
        self,
        kind: str,
        *,
        block_id: int,
        cycle: int,
        pass_label: str,
        lsl_timestamp: float,
        attempt: int = 1,
    ) -> float:
        if kind not in ("inhale", "exhale"):
            raise ValueError(f"breath kind must be inhale|exhale; got {kind!r}")
        payload: dict[str, Any] = {"block": block_id, "cycle": cycle, "pass": pass_label}
        if attempt > 1:
            payload["attempt"] = attempt
        return self.push(
            f"breath:{kind}", payload, lsl_timestamp=lsl_timestamp
        )

    def artifact(
        self,
        *,
        label: str,
        kind: str,
        pass_label: str,
        lsl_timestamp: float,
        block_id: int = 4,
        attempt: int = 1,
    ) -> float:
        if kind not in ("start", "end"):
            raise ValueError(f"artifact kind must be start|end; got {kind!r}")
        payload: dict[str, Any] = {"block": block_id, "pass": pass_label}
        if attempt > 1:
            payload["attempt"] = attempt
        return self.push(
            f"artifact:{label}:{kind}", payload, lsl_timestamp=lsl_timestamp
        )

    def tts(self, *, clip: str, kind: str, lsl_timestamp: float | None = None) -> float:
        if kind not in ("start", "end"):
            raise ValueError(f"tts kind must be start|end; got {kind!r}")
        return self.push(f"tts:{clip}:{kind}", lsl_timestamp=lsl_timestamp)

    # ----- lifecycle ------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # pylsl outlets are torn down by garbage collection; explicit del
        # ensures Lab Recorder sees the disappearance promptly.
        try:
            self._outlet = None
        except Exception:  # noqa: BLE001
            pass
        if self._csv is not None:
            try:
                self._csv.close()
            except Exception:  # noqa: BLE001
                pass
            self._csv = None

    def __enter__(self) -> MuseProxyMarkerOutlet:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _iso_from_lsl(lsl_ts: float) -> str:
    """Best-effort: convert an LSL local_clock timestamp to UTC ISO.

    ``pylsl.local_clock()`` returns seconds since an unspecified epoch on
    the local machine. We compute the offset between ``local_clock`` and
    ``time.time`` once per call — cheap, and lets us spell each row as a
    real UTC timestamp.
    """
    import time as _time

    now_wall = _time.time()
    now_lsl = pylsl.local_clock()
    offset = now_wall - now_lsl
    return datetime.fromtimestamp(lsl_ts + offset, tz=UTC).isoformat(
        timespec="milliseconds"
    )
