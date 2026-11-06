# MuseProxy

Experiment control + LSL marker stream for the IACS *Muse Proxy Metrics and Denoising Study*.

Each participant visit runs the protocol **twice** in one sitting — once wearing the Muse 2, once wearing the Muse Athena — with an in-app device swap between passes. Each pass produces its own Lab Recorder XDF.

> Full scientific brief: [`CLAUDE.md`](./CLAUDE.md).
> Authoritative build spec: [`PROJECT_PLAN.md`](./PROJECT_PLAN.md).

---

## Quickstart (RAs)

### First-time install (per laptop)

1. Install Python 3.11+ from https://www.python.org/downloads/ (verify with `py --version`).
2. Double-click **`INSTALL.bat`**.

The installer fetches `uv`, runs `uv sync`, renders all audio assets via `edge-tts` (internet required at install only), and runs a self-check. ~60–90 s total.

### Each visit

1. Plug headphones into the laptop. Verify the participant can hear comfortably.
2. Start the **headset bridge** for the device the participant is wearing first
   (`launch_bridge.bat` to scan, then e.g.
   `launch_bridge.bat stream --model muse2 --address <address>`). Leave its window
   open; the status line should say `streaming` with low `% lost`. See
   [`docs/BLE_BRIDGE.md`](docs/BLE_BRIDGE.md).
3. Open **Lab Recorder**. Confirm it has discovered:
   - `Muse2-EEG` + `Muse2-PPG` (Muse 2) or `Muse-EEG` + `Muse-OPTICS` (Athena)
   - The CGX AIM-II stream
   - `MuseProxyMarkers` (this app — note: it appears only *after* you launch the app)
4. Launch **this app** (`launch.bat`). The GUI opens at the Intake screen.
5. Fill in intake. Pick Pass A device (the headset the participant is currently wearing).
6. Walk through pre-launch checklist → volume check → settle.
7. **Start a Lab Recorder recording before clicking Start** — name the file something like `P001_passA_muse2.xdf`.
8. Hand the laptop to the participant. They walk Blocks 1–4 by reading instructions and pressing SPACE.
9. At the **Pass Switch** screen, follow the on-screen RA checklist:
   - Stop the current Lab Recorder recording
   - Ctrl+C the Pass A bridge, swap headset, start the bridge for the new
     device; verify good signal on the new one
   - **Start a new Lab Recorder recording** with a Pass B filename (e.g., `P001_passB_athena.xdf`)
   - Tick the manual confirmation boxes in-app
10. Pass B walks the same Blocks 1–4 with the new device.
11. Completion → Save & Exit.

### Aborting

Press **Escape** or close the window. On confirmation:

- `session_abort` marker is emitted
- Current block's CSV row gets `status: "aborted"`
- `session.json` for the current pass is written with `status: "aborted"`
- `PARTIAL_session.json` is **retained** (forensic snapshot — cleaned only on a fresh start)
- LSL outlet is closed
- App exits

If abort happens during Pass B, Pass A's data is already saved and untouched.

### Where data lands

```
Data\P001\
├── intake.json                     # visit-level intake
├── pass_A_muse2\
│   ├── session.json                # Pass A master record
│   ├── markers.csv                 # CSV mirror of Pass A LSL markers
│   ├── block_01_baseline.csv
│   ├── block_02_paced_0p1Hz.csv
│   ├── block_03_paced_0p2Hz.csv
│   └── block_04_artifact.csv
└── pass_B_athena\
    └── (same structure)
```

`Data\` is gitignored. Nothing personal goes to version control.

### Post-session audit

```
uv run python scripts\verify_xdf.py path\to\P001_passA_muse2.xdf
```

Prints a coverage table (every expected marker family vs. observed count), breath-cycle counts vs. expected, artifact-cue presence, and any constraint violations. Exit 0 = clean.

Run it on each XDF after each session — catches Lab Recorder misconfigurations immediately while you can still re-collect.

### Counterbalancing across the 5 participants

The app does **not** enforce device order. Suggested assignment (operator's choice):

| Participant | Pass A | Pass B |
|---|---|---|
| P001 | Muse 2 | Muse Athena |
| P002 | Muse Athena | Muse 2 |
| P003 | Muse 2 | Muse Athena |
| P004 | Muse Athena | Muse 2 |
| P005 | Muse 2 | Muse Athena |

(3 of one order, 2 of the other.)

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `INSTALL.bat`: "uv not found" after first run | New install needs a fresh shell | Open a new Command Prompt and re-run |
| Self-check: `FAIL audio device` | Headphones unplugged, or wrong default device | Plug in; or set `audio.device_name_substring` in `config\session_defaults.yaml` |
| `launch.bat` opens then closes | Python crash on startup | Run `uv run python -m muse_proxy.app --self-check` to see the error |
| Audio cues sound wrong | Stale `.wav` files | `uv run python scripts\generate_audio.py --force` |
| Lab Recorder doesn't see `MuseProxyMarkers` | App not yet launched, or LR refresh needed | Launch the app first, then refresh streams in Lab Recorder |
| Pass B XDF is empty / has no markers | Forgot to start a new Lab Recorder recording at pass switch | Pass A data is intact; re-collect Pass B in a follow-up visit if needed |
| Pre-launch checklist won't enable Start | One of the stream-visibility checkboxes is unticked | Verify in Lab Recorder; tick when confirmed |
| Volume too low/high | Operator-chosen | Adjust Windows system volume; the cue audio is rendered at a fixed amplitude |

---

## Quickstart (developers)

```bash
git clone <repo>
cd MuseProxy

uv sync
uv run python scripts\generate_audio.py

uv run pytest
uv run ruff check src\ tests\ scripts\

uv run python -m muse_proxy.app
# or: uv run python -m muse_proxy.app --self-check
```

### Architecture

See [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) for the full spec. High level:

- **Stage controller** (`app.py`) swaps Tk frames; each stage has `on_enter` / `on_leave`.
- **Worker-thread + Tk poller** for any blocking work (audio playback, timers). Cross-thread `tk.after()` is unsafe; we use a `queue.Queue` polled every 50 ms.
- **Sample-anchored markers** for every audio cue. PortAudio's `outputBufferDacTime` is captured once at stream start and converted to `local_clock`; each cue's marker is emitted with a precomputed timestamp `t0 + onset_sample / sample_rate`.
- **Two-pass** is the most novel piece. Pass A → `pass_switch` → Pass B state machine, with the LSL outlet closed and reopened (new `source_id`) at the boundary. Two output directories per visit; one shared `intake.json`.

### Tests

Target ≥ 150 tests at completion. Run with `uv run pytest`. One opt-in `test_audio_timing.py` documents a manual loopback procedure; skipped by default.

---

## Related projects

- [`Jazzo`](https://github.com/Institute-for-Advanced-Consciousness/Jazzo) — SMF EEG study; reference for the pre-launch checklist UI and the CSV marker mirror.
- [`SuperChillsOddball`](https://github.com/Institute-for-Advanced-Consciousness/SuperChillsOddball) — closest template; reference for everything else.

---

## Contact

PI: Nicco Reggente, Ph.D. (IACS).
