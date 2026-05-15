# MuseProxy — Build Plan

Build spec for a **Windows-only, double-click-launch GUI experiment runner** for the IACS *Muse Proxy Metrics and Denoising Study* (2026). This document is authoritative for all build decisions; when in doubt, follow it. Where it leaves a choice open, ask before guessing.

---

## 1. Study context

The study collects parallel physiological recordings from two devices simultaneously:

- **Muse 2** *or* **Muse Athena** (EEG, PPG, GYR, ACC) — one headset per pass
- **CGX AIM-II** (ECG via channel 1; bilateral anterior temporalis EMG via channels 2–3; frontalis/corrugator EMG via channel 4)

Five participants each complete the protocol **twice** in a single visit — once wearing the Muse 2, once wearing the Muse Athena — with an in-app device swap between passes. Each pass produces its own Lab Recorder XDF.

Three downstream analytic aims drive the marker schema:

1. PPG → ECG-aligned R-peak proxy (for HEP timing work)
2. PPG → breath-rate proxy
3. Optimal EEG denoising strategies for the Muse montage

Every breath onset, every artifact-creation cue onset, and every block boundary is emitted as an LSL marker, mirrored to a local CSV for forensic redundancy.

---

## 2. Reference repositories

Two prior IACS projects are explicit templates. **Study them before writing any code.**

### SuperChillsOddball — closest template
<https://github.com/Institute-for-Advanced-Consciousness/SuperChillsOddball>

Mirror wholesale:

