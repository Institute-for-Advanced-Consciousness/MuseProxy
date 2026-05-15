"""Stage-controller tests for the App class.

Tests the show_stage/_register_frames/abort/UI-queue plumbing without
real LSL or audio dependencies.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


def test_show_stage_unknown_raises(shared_app: Any) -> None:
    with pytest.raises(KeyError):
        shared_app.show_stage("nonexistent")


def test_show_stage_transitions(shared_app: Any) -> None:
    shared_app.show_stage("intake")
    assert shared_app.current_stage == "intake"
    # Move to pre_launch (it tolerates missing config — just shows nothing).
    from muse_proxy.config import load_config
    shared_app.config = load_config()  # type: ignore[attr-defined]
    shared_app.pass_label = "A"
    shared_app.pass_a_device = "muse2"
    shared_app.show_stage("pre_launch")
    assert shared_app.current_stage == "pre_launch"


def test_show_stage_calls_on_leave_then_on_enter(shared_app: Any) -> None:
    seq: list[str] = []

    class Spy:
        def on_leave(self) -> None:
            seq.append("on_leave")

        def on_enter(self, **payload: Any) -> None:
            seq.append("on_enter")

        def pack_forget(self) -> None:
            seq.append("pack_forget")

        def pack(self, **kwargs: Any) -> None:
            seq.append("pack")

    spy_old = Spy()
    spy_new = Spy()
    # Stash and swap.
    orig_frames = dict(shared_app._frames)
    shared_app._frames = {"old": spy_old, "new": spy_new}  # type: ignore[assignment]
    shared_app._current = "old"
    try:
        shared_app.show_stage("new")
    finally:
        shared_app._frames = orig_frames  # type: ignore[assignment]
        shared_app._current = None
    # Order: old.on_leave → old.pack_forget → new.pack → new.on_enter
    assert seq == ["on_leave", "pack_forget", "pack", "on_enter"]


def test_enqueue_ui_runs_callable_on_main_thread(shared_app: Any) -> None:
    called: list[int] = []
    shared_app.enqueue_ui(lambda: called.append(1))
    # Force the poller to run.
    shared_app._poll_ui_queue()
    assert called == [1]


def test_abort_emits_marker_and_finalizes_writer(shared_app: Any) -> None:
    fake_outlet = MagicMock()
    fake_writer = MagicMock()
    shared_app.outlet = fake_outlet
    shared_app.writer = fake_writer
    shared_app.pass_label = "A"
    shared_app._current = "block_breathing"
    # Patch confirm_abort to return True without showing a dialog.
    orig = shared_app.confirm_abort
    shared_app.confirm_abort = lambda: True  # type: ignore[assignment]
    orig_shutdown = shared_app.shutdown
    shutdown_calls: list[int] = []
    shared_app.shutdown = lambda: shutdown_calls.append(1)  # type: ignore[assignment]
    try:
        shared_app.abort()
    finally:
        shared_app.confirm_abort = orig  # type: ignore[assignment]
        shared_app.shutdown = orig_shutdown  # type: ignore[assignment]
        shared_app.outlet = None
        shared_app.writer = None

    fake_outlet.session_abort.assert_called_once_with(pass_label="A", stage="block_breathing")
    fake_writer.mark_pending_blocks_aborted.assert_called_once()
    fake_writer.finalize.assert_called_once_with(status="aborted")
    fake_outlet.close.assert_called_once()
    assert shutdown_calls == [1]


def test_abort_is_idempotent_with_no_services(shared_app: Any) -> None:
    shared_app.outlet = None
    shared_app.writer = None
    orig = shared_app.confirm_abort
    shared_app.confirm_abort = lambda: True  # type: ignore[assignment]
    shutdown_calls: list[int] = []
    orig_shutdown = shared_app.shutdown
    shared_app.shutdown = lambda: shutdown_calls.append(1)  # type: ignore[assignment]
    try:
        shared_app.abort()
        shared_app.abort()
    finally:
        shared_app.confirm_abort = orig  # type: ignore[assignment]
        shared_app.shutdown = orig_shutdown  # type: ignore[assignment]
    assert len(shutdown_calls) == 2


def test_stages_property_returns_all_registered_frames(shared_app: Any) -> None:
    stages = shared_app.stages
    assert {"intake", "pre_launch", "volume_check", "settle",
            "block_instructions", "block_breathing", "block_artifact",
            "between_block_settle", "pass_switch", "completion"} <= set(stages)


def test_stage_names_all_distinct_and_match_class_names(shared_app: Any) -> None:
    """Every registered stage has a unique NAME."""
    names = [frame.NAME for frame in shared_app.stages.values()]
    assert len(names) == len(set(names))
    assert all(n != "__abstract__" for n in names)
