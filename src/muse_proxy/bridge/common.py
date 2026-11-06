"""Shared pieces for the direct-BLE bridges: clock model, LSL outlets, raw log.

Everything here is device-agnostic. Device specifics (GATT layout, packet
decode, handshake) live in ``muse2.py`` and ``athena.py``.

Timing model (both devices)
---------------------------
Each sample gets a *device time* — derived from the headset's own clock
(Athena: 256 kHz tick counter in every packet) or from the packet sequence
number at the nominal rate (Muse 2: a 16-bit per-characteristic counter, so
``device_time = packet_index * samples_per_packet / fs``). Device time is then
mapped onto the LSL clock by a windowed linear regression of
``(device_time, BLE arrival time)`` pairs. The regression absorbs crystal drift
(slope) and the average BLE delivery latency (intercept); it does NOT inherit
per-packet arrival jitter, which is what made muselsl's ACC/GYRO timestamps run
backwards. Outputs are additionally forced monotonic per stream.

Missing data is NaN, never zero. Zero is a legal sensor value; NaN is not.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pylsl

BRIDGE_VERSION = "1.0.0"

# Minimum forward step when nudging a batch to keep timestamps monotonic
# (~10 µs, far below any sensor sample interval).
_TS_EPS = 1e-5


def utc_iso() -> str:
    """UTC ISO-8601 with microseconds — the raw-log line timestamp."""
    return datetime.now(UTC).isoformat()


# -----------------------------------------------------------------------------
# Clock
# -----------------------------------------------------------------------------
class WindowedRegressionClock:
    """``lsl_time = slope * device_time + intercept`` over a sliding window.

    Adapted from OpenMuse's ``WindowedRegressionClock`` (the model the IACS
    relay validated against a photodiode ground truth). Refit at most once per
    second. Before 10 points exist it falls back to the most recent offset.
    """

    def __init__(self, window_s: float = 30.0, fit_interval_s: float = 1.0) -> None:
        self.window_s = window_s
        self.fit_interval_s = fit_interval_s
        self.history: deque[tuple[float, float]] = deque()
        self.slope = 1.0
        self.intercept = 0.0
        self.initialized = False
        self._last_fit = -1e18

    def update(self, device_time: float, lsl_arrival: float) -> None:
        self.history.append((device_time, lsl_arrival))
        limit = device_time - self.window_s
        while self.history and self.history[0][0] < limit:
            self.history.popleft()
        if (lsl_arrival - self._last_fit) > self.fit_interval_s or (
            not self.initialized and len(self.history) >= 10
        ):
            self._fit()
            self._last_fit = lsl_arrival

    def _fit(self) -> None:
        if len(self.history) < 10:
            return
        data = np.asarray(self.history, dtype=np.float64)
        x, y = data[:, 0], data[:, 1]
        xc = x - x.mean()
        denom = float(np.sum(xc * xc))
        if denom < 1e-9:
            return
        slope = float(np.sum(xc * (y - y.mean())) / denom)
        # A wildly wrong slope means the window straddles a discontinuity the
        # caller failed to reset on; keep the previous model rather than
        # publishing nonsense timestamps.
        if not 0.95 < slope < 1.05:
            return
        self.slope = slope
        self.intercept = float(y.mean() - slope * x.mean())
        self.initialized = True

    def map(self, device_times: np.ndarray) -> np.ndarray:
        device_times = np.asarray(device_times, dtype=np.float64)
        if self.initialized:
            return self.intercept + self.slope * device_times
        if self.history:
            dt, lt = self.history[-1]
            return device_times + (lt - dt)
        if device_times.size:
            return device_times + (pylsl.local_clock() - device_times[-1])
        return device_times


class MonotonicGuard:
    """Shift a batch forward if it would start at/before the last pushed stamp."""

    def __init__(self) -> None:
        self.last: float | None = None

    def apply(self, ts: np.ndarray) -> np.ndarray:
        if ts.size == 0:
            return ts
        if self.last is not None and ts[0] <= self.last:
            ts = ts + (self.last + _TS_EPS - ts[0])
        # Within-batch: enforce non-decreasing (device-derived times already are).
        ts = np.maximum.accumulate(ts)
        self.last = float(ts[-1])
        return ts


# -----------------------------------------------------------------------------
# LSL outlets
# -----------------------------------------------------------------------------
def make_outlet(
    *,
    name: str,
    stype: str,
    labels: Sequence[str],
    units: Sequence[str] | str,
    fs: float,
    source_id: str,
    desc: Mapping[str, Any],
    channel_format: str = "float32",
) -> pylsl.StreamOutlet:
    """Build an outlet whose metadata documents exactly what is in it."""
    info = pylsl.StreamInfo(
        name=name,
        type=stype,
        channel_count=len(labels),
        nominal_srate=fs,
        channel_format=channel_format,
        source_id=source_id,
    )
    d = info.desc()
    for key, value in desc.items():
        d.append_child_value(str(key), str(value))
    chans = d.append_child("channels")
    if isinstance(units, str):
        units = [units] * len(labels)
    for label, unit in zip(labels, units, strict=True):
        ch = chans.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", unit)
        ch.append_child_value("type", stype)
    return pylsl.StreamOutlet(info, chunk_size=0, max_buffered=360)


class EventOutlet:
    """Irregular string stream of bridge lifecycle/health events.

    Type is ``BridgeEvents`` (not ``Markers``) so nothing that looks for the
    experiment's marker stream by type can confuse the two.
    """

    def __init__(self, device_id: str, model: str) -> None:
        info = pylsl.StreamInfo(
            name=f"MuseBridge-Events ({device_id})",
            type="BridgeEvents",
            channel_count=1,
            nominal_srate=0,
            channel_format="string",
            source_id=f"musebridge_{model}_{device_id}_events",
        )
        info.desc().append_child_value("bridge_version", BRIDGE_VERSION)
        info.desc().append_child_value("model", model)
        self._outlet: pylsl.StreamOutlet | None = pylsl.StreamOutlet(info)
        self._lock = threading.Lock()

    def push(self, event: str, payload: Mapping[str, Any] | None = None) -> str:
        parts = [event]
        for k, v in (payload or {}).items():
            parts.append(f"{k}={_clean(v)}")
        text = ":".join(parts)
        with self._lock:
            if self._outlet is not None:
                self._outlet.push_sample([text], pylsl.local_clock())
        return text

    def close(self) -> None:
        with self._lock:
            self._outlet = None


def _clean(v: Any) -> str:
    """Marker-grammar safe value (``:`` and ``=`` are reserved)."""
    if isinstance(v, float):
        v = f"{v:.4f}".rstrip("0").rstrip(".")
    return str(v).replace(":", "_").replace("=", "_").replace("\n", " ")


def push_rows(outlet: pylsl.StreamOutlet, rows: np.ndarray, ts: np.ndarray) -> None:
    """Push a (n_samples, n_channels) block with one timestamp per row."""
    if rows.shape[0] == 0:
        return
    outlet.push_chunk(
        np.ascontiguousarray(rows, dtype=np.float32), ts.astype(np.float64).tolist()
    )


# -----------------------------------------------------------------------------
# Raw packet log
# -----------------------------------------------------------------------------
class RawPacketLog:
    """Every BLE notification, verbatim, in OpenMuse's line format.

    ``<utc_iso>\\t<characteristic_uuid>\\t<hex payload>`` — one line per
    notification. For the Athena this file is directly readable by OpenMuse's
    ``parse_message``; for the Muse 2 the same 3-column shape is used so one
    reader handles both.

    A sidecar ``*_clock.csv`` records ``utc_iso, lsl_local_clock`` once per
    second so raw-log times can be mapped onto the XDF's LSL clock afterwards
    (this is what was missing for the 2026 Athena logs).

    This log is the ground truth: if Lab Recorder or an LSL hop fails, the
    session can be fully re-decoded from it.
    """

    def __init__(self, directory: Path, stem: str, header: Mapping[str, Any]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.path = directory / f"{stem}_{stamp}.txt"
        self.clock_path = directory / f"{stem}_{stamp}_clock.csv"
        self.meta_path = directory / f"{stem}_{stamp}_meta.txt"
        self._fh = open(self.path, "a", encoding="ascii", newline="\n")  # noqa: SIM115
        self._clock = open(self.clock_path, "a", encoding="ascii", newline="\n")  # noqa: SIM115
        self._clock.write("utc_iso,lsl_local_clock\n")
        with open(self.meta_path, "w", encoding="utf-8", newline="\n") as fh:
            for k, v in header.items():
                fh.write(f"{k}: {v}\n")
        self.lines = 0
        self._last_anchor = 0.0
        self._last_flush = time.monotonic()
        self._closed = False

    def write(self, utc: str, uuid: str, raw: bytes) -> None:
        if self._closed:
            return
        self._fh.write(f"{utc}\t{uuid}\t{raw.hex()}\n")
        self.lines += 1
        now = time.monotonic()
        if now - self._last_anchor >= 1.0:
            self._clock.write(f"{utc_iso()},{pylsl.local_clock():.6f}\n")
            self._last_anchor = now
        if now - self._last_flush >= 1.0:
            self.flush()

    def note(self, text: str) -> None:
        """Append a free-text line to the meta file (connect/disconnect, etc.)."""
        if self._closed:
            return
        with open(self.meta_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{utc_iso()} {text}\n")

    def flush(self) -> None:
        self._fh.flush()
        self._clock.flush()
        self._last_flush = time.monotonic()

    def close(self) -> None:
        if self._closed:
            return
        self._clock.write(f"{utc_iso()},{pylsl.local_clock():.6f}\n")
        self.flush()
        self._fh.close()
        self._clock.close()
        self._closed = True
