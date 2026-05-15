# Kickoff Prompt for Claude Code

> Paste the block below as your first message in a fresh Claude Code session opened at the repo root. Make sure all the seed files (PROJECT_PLAN.md, CLAUDE.md, pyproject.toml, config/session_defaults.yaml, INSTALL.bat, launch.bat, README.md, .gitignore) are already in place when you do.

---

You are building **MuseProxy**, a Windows-only, double-click-launch GUI experiment runner for the IACS *Muse Proxy Metrics and Denoising Study*. The study runs 5 participants through a 4-block protocol (baseline + 0.1 Hz paced + 0.2 Hz paced breathing + an artifact-creation block) **twice per visit** — once wearing a Muse 2, once wearing a Muse Athena — with an in-app device swap between passes. Each pass produces its own Lab Recorder XDF; this app emits the LSL marker stream that Lab Recorder picks up alongside the Muse and CGX AIM-II streams.

## Read these files in order before writing any code

1. `PROJECT_PLAN.md` — authoritative build spec. Treat as a contract. Pay particular attention to **§ 3 (two-pass architecture)** and **§ 10 (sample-anchored audio markers)** — these are the two pieces most likely to be done wrong if you pattern-match too quickly to the reference repos.
2. `CLAUDE.md` — scientific brief and design rationale. Read for context on *why* the marker timing has to be tight.
3. `config/session_defaults.yaml` — the full default config. Your `config.py` pydantic models must match this schema exactly.
4. `pyproject.toml` — dependencies are locked. Do not add anything not already listed (and ask before doing so).

## Then study these two reference repositories

Both are public IACS projects. Browse the code on GitHub before implementing:

- **SuperChillsOddball** — <https://github.com/Institute-for-Advanced-Consciousness/SuperChillsOddball>
  Closest template. Mirror its file layout, stage controller pattern, sample-anchored audio markers (`ToneScheduler`), atomic persistence, `--self-check` mode, and `verify_xdf.py` approach. The README has the architecture cheatsheet you want.

- **Jazzo** — <https://github.com/Institute-for-Advanced-Consciousness/Jazzo>
  Mirror its pre-launch checklist UI, CSV mirror of LSL markers, IACS color scheme (`PURPLE = "#5d5179"`, white, black), and the thread-safe UI update queue (`_ui_queue` polled by `_poll_ui_queue`).

Both use Python 3.11+, `uv`, tkinter, `sounddevice`, `pylsl`, pydantic, PyYAML, pytest, ruff. **Do not deviate from this stack.**

## Build order

Follow `PROJECT_PLAN.md` § 16. Eight phases. After each phase, run:

```
uv run pytest
uv run ruff check src/ tests/ scripts/
uv run python -m muse_proxy.app --self-check
```

…and manually walk the GUI for any new stages. Commit after each phase with a clear message.

## Ground rules (also in PROJECT_PLAN.md § 17)

- **Ask, don't guess.** Surface ambiguities before implementing them. § 18 of the plan already lists three known opens — clear those with the user before phase 3.
- **Tests are not optional.** Target ≥ 150 tests at completion. Every module ships with its tests in the same commit.
- **The two-pass architecture is the most novel piece.** Write `test_two_pass.py` *early* (phase 2 if you can frame it, no later than the start of phase 6). Don't blur it with the SuperChillsOddball single-session model.
- **Sample-anchored audio markers are non-negotiable.** Every breath cue and every artifact cue uses the PortAudio `outputBufferDacTime` → `local_clock` mapping. No `time.sleep` on the timing-critical path. The whole study depends on this.
- **Windows only.** Test on Windows. Do not branch on macOS/Linux unless explicitly asked.
- **No silent failures.** Every IO/LSL/audio error is surfaced to both the GUI event log and stdout.

## Your first message back

After reading the plan + walking the reference repos, post:

1. A short summary of anything in `PROJECT_PLAN.md` that you'd push back on or that you find ambiguous. § 18 already lists three opens — those count.
2. The exact list of files you intend to create in **Phase 1 (scaffold)** with one-line descriptions.
3. Any package versions you want to pin tighter than what's in `pyproject.toml`.

Then wait for go-ahead before writing any phase-1 code.

---

Good luck. The bar is the test count and architectural cleanliness of SuperChillsOddball, plus correct handling of the two-pass-per-visit workflow that's specific to this study.
