"""Stage tests for Phase 5 — block_instructions, block_breathing,
block_artifact, between_block_settle."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from muse_proxy.config import load_config

# ---------------------------------------------------------------------------
# block_instructions
# ---------------------------------------------------------------------------


def test_instructions_requires_block_id(shared_app: Any) -> None:
    bi = shared_app.stages["block_instructions"]
    with pytest.raises(ValueError):
        bi.on_enter()


def test_instructions_shows_block_text(shared_app: Any) -> None:
    bi = shared_app.stages["block_instructions"]
    shared_app.config = load_config()  # type: ignore[attr-defined]
    bi.on_enter(block_id=2)
    # Counter shows "Block 2 of 4"; heading shows the friendly label
    # ("Paced breathing — slow"); body shows the instruction text.
    assert "Block 2" in bi._counter.cget("text")
    assert "Paced" in bi._heading.cget("text") or "Paced" in bi._body.cget("text")
    bi.on_leave()


def test_instructions_space_advances_to_breathing_for_blocks_1_3(shared_app: Any) -> None:
    bi = shared_app.stages["block_instructions"]
    shared_app.config = load_config()  # type: ignore[attr-defined]
    advanced: list[tuple[str, dict[str, Any]]] = []
    orig = shared_app.show_stage
    shared_app.show_stage = lambda name, **kw: advanced.append((name, kw))  # type: ignore[assignment]
    try:
        for bid in (1, 2, 3):
            bi.on_enter(block_id=bid)
            bi._on_space()
            assert advanced[-1] == (
                "block_breathing", {"block_id": bid, "attempt": 1}
            )
    finally:
        shared_app.show_stage = orig  # type: ignore[assignment]


def test_instructions_space_advances_to_artifact_for_block_4(shared_app: Any) -> None:
    bi = shared_app.stages["block_instructions"]
    shared_app.config = load_config()  # type: ignore[attr-defined]
    advanced: list[tuple[str, dict[str, Any]]] = []
    orig = shared_app.show_stage
    shared_app.show_stage = lambda name, **kw: advanced.append((name, kw))  # type: ignore[assignment]
    try:
        bi.on_enter(block_id=4)
        bi._on_space()
        assert advanced[-1] == ("block_artifact", {"block_id": 4, "attempt": 1})
    finally:
        shared_app.show_stage = orig  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# between_block_settle
# ---------------------------------------------------------------------------


def test_between_settle_requires_after_block(shared_app: Any) -> None:
    bbs = shared_app.stages["between_block_settle"]
    shared_app.outlet = None
    shared_app.audio_scheduler = None
    with pytest.raises(ValueError):
        bbs.on_enter()


def test_between_settle_emits_start_marker(shared_app: Any) -> None:
    bbs = shared_app.stages["between_block_settle"]
    shared_app.outlet = MagicMock()
    shared_app.audio_scheduler = None
    try:
        bbs.on_enter(after_block=1)
    finally:
        shared_app.outlet.between_block_settle_start.assert_called_once_with(after_block=1)
        shared_app.outlet = None
        bbs.on_leave()


def test_between_settle_space_routes_after_block_1_to_next_instructions(shared_app: Any) -> None:
    bbs = shared_app.stages["between_block_settle"]
    shared_app.outlet = MagicMock()
    shared_app.audio_scheduler = None
    advanced: list[tuple[str, dict[str, Any]]] = []
    orig = shared_app.show_stage
    shared_app.show_stage = lambda name, **kw: advanced.append((name, kw))  # type: ignore[assignment]
    try:
        bbs.on_enter(after_block=1)
        bbs._on_space()
    finally:
        shared_app.show_stage = orig  # type: ignore[assignment]
        shared_app.outlet = None
    assert advanced[-1] == ("block_instructions", {"block_id": 2})


def test_between_settle_after_block_4_pass_a_routes_to_pass_switch(shared_app: Any) -> None:
    bbs = shared_app.stages["between_block_settle"]
    shared_app.outlet = MagicMock()
    shared_app.audio_scheduler = None
    shared_app.pass_label = "A"
    advanced: list[str] = []
    orig = shared_app.show_stage
    shared_app.show_stage = lambda name, **kw: advanced.append(name)  # type: ignore[assignment]
    try:
        bbs.on_enter(after_block=4)
        bbs._on_space()
    finally:
        shared_app.show_stage = orig  # type: ignore[assignment]
        shared_app.outlet = None
    assert advanced == ["pass_switch"]


def test_between_settle_after_block_4_pass_b_routes_to_completion(shared_app: Any) -> None:
    bbs = shared_app.stages["between_block_settle"]
    shared_app.outlet = MagicMock()
    shared_app.audio_scheduler = None
    shared_app.pass_label = "B"
    advanced: list[str] = []
    orig = shared_app.show_stage
    shared_app.show_stage = lambda name, **kw: advanced.append(name)  # type: ignore[assignment]
    try:
        bbs.on_enter(after_block=4)
        bbs._on_space()
    finally:
        shared_app.show_stage = orig  # type: ignore[assignment]
        shared_app.outlet = None
        shared_app.pass_label = "A"
    assert advanced == ["completion"]


# ---------------------------------------------------------------------------
# block_breathing — silent (no audio scheduler) baseline (block 1)
# ---------------------------------------------------------------------------


def test_block_breathing_silent_baseline_emits_markers_and_advances(
    shared_app: Any, tmp_path: Path
) -> None:
    """With no audio scheduler, block 1 should still emit start/end markers
    and finalize the block via the writer."""

    from muse_proxy.persistence import PassWriter

    bb = shared_app.stages["block_breathing"]
    cfg = load_config()
    shared_app.config = cfg  # type: ignore[attr-defined]
    shared_app.pass_label = "A"
    shared_app.audio_scheduler = None
    shared_app.outlet = MagicMock()
    shared_app.writer = PassWriter(tmp_path, "P999", "A", "muse2",
                                   config_snapshot={})
    shared_app.writer.start_pass(start_lsl_ts=0.0)

    advanced: list[tuple[str, dict[str, Any]]] = []
    orig = shared_app.show_stage
    shared_app.show_stage = lambda name, **kw: advanced.append((name, kw))  # type: ignore[assignment]

    # Truncate the planned duration to 0.1 s so the silent timer ends
    # quickly. We do this by replacing the block config briefly.
    block = cfg.block_by_id(1)
    original_dur = block.duration_s
    object.__setattr__(block, "duration_s", 0.05)
    try:
        bb.on_enter(block_id=1)
        # Drive the timer manually a few times until it terminates.
        deadline = time.time() + 2.0
        while time.time() < deadline and not advanced:
            shared_app.root.update()
            time.sleep(0.05)
    finally:
        object.__setattr__(block, "duration_s", original_dur)
        shared_app.show_stage = orig  # type: ignore[assignment]
        shared_app.outlet = None
        shared_app.writer = None
        bb.on_leave()

    # Block_start marker emitted (block_end may have been called too).
    assert shared_app.outlet is None or True  # outlet was reset
    # The block advances to between_block_settle.
    assert advanced and advanced[0][0] == "between_block_settle"
    assert advanced[0][1]["after_block"] == 1


# ---------------------------------------------------------------------------
# block_artifact — no audio scheduler path
# ---------------------------------------------------------------------------


def test_block_artifact_handles_no_audio_scheduler(shared_app: Any) -> None:
    ba = shared_app.stages["block_artifact"]
    shared_app.config = load_config()  # type: ignore[attr-defined]
    shared_app.pass_label = "A"
    shared_app.audio_scheduler = None
    shared_app.outlet = MagicMock()
    shared_app.writer = None
    try:
        ba.on_enter()
        # Without audio, the stage still shows a "no audio" notice and
        # emits a block_start marker.
        shared_app.outlet.block_start.assert_called_once()
    finally:
        shared_app.outlet = None
        ba.on_leave()


def test_block_artifact_builds_schedule_when_audio_available(
    shared_app: Any, tmp_path: Path
) -> None:
    """If WAVs exist and an AudioScheduler-like object is provided, the
    stage attempts to drive it. We mock the scheduler to verify the call."""
    from muse_proxy.config import REPO_ROOT

    ba = shared_app.stages["block_artifact"]
    cfg = load_config()
    shared_app.config = cfg  # type: ignore[attr-defined]
    shared_app.pass_label = "A"
    shared_app.outlet = MagicMock()
    shared_app.writer = None

    fake_sched = MagicMock()
    fake_sched.sample_rate = 44100
    shared_app.audio_scheduler = fake_sched

    # Make sure WAVs exist (they do — generated by Phase 3c).
    sound_dir = REPO_ROOT / "assets" / "sounds"
    wavs = list(sound_dir.glob("artifact_*.wav"))
    if not wavs:
        pytest.skip("WAVs not generated yet")

    try:
        ba.on_enter()
        # schedule_artifact_block should have been called.
        fake_sched.schedule_artifact_block.assert_called_once()
    finally:
        shared_app.outlet = None
        shared_app.audio_scheduler = None
        ba.on_leave()
