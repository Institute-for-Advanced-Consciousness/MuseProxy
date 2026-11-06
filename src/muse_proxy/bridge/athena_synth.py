"""Synthetic Muse S Athena packet codec + signal source.

Encoders are the exact inverse of ``core.decode`` (14-bit LSB-first EEG,
20-bit LSB-first optics, int16-LE IMU, uint16-LE battery, 14-byte packet
header), verified by a round-trip test. ``SyntheticAthena`` produces realistic,
real-time-paced messages (visible heartbeat on optics, alpha on EEG) so the
full BLE→decode→LSL→viz pipeline can be exercised with no headset.

Wire format recap (see core/decode.py):
  message = "<iso8601>\\t<char_uuid>\\t<hex>"
  packet  = [len][pkt_index][tick u32 LE][3 unk][pkt_id][3 unk][b13] + data
  the FIRST subpacket is raw data of type pkt_id (no TAG, no subheader).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from .athena_decode import (
    ACC_SCALE,
    DEVICE_CLOCK_HZ,
    EEG_SCALE,
    GYRO_SCALE,
    OPTICS_SCALE,
    SENSORS,
)

# Athena GATT data characteristics (packets arrive on these).
DATA_UUID = "273e0013-4c4d-454d-96be-f03bac821358"

PACKET_HEADER_SIZE = 14


# --- low-level bit packing ----------------------------------------------------
def _pack_lsb(values: List[int], width: int, n_bytes: int) -> bytes:
    """Pack ints as a continuous LSB-first bit stream (inverse of _bytes_to_bits)."""
    bits = bytearray(n_bytes * 8)
    for k, v in enumerate(values):
        base = k * width
        for i in range(width):
            if (v >> i) & 1:
                bits[base + i] = 1
    out = bytearray(n_bytes)
    for byte_idx in range(n_bytes):
        b = 0
        for bit_pos in range(8):
            if bits[byte_idx * 8 + bit_pos]:
                b |= 1 << bit_pos
        out[byte_idx] = b
    return bytes(out)


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


# --- per-sensor encoders (inverse of core.decode) ----------------------------
def encode_eeg(samples_uv: np.ndarray) -> bytes:
    """``samples_uv`` shape (n_samples, n_channels) of microvolts (0..1450)."""
    n_samples, n_ch = samples_uv.shape
    vals: List[int] = []
    for s in range(n_samples):
        for c in range(n_ch):
            iv = int(round(float(samples_uv[s, c]) / EEG_SCALE))
            vals.append(_clamp(iv, 0, (1 << 14) - 1))
    return _pack_lsb(vals, 14, 28)


def encode_optics(samples: np.ndarray) -> bytes:
    """``samples`` shape (n_samples, n_channels) in decoded optics units."""
    n_samples, n_ch = samples.shape
    n_bytes = 30 if n_ch == 4 else 40
    vals: List[int] = []
    for s in range(n_samples):
        for c in range(n_ch):
            iv = int(round(float(samples[s, c]) / OPTICS_SCALE))
            vals.append(_clamp(iv, 0, (1 << 20) - 1))
    return _pack_lsb(vals, 20, n_bytes)


def encode_accgyro(acc_g: np.ndarray, gyro_dps: np.ndarray) -> bytes:
    """``acc_g``/``gyro_dps`` shape (3, 3): 3 samples of XYZ."""
    out: List[int] = []
    for s in range(3):
        row = [
            _clamp(int(round(float(acc_g[s, j]) / ACC_SCALE)), -32768, 32767) for j in range(3)
        ] + [
            _clamp(int(round(float(gyro_dps[s, j]) / GYRO_SCALE)), -32768, 32767) for j in range(3)
        ]
        out.extend(row)
    return struct.pack("<18h", *out)


def encode_battery(percent: float, tag: int = 0x88) -> bytes:
    raw = _clamp(int(round(percent * 256.0)), 0, 65535)
    data_len = SENSORS[tag]["data_len"]
    body = bytearray(data_len)
    struct.pack_into("<H", body, 0, raw)
    return bytes(body)


def build_packet(pkt_id: int, pkt_index: int, pkt_time_raw: int, data: bytes) -> bytes:
    """One packet: 14-byte header + a single first-subpacket data section."""
    pkt_len = PACKET_HEADER_SIZE + len(data)
    header = struct.pack(
        "<BBI", pkt_len & 0xFF, pkt_index & 0xFF, pkt_time_raw & 0xFFFFFFFF
    ) + bytes([0, 0, 0, pkt_id, 0, 0, 0, 0])
    return header + data


def build_message(packets: List[bytes], uuid: str = DATA_UUID,
                  utc_iso: Optional[str] = None) -> str:
    if utc_iso is None:
        utc_iso = datetime.now(timezone.utc).isoformat()
    payload = b"".join(packets)
    return f"{utc_iso}\t{uuid}\t{payload.hex()}"


# --- signal source ------------------------------------------------------------
# Per-sensor wire config: TAG, channels, samples-per-packet, rate.
_TAG_EEG4, _TAG_OPT4, _TAG_OPT8, _TAG_OPT16 = 0x11, 0x34, 0x35, 0x36
_TAG_ACCGYRO, _TAG_BATTERY = 0x47, 0x88

# preset -> (eeg_tag, eeg_ch, optics_tag, optics_ch)
PRESET_PROFILES: Dict[str, Tuple[int, int, int, int]] = {
    "p1035": (0x11, 4, _TAG_OPT4, 4),    # EEG4 + Optics4 (lightest full-sensor)
    "p1034": (0x12, 8, _TAG_OPT8, 8),    # EEG8 + Optics8
    "p1041": (0x12, 8, _TAG_OPT16, 16),  # EEG8 + Optics16 (richest)
}


def _optics_ir_indices(n_ch: int) -> List[int]:
    # Mirror ingester.workers.ir_indices_for so the synthetic pulse lands on the
    # channels the PPG detector actually reads.
    if n_ch == 4:
        return [2, 3]
    if n_ch >= 8:
        return [2, 3, 6, 7]
    return [0]


@dataclass
class _SensorCursor:
    tag: int
    n_channels: int
    n_samples: int
    rate: float
    emitted: int = 0
    pkt_index: int = 0


class SyntheticAthena:
    """Generates realistic, real-time-paced Athena packets for one fake device.

    Call :meth:`due_packets` with a monotonically increasing elapsed-seconds
    value; it returns ``(uuid, raw_bytes)`` packets that should have been
    delivered by now, advancing internal device clocks so decoded timestamps
    come out uniformly spaced at the nominal rates.
    """

    def __init__(self, device_id: str, preset: str = "p1035", hr_bpm: float = 64.0,
                 alpha_hz: float = 10.0) -> None:
        self.device_id = device_id
        eeg_tag, eeg_ch, opt_tag, opt_ch = PRESET_PROFILES.get(preset, PRESET_PROFILES["p1035"])
        self._hr_hz = hr_bpm / 60.0
        self._alpha_hz = alpha_hz
        self._opt_ir = _optics_ir_indices(opt_ch)
        # samples-per-packet from the decoder's SENSORS table.
        self._cursors: Dict[str, _SensorCursor] = {
            "EEG": _SensorCursor(eeg_tag, eeg_ch, SENSORS[eeg_tag]["n_samples"], 256.0),
            "OPTICS": _SensorCursor(opt_tag, opt_ch, SENSORS[opt_tag]["n_samples"], 64.0),
            "ACCGYRO": _SensorCursor(_TAG_ACCGYRO, 6, 3, 52.0),
            "BATTERY": _SensorCursor(_TAG_BATTERY, 1, 1, 0.2),
        }
        self._battery = 92.0
        # Deterministic-ish phase variety per device.
        self._seed = abs(hash(device_id)) & 0xFFFF
        self._rng = np.random.default_rng(self._seed)

    # --- signal models ----------------------------------------------------
    def _eeg_block(self, start_idx: int, n: int, n_ch: int) -> np.ndarray:
        t = (start_idx + np.arange(n)) / 256.0
        out = np.empty((n, n_ch), dtype=np.float64)
        for c in range(n_ch):
            phase = (self._seed + c * 37) % 628 / 100.0
            sig = (
                40.0 * np.sin(2 * math.pi * self._alpha_hz * t + phase)
                + 12.0 * np.sin(2 * math.pi * 6.0 * t + phase * 1.7)
                + 8.0 * np.sin(2 * math.pi * 20.0 * t + phase * 0.5)
            )
            noise = self._rng.standard_normal(n) * 6.0
            out[:, c] = 725.0 + sig + noise  # centred mid-scale (0..1450 uV)
        np.clip(out, 0.0, 1450.0, out=out)
        return out

    def _optics_block(self, start_idx: int, n: int, n_ch: int) -> np.ndarray:
        t = (start_idx + np.arange(n)) / 64.0
        out = np.full((n, n_ch), 5.0, dtype=np.float64)  # quiet baseline
        # PPG pulse on the IR channels the detector reads.
        phase = (self._hr_hz * t) % 1.0
        systolic = np.exp(-((phase - 0.12) ** 2) / (2 * 0.045 ** 2))
        dicrotic = 0.35 * np.exp(-((phase - 0.38) ** 2) / (2 * 0.08 ** 2))
        pulse = systolic + dicrotic
        for c in self._opt_ir:
            if c < n_ch:
                out[:, c] = 6.0 + 0.8 * pulse + self._rng.standard_normal(n) * 0.02
        return out

    def _accgyro_block(self, n: int) -> Tuple[np.ndarray, np.ndarray]:
        acc = np.tile(np.array([0.0, 0.0, 1.0]), (n, 1)) + self._rng.standard_normal((n, 3)) * 0.01
        gyro = self._rng.standard_normal((n, 3)) * 0.5
        return acc, gyro

    # --- packet generation ------------------------------------------------
    def _encode(self, sensor: str, cur: _SensorCursor) -> bytes:
        if sensor == "EEG":
            return encode_eeg(self._eeg_block(cur.emitted, cur.n_samples, cur.n_channels))
        if sensor == "OPTICS":
            return encode_optics(self._optics_block(cur.emitted, cur.n_samples, cur.n_channels))
        if sensor == "ACCGYRO":
            acc, gyro = self._accgyro_block(cur.n_samples)
            return encode_accgyro(acc, gyro)
        if sensor == "BATTERY":
            self._battery = max(0.0, self._battery - 0.001)
            return encode_battery(self._battery, cur.tag)
        raise ValueError(sensor)

    def due_packets(self, elapsed_s: float) -> List[Tuple[str, bytes]]:
        """Return ``(uuid, raw_bytes)`` packets due by ``elapsed_s``."""
        packets: List[Tuple[str, bytes]] = []
        for sensor, cur in self._cursors.items():
            target = int(elapsed_s * cur.rate)
            while cur.emitted + cur.n_samples <= target:
                tick = int(round(cur.emitted / cur.rate * DEVICE_CLOCK_HZ)) & 0xFFFFFFFF
                data = self._encode(sensor, cur)
                packets.append((DATA_UUID, build_packet(cur.tag, cur.pkt_index, tick, data)))
                cur.emitted += cur.n_samples
                cur.pkt_index = (cur.pkt_index + 1) & 0xFF
        return packets
