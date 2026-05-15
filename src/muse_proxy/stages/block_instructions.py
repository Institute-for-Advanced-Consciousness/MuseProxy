"""Per-block participant-facing instruction screen.

Shows ``config.blocks[idx].instruction`` text. SPACE advances to the
block runner. Transitions to ``block_breathing`` for blocks 1/2/3
or ``block_artifact`` for block 4.
"""

from __future__ import annotations

import tkinter as tk
from typing import Any

from .. import theme
from ._base import StageFrame


class BlockInstructionsFrame(StageFrame):
    NAME = "block_instructions"

    def build(self) -> None:
        self.configure(bg=theme.TASK_BG)

        # Top spacer.
        tk.Frame(self, bg=theme.TASK_BG, height=60).pack(side="top")

        # "Block X of 4" pre-heading.
        self._counter = tk.Label(
            self, text="Block",
            bg=theme.TASK_BG, fg="#888888", font=theme.FONT_SUBHEADING,
        )
        self._counter.pack(pady=(0, 6))

        # Big block label.
        self._heading = tk.Label(
            self, text="",
            bg=theme.TASK_BG, fg=theme.TASK_TEXT, font=theme.FONT_HEADING,
        )
        self._heading.pack(pady=(0, 30))

        # Instruction body.
        self._body = tk.Label(
            self, text="",
            bg=theme.TASK_BG, fg=theme.TASK_TEXT, font=theme.FONT_SUBHEADING,
            justify="center", wraplength=900,
        )
        self._body.pack(pady=20, padx=60)
        self.register_wrappable(self._body, ratio=0.7, min_px=500)

        # Bottom hint.
        self._hint = tk.Label(
            self, text="Press SPACE when ready",
            bg=theme.TASK_BG, fg="#888888", font=theme.FONT_BODY,
        )
        self._hint.pack(side="bottom", pady=(0, 60))

        self.bind_all("<space>", self._on_space)

        self._block_id: int | None = None

    def on_enter(self, **payload: Any) -> None:
        block_id = payload.get("block_id")
        if block_id is None:
            raise ValueError("block_instructions requires block_id payload")
        self._block_id = int(block_id)

        # Rebind space (may have been unbound by a prior on_leave).
        self.bind_all("<space>", self._on_space)

        cfg = getattr(self.app, "config", None)
        if cfg is None:
            self._body.configure(text="(config not loaded)")
            return
        block = cfg.block_by_id(self._block_id)

        self._counter.configure(text=f"Block {self._block_id} of 4")
        # Friendly label
        friendly = {
            "baseline": "Natural breathing",
            "paced_0p1Hz": "Paced breathing — slow",
            "paced_0p2Hz": "Paced breathing — fast",
            "artifact": "Artifact creation",
        }.get(block.label, block.label)
        self._heading.configure(text=friendly)
        # Strip the "Block N of 4:" prefix from the YAML instruction so we
        # don't double it with the counter line.
        body_text = block.instruction.strip()
        for prefix in (
            f"Block {self._block_id} of 4:",
            "Block 1 of 4:",
            "Block 2 of 4:",
            "Block 3 of 4:",
            "Block 4 of 4:",
        ):
            if body_text.startswith(prefix):
                body_text = body_text[len(prefix):].strip()
                break
        # Drop the "Press SPACE when you're ready..." tail since we show our own.
        for tail in (
            "Press SPACE when you're ready to begin.",
            "Press SPACE when you're ready.",
        ):
            if body_text.endswith(tail):
                body_text = body_text[: -len(tail)].rstrip()
        self._body.configure(text=body_text)

    def on_leave(self) -> None:
        try:
            self.unbind_all("<space>")
        except tk.TclError:
            pass

    def _on_space(self, _event: tk.Event | None = None) -> None:
        if self._block_id is None:
            return
        try:
            self.unbind_all("<space>")
        except tk.TclError:
            pass
        target = "block_artifact" if self._block_id == 4 else "block_breathing"
        self.app.show_stage(target, block_id=self._block_id)
