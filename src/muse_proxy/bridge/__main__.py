"""MuseProxy direct-BLE bridge — command line.

Examples (from the repo root)::

    uv run python -m muse_proxy.bridge scan
    uv run python -m muse_proxy.bridge stream --model muse-2  --address 00:55:DA:B7:E0:14
    uv run python -m muse_proxy.bridge stream --model muse-athena --name MuseS-0DBB
    uv run python -m muse_proxy.bridge stream --model muse-2 --fake --duration 20

``stream`` refuses to guess: if more than one matching headset is in range and
neither ``--address`` nor ``--name`` was given, it lists them and exits. In a
room with several headsets, connecting to the wrong one silently is the worst
possible failure.

Leave it running for the whole pass; Ctrl+C stops it cleanly (sends ``h``,
flushes and closes the raw log). Start it BEFORE the experiment app's
pre-launch checklist so the streams are visible there and in Lab Recorder.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path

from .device import BridgeConfig, DeviceBridge, Target, new_ble_loop


def _scan(timeout: float) -> list:
    from .transport import BleakTransport

    loop = new_ble_loop()
    try:
        return loop.run_until_complete(BleakTransport().scan(timeout))
    finally:
        loop.close()


_MODEL_ALIASES = {"muse-2": "muse-2", "muse2": "muse-2", "muse-athena": "muse-athena",
                  "museathena": "muse-athena", "athena": "muse-athena"}
LABELS = {"muse-2": "Muse-2", "muse-athena": "Muse-Athena", "unknown": "unknown"}


def _model_arg(value: str) -> str:
    try:
        return _MODEL_ALIASES[value.strip().lower()]
    except KeyError:
        raise argparse.ArgumentTypeError("must be muse-2 or muse-athena") from None


def cmd_scan(args: argparse.Namespace) -> int:
    print(f"Scanning {args.timeout:.0f} s for Muse headsets...")
    found = _scan(args.timeout)
    if not found:
        print("None found. Is the headset on (LED pulsing) and NOT connected to a phone?")
        return 1
    print(f"{'device':<12} {'name':<14} {'address':<40} {'id':<6} rssi")
    for d in found:
        print(f"{LABELS.get(d.model, d.model):<12} {d.name:<14} {d.address:<40} "
              f"{d.device_id:<6} {d.rssi}")
    return 0


def _pick_target(args: argparse.Namespace) -> Target | None:
    if args.fake:
        from .transport import FakeTransport

        ft = FakeTransport(args.model, drop_prob=args.fake_drop)
        args._transport = ft
        d = ft.device
        return Target(args.model, d.name, d.address, d.device_id, d.rssi)
    found = [d for d in _scan(args.scan_timeout) if d.model == args.model]
    if args.address:
        found = [d for d in found if d.address.lower() == args.address.lower()]
    if args.name:
        found = [d for d in found if d.name.lower() == args.name.lower()]
    if not found:
        print(f"No {LABELS[args.model]} headset matching the filters is advertising.")
        return None
    if len(found) > 1:
        print("More than one headset matches — pass --address or --name to choose:")
        for d in found:
            print(f"  {d.name:<14} {d.address}  rssi={d.rssi}")
        return None
    d = found[0]
    return Target(d.model, d.name, d.address, d.device_id, d.rssi, d.ble_device)


def _status_line(s: dict, label: str = "") -> str:
    head = f"[{time.strftime('%H:%M:%S')}] {label}".rstrip()
    parts = [f"{head} {s['state']:<12}"]
    if s["battery"] is not None:
        parts.append(f"batt {s['battery']:.0f}%")
    parts.append(f"pkts {s['packets']}")
    for k, v in s["streams"].items():
        if isinstance(v, dict) and "samples" in v and k in ("EEG", "PPG", "OPTICS"):
            lost = v.get("lost_pct", 0.0)
            parts.append(f"{k} {v['samples']} ({lost:.1f}% lost)")
    if s["reconnects"]:
        parts.append(f"reconnects {s['reconnects']}")
    if s["queue_overflow"]:
        parts.append(f"OVERFLOW {s['queue_overflow']}")
    if s["seconds_since_data"] is not None and s["seconds_since_data"] > 2:
        parts.append(f"quiet {s['seconds_since_data']:.1f}s")
    return "  ".join(parts)


def cmd_stream(args: argparse.Namespace) -> int:
    target = _pick_target(args)
    if target is None:
        return 2
    cfg = BridgeConfig(preset=args.preset, include_aux=args.aux,
                       include_imu=not args.no_imu,
                       athena_gap_correction=args.athena_gap_correction)
    raw_dir = None if args.no_raw_log else Path(args.raw_dir)

    def on_event(level: str, msg: str) -> None:
        print(f"  {level.upper()}: {msg}", flush=True)

    bridge = DeviceBridge(target, cfg, raw_dir=raw_dir,
                          transport=getattr(args, "_transport", None), on_event=on_event)
    stop = {"flag": False}

    def _sig(*_: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _sig)
    print(f"Connecting to {LABELS[target.model]} {target.name} ({target.address})...")
    if bridge.raw is not None:
        print(f"Raw packet log: {bridge.raw.path}")
    bridge.start()
    t0 = time.monotonic()
    try:
        while not stop["flag"]:
            time.sleep(args.status_interval)
            print(_status_line(bridge.snapshot(), LABELS[target.model]), flush=True)
            if args.duration and time.monotonic() - t0 >= args.duration:
                break
    finally:
        print("Stopping...")
        bridge.stop()
        snap = bridge.snapshot()
        print(_status_line(snap, LABELS[target.model]))
        for k, v in snap["streams"].items():
            if isinstance(v, dict):
                print(f"  {k}: {v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        # Proactor is already the Windows default (and new_ble_loop uses it
        # explicitly); this only matters on old Pythons. Silence the 3.14+
        # deprecation notice so it doesn't clutter the RA's console.
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    p = argparse.ArgumentParser(prog="muse_proxy.bridge", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="list Muse headsets in range")
    s.add_argument("--timeout", type=float, default=8.0)
    s.set_defaults(func=cmd_scan)

    st = sub.add_parser("stream", help="connect to one headset and publish LSL streams")
    st.add_argument("--model", type=_model_arg, required=True,
                    help="muse-2 or muse-athena")
    st.add_argument("--address", help="BLE address from `scan` (recommended)")
    st.add_argument("--name", help="advertised name, e.g. Muse-1A2B")
    st.add_argument("--preset", help="override preset (Muse-2: p50; Muse-Athena: p1035)")
    st.add_argument("--aux", action="store_true", help="Muse-2: also stream the AUX channel")
    st.add_argument("--no-imu", action="store_true", help="Muse-2: skip ACC/GYRO")
    st.add_argument("--athena-gap-correction", action="store_true",
                    help="Muse-Athena: shift samples after a detected packet loss (experimental)")
    st.add_argument("--raw-dir", default="Data/raw_ble", help="raw packet log directory")
    st.add_argument("--no-raw-log", action="store_true")
    st.add_argument("--scan-timeout", type=float, default=8.0)
    st.add_argument("--status-interval", type=float, default=2.0)
    st.add_argument("--duration", type=float, default=0.0, help="stop after N s (0 = run)")
    st.add_argument("--fake", action="store_true", help="synthetic headset, no Bluetooth")
    st.add_argument("--fake-drop", type=float, default=0.0,
                    help="with --fake: per-notification drop probability")
    st.set_defaults(func=cmd_stream)
    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
