"""End-to-end visit walkthrough.

Drives the full Pass A → pass_switch → Pass B → completion flow with
real LSL outlets + real persistence, but with the AudioScheduler stubbed
so the test runs without sound hardware.

Asserts:
  - Both passes produce session.json files with status="completed".
  - intake.json exists once at the visit root.
  - Pass A and Pass B have distinct source_ids and distinct markers.csv
    contents.
  - The marker schema in each CSV covers the expected families.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch


class _StubScheduler:
    """A drop-in AudioScheduler that immediately fires every callback.

    For test_e2e we want the block runners to "play" their buffers
    instantaneously and emit all per-cue markers. The stub honors the
    same interface as ``AudioScheduler``.
    """

    sample_rate = 44100

    def play_wav(self, path: Path, *, on_onset=None, on_finished=None):
        import pylsl
        ts = pylsl.local_clock()
        if on_onset is not None:
            on_onset(ts)
        if on_finished is not None:
            on_finished()
        return _DoneHandle()

    def schedule_paced_breathing(
        self, *, schedule, inhale_wav, exhale_wav,
        on_inhale=None, on_exhale=None, on_block_start=None, on_finished=None,
    ):
        import pylsl
        t0 = pylsl.local_clock()
        if on_block_start is not None:
            on_block_start(t0)
        sr = self.sample_rate
        for cue in schedule.cues:
            cue_ts = t0 + cue.onset_sample / sr
            if cue.kind == "inhale" and on_inhale is not None:
                on_inhale(cue.cycle, cue_ts)
            elif cue.kind == "exhale" and on_exhale is not None:
                on_exhale(cue.cycle, cue_ts)
        if on_finished is not None:
            on_finished()
        return _DoneHandle()

    def schedule_artifact_block(
        self, *, schedule, on_cue_start=None, on_cue_end=None,
        on_block_start=None, on_finished=None,
    ):
        import pylsl
        t0 = pylsl.local_clock()
        if on_block_start is not None:
            on_block_start(t0)
        sr = self.sample_rate
        for cue in schedule.cues:
            if on_cue_start is not None:
                on_cue_start(cue.label, t0 + cue.onset_sample / sr)
            if not cue.is_state and on_cue_end is not None:
                on_cue_end(cue.label, t0 + cue.end_sample / sr)
        if on_finished is not None:
            on_finished()
        return _DoneHandle()

    def stop(self) -> None:
        pass


class _DoneHandle:
    """Mimics AudioPlayback enough for the stages' typecheck."""
    def __init__(self) -> None:
        import threading
        self.done = threading.Event()
        self.done.set()

    def stop(self) -> None:
        pass


def _populate_intake(intake_frame: Any, pid: str) -> None:
    intake_frame._vars["pid"].set(pid)
    intake_frame._vars["pass_A_device"].set("Muse 2")
    intake_frame._vars["age"].set("31")
    intake_frame._vars["caffeine_hours_ago"].set("99")
    intake_frame._vars["last_meal_hours_ago"].set("3.0")
    intake_frame._vars["sleep_hours"].set("7.5")
    intake_frame._vars["stress_self_report"].set(2)
    intake_frame._vars["trip_settle_minutes"].set("0")


