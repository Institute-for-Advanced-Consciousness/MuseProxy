"""Direct-BLE bridge: decode, gap handling, timestamps, supervisor, LSL output."""

from __future__ import annotations

import time

import numpy as np
import pylsl
import pytest

from muse_proxy.bridge import athena, muse2
from muse_proxy.bridge.athena_decode import parse_message
from muse_proxy.bridge.athena_synth import SyntheticAthena
from muse_proxy.bridge.common import MonotonicGuard, WindowedRegressionClock
from muse_proxy.bridge.device import BridgeConfig, DeviceBridge, State, Target, encode_command
from muse_proxy.bridge.muse2 import SequenceAssembler
from muse_proxy.bridge.transport import FakeTransport, classify, device_id_from


# --------------------------------------------------------------------------- fakes
class RecordingOutlet:
    def __init__(self, **kw):
        self.kw = kw
        self.rows: list[np.ndarray] = []
        self.ts: list[float] = []

    def push_chunk(self, x, timestamp):
        self.rows.append(np.asarray(x))
        self.ts.extend(timestamp)

    @property
    def data(self) -> np.ndarray:
        return np.concatenate(self.rows) if self.rows else np.empty((0, 0))


def recording_factory(store: dict):
    def make(**kw):
        o = RecordingOutlet(**kw)
        store[kw["name"]] = o
        return o

    return make


# --------------------------------------------------------------------------- muse 2 codec
def test_muse2_eeg_roundtrip():
    uv = np.linspace(-900, 900, 12)
    raw = muse2.encode_eeg(0xABCD, uv)
    assert len(raw) == 20
    assert muse2.packet_index(raw) == 0xABCD
    np.testing.assert_allclose(muse2.decode_eeg(raw), uv, atol=muse2.EEG_UV_PER_LSB / 2 + 1e-9)


def test_muse2_eeg_known_bytes():
    # 0x800 is mid-scale → 0 µV; 0xFFF → +999.51 µV; 0x000 → -1000 µV
    raw = bytes([0, 1]) + bytes([0x80, 0x0F, 0xFF, 0x00, 0x08, 0x00] * 3)
    out = muse2.decode_eeg(raw)
    np.testing.assert_allclose(out[:4], [0.0, 999.51171875, -1000.0, 0.0])


def test_muse2_ppg_imu_telemetry_roundtrip():
    counts = np.array([0, 1, 255, 65536, 1_000_000, 0xFFFFFF], dtype=float)
    np.testing.assert_array_equal(muse2.decode_ppg(muse2.encode_ppg(5, counts)), counts)
    acc = np.array([[0.0, 0.0, 1.0], [0.1, -0.1, 0.9], [0.5, 0.5, 0.5]])
    np.testing.assert_allclose(
        muse2.decode_imu(muse2.encode_imu(7, acc, muse2.ACC_G_PER_LSB), muse2.ACC_G_PER_LSB),
        acc, atol=muse2.ACC_G_PER_LSB)
    tel = muse2.decode_telemetry(muse2.encode_telemetry(1, 87.5))
    assert tel[0] == pytest.approx(87.5)


# --------------------------------------------------------------------------- assembler
def _ch(v: float) -> np.ndarray:
    return np.full(12, v)


def test_assembler_complete_frames_in_order():
    a = SequenceAssembler(2, 12)
    out = []
    for i in range(3):
        out += a.feed(0, i, _ch(i))
        out += a.feed(1, i, _ch(i + 0.5))
    assert [idx for idx, _ in out] == [0, 1, 2]
    assert not np.isnan(np.concatenate([b for _, b in out])).any()
    assert a.lost_fraction == 0.0


def test_assembler_missing_channel_is_nan_not_zero():
    a = SequenceAssembler(2, 12, max_wait=2)
    out = a.feed(0, 0, _ch(1.0))  # channel 1 of packet 0 never arrives
    for i in range(1, 4):
        out += a.feed(0, i, _ch(1.0))
        out += a.feed(1, i, _ch(2.0))
    first = dict(out)[0]
    assert np.isnan(first[:, 1]).all()
    assert (first[:, 0] == 1.0).all()
    assert a.stats.channel_packets_missing == 1


def test_assembler_whole_packet_missing_emits_nan_frame():
    a = SequenceAssembler(1, 3, (3,), max_wait=2)
    out = []
    for i in (0, 1, 3, 4, 5, 6):  # index 2 lost entirely
        out += a.feed(0, i, np.ones((3, 3)))
    idxs = [i for i, _ in out]
    assert idxs == sorted(idxs) and 2 in idxs
    assert np.isnan(dict(out)[2]).all()
    assert a.stats.frames_missing == 1


def test_assembler_reorders_within_window_and_drops_late():
    a = SequenceAssembler(1, 12, max_wait=3)
    out = []
    for i in (0, 2, 1, 3, 4, 5, 6, 7):
        out += a.feed(0, i, _ch(i))
    assert [i for i, _ in out] == list(range(len(out)))
    assert all(blk[0, 0] == i for i, blk in out)
    out += a.feed(0, 0, _ch(0))  # long-emitted
    assert a.stats.late_discarded == 1


