"""Volume check — loops a 1 kHz reference tone until the RA confirms.

Emits ``volume_check:start`` on entry and ``volume_check:end:level=ok``
on confirm. The tone is rendered at install time
(``assets/sounds/volume_check.wav``) by ``scripts/generate_audio.py``.
"""

from __future__ import annotations

import logging
import tkinter as tk
from typing import Any

from .. import theme
from ..config import REPO_ROOT
from ._base import StageFrame

logger = logging.getLogger(__name__)


class VolumeCheckFrame(StageFrame):
    NAME = "volume_check"

    def build(self) -> None:
        self.configure(bg=theme.CHROME_BG)

        # Bottom: Play again + Confirm pinned.
        btn_row = tk.Frame(self, bg=theme.CHROME_BG)
        btn_row.pack(side="bottom", pady=(6, 22))
        self._play_btn = theme.secondary_button(
            btn_row, "Play tone again", self._loop_once, width=22,
        )
        self._play_btn.pack(side="left", padx=10)
        self._confirm_btn = theme.primary_button(
            btn_row, "Confirm & Continue →", self._on_confirm,
            width=26, font=theme.FONT_BUTTON_LARGE, padx=20, pady=12,
        )
        self._confirm_btn.pack(side="left", padx=10)

        # Top: heading + instruction.
        theme.heading(self, "Volume Check").pack(side="top", pady=(28, 4))
        instr = theme.body(
            self,
            "A 1 kHz reference tone will loop until you click Confirm.\n"
            "Adjust Windows system volume so the participant can hear it "
            "clearly but comfortably.",
            wraplength=900, justify="center",
        )
        instr.configure(anchor="center", fg=theme.TEXT_MUTED)
        instr.pack(side="top", pady=(0, 24))
        self.register_wrappable(instr, ratio=0.75, min_px=500)

        # Status panel in the middle.
        status_panel = theme.panel(self, padx=24, pady=20)
        status_panel.pack(side="top", pady=10)
        theme.panel_header(status_panel, "TONE STATUS").pack(fill="x", pady=(0, 6))
        self._status = tk.Label(
            status_panel, text="not yet started",
            font=theme.FONT_SUBHEADING, fg=theme.ACCENT_LIGHT,
            bg=theme.SURFACE,
        )
        self._status.pack(pady=(2, 4))

        self._handle = None  # type: ignore[var-annotated]
        self._wav_path = REPO_ROOT / "assets" / "sounds" / "volume_check.wav"

    def on_enter(self, **payload: Any) -> None:
        if self.app.outlet is not None:
            try:
                self.app.outlet.volume_check_start()
            except Exception:  # noqa: BLE001
                logger.exception("volume_check_start marker failed")
        self._loop_once()

    def on_leave(self) -> None:
        self._stop_playback()

    def _loop_once(self) -> None:
        sched = self.app.audio_scheduler
        if sched is None:
            self._status.configure(text="no audio scheduler (skipping)")
            return
        if not self._wav_path.exists():
            self._status.configure(text=f"WAV missing at {self._wav_path}")
            return
        self._stop_playback()
        self._status.configure(text="playing…")
        try:
            self._handle = sched.play_wav(
                self._wav_path,
                on_finished=lambda: self.app.enqueue_ui(self._on_play_finished),
            )
        except Exception:  # noqa: BLE001
            logger.exception("volume_check play_wav failed")
            self._status.configure(text="playback failed (see log)")

    def _on_play_finished(self) -> None:
        self._handle = None
        self._status.configure(text="stopped — click 'Play tone again' to repeat")

    def _stop_playback(self) -> None:
        if self._handle is not None:
            try:
                self._handle.stop()
            except Exception:  # noqa: BLE001
                logger.exception("volume_check stop failed")
            self._handle = None

    def _on_confirm(self) -> None:
        if self.app.outlet is not None:
            try:
                self.app.outlet.volume_check_end(level="ok")
            except Exception:  # noqa: BLE001
                logger.exception("volume_check_end marker failed")
        self._stop_playback()
        self.app.show_stage("settle")
