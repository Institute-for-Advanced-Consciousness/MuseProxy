"""Pre-session settle timer.

Pass A default 5 min, Pass B default 2 min (configurable). RA-skippable
via a "Skip" button. Records elapsed time in the
``settle:end:duration_elapsed_s=N`` marker.
"""

from __future__ import annotations

import logging
import time
import tkinter as tk
from typing import Any

from .. import theme
from ._base import StageFrame

logger = logging.getLogger(__name__)


class SettleFrame(StageFrame):
    NAME = "settle"

    def build(self) -> None:
        self._tick_after: str | None = None
        self._start_ts: float | None = None
        self._planned_s: float = 0.0

        # Settle is task-mode: pure black, calming.
        self.configure(bg=theme.TASK_BG)

        # Pinned RA skip at the bottom.
        self._skip_btn = theme.secondary_button(
            self, "Skip (RA)", self._finish, width=12,
        )
        self._skip_btn.pack(side="bottom", pady=(0, 28))

        # Spacer top.
        tk.Frame(self, bg=theme.TASK_BG, height=120).pack(side="top")

        # Heading
        self._heading = tk.Label(
            self, text="Settle",
            bg=theme.TASK_BG, fg=theme.TASK_TEXT, font=theme.FONT_HEADING,
        )
        self._heading.pack(pady=(0, 20))

        # Body text (eyes-closed instruction).
        self._body = tk.Label(
            self,
            text=("Sit comfortably and close your eyes.\n"
                  "Rest until the timer reaches zero."),
            bg=theme.TASK_BG, fg=theme.TASK_TEXT, font=theme.FONT_SUBHEADING,
            justify="center",
        )
        self._body.pack(pady=10)

        # Big timer.
        self._timer_label = tk.Label(
            self, text="--:--",
            bg=theme.TASK_BG, fg=theme.TASK_TEXT, font=theme.FONT_TASK,
        )
        self._timer_label.pack(pady=40)

    def on_enter(self, **payload: Any) -> None:
        cfg = getattr(self.app, "config", None)
        if cfg is None:
            self._planned_s = 0.0
        else:
            mins = (
                cfg.settle.initial_pass_a_default_min
                if self.app.pass_label == "A"
                else cfg.settle.initial_pass_b_default_min
            )
            self._planned_s = mins * 60.0

        if self.app.outlet is not None:
            try:
                self.app.outlet.settle_start(
                    pass_label=self.app.pass_label,
                    duration_planned_s=self._planned_s,
                )
            except Exception:  # noqa: BLE001
                logger.exception("settle_start marker failed")

        self._start_ts = time.monotonic()
        self._tick()

    def on_leave(self) -> None:
        self._cancel_tick()

    def _tick(self) -> None:
        if self._start_ts is None:
            return
        elapsed = time.monotonic() - self._start_ts
        remaining = max(0.0, self._planned_s - elapsed)
        m, s = divmod(int(remaining), 60)
        self._timer_label.configure(text=f"{m:02d}:{s:02d}")
        if remaining <= 0:
            self._finish()
            return
        self._tick_after = self.after(250, self._tick)

    def _cancel_tick(self) -> None:
        if self._tick_after is not None:
            try:
                self.after_cancel(self._tick_after)
            except tk.TclError:
                pass
            self._tick_after = None

    def _finish(self) -> None:
        elapsed = 0.0
        if self._start_ts is not None:
            elapsed = time.monotonic() - self._start_ts
        if self.app.outlet is not None:
            try:
                self.app.outlet.settle_end(
                    pass_label=self.app.pass_label,
                    duration_elapsed_s=elapsed,
                )
            except Exception:  # noqa: BLE001
                logger.exception("settle_end marker failed")
        self._cancel_tick()
        self.app.show_stage("block_instructions", block_id=1)