def test_assembler_16bit_wraparound():
    a = SequenceAssembler(1, 12)
    out = []
    for i in range(65533, 65533 + 6):
        out += a.feed(0, i & 0xFFFF, _ch(1))
    idxs = [i for i, _ in out]
    assert idxs == list(range(65533, 65533 + len(idxs)))
    assert a.stats.frames_missing == 0


def test_assembler_large_gap_resyncs_without_inventing_data():
    a = SequenceAssembler(1, 12, max_wait=2, max_fill=10)
    out = []
    for i in range(5):
        out += a.feed(0, i, _ch(1))
    for i in range(1000, 1010):
        out += a.feed(0, i, _ch(1))
    assert a.stats.resyncs == 1
    assert len(out) < 30  # did not fill ~1000 NaN frames


# --------------------------------------------------------------------------- publishers
def test_muse2_publisher_nan_timeline_and_monotonic():
    store: dict = {}
    pub = muse2.Muse2Publisher("T1", outlet_factory=recording_factory(store))
    synth = muse2.SyntheticMuse2(drop_prob=0.05, seed=3)
    t_arrival = 1000.0
    for step in range(1, 201):  # 10 s in 50 ms steps
        for uuid, raw in synth.due_packets(step * 0.05):
            pub.process(t_arrival + step * 0.05 + 0.02, uuid, raw)
    pub.close()
    eeg = store["Muse-2-EEG (T1)"]
    data, ts = eeg.data, np.asarray(eeg.ts)
    assert data.shape[1] == 4
    assert abs(data.shape[0] - 10 * 256) <= 48           # full timeline kept
    assert np.isnan(data).any()                          # drops visible…
    assert not (data == 0).all(axis=1).any()             # …and never as zeros
    assert (np.diff(ts) > 0).all()
    # Regression clock: sample spacing ~1/256 s
    assert np.median(np.diff(ts)) == pytest.approx(1 / 256, rel=0.02)
    st = pub.stats()
    assert 2.0 < st["EEG"]["lost_pct"] < 9.0
    ppg = store["Muse-2-PPG (T1)"]
    assert ppg.data.shape[1] == 3 and ppg.kw["labels"] == ["AMBIENT", "IR", "RED"]


def test_muse2_publisher_reconnect_segment_keeps_time_moving():
    store: dict = {}
    pub = muse2.Muse2Publisher("T2", include_imu=False, outlet_factory=recording_factory(store))
    s1 = muse2.SyntheticMuse2(seed=1, start_index=100)
    for step in range(1, 41):
        for uuid, raw in s1.due_packets(step * 0.05):
            pub.process(500 + step * 0.05, uuid, raw)
    pub.new_segment()
    s2 = muse2.SyntheticMuse2(seed=2, start_index=0)  # counter restarted after reconnect
    for step in range(1, 41):
        for uuid, raw in s2.due_packets(step * 0.05):
            pub.process(505 + step * 0.05, uuid, raw)
    pub.close()
    ts = np.asarray(store["Muse-2-EEG (T2)"].ts)
    assert (np.diff(ts) > 0).all()
    assert ts[-1] > 505  # second segment mapped onto its own arrival times


def test_athena_publisher_decodes_and_counts_gaps():
    store: dict = {}
    pub = athena.AthenaPublisher("A1", outlet_factory=recording_factory(store))
    synth = SyntheticAthena("A1", preset="p1035")
    rng = np.random.default_rng(0)
    dropped = 0
    for step in range(1, 201):
        for uuid, raw in synth.due_packets(step * 0.05):
            if step > 40 and rng.random() < 0.05:
                dropped += 1
                continue
            pub.process(2000 + step * 0.05, "2026-11-06T00:00:00+00:00", uuid, raw)
    eeg = store["Muse-Athena-EEG (A1)"]
    assert eeg.kw["stype"] == "EEG" and eeg.data.shape[1] == 4
    assert store["Muse-Athena-OPTICS (A1)"].kw["stype"] == "PPG"
    assert (np.diff(np.asarray(eeg.ts)) > 0).all()
    st = pub.stats()
    assert dropped > 0 and st["EEG"]["gap_samples"] > 0


def test_athena_gap_correction_restores_true_time():
    def run(correct: bool):
        store: dict = {}
        pub = athena.AthenaPublisher("A2", sensors=("EEG",), gap_correction=correct,
                                     outlet_factory=recording_factory(store))
        synth = SyntheticAthena("A2", preset="p1035")
        for step in range(1, 121):
            for uuid, raw in synth.due_packets(step * 0.05):
                if 60 <= step < 62:  # lose ~100 ms of packets
                    continue
                pub.process(3000 + step * 0.05, "2026-11-06T00:00:00+00:00", uuid, raw)
        return np.asarray(store["Muse-Athena-EEG (A2)"].ts), pub.stats()["EEG"]

    ts_raw, st = run(False)
    ts_fix, _ = run(True)
    assert st["gap_events"] >= 1 and 15 <= st["gap_samples"] <= 40
    # Uncorrected (OpenMuse/relay behaviour): the ~100 ms loss never appears
    # where it happened — it leaks in later as a smaller step when the
    # regression refits. Corrected: one ~100 ms gap at the loss.
    assert np.diff(ts_raw).max() < 0.08
    assert np.diff(ts_fix).max() > 0.09
    i_fix = int(np.argmax(np.diff(ts_fix)))
    assert 0.40 < i_fix / ts_fix.size < 0.60  # at the loss (half-way through)