- `INSTALL.bat` / `launch.bat` for double-click Windows operation
- `src/<pkg>/` layout: `app.py` (Tk root + stage controller + `--self-check`), `config.py` (pydantic-validated YAML), `markers.py` (LSL outlet wrapper), `audio.py` (`ToneScheduler` with sample-anchored markers via PortAudio's `outputBufferDacTime` → `local_clock`), `persistence.py` (`SessionWriter` — atomic JSON + per-block CSV + `PARTIAL_session.json` on abort), `stages/` (one Tk frame per screen)
- Stage controller pattern (`App.show_stage(name, **payload)`)
- Worker-thread + Tk `after(50, ...)` poller — cross-thread `tk.after()` is **not** safe under uv's bundled Tk
- Service-optional stages — every stage tolerates `app.outlet`, `app.writer`, `app.audio_scheduler` being `None` (enables mocked tests and a no-LSL GUI walk for layout review)
- Atomic writes via `tmp + rename`
- `--self-check` startup mode
- `scripts/verify_xdf.py` post-session auditor
- Test layout (~190 tests across config, schedule, markers, audio, persistence, stages, e2e)

### Jazzo — selective borrowings
<https://github.com/Institute-for-Advanced-Consciousness/Jazzo>

Mirror selectively:

- Pre-launch checklist UI pattern (Start button disabled until every checkbox ticked)
- CSV mirror of LSL markers as `(timestamp_iso, timestamp_lsl, marker_string)`
- Thread-safe UI update queue (`_ui_queue` polled by `_poll_ui_queue` every 50 ms)
- The IACS color scheme (`PURPLE = "#5d5179"`, white, black) — keep for institutional continuity

### What NOT to inherit
- No VLC video player (no video stimuli here)
- No PsychoPy (avoids MSVC Build Tools install on Windows)
- No `pygame` (`sounddevice` gives sample-accurate marker emission; `pygame` does not)

---

## 3. Two-pass visit architecture *(novel — read carefully)*

Each participant visit produces **two** XDF recordings:

```
Visit ─┬─ Pass A (e.g., Muse 2)      →  one XDF
       └─ Pass B (e.g., Muse Athena) →  one XDF
```

Operationally:

1. Operator chooses device order at intake (Pass A device + Pass B device auto-fills as the other).
2. App walks Pass A: pre-launch → volume check → settle → Blocks 1–4.
3. At the **inter-pass switch screen**, the app:
   - Closes the Pass A LSL marker outlet
   - Finalizes Pass A data (`session.json` written under `Data\P{NNN}\pass_A_<device>\`)
   - Shows an RA-facing checklist for the device swap (stop Lab Recorder, swap headset, verify signal, start a *new* Lab Recorder file)
   - Offers an optional participant break (default 5 min, RA-skippable)
   - Opens a fresh Pass B LSL marker outlet with a new `source_id`
4. App walks Pass B: pre-launch (for the new device's streams) → brief volume re-check → shorter settle → Blocks 1–4.
5. Visit completes after Pass B Block 4.

### LSL outlet lifecycle

Two distinct outlets per visit. Same `name` (`MuseProxyMarkers`), unique `source_id`:

- Pass A: `source_id = f"MuseProxy_passA_{pid}_{yyyymmdd}"`
- Pass B: `source_id = f"MuseProxy_passB_{pid}_{yyyymmdd}"`

The Pass A outlet is closed (`del outlet`) before the Pass B outlet is created, so there is no cross-pollination between recordings. Lab Recorder discovers the active outlet when each new recording starts — each XDF therefore contains exactly one pass's markers.

The pre-launch checklist for Pass B includes an explicit "Lab Recorder is recording **a new** XDF file" checkbox to prevent the failure mode of forgetting to restart Lab Recorder at the swap.

### Data layout

```
Data\
└── P001\
    ├── intake.json                      # visit-level intake (both devices), written once at intake submit
    ├── pass_A_muse2\
    │   ├── session.json                 # Pass A master record
    │   ├── markers.csv                  # CSV mirror of Pass A LSL markers
    │   ├── block_01_baseline.csv
    │   ├── block_02_paced_0p1Hz.csv
    │   ├── block_03_paced_0p2Hz.csv
    │   └── block_04_artifact.csv
    └── pass_B_athena\
        └── ... (same structure)
```

`Data\` is `.gitignore`'d.

---

## 4. Tech stack — locked

| Component | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Match prior IACS repos |
| Package manager | `uv` | Match prior IACS repos |
| GUI | tkinter (stdlib) | Zero install, sufficient |
| Audio playback | `sounddevice` + `soundfile` | Sample-accurate marker emission via `outputBufferDacTime` |
| Audio synthesis | `numpy` for tone math; pre-rendered `.wav` via `edge-tts` for spoken cues (rendered at install time, committed) | Avoids per-machine TTS voice variance |
| LSL | `pylsl` | Standard |
| Config | `pydantic` + `PyYAML` | Validation + readability |
| Persistence | stdlib `json`, `csv` | No DB needed |
| Test | `pytest`, `pytest-cov` | Match prior |
| Lint | `ruff` | Match prior |
| Post-session audit | `pyxdf` (dev dep) | XDF parsing |

**Forbidden:** PsychoPy, pygame, OpenGL-based libs (Windows install friction), VLC (no video here).

---

## 5. Repository layout

```
MuseProxy/
├── README.md                          # Operator + developer guide
├── CLAUDE.md                          # Scientific brief + design rationale
├── PROJECT_PLAN.md                    # This file (authoritative)
├── INSTALL.bat                        # First-run installer
├── launch.bat                         # Every-session launcher
├── pyproject.toml                     # uv project + deps + ruff/pytest config
├── uv.lock                            # Generated
├── .gitignore
│
├── config/
│   └── session_defaults.yaml          # All operator-tunable parameters
│
├── src/muse_proxy/
│   ├── __init__.py
│   ├── app.py                         # Tk root + stage controller + abort + --self-check entry
│   ├── config.py                      # YAML loader + pydantic models
│   ├── markers.py                     # LSL outlet wrapper + CSV mirror; lifecycle for two-pass
│   ├── audio.py                       # AudioScheduler (sample-anchored marker emission)
│   ├── persistence.py                 # SessionWriter (atomic JSON + per-block CSV + PARTIAL on abort)
│   ├── breathing.py                   # Paced-breath cue scheduler (buffer construction + per-cycle marker times)
│   ├── artifacts.py                   # Artifact-creation cue schedule (configurable from YAML)
│   └── stages/
│       ├── __init__.py
│       ├── _base.py                   # StageFrame base (on_enter, on_leave, abort hook)
│       ├── intake.py                  # RA: subject info, device order, full intake metadata
│       ├── pre_launch.py              # Dynamic checklist (depends on current pass's device)
│       ├── volume_check.py            # Looping 1 kHz tone, RA confirms participant can hear
│       ├── settle.py                  # Pre-session calm-down timer (RA-skippable)
│       ├── block_instructions.py      # Per-block participant-facing instructions, SPACE to begin
│       ├── block_breathing.py         # Generic runner for blocks 1/2/3
│       ├── block_artifact.py          # Block 4: cued artifact creation
│       ├── between_block_settle.py    # Participant-paced rest, SPACE to advance
│       ├── pass_switch.py             # Inter-pass device swap workflow
│       └── completion.py              # Visit-end summary + Save & Exit
│
├── assets/
│   └── sounds/                        # All committed .wav files (rendered at install)
│       ├── volume_check.wav
│       ├── in.wav
│       ├── out.wav
│       ├── close_eyes.wav
│       ├── open_eyes.wav
│       ├── take_a_moment.wav
│       └── artifact_*.wav             # One per artifact cue
│
├── scripts/
│   ├── generate_audio.py              # Renders all .wav assets via edge-tts (idempotent)
│   ├── self_check.py                  # Standalone diagnostic (also reachable via --self-check)
│   └── verify_xdf.py                  # Post-session marker / timing audit
│
└── tests/
    ├── test_config.py
    ├── test_markers.py                # Outlet → Inlet round-trip per marker family
    ├── test_audio.py                  # AudioScheduler buffer math (mocked sounddevice)
    ├── test_breathing.py              # Cue schedule generation for 0.1 / 0.2 Hz
    ├── test_artifacts.py              # Artifact cue schedule generation
    ├── test_persistence.py            # Atomic write + abort retention + two-pass directory layout
    ├── test_app.py                    # Stage controller transitions (mocked services)
    ├── test_stages_*.py               # One per stage, with shared App fixture
    ├── test_two_pass.py               # Pass-A → pass_switch → Pass-B with mocked outlets
    ├── test_verify_xdf.py             # XDF auditor logic
    └── test_e2e.py                    # Full visit walkthrough with real outlet + writer
```

---

## 6. Stage flow

Linear unless aborted. Each row is one `StageFrame`.

| # | Stage | Pass | Facing | Description |
|---|---|---|---|---|
| 1 | `intake` | — | RA | Subject ID (auto-suggested), Pass A device, full intake form (see § 8) |
| 2 | `pre_launch` | A | RA | Checklist gates Start until all expected streams visible in Lab Recorder |
| 3 | `volume_check` | A | RA + P | Loops 1 kHz tone, RA confirms comfortable level |
| 4 | `settle` | A | P | Pre-session calm-down (default 5 min, RA-skippable; elapsed time logged) |
| 5 | `block_instructions` (1) | A | P | "Block 1: Natural breathing. Eyes closed. SPACE when ready." |
| 6 | `block_breathing` (1) | A | P | 5 min, no audio cues, only `block_start` / `block_end` markers |
| 7 | `between_block_settle` | A | P | "Take a moment. SPACE when ready." |
| 8 | `block_instructions` (2) | A | P | "Block 2: Paced breathing. Follow the 'in' and 'out' cues. SPACE to begin." |
| 9 | `block_breathing` (2) | A | P | 5 min at 0.1 Hz (5 s inhale, 5 s exhale; 30 cycles) |
| 10 | `between_block_settle` | A | P | |
| 11 | `block_instructions` (3) | A | P | "Block 3: Faster paced. SPACE to begin." |
| 12 | `block_breathing` (3) | A | P | 5 min at 0.2 Hz (2.5 s inhale, 2.5 s exhale; 60 cycles) |
| 13 | `between_block_settle` | A | P | |
| 14 | `block_instructions` (4) | A | P | "Block 4: Follow the audio prompts. You'll be asked to make specific facial movements." |
| 15 | `block_artifact` (4) | A | P | 1:30, ~13 audio-cued artifact tasks |
| 16 | `pass_switch` | A→B | RA + P | Save Pass A, close outlet; checklist for device swap; participant break; open Pass B outlet |
| 17 | `pre_launch` | B | RA | Same checklist UI, but for Pass B device's streams + "new Lab Recorder file started" |
| 18 | `volume_check` | B | RA | Brief re-check |
| 19 | `settle` | B | P | Shorter default (2 min, RA-skippable) |
| 20–29 | Blocks 1–4 + between-block settles | B | P | Identical structure to Pass A |
| 30 | `completion` | — | RA | Summary of both passes, Save & Exit |

### Abort behavior (any stage)

Escape key OR window-close → confirmation dialog. On confirm:

1. Emit `session_abort:pass={A|B}:stage={current}` marker
2. Mark in-progress block (if any) with `status: "aborted"` in its CSV row
3. Write `session.json` for the *current* pass with `status: "aborted"`
4. Retain `PARTIAL_session.json` (forensic snapshot — kept on abort, deleted on clean exit)
5. Close LSL outlet
6. Exit

If abort occurs during Pass B, Pass A's data is already saved and untouched.

---

## 7. Block specifications

### Block 1 — Baseline natural breathing (5 min)
- No audio cues during the block
- Markers: `block_start`, `block_end`
- Pre-block TTS clip: `close_eyes.wav` ("Close your eyes")
- Post-block TTS clip: `open_eyes.wav` ("All done. Open your eyes.")
- TTS clips emit their own `tts:close_eyes:start/:end` markers at audio-sample boundaries

### Block 2 — Paced breathing, 0.1 Hz (5 min)
- 5 s inhale + 5 s exhale = 10 s/cycle × 30 cycles = 300 s
- At each inhale onset: play `in.wav`, emit `breath:inhale:block=2:cycle=N:pass=A|B` at the sample-anchored audio onset
- At each exhale onset: play `out.wav`, emit `breath:exhale:block=2:cycle=N:pass=A|B`
- Markers: `block_start`, 30 × `breath:inhale`, 30 × `breath:exhale`, `block_end`
- Pre/post TTS: same as Block 1

### Block 3 — Paced breathing, 0.2 Hz (5 min)
- 2.5 s inhale + 2.5 s exhale = 5 s/cycle × 60 cycles = 300 s
- Same marker scheme as Block 2 with `block=3`, `cycle=1..60`
- `in.wav` / `out.wav` must each be ≤ 400 ms to fit cleanly in 2.5 s phases

### Block 4 — Artifact creation (1:30)
- ~13 cues (default), each: 2–3 s of TTS prompt + 4–5 s of execution window
- See `config/session_defaults.yaml` → `artifact_block.cues` for the full schedule
- Each cue emits:
  - `artifact:<label>:start:block=4:pass=A|B` at TTS onset (sample-anchored)
  - `artifact:<label>:end:block=4:pass=A|B` at (start_s + window_s)
- State cues (`eyes_open`, `eyes_closed`) emit only `:start`

#### Default cue list (see config for exact timings)

| Label | Spoken text | Target signal |
|---|---|---|
| `eyes_open` | "Open your eyes and keep them open" | state |
| `blink_slow` | "Blink slowly several times" | EOG / AF7-8 |
| `blink_rapid` | "Now blink rapidly several times" | EOG / AF7-8 |
| `saccade_horizontal` | "Look left and right repeatedly" | EOG horizontal |
| `saccade_vertical` | "Look up and down repeatedly" | EOG vertical |
| `jaw_clench_left` | "Clench your jaw on the left side, then relax" | Left ant. temporalis (EMG ch 2), TP9/AF7 contamination |
| `jaw_clench_right` | "Clench your jaw on the right side, then relax" | Right ant. temporalis (EMG ch 3), TP10/AF8 contamination |
| `jaw_clench_bilateral` | "Clench your whole jaw, then relax" | Both temporalis |
| `brow_raise` | "Raise your eyebrows" | Frontalis (EMG ch 4) |
| `brow_furrow` | "Furrow your brow, then relax" | Corrugator/frontalis |
| `eyes_closed` | "Now close your eyes and keep them closed" | state |
| `jaw_clench_bilateral_closed` | "Clench your whole jaw again" | Both temporalis, eyes-closed |
| `brow_furrow_closed` | "Furrow your brow one more time" | Corrugator/frontalis, eyes-closed |

The eyes-closed repeats of jaw clench and brow furrow let the analyst dissociate ocular artifact (which largely vanishes eyes-closed) from muscle artifact (which does not).

---

## 8. Intake form

Single Tk frame, RA-facing. Every field below is saved to `Data\P{NNN}\intake.json` AND mirrored to **both passes'** LSL marker streams (intake is visit-level). Exception: `ra_notes` is JSON-only — LSL string markers can't safely encode arbitrary characters (`:` and `=` are reserved by our marker grammar).

| Field | Type | Required | LSL key |
|---|---|---|---|
| Participant ID | text, auto-suggested as next `P{NNN}` | yes | `pid` |
| Date | auto, `YYYYMMDD` | auto | `date` |
| Pass A device | dropdown: `Muse 2` / `Muse Athena` | yes | `pass_A_device` |
| Pass B device | dropdown: auto-fills as the other | yes | `pass_B_device` |
| Age | int | yes | `age` |
| Sex | dropdown: M / F / Other / Prefer not to say | yes | `sex` |
| Handedness | dropdown: R / L / Ambi | yes | `handedness` |
| Wears glasses or contacts | Y/N | yes | `glasses` |
| Recent dental work (past month) | Y/N | yes | `dental_work` |
| Current facial or jaw pain | Y/N + optional text | yes | `facial_pain` |
| Hours since last caffeine (use 99 for none today) | float | yes | `caffeine_hours_ago` |
| Hours since last meal | float | yes | `last_meal_hours_ago` |
| Hours of sleep last night | float | yes | `sleep_hours` |
| Recent vigorous exercise (past 2 h) | Y/N | yes | `recent_exercise` |
| Self-reported stress (0–10) | int slider | yes | `stress_self_report` |
| Stressful trip in / parking issues | Y/N | yes | `trip_stress` |
| Minutes given to settle after stressful trip | int | yes (if above Y) | `trip_settle_minutes` |
| Water offered and accepted | Y/N | yes | `hydration` |
| Participant warm enough | Y/N | yes | `warm` |
| RA notes | free-text | no | (JSON only) |

The intake marker is emitted as one compound string at intake submit:
`intake:age=34:sex=F:handedness=R:glasses=N:...:warm=Y`

---

## 9. Marker schema

Full grammar. All markers are strings on the `MuseProxyMarkers` outlet. Subfields separated by `:`. Key-value pairs use `=`. Free-text never enters the marker stream.

### Visit-level (emitted on both passes' outlets at appropriate moments)
```
session_start:pid=P001:date=20260512:visit_start_iso=2026-05-12T10:30:00.123
intake:age=34:sex=F:handedness=R:glasses=N:dental_work=N:...:warm=Y
pass_a_device:muse2|athena
pass_b_device:muse2|athena
session_end:pid=P001:visit_end_iso=2026-05-12T11:42:18.456
session_abort:pass=A|B:stage=<stage_name>
```

### Pass-level
```
pass_start:pass=A|B:device=muse2|athena:source_id=<source_id>
volume_check:start
volume_check:end:level=ok
settle:start:pass=A|B:duration_planned_s=300
settle:end:pass=A|B:duration_elapsed_s=287
pass_end:pass=A|B
```

### Block-level
```
block_start:block=N:label=<label>:pass=A|B:duration_planned_s=N
block_end:block=N:label=<label>:pass=A|B:duration_actual_s=N
between_block_settle:start:after_block=N
between_block_settle:end:after_block=N:duration_s=N
```

### Breathing (blocks 2 and 3 only)
```
breath:inhale:block=N:cycle=N:pass=A|B
breath:exhale:block=N:cycle=N:pass=A|B
```

### Artifact creation (block 4)
```
artifact:<label>:start:block=4:pass=A|B
artifact:<label>:end:block=4:pass=A|B
```
State cues (`eyes_open`, `eyes_closed`) emit only `:start`.

### TTS bookends
```
tts:<clip_name>:start
tts:<clip_name>:end
```
e.g. `tts:close_eyes:start`, `tts:open_eyes:end`.

### CSV mirror format

`Data\P{NNN}\pass_{A|B}_{device}\markers.csv`:
```
timestamp_iso,timestamp_lsl,marker_string
```

---

## 10. Audio assets

All audio is **pre-rendered to `.wav` at install time** and committed to `assets/sounds/`. The runtime never invokes TTS — only `sounddevice` playback of static WAVs. This guarantees identical cue audio across deployments and identical onset characteristics for marker timing.

### `scripts/generate_audio.py`

Idempotent renderer. Called by `INSTALL.bat`; also runnable as `--force`. Steps:

1. Read `config/session_defaults.yaml` for the cue list and TTS voice (`audio.tts_voice`, default `en-US-AriaNeural`)
2. For each cue: render to MP3 via `edge-tts`, decode to mono 44.1 kHz numpy array, trim leading/trailing silence below an amplitude threshold, write as `.wav` via `soundfile`
3. Synthesize `volume_check.wav` directly from numpy (1 kHz sinusoid, cosine ramps, no TTS)
4. Print a manifest showing each `.wav` and its duration; warn if any breathing cue exceeds 400 ms

`edge-tts` requires internet at install time only; the runtime is fully offline.

### Audio playback contract (`audio.py`)

```python
class AudioScheduler:
    def __init__(self, sample_rate=44100, device=None): ...

    def play_wav(self, path: Path,
                 on_onset: Callable[[float], None] | None = None,
                 on_end: Callable[[], None] | None = None) -> threading.Event:
        """
        Play WAV. If on_onset is given, call it with the LSL local_clock
        timestamp corresponding to the first audio sample reaching the DAC.
        Returns an Event that .set()s when playback finishes.
        """

    def schedule_paced_breathing(self,
                                 rate_hz: float,
                                 duration_s: float,
                                 inhale_wav: Path,
                                 exhale_wav: Path,
                                 on_inhale: Callable[[int, float], None],
                                 on_exhale: Callable[[int, float], None]) -> threading.Event:
        """
        Build a single contiguous audio buffer (silence + inhale wav + silence + exhale wav, repeated),
        play it in one stream so the audio clock is monotonic, and emit
        on_inhale(cycle_idx, lsl_ts) and on_exhale(cycle_idx, lsl_ts) at the
        exact LSL timestamp of each cue's first audio sample.
        """

    def schedule_artifact_block(self,
                                cues: list[ArtifactCueConfig],
                                cue_wav_dir: Path,
                                on_cue_start: Callable[[str, float], None],
                                on_cue_end: Callable[[str, float], None]) -> threading.Event:
        """
        Build a contiguous buffer with every cue's wav at its configured start_s
        (silence between), play in one stream, emit start/end markers per cue
        at sample-anchored timestamps.
        """
```

The **single contiguous audio buffer** approach is non-negotiable. `time.sleep`-based scheduling accumulates jitter and breaks the whole rationale for this study. By precomputing the buffer and capturing PortAudio's `outputBufferDacTime` once at stream start, every cue onset is derived as `t0 + onset_sample / sample_rate`.

---

## 11. Configuration

`config/session_defaults.yaml` is the single source of truth for all tunable parameters. Loaded once at app startup, validated by pydantic models in `config.py`. Operators may edit between sessions, never mid-session.

See the included `session_defaults.yaml` for the full default config. Key sections:

- `participant.auto_suggest_next_id` — scan `Data\` for highest `P{NNN}`, suggest `P{NNN+1}`
- `lsl.outlet_name`, `lsl.source_id_template`
- `pre_launch_streams` — per-device expected stream names; operator edits to match Muse-streaming software (BlueMuse / Petal Metrics / Mind Monitor / Athena-side software). Matched as case-insensitive substring against streams visible in Lab Recorder
- `audio.device_name_substring`, `audio.sample_rate`, `audio.tts_voice`
- `settle.initial_pass_a_default_min`, `settle.initial_pass_b_default_min`, `settle.inter_pass_break_default_min`
- `blocks` — list of 4 block configs with timing
- `artifact_block.cues` — full cue schedule
- `ui` colors
- `data_dir`

---

## 12. Self-check mode

`uv run python -m muse_proxy.app --self-check` exits 0 if all pass, 1 if any fail:

1. Python version ≥ 3.11
2. All deps importable (`pylsl`, `sounddevice`, `soundfile`, `numpy`, `pydantic`, `yaml`)
3. Config YAML parses and validates against pydantic models
4. All expected WAV files exist in `assets/sounds/` and load
5. Audio output device discovered (configured device-name substring matches an available device)
6. LSL outlet can be created and a test sample pushed
7. Tk display can be opened (`tk.Tk()` + `withdraw()`)
8. `Data\` directory writable

Output: one line per check, `PASS` / `FAIL` / `WARN`, diagnostic detail on failure. Called by `INSTALL.bat` and reusable for troubleshooting.

---

## 13. `scripts/verify_xdf.py`

Post-session auditor. Usage:
```
uv run python scripts\verify_xdf.py path\to\P001_passA_muse2.xdf
```

Parses the `MuseProxyMarkers` stream and prints:

- **Coverage table** — every expected marker family vs. observed count
- **Per breathing block** — cycle count vs. expected, mean inter-inhale interval vs. target (1 / rate_hz seconds), SD
- **Artifact block** — every expected cue label vs. presence, mean cue-window duration
- **Pass detection** — single pass label across all markers (sanity check that we didn't mix passes in one file)

Exit 0 = clean; exit 1 = at least one violation.

---

## 14. Tests

Target **≥ 150 tests** at completion, modeled on SuperChillsOddball's 190.

Required modules:

- `test_config.py` — YAML loader, pydantic invariants, missing-field rejection, ID auto-suggest from empty / populated `Data\`
- `test_markers.py` — each marker family round-trips through pylsl outlet → inlet on the same machine; outlet open/close lifecycle
- `test_audio.py` — pure helpers: buffer construction math, ISI math, WAV loading; `sounddevice` mocked
- `test_breathing.py` — paced-breath schedule: correct cycle counts (30 at 0.1 Hz, 60 at 0.2 Hz), correct inhale/exhale onset offsets in the buffer, marker timestamps match buffer math
- `test_artifacts.py` — artifact cue schedule: every cue present, total duration ≤ 90 s, no overlapping cue audio
- `test_persistence.py` — atomic write, abort-retains-PARTIAL, clean-exit-deletes-PARTIAL, two-pass directory layout, intake.json shared across passes
- `test_two_pass.py` — Pass-A → pass_switch → Pass-B with mocked outlets; assert new `source_id`, two separate `markers.csv` files in correct dirs, one shared `intake.json`, no marker leakage across passes
- `test_app.py` — stage controller transitions
- `test_stages_*.py` — each stage's `on_enter` / `on_leave` behavior, button enablement, abort propagation; shared App fixture
- `test_verify_xdf.py` — marker parser, cycle-count enforcement, missing-cue detection, pass-mixing detection
- `test_e2e.py` — full visit walkthrough (intake → Pass A all blocks → pass_switch → Pass B all blocks → completion) with a real outlet + writer, mocked audio

One opt-in `test_audio_timing.py` documents a manual loopback procedure for verifying real audio onset timing matches the marker timestamp; skipped by default in CI.

---

## 15. Build artifacts

### `INSTALL.bat`
Installs `uv` if absent, runs `uv sync`, generates audio assets, runs self-check.

### `launch.bat`
Single line: `uv run python -m muse_proxy.app` with `cd /d "%~dp0"`.

Both files are included in the seed repo files.

---

## 16. Implementation order

Suggested phasing. Each phase ends with passing tests and a green self-check.

1. **Scaffold** — `pyproject.toml`, directory tree, empty modules with type signatures, `pytest` + `ruff` configured. Self-check returns "not yet implemented" but the entry point works.
2. **Config + persistence** — `config.py` with pydantic models matching `session_defaults.yaml`. `persistence.py` with atomic writes, abort handling, two-pass layout. Full tests for both.
3. **Markers + audio core** — `markers.py` (LSL outlet + CSV mirror; supports close-and-reopen for pass switch), `audio.py` (`AudioScheduler` with sample-anchored marker emission), tests with mocked sounddevice. Implement and run `scripts/generate_audio.py` to produce WAV assets.
4. **Single-pass shell** — `app.py` stage controller, `_base.py`, intake / pre_launch / volume_check / settle stages. App walks through to a stub block stage. Two-pass logic not yet wired.
5. **Blocks** — `breathing.py`, `artifacts.py`, `block_breathing.py`, `block_artifact.py`. Full block specs implemented. Tests for cue schedules.
6. **Two-pass orchestration** — `pass_switch.py`, app's Pass-A → Pass-B state machine, separate output directories, fresh LSL outlet between passes. `test_two_pass.py` written *first*, then make it pass.
7. **Build artifacts** — `INSTALL.bat`, `launch.bat`, full `--self-check`, `scripts/verify_xdf.py`. README polish.
8. **E2E test + harden** — `test_e2e.py`, abort-from-every-stage tests, edge cases (no audio device, missing WAVs, config parse failures, Lab Recorder forgotten at pass switch).

After each phase:
```
uv run pytest
uv run ruff check src/ tests/ scripts/
uv run python -m muse_proxy.app --self-check
```
…and manually walk the GUI for any new stages.

---

## 17. Ground rules

- **Ask before guessing.** If a design choice isn't specified here, raise it in the session before implementing.
- **No silent failures.** Every IO/LSL/audio operation either succeeds or logs a clearly-attributed error to BOTH the GUI event log AND stdout. Exceptions in worker threads must be caught and surfaced.
- **Tests before implementation for new modules.** Write the test, then the implementation, then run.
- **No new dependencies without justification.** If the stack in § 4 doesn't cover something, raise it.
- **Match the prior repos' style.** Variable naming, file headers, docstring conventions, error-message tone — pull from SuperChillsOddball.
- **Sample-anchored markers are non-negotiable.** Every audio cue marker uses `outputBufferDacTime` → `local_clock` mapping. No `time.sleep`-based scheduling on the timing-critical path.
- **The two-pass architecture is the most novel piece.** Do NOT blur it with the SuperChillsOddball single-session model. Write `test_two_pass.py` early.

---

## 18. Open questions to surface

Surface these to the user *before* implementation if they bite:

1. **Muse LSL stream layout.** The `pre_launch_streams` config has a placeholder substring match. The exact stream names depend on whether the operator uses BlueMuse, Petal Metrics, Mind Monitor (for Muse 2) or the Athena-side software. Operator should edit before first session, but you may want to ask for the canonical stream names at scaffold time.
2. **TTS voice approval.** Default is `en-US-AriaNeural` (Microsoft Edge TTS). Render once and ask user to listen before locking in.
3. **Counterbalancing across the 5 participants.** Document the device-order assignment in the README (e.g., "P001, P003, P005 = Pass A Muse 2; P002, P004 = Pass A Athena"). The app records the order but doesn't enforce it.

---

End of plan.
