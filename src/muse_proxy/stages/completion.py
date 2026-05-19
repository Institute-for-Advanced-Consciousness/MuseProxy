"""Visit-end summary + Save & Exit.

Shows both passes' status and on click:
  - emits ``session_end`` on the Pass B outlet
  - calls ``writer.finalize(status='completed')`` for Pass B
  - closes the Pass B outlet
  - shuts down the App
"""

from __future__ import annotations

import logging
import tkinter as tk
from typing import Any

from .. import theme
from ._base import StageFrame

logger = logging.getLogger(__name__)


class CompletionFrame(StageFrame):
    NAME = "completion"

    def build(self) -> None:
        self.configure(bg=theme.CHROME_BG)

        # Pinned Save & Exit at the bottom.
        self._save_exit_btn = theme.primary_button(
            self, "Save & Exit", self._on_save_exit,
            width=22, font=theme.FONT_BUTTON_LARGE, padx=24, pady=12,
        )
        self._save_exit_btn.pack(side="bottom", pady=(6, 28))

        # Top: big celebration heading + visit summary panel.
        tk.Frame(self, bg=theme.CHROME_BG, height=60).pack(side="top")
        theme.heading(self, "Visit Complete").pack(side="top", pady=(0, 8))
        theme.muted(
            self, "Both passes recorded. Click Save & Exit to finalize."
        ).pack(side="top", pady=(0, 20))

        panel = theme.panel(self, padx=24, pady=20)
        panel.pack(side="top", padx=40)
        theme.panel_header(panel, "VISIT SUMMARY").pack(fill="x", pady=(0, 8))
        self._summary = tk.Label(
            panel, text="",
            bg=theme.SURFACE, fg=theme.TEXT,
            font=theme.FONT_BODY, justify="left",
        )
        self._summary.pack(pady=(2, 4))

        # Footer hint.
        theme.muted(
            self,
            "After exit: run scripts\\verify_xdf.py on each XDF to confirm marker streams.",
        ).pack(side="bottom", pady=(0, 6))

    def on_enter(self, **payload: Any) -> None:
        from ..stream_health import block_health_summary

        pid = self.app.participant_id or "?"
        pa = self.app.pass_a_device or "?"
        pb = self.app.pass_b_device or "?"
        text = (
            f"  Participant:   {pid}\n"
            f"  Pass A device: {_pretty(pa)}   →  saved\n"
            f"  Pass B device: {_pretty(pb)}   →  ready to finalize"
        )
        blocks = list(self.app.writer.blocks) if self.app.writer is not None else []
        lines = block_health_summary(blocks)
        if lines:
            text += "\n\n  Pass B stream coverage:\n"
            text += "\n".join(f"    {ln}" for ln in lines)
        self._summary.configure(text=text)

    def _on_save_exit(self) -> None:
        outlet = self.app.outlet
        writer = self.app.writer
        pid = self.app.participant_id or "?"
        if outlet is not None:
            try:
                outlet.pass_end(pass_label="B")
                outlet.session_end(pid=pid)
            except Exception:  # noqa: BLE001
                logger.exception("pass_end / session_end failed")
            try:
                outlet.close()
            except Exception:  # noqa: BLE001
                logger.exception("outlet.close failed")
        if writer is not None:
            try:
                writer.finalize(status="completed")
            except Exception:  # noqa: BLE001
                logger.exception("writer.finalize(B) failed")
        if self.app.monitor is not None:
            try:
                self.app.monitor.stop()
            except Exception:  # noqa: BLE001
                logger.exception("monitor.stop failed")
            self.app.monitor = None
        self.app.outlet = None
        self.app.writer = None
        self.app.shutdown()


def _pretty(device: str) -> str:
    return {"muse2": "Muse 2", "athena": "Muse Athena"}.get(device, device)
