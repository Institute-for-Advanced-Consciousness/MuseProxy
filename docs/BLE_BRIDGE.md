# Direct BLE bridge — Muse-2 and Muse-Athena

`python -m muse_proxy.bridge` connects the laptop straight to one headset over
Bluetooth LE and publishes LSL streams for Lab Recorder. It replaces **muselsl**
(Muse-2) and the **OpenMuse `.txt` → relay** path (Muse-Athena) used in the 2026 pilot.
The Muse-Athena side is ported from the IACS relay
([Muse-S-Athena-RealTime-Visualization](https://github.com/Institute-for-Advanced-Consciousness/Muse-S-Athena-RealTime-Visualization),
`muse_relay/bridge`, commit `c9a7795`).

## What it fixes from the 2026 data

| 2026 problem | Cause | Bridge behaviour |
|---|---|---|
| Muse-2 EEG/PPG runs of exact zeros (12 / 6 samples) | muselsl zero-fills any channel whose packet didn't arrive before AF7 (EEG) / RED (PPG) triggered a push | Every Muse-2 packet carries a 16-bit sequence number; a missing packet is detected exactly and written as **NaN**, never 0. The sample timeline stays intact. |
| ACC/GYRO timestamps go backwards | muselsl stamps IMU packets by arrival and back-fills | All streams use device time (sequence number or Muse-Athena 256 kHz tick) mapped to LSL with a windowed regression; output is forced monotonic. |
| Muse-Athena streams absent from the XDFs | The relay hop between OpenMuse's `.txt` and LSL failed silently | One process: BLE → LSL directly. The pre-launch monitor and `verify_xdf.py` now fail loudly when the expected Muse-Athena streams are missing or empty. |
| Muse-Athena logs could not be aligned to the XDF | Raw `.txt` had wall-clock times only | Raw log plus a `*_clock.csv` sidecar recording `utc_iso, lsl_local_clock` once per second. |
| AUX exported as "PPG" | Mislabelled export | AUX is off by default (`--aux` to enable). PPG channels are labelled `AMBIENT, IR, RED`. |

## Running it

```bat
launch_bridge.bat                                   :: scan
launch_bridge.bat stream --model muse-2  --address 00:55:DA:B7:E0:14
launch_bridge.bat stream --model muse-athena --address 00:55:DA:BB:DA:B3
launch_bridge.bat stream --model muse-2 --fake --fake-drop 0.05     :: no hardware
```

* **Pinned to one headset.** `stream` refuses to pick when more than one
  matching headset is advertising. Pass `--address` (from `scan`) or `--name`.
  It never silently re-binds to a different device. If the address rotates
  (which can happen on macOS), it re-binds only to the identical advertised name.
* Start the bridge **before** the experiment app's pre-launch checklist, and
  leave the window open for the whole pass. Ctrl+C sends `h` (halt), flushes and
  closes the raw log, and removes the streams from LSL.
* An older gen-1/2 Muse S (not a Muse-Athena) also advertises `MuseS-…` but
  speaks the Muse-2 protocol; for one of those use `--model muse-2`. Not used in
  this study.
* **Naming convention.** Everything is labelled `Muse-2` or `Muse-Athena`: the
  `--model` values (`muse-2` / `muse-athena`), every LSL stream name, the raw-log
  filenames, the intake choices, the `Data/P###/pass_X_<device>` folders and the
  `pass_start` / `pass_x_device` markers. The pre-rename codes `muse2` / `athena`
  are still accepted as aliases so the 2026 pilot files can still be audited.

### Streams published

| Device | Name | Type | Rate | Channels |
|---|---|---|---|---|
| Muse-2 | `Muse-2-EEG (<id>)` | EEG | 256 | TP9, AF7, AF8, TP10 (+AUX) µV |
| Muse-2 | `Muse-2-PPG (<id>)` | PPG | 64 | AMBIENT, IR, RED (raw counts) |
| Muse-2 | `Muse-2-ACC (<id>)` / `Muse-2-GYRO (<id>)` | ACC / GYRO | 52 | x, y, z (g / dps) |
| Muse-2 | `Muse-2-TELEMETRY (<id>)` | Telemetry | irregular | battery %, fuel mV, adc, temp |
| Muse-Athena | `Muse-Athena-EEG (<id>)` | EEG | 256 | 4 or 8 ch per preset, µV |
| Muse-Athena | `Muse-Athena-OPTICS (<id>)` | PPG | 64 | 4 / 8 / 16 optics ch per preset |
| Muse-Athena | `Muse-Athena-ACCGYRO (<id>)` | ACCGYRO | 52 | ACC xyz, GYRO xyz |
| Muse-Athena | `Muse-Athena-BATTERY (<id>)` | Battery | irregular | % |
| both | `Muse-2-BridgeEvents (<id>)` or `Muse-Athena-BridgeEvents (<id>)` | BridgeEvents | irregular | `bridge:start`, `link:connected`, `link:streaming`, `link:lost:reason=…`, `stats:…` every 10 s, `bridge:stop:…` |

Each stream's `desc` records the model, bridge version, BLE name and address,
how missing samples are represented, and the timestamp model.
The `…-BridgeEvents` stream has type `BridgeEvents`, not `Markers`, so it can never be
confused with the experiment's `MuseProxyMarkers`.

### Raw packet log (ground truth)

`Data/raw_ble/raw_ble_<model>_<id>_<stamp>.txt` holds one line per BLE
notification: `utc_iso<TAB>characteristic_uuid<TAB>hex`. This is OpenMuse's
format, so Muse-Athena logs decode with `muse_proxy.bridge.athena_decode.parse_message`
(or OpenMuse itself). Alongside it are `_clock.csv` (UTC ↔ LSL anchors) and
`_meta.txt` (device, preset, link events, final loss summary). Any decode or
timestamp bug can be fixed after the fact by re-decoding this file.

## Protocol notes

**Commands, both models:** written to the control characteristic
`273e0001-…` as length-prefixed ASCII `[len][token]\n`. Tokens are validated
(ASCII, no embedded newline) before sending.

**Muse-2:** each sensor channel is its own characteristic (EEG `0003–0007`,
GYRO `0009`, ACC `000a`, telemetry `000b`, PPG `000f–0011`). Every
notification is 20 bytes: a uint16 BE packet index, then 12 × uint12 EEG,
6 × uint24 PPG, or 3 × xyz int16 IMU samples. Start sequence:
`h, v1, s, p50, s, d`. Keep-alive: `k` every 5 s. If the data goes quiet for
2.5 s it re-sends `d`; after 5 s it reconnects.

**Muse-Athena:** two data characteristics (`0013`, `0014`) carry multiplexed
packets (14-byte header with a 256 kHz tick, then tagged subpackets). The
handshake is the relay's research-validated primed sequence (`p21` prime,
then the target preset with a double `dc001`). The default preset is `p1035`
(EEG4 + Optics4 + IMU + battery). Keep-alive: `s`. If the data goes quiet it
re-sends `dc001`/`L1`.