def test_clock_and_guard():
    c = WindowedRegressionClock()
    for i in range(50):
        c.update(i * 0.1, 100 + i * 0.1 * 1.0001 + 0.03)
    assert c.initialized and c.slope == pytest.approx(1.0001, rel=1e-6)
    g = MonotonicGuard()
    a = g.apply(np.array([1.0, 2.0]))
    b = g.apply(np.array([1.5, 2.5]))
    assert b[0] > a[-1]


# --------------------------------------------------------------------------- protocol bits
def test_encode_command_and_rejects_injection():
    assert encode_command("p50") == bytes([4]) + b"p50\n"
    with pytest.raises(ValueError):
        encode_command("h\nd")
    with pytest.raises(ValueError):
        encode_command("")


def test_classify_and_device_id():
    assert classify("Muse-1A2B") == "muse-2"
    assert classify("MuseS-0DBB") == "muse-athena"
    assert classify("Fitbit") == "unknown"
    assert device_id_from("Muse-1a2b", "x") == "1A2B"
    assert device_id_from(None, "00:55:DA:B7:E0:14") == "E014"


def test_control_info_parses_both_formats():
    ci = athena.ControlInfo()
    ci.feed(bytes([20]) + b'{"fw":"1.2.13","bp":')
    ci.feed(b'64.5,"hw":"03.1"}')
    assert ci.firmware == "1.2.13" and ci.battery == 64.5 and ci.hardware == "03.1"


# --------------------------------------------------------------------------- supervisor (fake BLE)
FAST = dict(handshake_delay_scale=0.05, initial_backoff=0.05, scan_timeout=0.1,
            reconnect_scan_timeout=0.1, first_data_timeout=2.0, stats_event_interval=0.5)


def _target(ft: FakeTransport) -> Target:
    d = ft.device
    return Target(d.model, d.name, d.address, d.device_id, d.rssi)


def _wait(pred, timeout=8.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("model", ["muse-2", "muse-athena"])
def test_bridge_streams_and_logs_raw(model, tmp_path):
    ft = FakeTransport(model, drop_prob=0.02)
    b = DeviceBridge(_target(ft), BridgeConfig(**FAST), raw_dir=tmp_path, transport=ft)
    b.start()
    try:
        assert _wait(lambda: b.state == State.STREAMING)
        key = "EEG"
        assert _wait(lambda: b.snapshot()["streams"].get(key, {}).get("samples", 0) > 256)
    finally:
        b.stop()
    lines = b.raw.path.read_text().splitlines()
    assert len(lines) > 10
    utc, uuid, hexs = lines[-1].split("\t")
    assert len(bytes.fromhex(hexs)) > 0 and uuid.startswith("273e")
    if model == "muse-athena":
        parsed = [parse_message(ln) for ln in lines]
        assert sum(len(p["EEG"]) for p in parsed) > 0  # OpenMuse-compatible
    assert b.raw.clock_path.read_text().count("\n") >= 2
    assert "h" in ft.clients[-1].commands  # clean halt on stop


def test_bridge_reconnects_after_drop():
    ft = FakeTransport("muse-2", scenarios={0: {"drop_after_s": 1.0}})
    b = DeviceBridge(_target(ft), BridgeConfig(**FAST), transport=ft)
    b.start()
    try:
        assert _wait(lambda: b.snapshot()["reconnects"] >= 1 and b.state == State.STREAMING)
        n = b.snapshot()["streams"]["EEG"]["samples"]
        assert _wait(lambda: b.snapshot()["streams"]["EEG"]["samples"] > n + 128)
    finally:
        b.stop()
    assert ft.attempts >= 2


def test_bridge_lsl_end_to_end_nan_and_monotonic():
    ft = FakeTransport("muse-2", drop_prob=0.05, device_id="E2E1")
    b = DeviceBridge(_target(ft), BridgeConfig(**FAST), transport=ft)
    b.start()
    try:
        streams = pylsl.resolve_byprop("name", "Muse-2-EEG (E2E1)", minimum=1, timeout=5)
        assert streams, "bridge EEG outlet not visible on LSL"
        inlet = pylsl.StreamInlet(streams[0])
        inlet.open_stream(timeout=3)
        rows, stamps = [], []
        t0 = time.monotonic()
        while time.monotonic() - t0 < 4:
            chunk, ts = inlet.pull_chunk(timeout=0.2)
            rows += chunk
            stamps += ts
        inlet.close_stream()
    finally:
        b.stop()
    data = np.asarray(rows)
    assert data.shape[0] > 256 and data.shape[1] == 4
    assert np.isnan(data).any()
    assert (np.diff(stamps) > 0).all()