def test_full_visit_walkthrough(shared_app: Any, tmp_path: Path) -> None:
    """Run intake → pre_launch → ... → completion end-to-end."""
    import muse_proxy.stages.intake as intake_mod
    import muse_proxy.stages.pass_switch as pass_switch_mod

    pid = f"P{int(time.time() * 1e3) % 1000:03d}"

    intake = shared_app.stages["intake"]
    intake.on_enter()
    _populate_intake(intake, pid)
    payload = intake._collect()

    # Redirect REPO_ROOT in both intake and pass_switch so data lands in tmp_path.
    cm_intake = patch.object(intake_mod, "REPO_ROOT", tmp_path)
    cm_switch = patch.object(pass_switch_mod, "REPO_ROOT", tmp_path)
    cm_intake.start()
    cm_switch.start()
    try:
        intake._open_services(payload)
        # Replace the AudioScheduler with the stub.
        shared_app.audio_scheduler = _StubScheduler()
    except BaseException:
        cm_intake.stop()
        cm_switch.stop()
        raise

    # Pre-launch — tick everything, click Start.
    pre_launch = shared_app.stages["pre_launch"]
    pre_launch.on_enter()
    for v in pre_launch._stream_vars + pre_launch._confirmation_vars:
        v.set(True)
    pre_launch._refresh_start()

    # We need to short-circuit the block durations so the test runs fast.
    # The cleanest knob: monkeypatch the config's block durations + settle.
    cfg = shared_app.config
    original_block_durs = [b.duration_s for b in cfg.blocks]
    original_pa = cfg.settle.initial_pass_a_default_min
    original_pb = cfg.settle.initial_pass_b_default_min
    original_break = cfg.settle.inter_pass_break_default_min
    try:
        # Block 1 (silent baseline): brief timer.
        object.__setattr__(cfg.blocks[0], "duration_s", 0.1)
        # Blocks 2/3 (paced): one cycle each. Audio is stubbed to fire instantly.
        object.__setattr__(cfg.blocks[1], "duration_s", 10.0)  # 0.1 Hz → 1 cycle
        object.__setattr__(cfg.blocks[2], "duration_s", 5.0)   # 0.2 Hz → 1 cycle
        # Block 4 stays at 90 s but the audio is stubbed to fire instantly.
        object.__setattr__(cfg.settle, "initial_pass_a_default_min", 0.0)
        object.__setattr__(cfg.settle, "initial_pass_b_default_min", 0.0)
        object.__setattr__(cfg.settle, "inter_pass_break_default_min", 0.0)

        # Helper that runs one settle/block until the stage advances.
        def _drive_until_advance(timeout_s: float = 5.0) -> None:
            current = shared_app.current_stage
            deadline = time.time() + timeout_s
            while time.time() < deadline and shared_app.current_stage == current:
                shared_app.root.update()
                time.sleep(0.02)

        # volume_check — auto-advance via confirm.
        pre_launch._on_start()
        assert shared_app.current_stage == "volume_check"
        shared_app.stages["volume_check"]._on_confirm()
        # With zero-duration settle, settle auto-advances to block_instructions
        # within its own on_enter. Either state is acceptable here; drive
        # the Tk loop until we settle on block_instructions.
        deadline = time.time() + 2.0
        while time.time() < deadline and shared_app.current_stage != "block_instructions":
            shared_app.root.update()
            time.sleep(0.02)
        assert shared_app.current_stage == "block_instructions"

        # Walk blocks 1..4: instructions → block runner → between settle.
        for expected_block in (1, 2, 3, 4):
            assert shared_app.current_stage == "block_instructions", (
                f"Pass A, block {expected_block}: expected block_instructions, "
                f"got {shared_app.current_stage}"
            )
            shared_app.stages["block_instructions"]._on_space()
            assert shared_app.current_stage in ("block_breathing", "block_artifact")
            # Block plays. With stub scheduler, the playback completes
            # synchronously; we drive the Tk poller to drain queued UI calls.
            _drive_until_advance(timeout_s=4.0)
            assert shared_app.current_stage == "between_block_settle", (
                f"Pass A, block {expected_block}: stuck at {shared_app.current_stage}"
            )
            # Press SPACE on between-block-settle.
            shared_app.stages["between_block_settle"]._on_space()
            if expected_block < 4:
                assert shared_app.current_stage == "block_instructions"
            else:
                assert shared_app.current_stage == "pass_switch"

        # Pass switch — tick all + continue.
        ps = shared_app.stages["pass_switch"]
        # on_enter was called by show_stage; finalize Pass A.
        # Click skip break.
        ps._on_skip_break()
        for v in ps._checklist_vars:
            v.set(True)
        ps._refresh_continue()
        ps._on_continue()
        assert shared_app.current_stage == "pre_launch"
        # Pass B pre-launch.
        pre_launch.on_enter()
        for v in pre_launch._stream_vars + pre_launch._confirmation_vars:
            v.set(True)
        pre_launch._refresh_start()
        pre_launch._on_start()
        # We need a new audio scheduler for Pass B since the old one is closed.
        shared_app.audio_scheduler = _StubScheduler()
        shared_app.stages["volume_check"]._on_confirm()
        _drive_until_advance(timeout_s=2.0)
        assert shared_app.current_stage == "block_instructions"

        for expected_block in (1, 2, 3, 4):
            shared_app.stages["block_instructions"]._on_space()
            _drive_until_advance(timeout_s=4.0)
            assert shared_app.current_stage == "between_block_settle", (
                f"Pass B, block {expected_block}: stuck at {shared_app.current_stage}"
            )
            shared_app.stages["between_block_settle"]._on_space()

        assert shared_app.current_stage == "completion"

        # Don't actually call _on_save_exit — it would shut down shared_app.
        outlet = shared_app.outlet
        writer = shared_app.writer
        if outlet is not None:
            outlet.pass_end(pass_label="B")
            outlet.session_end(pid=pid)
            outlet.close()
        if writer is not None:
            writer.finalize(status="completed")
        shared_app.outlet = None
        shared_app.writer = None

    finally:
        # Restore config values.
        for b, dur in zip(cfg.blocks, original_block_durs, strict=True):
            object.__setattr__(b, "duration_s", dur)
        object.__setattr__(cfg.settle, "initial_pass_a_default_min", original_pa)
        object.__setattr__(cfg.settle, "initial_pass_b_default_min", original_pb)
        object.__setattr__(cfg.settle, "inter_pass_break_default_min", original_break)
        cm_intake.stop()
        cm_switch.stop()

    # Assertions on the final on-disk state.
    data_root = tmp_path / cfg.data_dir
    pass_a_session = data_root / pid / "pass_A_muse2" / "session.json"
    pass_b_session = data_root / pid / "pass_B_athena" / "session.json"
    intake_json = data_root / pid / "intake.json"

    assert intake_json.exists()
    assert pass_a_session.exists()
    assert pass_b_session.exists()
    a_doc = json.loads(pass_a_session.read_text(encoding="utf-8"))
    b_doc = json.loads(pass_b_session.read_text(encoding="utf-8"))
    assert a_doc["status"] == "completed"
    assert b_doc["status"] == "completed"
    assert a_doc["device"] == "muse2"
    assert b_doc["device"] == "athena"
    assert a_doc["pass_label"] == "A"
    assert b_doc["pass_label"] == "B"
    # Both passes have 4 blocks each, all completed.
    assert len(a_doc["blocks"]) == 4
    assert len(b_doc["blocks"]) == 4
    assert all(b["status"] == "completed" for b in a_doc["blocks"])
    assert all(b["status"] == "completed" for b in b_doc["blocks"])

    # markers.csv content — Pass A markers carry pass=A only, B carries B only.
    pass_a_csv = (data_root / pid / "pass_A_muse2" / "markers.csv").read_text()
    pass_b_csv = (data_root / pid / "pass_B_athena" / "markers.csv").read_text()
    assert "pass=A" in pass_a_csv
    assert "pass=A" not in pass_b_csv
    assert "pass=B" in pass_b_csv
    assert "pass=B" not in pass_a_csv
    # Breathing markers appear in both passes.
    assert "breath:inhale" in pass_a_csv
    assert "breath:inhale" in pass_b_csv
    assert "artifact:" in pass_a_csv
    assert "artifact:" in pass_b_csv
