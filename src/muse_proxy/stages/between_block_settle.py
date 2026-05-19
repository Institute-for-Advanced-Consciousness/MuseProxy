"""Participant-paced inter-block rest.

"Take a moment. Press SPACE when ready." Emits
``between_block_settle:start`` and
``between_block_settle:end:duration_s=N``.

After block 4: routes to ``pass_switch`` (Pass A) or ``completion``
(Pass B), so this frame also acts as the per-pass terminator.
"""

from __future__ import annotations

import logging
import tkinter as tk
from typing import Any

import pylsl

from .. import theme
from ._base import StageFrame
from .stream_recovery import advance_to_next

logger = logging.getLogger(__name__)


class BetweenBlockSettleFrame(StageFrame):
    NAME = "between_block_settle"

    def build(self) -> None:
        self.configure(bg=theme.TASK_BG)

        # Top spacer.
        tk.Frame(self, bg=theme.TASK_BG, height=100).pack(side="top")

        tk.Label(
            self, text="Take a moment.",
            bg=theme.TASK_BG, fg=theme.TASK_TEXT, font=theme.FONT_HEADING,
        ).pack(side="top", pady=(0, 24))

        tk.Label(
            self, text="Rest as long as you'd like.",
            bg=theme.TASK_BG, fg="#bbbbbb", font=theme.FONT_SUBHEADING,
        ).pack(side="top", pady=4)

        # Bottom hint.
        tk.Label(
            self, text="Press SPACE when ready",
            bg=theme.TASK_BG, fg="#888888", font=theme.FONT_BODY,
        ).pack(side="bottom", pady=(0, 60))

        self._after_block: int | None = None
        self._start_lsl: float | None = None
        self._attempt: int = 1
        self._health_report: dict[str, str] = {}

    def on_enter(self, **payload: Any) -> None:
        after_block = payload.get("after_block")
        if after_block is None:
            raise ValueError("between_block_settle requires after_block payload")
        self._after_block = int(after_block)
        self._attempt = int(payload.get("attempt", 1))
        self._health_report = dict(payload.get("health_report", {}) or {})
        self._start_lsl = pylsl.local_clock()
        outlet = self.app.outlet
        if outlet is not None:
            try:
                outlet.between_block_settle_start(after_block=self._after_block)
            except Exception:  # noqa: BLE001
                logger.exception("between_block_settle_start marker failed")
        self._play_optional_tts()
        self.bind_all("<space>", self._on_space)

    def on_leave(self) -> None:
        try:
            self.unbind_all("<space>")
        except tk.TclError:
            pass

    def _play_optional_tts(self) -> None:
        from ..config import REPO_ROOT
        sched = self.app.audio_scheduler
        wav = REPO_ROOT / "assets" / "sounds" / "take_a_moment.wav"
        if sched is None or not wav.exists():
            return
        try:
            sched.play_wav(wav)
        except Exception:  # noqa: BLE001
            logger.exception("take_a_moment TTS failed")

    def _on_space(self, _event: tk.Event | None = None) -> None:
        if self._after_block is None:
            return
        end_lsl = pylsl.local_clock()
        duration_s = end_lsl - (self._start_lsl or end_lsl)
        outlet = self.app.outlet
        if outlet is not None:
            try:
                outlet.between_block_settle_end(
                    after_block=self._after_block, duration_s=duration_s
                )
            except Exception:  # noqa: BLE001
                logger.exception("between_block_settle_end marker failed")
        try:
            self.unbind_all("<space>")
        except tk.TclError:
            pass
        self._route()

    def _route(self) -> None:
        if self._after_block is None:
            return
        # A stream that went SILENT/MISSING during the block just finished
        # diverts to the RA-facing recovery screen before moving on.
        degraded = any(v != "alive" for v in self._health_report.values())
        if degraded:
            logger.warning(
                "block %s degraded streams: %s — routing to stream_recovery",
                self._after_block, self._health_report,
            )
            self.app.show_stage(
                "stream_recovery",
                block_id=self._after_block,
                attempt=self._attempt,
                report=self._health_report,
            )
            return
        advance_to_next(self.app, self._after_block)
