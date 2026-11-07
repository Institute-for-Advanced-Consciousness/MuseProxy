"""Muse-Athena ("MuseS-XXXX") direct BLE protocol and publisher.

Ported from the IACS relay (Institute-for-Advanced-Consciousness/
Muse-S-Athena-RealTime-Visualization, ``muse_relay/bridge``): same GATT layout,
the same primed init handshake (``p21`` → ``dc001`` → target preset →
double ``dc001``) and the same OpenMuse-derived decoder (vendored in
``athena_decode.py``). Differences from the relay, for this study:

* publishes with ``pylsl`` (the rest of MuseProxy's stack) instead of mne-lsl;
* optics are always one combined ``Muse-Athena-OPTICS`` stream (stream type ``PPG``)
  carrying every optics channel the preset provides — PPG and fNIRS both;
* packet loss is detected from the device tick (see ``TickGapDetector``).

A finding from porting this: the OpenMuse ``make_timestamps`` the relay
vendors measures ticks relative to the first tick *of each batch*, so the tick
only orders packets — sample times are effectively a running sample counter.
A lost packet therefore does not leave a gap: every later sample is placed
one packet too early until the 30 s regression window absorbs it. That is
harmless for visualisation but not for HEP timing. ``TickGapDetector``
measures the loss against a persistent tick; by default it only reports it
(counts + BridgeEvents), and with ``gap_correction=True`` it also shifts later
samples by the lost duration. Correction stays opt-in until validated on
hardware, because the tick's exact semantics within a packet group are not
documented. Nothing is ever zero-filled.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any

import numpy as np

from .athena_decode import (
    ACCGYRO_CHANNELS,
    BATTERY_CHANNELS,
    DEVICE_CLOCK_HZ,
    SENSORS,
    make_timestamps,
    parse_message,
    select_eeg_channels,
    select_optics_channels,
)
from .common import (
    BRIDGE_VERSION,
    MonotonicGuard,
    WindowedRegressionClock,
    make_outlet,
    push_rows,
)

SERVICE_UUID = "0000fe8d-0000-1000-8000-00805f9b34fb"
CONTROL_UUID = "273e0001-4c4d-454d-96be-f03bac821358"
DATA_UUIDS = (
    "273e0013-4c4d-454d-96be-f03bac821358",
    "273e0014-4c4d-454d-96be-f03bac821358",
)

PRESETS = {
    "p1035": "EEG4 + Optics4 (PPG) + ACC/GYRO + Battery — lightest full-sensor (default)",
    "p1034": "EEG8 + Optics8 + ACC/GYRO + Battery",
    "p1041": "EEG8 + Optics16 (fNIRS) + ACC/GYRO + Battery — highest bandwidth",
    "p1046": "EEG8 + Optics4 + ACC/GYRO + Battery",
    "p21": "EEG4 + ACC/GYRO + Battery — no optics",
}
DEFAULT_PRESET = "p1035"
KEEPALIVE_TOKEN = "s"


def handshake_tokens(preset: str, prime: bool = True) -> list[tuple[str, float]]:
    """The relay's research-validated start sequence (``prime`` on cold start)."""
    if prime:
        return [
            ("v6", 0.20), ("s", 0.20), ("h", 0.20),
            ("p21", 0.20), ("s", 0.10),
            ("dc001", 0.05), ("L1", 0.30),
            ("h", 0.20),
            (preset, 0.20), ("s", 0.10),
            ("dc001", 0.05), ("dc001", 0.05), ("L1", 0.30),
            ("s", 0.20),
        ]
    return [
        ("v6", 0.20), ("s", 0.20), ("h", 0.20),
        (preset, 0.20), ("s", 0.20),
        ("dc001", 0.05), ("dc001", 0.10), ("L1", 0.30),
        ("s", 0.20),
    ]


_UNITS = {"EEG": "microvolts", "ACCGYRO": None, "OPTICS": "a.u.", "BATTERY": "percent"}
_STYPE = {"EEG": "EEG", "ACCGYRO": "ACCGYRO", "OPTICS": "PPG", "BATTERY": "Battery"}
_FS = {"EEG": 256.0, "ACCGYRO": 52.0, "OPTICS": 64.0, "BATTERY": 0.0}


def _labels(sensor: str, n: int) -> list[str]:
    if sensor == "EEG":
        return select_eeg_channels(n)
    if sensor == "OPTICS":
        return select_optics_channels(n)
    if sensor == "ACCGYRO":
        return list(ACCGYRO_CHANNELS)
    return list(BATTERY_CHANNELS)


def _units(sensor: str, labels: list[str]) -> list[str]:
    if sensor == "ACCGYRO":
        return ["g" if lab.startswith("ACC") else "dps" for lab in labels]
    return [_UNITS[sensor]] * len(labels)  # type: ignore[list-item]


class _ClosedOutlet:
    def push_chunk(self, *_a: Any, **_k: Any) -> None:
        pass


