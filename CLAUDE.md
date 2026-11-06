# MuseProxy — Scientific brief

## Why this exists

The Muse 2 and Muse Athena are consumer EEG headsets with on-device PPG (and on the Athena, fNIRS). They are attractive for high-N field psychophysiology, but three gaps separate them from a research-grade rig:

1. **No ECG.** Heart-evoked-potential (HEP) analyses need R-peak timing to roughly tens of milliseconds. PPG peaks lag R-peaks by a participant- and limb-specific pulse-transit time, so PPG alone is not an ECG substitute without modeling.
2. **No respiration belt.** Respiratory-phase analyses need a per-breath waveform. PPG contains respiration-coupled amplitude and frequency modulation, but extracting a clean breath estimate is non-trivial.
3. **High EMG/EOG contamination.** The four Muse electrodes sit on the forehead and behind the ears — exactly where jaw, brow, and ocular activity dominate the signal.

This study collects ground-truth ECG + respiration-paced data + intentional EMG/EOG artifacts alongside Muse signals, supporting three downstream aims:

- **Aim 1.** PPG → R-peak proxy (for HEP timing)
- **Aim 2.** PPG → breath-rate proxy
- **Aim 3.** Optimal denoising strategies for the Muse montage

## Design — minimal viable for the three aims

- **Two-pass per visit.** Each participant runs the full protocol twice — once with Muse 2, once with Muse Athena — so the same physiology under the same paced respiratory ground truth is captured on both devices. With only 5 participants total, running the same person twice controls for cross-participant variance, which would otherwise dominate Aim 3's denoising comparison.
- **Three breathing blocks bracket the respiratory frequency space:** spontaneous (baseline), slow paced (0.1 Hz), fast paced (0.2 Hz). 0.1 Hz is near the resonance frequency that maximizes respiratory sinus arrhythmia in many adults, which gives the PPG breath-modulation signal the best chance to be visible.
- **One 90 s artifact-creation block** deliberately produces the EMG/EOG contamination categories most relevant to the Muse montage: bilateral anterior temporalis (TP9/AF7 and TP10/AF8 contamination), frontalis/corrugator (AF7/AF8 contamination), and the full ocular suite. Each cue is timestamped so analysts can train denoisers against labeled artifact epochs.
- **Eyes-closed repeats** of jaw clench and brow furrow in the artifact block let the analyst dissociate ocular artifact (which largely vanishes eyes-closed) from muscle artifact (which does not).

## Counterbalancing

With 5 participants and 2 device orders, we can only approximate counterbalancing. Default scheme: alternate device order across participants (P001 → Muse 2 first; P002 → Athena first; P003 → Muse 2 first; etc.). The app records each participant's order but does not enforce a specific assignment — the operator chooses at intake.

## What the marker stream is for

Every event the analyst will want to epoch against — block boundaries, individual inhale/exhale onsets, individual artifact-task onsets — is emitted as an LSL string marker on a single outlet, with the timestamp **anchored to the audio cue's first sample reaching the DAC**, not to wall clock. The marker stream is captured in the same XDF as the Muse and CGX streams, so post-hoc alignment is exact.

A CSV mirror of every marker is written locally as redundancy — if Lab Recorder crashes mid-session, the CSV is authoritative.

### Why sample-anchored markers matter

If the app schedules cues via `time.sleep(2.5)` in a loop, the actual audio onset drifts by tens to hundreds of milliseconds across a 5-minute block — dominated by Python GC pauses, OS scheduling, and audio buffer latency. For Aim 1 (HEP timing) that drift is unacceptable; for Aim 2 (per-breath estimation) it muddies the ground truth. PortAudio exposes the time at which the next audio buffer will reach the DAC; we capture this once at stream start, convert it to the LSL clock, and derive every cue's marker timestamp from the buffer math (`t0 + onset_sample / sample_rate`). This pattern is borrowed from the SuperChillsOddball `ToneScheduler`.

## Two-pass architecture rationale

A device-swap mid-visit introduces two operational hazards:

1. **Lab Recorder forgets to start a new file.** Mitigated by an explicit checkbox in the Pass B pre-launch checklist that the RA must tick.
2. **LSL stream pollution across passes.** If the app's marker outlet stays open across the swap, Lab Recorder may discover it in either or both XDF files in confusing ways. Mitigated by *closing* the outlet at the end of Pass A and *re-opening* with a new `source_id` for Pass B. The two XDF files are then unambiguous.

The trade-off for running the same person twice is order effects (e.g., calmer in Pass B because they've been resting for an hour). This is acknowledged and not corrected for — with N=5 it's a known limitation, documented in the visit-level metadata.

## What this app does NOT do

- **No real-time signal display.** Lab Recorder is the recorder; this app is the experimental scaffolding and marker source.
- **No analysis.** `scripts/verify_xdf.py` audits that markers landed correctly; everything else is post-hoc.
- **No EEG processing libraries.** The signal path uses only `numpy` + `sounddevice` + `pylsl`, plus `bleak` for the headset bridge.

## Headset bridge

`python -m muse_proxy.bridge` (`src/muse_proxy/bridge/`) is a companion process
that connects directly to one Muse 2 or Muse S Athena over BLE and publishes
LSL streams; it replaces muselsl and the OpenMuse-.txt relay used in the 2026
pilot. Dropped packets are NaN (never zero), all timestamps derive from device
time, and every raw notification is logged. Protocol notes, the 2026 failure
analysis and the hardware bring-up checklist are in `docs/BLE_BRIDGE.md`. The
Athena decoder (`athena_decode.py`) and synth (`athena_synth.py`) are vendored
verbatim from the IACS relay — don't restyle them.

## Operational defaults

See `config/session_defaults.yaml` for the full set. Notable choices:

- **Pre-session settle:** 5 min for Pass A, 2 min for Pass B (RA-skippable). SOW says "several minutes."
- **Between-block settle:** participant-paced. The screen says "Take a moment. Press SPACE when ready." No timer.
- **Inter-pass break:** 5 min default (RA-skippable). Bathroom, stretch, water.
- **Volume check:** 1 kHz sinusoid, 3 s loop, RA confirms participant is comfortable.
- **Artifact block:** 13 cues over 90 s; see `artifact_block.cues` in the YAML.
- **TTS voice:** `en-US-AriaNeural` (Microsoft Edge TTS, neural). Rendered once at install.

## Related IACS projects

- [`Jazzo`](https://github.com/Institute-for-Advanced-Consciousness/Jazzo) — SMF EEG study; reference for the pre-launch checklist UI and the CSV marker mirror.
- [`SuperChillsOddball`](https://github.com/Institute-for-Advanced-Consciousness/SuperChillsOddball) — 2×2 chills/oddball study; closest template for everything else.
- [`ChillsFrissonDeviceDemo`](https://github.com/Institute-for-Advanced-Consciousness/ChillsFrissonDeviceDemo) — earliest stage-based-Tk-pattern reference.

## Contact

PI / author: Nicco Reggente, Ph.D. (IACS).