**Muse-Athena timing finding.** The OpenMuse `make_timestamps` (vendored unchanged
here and in the relay) measures ticks relative to the first tick *of each
batch*. The tick therefore only orders packets, and sample times are really a
running sample counter. A lost packet leaves no gap: later samples are placed
one packet too early until the 30 s regression window absorbs the error as a
step. The bridge adds `TickGapDetector`, which measures loss against a
persistent tick. By default it **reports** the loss (`gap_events`,
`gap_samples` in the stats and in BridgeEvents).
`--athena-gap-correction` also shifts later samples by the lost duration. It
stays opt-in until it has been checked on hardware (see the checklist below).

## Hardware bring-up checklist (first run on the lab laptop)

Do these in order and paste the console output back into the Claude session.

1. **Install:** `git pull`, then `INSTALL.bat` (or `uv sync`). Check that
   `uv run pytest -q` passes.
2. **Dry run:** `launch_bridge.bat stream --model muse-2 --fake --fake-drop 0.05 --duration 20`.
   About 5% loss should be reported, and the streams should be visible in Lab Recorder.
3. **Scan:** `launch_bridge.bat`. The Muse-2 should appear as `Muse-2       Muse-XXXX`
   and the Muse-Athena as `Muse-Athena  MuseS-XXXX`. Make sure no phone app is holding the headset.
4. **Muse-2, 5 minutes on head, about 1 m from the laptop:**
   `launch_bridge.bat stream --model muse-2 --address <addr> --duration 300`
   * EEG and PPG `lost_pct` should be low (< 2%). If EEG
     `channel_packets_missing` is roughly 75% of packets, the "channels share a
     packet index" assumption is wrong. Stop and report.
   * PPG samples should be > 0. If they are 0, the preset didn't enable PPG.
     Retry with `--preset p51` and report.
   * Battery % should be plausible. Upright, ACC_Z should read about +1 g.
5. **Muse-Athena, 5 minutes on head:** `launch_bridge.bat stream --model muse-athena --address <addr> --duration 300`
   * Note `gap_events`/`gap_samples` and keep the raw log from `Data/raw_ble`.
     This is the data that decides whether `--athena-gap-correction` becomes the default.
6. **Reconnect test:** while streaming, switch the headset off for 10 s, then
   back on. Expect `link lost (disconnected)` and then `streaming` again, with
   `reconnects 1` and no crash.
7. **End-to-end:** run the bridge, start Lab Recorder (all streams ticked),
   and run the MuseProxy app through Pass A with a test PID. Then run
   `uv run python scripts/verify_xdf.py <file>.xdf`. It should report **no
   FAIL**, `Muse-2-EEG`/`Muse-2-PPG` (or `Muse-Athena-EEG`/`Muse-Athena-OPTICS`) under
   *Required headset streams*, no zero-run warnings, and `backwards_ts=0`.
