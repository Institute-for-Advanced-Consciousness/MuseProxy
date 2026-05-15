"""Abort-from-every-stage tests.

Asserts that pressing Escape (or closing the window) at any stage
emits a ``session_abort`` marker, marks pending blocks aborted, writes a
PARTIAL_session.json + session.json(aborted), closes the outlet, and
shuts down — without raising. The MagicMock outlet/writer absorb the
calls so we can verify exact behavior.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

STAGES_THAT_CAN_BE_ABORTED = [
    "intake",
    "pre_launch",
    "volume_check",
    "settle",
    "block_instructions",
    "block_breathing",
    "block_artifact",
    "between_block_settle",
    "pass_switch",
    "completion",
]


@pytest.mark.parametrize("stage_name", STAGES_THAT_CAN_BE_ABORTED)
def test_abort_from_stage(shared_app: Any, stage_name: str) -> None:
    """Abort while on each stage; expect markers, writer finalize, shutdown."""
    fake_outlet = MagicMock()
    fake_writer = MagicMock()
    shared_app.outlet = fake_outlet
    shared_app.writer = fake_writer
    shared_app.pass_label = "A"
    shared_app._current = stage_name

    orig_confirm = shared_app.confirm_abort
    shared_app.confirm_abort = lambda: True  # type: ignore[assignment]
    orig_shutdown = shared_app.shutdown
    shutdowns: list[int] = []
    shared_app.shutdown = lambda: shutdowns.append(1)  # type: ignore[assignment]

    try:
        shared_app.abort()
    finally:
        shared_app.confirm_abort = orig_confirm  # type: ignore[assignment]
        shared_app.shutdown = orig_shutdown  # type: ignore[assignment]
        shared_app.outlet = None
        shared_app.writer = None

    fake_outlet.session_abort.assert_called_once_with(
        pass_label="A", stage=stage_name
    )
    fake_writer.mark_pending_blocks_aborted.assert_called_once()
    fake_writer.finalize.assert_called_once_with(status="aborted")
    fake_outlet.close.assert_called_once()
    assert shutdowns == [1]


def test_abort_during_pass_b_does_not_touch_pass_a_writer(shared_app: Any) -> None:
    """If abort fires during Pass B, only Pass B's writer is finalized."""
    fake_outlet = MagicMock()
    fake_writer_b = MagicMock()
    # Pass A's writer is already gone (set to None at pass_switch).
    shared_app.outlet = fake_outlet
    shared_app.writer = fake_writer_b
    shared_app.pass_label = "B"
    shared_app._current = "block_breathing"

    orig_confirm = shared_app.confirm_abort
    shared_app.confirm_abort = lambda: True  # type: ignore[assignment]
    orig_shutdown = shared_app.shutdown
    shared_app.shutdown = lambda: None  # type: ignore[assignment]
    try:
        shared_app.abort()
    finally:
        shared_app.confirm_abort = orig_confirm  # type: ignore[assignment]
        shared_app.shutdown = orig_shutdown  # type: ignore[assignment]
        shared_app.outlet = None
        shared_app.writer = None

    fake_outlet.session_abort.assert_called_once_with(
        pass_label="B", stage="block_breathing"
    )
    fake_writer_b.finalize.assert_called_once_with(status="aborted")


def test_abort_with_confirm_denied_does_nothing(shared_app: Any) -> None:
    fake_outlet = MagicMock()
    fake_writer = MagicMock()
    shared_app.outlet = fake_outlet
    shared_app.writer = fake_writer
    orig_confirm = shared_app.confirm_abort
    shared_app.confirm_abort = lambda: False  # type: ignore[assignment]
    try:
        shared_app._on_escape()
    finally:
        shared_app.confirm_abort = orig_confirm  # type: ignore[assignment]
        shared_app.outlet = None
        shared_app.writer = None
    fake_outlet.session_abort.assert_not_called()
    fake_writer.finalize.assert_not_called()


def test_abort_when_outlet_close_raises_still_finalizes(shared_app: Any) -> None:
    """Outlet.close raising should not prevent writer.finalize from running."""
    fake_outlet = MagicMock()
    fake_outlet.close.side_effect = RuntimeError("simulated close failure")
    fake_writer = MagicMock()
    shared_app.outlet = fake_outlet
    shared_app.writer = fake_writer
    orig_confirm = shared_app.confirm_abort
    shared_app.confirm_abort = lambda: True  # type: ignore[assignment]
    orig_shutdown = shared_app.shutdown
    shared_app.shutdown = lambda: None  # type: ignore[assignment]
    try:
        shared_app.abort()
    finally:
        shared_app.confirm_abort = orig_confirm  # type: ignore[assignment]
        shared_app.shutdown = orig_shutdown  # type: ignore[assignment]
        shared_app.outlet = None
        shared_app.writer = None
    fake_writer.finalize.assert_called_once_with(status="aborted")
