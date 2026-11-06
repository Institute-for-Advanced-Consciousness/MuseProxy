"""Muse 2 (2018/2019, "Muse-XXXX") direct BLE protocol, decode and publisher.

Why this exists
---------------
The 2026 study recorded the Muse 2 through muselsl, which (a) zero-fills any
channel whose notification didn't arrive before the AF7 (EEG) / RED (PPG)
notification that triggers a push, and (b) timestamps ACC/GYRO by arrival,
producing backwards time. This module replaces it.

Wire format (each sensor channel is its own GATT characteristic)
---------------------------------------------------------------
Every notification is 20 bytes: ``uint16 big-endian packet index`` followed by
18 payload bytes. The packet index is shared by all channels of one sensor
that belong to the same time block (TP9/AF7/AF8/TP10/AUX with index N cover the
same 12 samples), and increments by one per packet, wrapping at 65536.

=========  ==========================  =================================
Sensor     Payload                     Rate / samples per packet
=========  ==========================  =================================
EEG        12 × uint12 BE              256 Hz / 12  (µV = 0.48828125·(x−2048))
PPG        6 × uint24 BE               64 Hz / 6    (raw ADC counts)
ACC        3 × (x,y,z) int16 BE        52 Hz / 3    (g = 0.0000610352·x)
GYRO       3 × (x,y,z) int16 BE        52 Hz / 3    (dps = 0.0074768·x)
TELEMETRY  battery, fuel, adc, temp    ~0.1 Hz      (battery% = x/512)
=========  ==========================  =================================

Because the sequence number is in every packet, a dropped packet is detected
*exactly*: the assembler emits NaN for that channel/packet (never zero), keeps
the sample timeline intact, and counts the loss.

Commands (control characteristic, OpenMuse/muse-js length-prefixed ASCII):
``h`` halt · ``s`` status · ``v1`` version · ``p50`` preset with PPG ·
``p21`` EEG4 only · ``p20`` EEG4+AUX · ``d`` start/resume · ``k`` keep-alive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .common import (
    BRIDGE_VERSION,
    MonotonicGuard,
    WindowedRegressionClock,
    make_outlet,
    push_rows,
)

# --- GATT layout ---------------------------------------------------------------
_BASE = "273e{:04x}-4c4d-454d-96be-f03bac821358"
CONTROL_UUID = _BASE.format(0x0001)
SERVICE_UUID = "0000fe8d-0000-1000-8000-00805f9b34fb"

EEG_UUIDS: dict[str, str] = {
    "TP9": _BASE.format(0x0003),
    "AF7": _BASE.format(0x0004),
    "AF8": _BASE.format(0x0005),
    "TP10": _BASE.format(0x0006),
    "AUX": _BASE.format(0x0007),
}
GYRO_UUID = _BASE.format(0x0009)
ACC_UUID = _BASE.format(0x000A)
TELEMETRY_UUID = _BASE.format(0x000B)
PPG_UUIDS: dict[str, str] = {
    # Channel order matches the 2026 XDFs (LUX, IR, RED) so analysis code that
    # indexes PPG columns keeps working.
    "AMBIENT": _BASE.format(0x000F),
    "IR": _BASE.format(0x0010),
    "RED": _BASE.format(0x0011),
}

EEG_FS, EEG_N = 256.0, 12
PPG_FS, PPG_N = 64.0, 6
IMU_FS, IMU_N = 52.0, 3
EEG_UV_PER_LSB = 0.48828125
ACC_G_PER_LSB = 0.0000610352
GYRO_DPS_PER_LSB = 0.0074768
PACKET_LEN = 20

DEFAULT_PRESET = "p50"  # EEG4 + PPG (muse-js / muselsl --ppg use the same)
KEEPALIVE_TOKEN = "k"


def handshake_tokens(preset: str) -> list[tuple[str, float]]:
    """Ordered ``(command, sleep_after_s)`` start sequence.

    Halt anything in flight, ask version/status (populates control JSON with
    firmware + battery), select preset, re-poll status, start.
    """
    return [("h", 0.2), ("v1", 0.2), ("s", 0.2), (preset, 0.3), ("s", 0.2), ("d", 0.3)]


# --- decode ---------------------------------------------------------------------
def packet_index(raw: bytes) -> int:
    return (raw[0] << 8) | raw[1]


def decode_eeg(raw: bytes) -> np.ndarray:
    """20-byte EEG notification → 12 samples in µV."""
    b = np.frombuffer(raw, dtype=np.uint8, count=18, offset=2).astype(np.int32).reshape(6, 3)
    first = (b[:, 0] << 4) | (b[:, 1] >> 4)
    second = ((b[:, 1] & 0x0F) << 8) | b[:, 2]
    counts = np.empty(12, dtype=np.int32)
    counts[0::2] = first
    counts[1::2] = second
    return (counts - 2048).astype(np.float64) * EEG_UV_PER_LSB


def decode_ppg(raw: bytes) -> np.ndarray:
    """20-byte PPG notification → 6 raw uint24 counts."""
    b = np.frombuffer(raw, dtype=np.uint8, count=18, offset=2).astype(np.int64).reshape(6, 3)
    return ((b[:, 0] << 16) | (b[:, 1] << 8) | b[:, 2]).astype(np.float64)


def decode_imu(raw: bytes, scale: float) -> np.ndarray:
    """20-byte ACC/GYRO notification → (3 samples, 3 axes) scaled."""
    v = np.frombuffer(raw, dtype=">i2", count=9, offset=2).astype(np.float64)
    return v.reshape(3, 3) * scale


def decode_telemetry(raw: bytes) -> np.ndarray:
    """→ [battery_percent, fuel_gauge_mV, adc_mV, temperature_raw]."""
    v = np.frombuffer(raw, dtype=">u2", count=4, offset=2).astype(np.float64)
    return np.array([v[0] / 512.0, v[1] * 2.2, float(v[2]), float(v[3])])


# --- encode (synthetic device + tests) ---------------------------------------------
def encode_eeg(index: int, uv: np.ndarray) -> bytes:
    counts = np.clip(np.round(np.asarray(uv) / EEG_UV_PER_LSB) + 2048, 0, 4095).astype(int)
    out = bytearray([(index >> 8) & 0xFF, index & 0xFF])
    for i in range(0, 12, 2):
        a, c = int(counts[i]), int(counts[i + 1])
        out += bytes([a >> 4, ((a & 0x0F) << 4) | (c >> 8), c & 0xFF])
    return bytes(out)


def encode_ppg(index: int, counts: np.ndarray) -> bytes:
    out = bytearray([(index >> 8) & 0xFF, index & 0xFF])
    for v in np.clip(np.round(counts), 0, 0xFFFFFF).astype(int):
        out += bytes([(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])
    return bytes(out)


def encode_imu(index: int, values: np.ndarray, scale: float) -> bytes:
    ints = np.clip(np.round(np.asarray(values).reshape(9) / scale), -32768, 32767)
    return bytes([(index >> 8) & 0xFF, index & 0xFF]) + ints.astype(">i2").tobytes()


def encode_telemetry(index: int, battery_pct: float) -> bytes:
    vals = np.array([round(battery_pct * 512), 1800, 0, 300], dtype=">u2")
    return bytes([(index >> 8) & 0xFF, index & 0xFF]) + vals.tobytes() + bytes(10)


# --- sequence-aware assembly ----------------------------------------------------
@dataclass
class AssemblerStats:
    packets: int = 0
    frames_emitted: int = 0
    channel_packets_missing: int = 0   # one channel absent from an otherwise-present frame
    frames_missing: int = 0            # every channel absent (whole packet slot lost)
    late_discarded: int = 0            # arrived after its frame was already emitted
    duplicates: int = 0
    resyncs: int = 0                   # gap too large to fill → timeline restarted


@dataclass
class _Pending:
    data: np.ndarray
    have: set[int] = field(default_factory=set)


class SequenceAssembler:
    """Re-assembles per-channel notifications into frames by packet index.

    ``feed`` accepts one channel's packet; returns emitted frames, in strict
    index order, as ``(absolute_index, block)`` where ``block`` is
    ``(samples_per_packet, n_channels)`` with NaN wherever a channel's packet
    never arrived. A frame is emitted as soon as all channels are present, or
    once the stream has moved ``max_wait`` packets past it. Gaps up to
    ``max_fill`` packets are emitted as NaN frames so the sample timeline stays
    intact; a larger gap (reconnect, long stall) restarts the timeline instead.
    """

    def __init__(
        self,
        n_channels: int,
        samples_per_packet: int,
        sample_shape: tuple[int, ...] = (),
        max_wait: int = 4,
        max_fill: int = 64,
    ) -> None:
        self.n_channels = n_channels
        self.spp = samples_per_packet
        self.sample_shape = sample_shape
        self.max_wait = max_wait
        self.max_fill = max_fill
        self.stats = AssemblerStats()
        self._ref: int | None = None          # most recent unwrapped index seen
        self._next: int | None = None         # next index to emit
        self._max_seen: int | None = None
        self._pending: dict[int, _Pending] = {}

    def _unwrap(self, idx16: int) -> int:
        if self._ref is None:
            return idx16
        delta = ((idx16 - (self._ref & 0xFFFF) + 32768) % 65536) - 32768
        return self._ref + delta

    def _blank(self) -> np.ndarray:
        shape = (self.spp, self.n_channels, *self.sample_shape)
        return np.full(shape, np.nan, dtype=np.float64)

    def feed(self, channel: int, idx16: int, samples: np.ndarray) -> list[tuple[int, np.ndarray]]:
        self.stats.packets += 1
        idx = self._unwrap(idx16)
        if self._ref is None or idx > self._ref:
            self._ref = idx
        if self._next is None:
            self._next = idx
        if idx < self._next:
            self.stats.late_discarded += 1
            return []
        slot = self._pending.get(idx)
        if slot is None:
            slot = self._pending[idx] = _Pending(self._blank())
        if channel in slot.have:
            self.stats.duplicates += 1
            return []
        slot.data[:, channel, ...] = samples
        slot.have.add(channel)
        self._max_seen = idx if self._max_seen is None else max(self._max_seen, idx)
        return self._drain()

    def _drain(self) -> list[tuple[int, np.ndarray]]:
        out: list[tuple[int, np.ndarray]] = []
        assert self._next is not None and self._max_seen is not None
        # A gap bigger than we are willing to fill: restart the timeline at the
        # oldest pending frame rather than invent seconds of NaN.
        if self._max_seen - self._next > self.max_fill + self.max_wait:
            oldest = min(self._pending)
            if oldest - self._next > self.max_fill:
                self.stats.resyncs += 1
                self._next = oldest
        while self._next in self._pending or (
            self._max_seen - self._next >= self.max_wait
        ):
            slot = self._pending.get(self._next)
            if slot is not None and len(slot.have) < self.n_channels:
                if self._max_seen - self._next < self.max_wait:
                    break  # still waiting for the rest of this frame
            out.append(self._emit(self._next))
            self._next += 1
        return out

    def _emit(self, idx: int) -> tuple[int, np.ndarray]:
        slot = self._pending.pop(idx, None)
        self.stats.frames_emitted += 1
        if slot is None:
            self.stats.frames_missing += 1
            return idx, self._blank()
        self.stats.channel_packets_missing += self.n_channels - len(slot.have)
        return idx, slot.data

    def flush(self) -> list[tuple[int, np.ndarray]]:
        """Emit everything pending (end of a link segment)."""
        out: list[tuple[int, np.ndarray]] = []
        if self._next is None or not self._pending:
            return out
        while self._pending:
            out.append(self._emit(self._next))
            self._next += 1
        return out

    @property
    def lost_fraction(self) -> float:
        """Share of channel-packets that never arrived (0..1)."""
        expected = self.stats.frames_emitted * self.n_channels
        if not expected:
            return 0.0
        lost = self.stats.channel_packets_missing + self.stats.frames_missing * self.n_channels
        return lost / expected


class _ClosedOutlet:
    def push_chunk(self, *_a: Any, **_k: Any) -> None:
        pass


# --- publisher ----------------------------------------------------------------------
@dataclass
class _Stream:
    key: str
    fs: float
    spp: int
    assembler: SequenceAssembler | None
    outlet: Any
    clock: WindowedRegressionClock = field(default_factory=WindowedRegressionClock)
    guard: MonotonicGuard = field(default_factory=MonotonicGuard)
    samples_pushed: int = 0
    nan_samples: int = 0
    # Offset added to assembler indices so device time keeps increasing across
    # link segments (a reconnect resets the assembler).
    segment_offset: int = 0
    last_abs: int = -1
    rebase: bool = False


class Muse2Publisher:
    """Decode → assemble → timestamp → LSL for one Muse 2.

    Not thread-safe: called from the bridge's single decode thread.
    """

    MODEL = "muse2"

    def __init__(
        self,
        device_id: str,
        *,
        include_aux: bool = False,
        include_imu: bool = True,
        device_desc: dict[str, Any] | None = None,
        outlet_factory=make_outlet,
    ) -> None:
        self.device_id = device_id
        self.eeg_labels = ["TP9", "AF7", "AF8", "TP10"] + (["AUX"] if include_aux else [])
        self.ppg_labels = list(PPG_UUIDS)
        self.include_imu = include_imu
        base_desc = {
            "manufacturer": "Interaxon",
            "model": "Muse 2",
            "device_id": device_id,
            "bridge": "muse_proxy.bridge",
            "bridge_version": BRIDGE_VERSION,
            "missing_samples": "NaN (dropped BLE packet; detected by sequence number)",
            "timestamps": "device sequence clock -> LSL via windowed regression",
        }
        base_desc.update(device_desc or {})
        self._desc = base_desc
        self._factory = outlet_factory

        self._uuid_map: dict[str, tuple[str, int]] = {}
        for i, lab in enumerate(self.eeg_labels):
            self._uuid_map[EEG_UUIDS[lab]] = ("EEG", i)
        for i, lab in enumerate(self.ppg_labels):
            self._uuid_map[PPG_UUIDS[lab]] = ("PPG", i)
        if include_imu:
            self._uuid_map[ACC_UUID] = ("ACC", 0)
            self._uuid_map[GYRO_UUID] = ("GYRO", 0)
        self._uuid_map[TELEMETRY_UUID] = ("TELEMETRY", 0)

        self.streams: dict[str, _Stream] = {}
        self.battery: float | None = None
        self.bad_length = 0
        self._build_streams()

    @property
    def data_uuids(self) -> list[str]:
        return list(self._uuid_map)

    def _build_streams(self) -> None:
        did = self.device_id
        mk = self._factory
        self.streams["EEG"] = _Stream(
            "EEG", EEG_FS, EEG_N,
            SequenceAssembler(len(self.eeg_labels), EEG_N),
            mk(name=f"Muse2-EEG ({did})", stype="EEG", labels=self.eeg_labels,
               units="microvolts", fs=EEG_FS, source_id=f"muse2_{did}_eeg", desc=self._desc),
        )
        self.streams["PPG"] = _Stream(
            "PPG", PPG_FS, PPG_N,
            SequenceAssembler(len(self.ppg_labels), PPG_N, max_wait=3),
            mk(name=f"Muse2-PPG ({did})", stype="PPG", labels=self.ppg_labels,
               units="counts", fs=PPG_FS, source_id=f"muse2_{did}_ppg", desc=self._desc),
        )
        if self.include_imu:
            self.streams["ACC"] = _Stream(
                "ACC", IMU_FS, IMU_N, SequenceAssembler(1, IMU_N, (3,), max_wait=2),
                mk(name=f"Muse2-ACC ({did})", stype="ACC", labels=["ACC_X", "ACC_Y", "ACC_Z"],
                   units="g", fs=IMU_FS, source_id=f"muse2_{did}_acc", desc=self._desc),
            )
            self.streams["GYRO"] = _Stream(
                "GYRO", IMU_FS, IMU_N, SequenceAssembler(1, IMU_N, (3,), max_wait=2),
                mk(name=f"Muse2-GYRO ({did})", stype="GYRO",
                   labels=["GYRO_X", "GYRO_Y", "GYRO_Z"], units="dps", fs=IMU_FS,
                   source_id=f"muse2_{did}_gyro", desc=self._desc),
            )
        self.streams["TELEMETRY"] = _Stream(
            "TELEMETRY", 0.0, 1, None,
            mk(name=f"Muse2-TELEMETRY ({did})", stype="Telemetry",
               labels=["BATTERY_PERCENT", "FUEL_GAUGE_MV", "ADC_MV", "TEMPERATURE_RAW"],
               units=["percent", "mV", "mV", "raw"], fs=0.0,
               source_id=f"muse2_{did}_telemetry", desc=self._desc),
        )

    # --- ingest ---------------------------------------------------------------
    def process(self, arrival_lsl: float, uuid: str, raw: bytes) -> None:
        route = self._uuid_map.get(uuid.lower())
        if route is None:
            return
        if len(raw) != PACKET_LEN:
            self.bad_length += 1
            return
        key, ch = route
        s = self.streams[key]
        idx16 = packet_index(raw)
        if key == "TELEMETRY":
            vals = decode_telemetry(raw)
            self.battery = float(vals[0])
            ts = s.guard.apply(np.array([arrival_lsl]))
            push_rows(s.outlet, vals.reshape(1, -1), ts)
            s.samples_pushed += 1
            return
        if key == "EEG":
            samples = decode_eeg(raw)
        elif key == "PPG":
            samples = decode_ppg(raw)
        elif key == "ACC":
            samples = decode_imu(raw, ACC_G_PER_LSB)
        else:
            samples = decode_imu(raw, GYRO_DPS_PER_LSB)
        assert s.assembler is not None
        if s.rebase:
            # First packet of a new link segment: continue device time from
            # where the previous segment stopped, whatever the counter says.
            s.segment_offset = (s.last_abs + 1) - idx16
            s.rebase = False
        frames = s.assembler.feed(ch, idx16, samples)
        # Clock point: this packet's last sample, in device time, vs. arrival.
        abs_idx = s.assembler._unwrap(idx16) + s.segment_offset
        s.clock.update((abs_idx + 1) * s.spp / s.fs, arrival_lsl)
        self._push_frames(s, frames)

    def _push_frames(self, s: _Stream, frames: list[tuple[int, np.ndarray]]) -> None:
        if not frames:
            return
        blocks, dev_times = [], []
        for idx, block in frames:
            abs_idx = idx + s.segment_offset
            s.last_abs = abs_idx
            rows = block.reshape(s.spp, -1)
            blocks.append(rows)
            dev_times.append((abs_idx * s.spp + np.arange(s.spp)) / s.fs)
        rows = np.concatenate(blocks)
        ts = s.guard.apply(s.clock.map(np.concatenate(dev_times)))
        push_rows(s.outlet, rows, ts)
        s.samples_pushed += rows.shape[0]
        s.nan_samples += int(np.isnan(rows).any(axis=1).sum())

    def new_segment(self) -> None:
        """Call on every (re)connect: flush pending frames, restart timelines.

        The Muse 2 may or may not restart its packet counter after a reconnect,
        so device time is continued from the last emitted index plus one
        rather than trusting the new counter's absolute value; the clock is
        reset so the regression never straddles the discontinuity.
        """
        for s in self.streams.values():
            if s.assembler is None:
                continue
            self._push_frames(s, s.assembler.flush())
            old = s.assembler
            s.assembler = SequenceAssembler(
                old.n_channels, old.spp, old.sample_shape, old.max_wait, old.max_fill
            )
            s.assembler.stats = old.stats  # keep cumulative counters
            s.clock = WindowedRegressionClock()
            s.rebase = s.last_abs >= 0

    def close(self) -> None:
        """Flush, then destroy the outlets so the streams vanish from LSL —
        a stopped bridge must never look like a live-but-silent device."""
        for s in self.streams.values():
            if s.assembler is not None:
                self._push_frames(s, s.assembler.flush())
            s.outlet = _ClosedOutlet()

    # --- health -----------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"battery": self.battery, "bad_length": self.bad_length}
        for k, s in self.streams.items():
            entry: dict[str, Any] = {"samples": s.samples_pushed, "nan_samples": s.nan_samples}
            if s.assembler is not None:
                st = s.assembler.stats
                entry.update(
                    packets=st.packets,
                    lost_pct=round(100.0 * s.assembler.lost_fraction, 2),
                    frames_missing=st.frames_missing,
                    channel_packets_missing=st.channel_packets_missing,
                    late=st.late_discarded,
                    resyncs=st.resyncs,
                )
            out[k] = entry
        return out


# --- synthetic device -----------------------------------------------------------------
class SyntheticMuse2:
    """Real-time-paced fake Muse 2 notifications (alpha EEG, pulsatile PPG).

    ``drop_prob`` drops individual channel notifications at random so the NaN
    path is exercised; ``due_packets(elapsed)`` returns ``(uuid, raw)`` due by
    ``elapsed`` seconds, in a plausibly interleaved order.
    """

    def __init__(self, drop_prob: float = 0.0, seed: int = 7, include_aux: bool = False,
                 start_index: int = 65530) -> None:
        self.drop_prob = drop_prob
        self.rng = np.random.default_rng(seed)
        self.eeg_labels = ["TP9", "AF7", "AF8", "TP10"] + (["AUX"] if include_aux else [])
        # start near the wrap point so 16-bit unwrapping is always exercised
        self._next = {"EEG": 0, "PPG": 0, "IMU": 0, "TEL": 0}
        self._start = start_index
        self.dropped = 0

    def _keep(self) -> bool:
        if self.drop_prob and self.rng.random() < self.drop_prob:
            self.dropped += 1
            return False
        return True

    def due_packets(self, elapsed_s: float) -> list[tuple[str, bytes]]:
        out: list[tuple[str, bytes]] = []
        while (self._next["EEG"] + 1) * EEG_N / EEG_FS <= elapsed_s:
            n = self._next["EEG"]
            idx = (self._start + n) & 0xFFFF
            t = (n * EEG_N + np.arange(EEG_N)) / EEG_FS
            for c, lab in enumerate(self.eeg_labels):
                uv = 20 * np.sin(2 * math.pi * 10 * t + c) + self.rng.standard_normal(EEG_N) * 3
                if self._keep():
                    out.append((EEG_UUIDS[lab], encode_eeg(idx, uv)))
            self._next["EEG"] += 1
        while (self._next["PPG"] + 1) * PPG_N / PPG_FS <= elapsed_s:
            n = self._next["PPG"]
            idx = (self._start + n) & 0xFFFF
            t = (n * PPG_N + np.arange(PPG_N)) / PPG_FS
            pulse = np.exp(-(((1.1 * t) % 1.0 - 0.15) ** 2) / 0.004)
            for c, lab in enumerate(PPG_UUIDS):
                counts = 30000 + c * 20000 + 4000 * pulse * (c > 0)
                if self._keep():
                    out.append((PPG_UUIDS[lab], encode_ppg(idx, counts)))
            self._next["PPG"] += 1
        while (self._next["IMU"] + 1) * IMU_N / IMU_FS <= elapsed_s:
            idx = (self._start + self._next["IMU"]) & 0xFFFF
            acc = np.tile([0.0, 0.0, 1.0], (3, 1)) + self.rng.standard_normal((3, 3)) * 0.01
            gyro = self.rng.standard_normal((3, 3)) * 0.5
            if self._keep():
                out.append((ACC_UUID, encode_imu(idx, acc, ACC_G_PER_LSB)))
            if self._keep():
                out.append((GYRO_UUID, encode_imu(idx, gyro, GYRO_DPS_PER_LSB)))
            self._next["IMU"] += 1
        while self._next["TEL"] * 10.0 <= elapsed_s:
            out.append((TELEMETRY_UUID, encode_telemetry(self._next["TEL"], 87.5)))
            self._next["TEL"] += 1
        return out