class _Stream:
    def __init__(self, outlet: Any, n_channels: int, fs: float) -> None:
        self.outlet = outlet
        self.n_channels = n_channels
        self.fs = fs
        self.clock = WindowedRegressionClock()
        self.guard = MonotonicGuard()
        # make_timestamps rolling state
        self.base_time: float | None = None
        self.wrap_offset = 0
        self.last_abs_tick = 0
        self.sample_counter = 0
        self.last_update_device_time = -1.0
        self.last_device_time: float | None = None
        self.samples_pushed = 0
        self.mismatch_dropped = 0
        self.gaps = TickGapDetector(fs)


class TickGapDetector:
    """Counts samples lost between subpackets using the persistent 256 kHz tick.

    For each subpacket (in decode order) ``lead`` = tick-implied sample position
    minus samples actually received. Without loss ``lead`` hovers around a
    baseline (whatever the tick means within a packet group); a lost packet of
    ``k`` samples raises it by ``k`` for good. A gap is flagged when ``lead``
    exceeds the recent maximum by more than one subpacket's worth of samples.
    """

    def __init__(self, fs: float, history: int = 64, warmup: int = 16) -> None:
        self.fs = fs
        self.first_tick: int | None = None
        self.wrap = 0
        self.prev_raw = 0
        self.received = 0
        self.leads: deque[int] = deque(maxlen=history)
        self.warmup = warmup
        self.events = 0
        self.samples = 0
        self.cumulative = 0  # samples lost so far (for optional correction)

    def feed(self, raw_tick: int, n_samples: int) -> int:
        """Returns samples lost immediately before this subpacket (0 normally)."""
        if self.fs <= 0:
            return 0
        if self.first_tick is None:
            self.first_tick = raw_tick
            self.prev_raw = raw_tick
        if raw_tick < self.prev_raw and (self.prev_raw - raw_tick) > (1 << 31):
            self.wrap += 1 << 32
            self.prev_raw = raw_tick
        elif raw_tick >= self.prev_raw:
            self.prev_raw = raw_tick
        abs_tick = raw_tick + self.wrap
        rel = int(round((abs_tick - self.first_tick) / DEVICE_CLOCK_HZ * self.fs))
        lead = rel - (self.received + self.cumulative)
        lost = 0
        if len(self.leads) >= self.warmup:
            ref = max(self.leads)
            if lead >= ref + n_samples:
                lost = lead - ref
                self.events += 1
                self.samples += lost
                self.cumulative += lost
                lead -= lost
        self.leads.append(lead)
        self.received += n_samples
        return lost


class AthenaPublisher:
    """Decode → device-tick timestamps → regression clock → LSL, for one Muse-Athena.

    Outlets are created lazily on the first packet of each sensor, because the
    channel count depends on the preset the headset actually honoured.
    Not thread-safe: called from the bridge's single decode thread.
    """

    MODEL = "muse-athena"

    def __init__(
        self,
        device_id: str,
        *,
        sensors: tuple[str, ...] = ("EEG", "OPTICS", "ACCGYRO", "BATTERY"),
        device_desc: dict[str, Any] | None = None,
        outlet_factory=make_outlet,
        gap_correction: bool = False,
    ) -> None:
        self.device_id = device_id
        self.gap_correction = gap_correction
        self.sensors = tuple(s.upper() for s in sensors)
        desc = {
            "manufacturer": "Interaxon",
            "model": "Muse-Athena",
            "device_id": device_id,
            "bridge": "muse_proxy.bridge",
            "bridge_version": BRIDGE_VERSION,
            "missing_samples": "omitted (never zero-filled); losses counted in BridgeEvents",
            "gap_correction": str(gap_correction),
            "timestamps": "device 256 kHz tick -> LSL via windowed regression",
        }
        desc.update(device_desc or {})
        self._desc = desc
        self._factory = outlet_factory
        self.streams: dict[str, _Stream] = {}
        self.battery: float | None = None
        self.parse_errors = 0
        self.messages = 0
        self.opened: list[str] = []

    @property
    def data_uuids(self) -> list[str]:
        return list(DATA_UUIDS)

    def _ensure(self, sensor: str, n_channels: int) -> _Stream | None:
        s = self.streams.get(sensor)
        if s is not None:
            return s
        labels = _labels(sensor, n_channels)
        outlet = self._factory(
            name=f"Muse-Athena-{sensor} ({self.device_id})",
            stype=_STYPE[sensor],
            labels=labels,
            units=_units(sensor, labels),
            fs=_FS[sensor],
            source_id=f"muse-athena_{self.device_id}_{sensor.lower()}",
            desc=self._desc,
        )
        s = self.streams[sensor] = _Stream(outlet, len(labels), _FS[sensor])
        self.opened.append(sensor)
        return s

    def process(self, arrival_lsl: float, utc: str, uuid: str, raw: bytes) -> None:
        self.messages += 1
        try:
            sub = parse_message(f"{utc}\t{uuid}\t{raw.hex()}")
        except Exception:  # noqa: BLE001 — a malformed packet must never kill the stream
            self.parse_errors += 1
            return
        by_sensor: dict[str, list] = defaultdict(list)
        for sensor in ("EEG", "OPTICS", "ACCGYRO", "BATTERY"):
            if sensor in self.sensors:
                by_sensor[sensor] = sub.get(sensor, [])
        for sensor, pkts in by_sensor.items():
            if not pkts:
                continue
            n = pkts[0].get("n_channels")
            if not n:
                continue
            s = self._ensure(sensor, int(n))
            if s is None:
                continue
            try:
                arr, s.base_time, s.wrap_offset, s.last_abs_tick, s.sample_counter = (
                    make_timestamps(pkts, s.base_time, s.wrap_offset, s.last_abs_tick,
                                    s.sample_counter)
                )
            except Exception:  # noqa: BLE001
                self.parse_errors += 1
                continue
            if arr.ndim != 2 or arr.shape[0] == 0:
                continue
            dev_t, data = arr[:, 0], arr[:, 1:]
            if data.shape[1] != s.n_channels:
                # e.g. stray EEG4 packets after the target preset chose EEG8
                s.mismatch_dropped += 1
                continue
            offsets = self._gap_offsets(s, pkts)
            if self.gap_correction and offsets is not None and offsets.size == dev_t.size:
                dev_t = dev_t + offsets / s.fs
            if dev_t[-1] > s.last_update_device_time:
                s.clock.update(float(dev_t[-1]), arrival_lsl)
                s.last_update_device_time = float(dev_t[-1])
            ts = s.guard.apply(s.clock.map(dev_t))
            push_rows(s.outlet, data, ts)
            s.samples_pushed += data.shape[0]
            if sensor == "BATTERY":
                self.battery = float(data[-1, 0])

    @staticmethod
    def _gap_offsets(s: _Stream, pkts: list) -> np.ndarray | None:
        """Feed the gap detector in make_timestamps' order; per-row cumulative
        lost-sample offsets (used only when gap correction is on)."""
        if s.fs <= 0:
            return None
        ordered = sorted(
            pkts,
            key=lambda p: (p.get("pkt_time_raw", 0), p["pkt_index"],
                           p["subpkt_index"] if p["subpkt_index"] is not None else -1),
        )
        cfg = SENSORS.get(ordered[0].get("tag_byte"), {})
        rows = []
        for p in ordered:
            n = int(p.get("n_samples", cfg.get("n_samples", 1)))
            s.gaps.feed(int(p.get("pkt_time_raw", 0)), n)
            rows.append(np.full(n, s.gaps.cumulative, dtype=np.float64))
        return np.concatenate(rows) if rows else None

    def new_segment(self) -> None:
        """On reconnect: the device clock keeps running, but reset the
        regression so it doesn't straddle the outage."""
        for s in self.streams.values():
            s.clock = WindowedRegressionClock()
            s.last_update_device_time = -1.0

    def close(self) -> None:
        """Destroy outlets so the streams vanish from LSL when the bridge stops."""
        for s in self.streams.values():
            s.outlet = _ClosedOutlet()


    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"battery": self.battery, "parse_errors": self.parse_errors,
                               "messages": self.messages}
        for k, s in self.streams.items():
            expected = s.samples_pushed + s.gaps.samples
            out[k] = {
                "samples": s.samples_pushed,
                "gap_events": s.gaps.events,
                "gap_samples": s.gaps.samples,
                "lost_pct": round(100.0 * s.gaps.samples / expected, 2) if expected else 0.0,
                "mismatch_dropped": s.mismatch_dropped,
            }
        return out


# --- control-channel JSON (firmware / battery / serial) ------------------------------
_FW_RE = re.compile(r'"fw"\s*:\s*"([^"]+)"')
_BP_RE = re.compile(r'"bp"\s*:\s*([\d.]+)')
_SN_RE = re.compile(r'"sn"\s*:\s*"([^"]+)"')
_HW_RE = re.compile(r'"hw"\s*:\s*"([^"]+)"')


class ControlInfo:
    """Accumulates control-characteristic JSON fragments (both models)."""

    def __init__(self) -> None:
        self._buf = ""
        self.firmware: str | None = None
        self.hardware: str | None = None
        self.battery: float | None = None
        self.serial: str | None = None

    def feed(self, data: bytes) -> None:
        # Muse-2 control notifications carry a 1-byte length prefix before the
        # text fragment; Muse-Athena's are plain text. Strip non-printables either way.
        text = "".join(ch for ch in data.decode("latin-1") if 32 <= ord(ch) < 127)
        self._buf = (self._buf + text)[-4096:]
        if m := _FW_RE.search(self._buf):
            self.firmware = m.group(1)
        if m := _HW_RE.search(self._buf):
            self.hardware = m.group(1)
        if m := _BP_RE.search(self._buf):
            try:
                self.battery = float(m.group(1))
            except ValueError:
                pass
        if m := _SN_RE.search(self._buf):
            self.serial = m.group(1)
