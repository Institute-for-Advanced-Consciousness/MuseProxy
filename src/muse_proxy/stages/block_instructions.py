"""Per-block participant-facing instruction screen.

Shows ``config.blocks[idx].instruction`` text. SPACE advances to the
block runner — ``block_breathing`` for blocks 1/2/3, ``block_artifact``
for block 4 — forwarding the re-run ``attempt`` number.

Pre-block stream gate: while any required LSL stream is SILENT/MISSING,
a red banner names it and SPACE is disabled, so a block never starts
with a dead stream. The gate clears automatically once the RA has the
headset streaming again. With no monitor attached (dev/test walk-through)
the gate is open.
"""

from __future__ import annotations

import logging
import tkinter as tk
from typing import Any

from .. import theme
from ..stream_health import StreamStatus
from ._base import StageFrame

logger = logging.getLogger(__name__)


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

        # RA-facing stream-health banner — empty unless a required stream
        # is down, in which case SPACE is gated until it recovers.
        self._banner = tk.Label(
            self, text="",
            bg=theme.TASK_BG, fg=theme.ERROR, font=theme.FONT_BODY_BOLD,
            wraplength=1000, justify="center",
        )
        self._banner.pack(pady=(0, 6))
        self.register_wrappable(self._banner, ratio=0.8, min_px=500)

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
        # RA override — starts the block even while a stream is down, so a
        # misconfigured stream name can never trap the session.
        self.bind_all("<Control-Return>", self._on_ra_override)

        self._block_id: int | None = None
        self._attempt: int = 1
        self._streams_ok: bool = True
        self._poll_after: str | None = None

    def on_enter(self, **payload: Any) -> None:
        block_id = payload.get("block_id")
        if block_id is None:
            raise ValueError("block_instructions requires block_id payload")
        self._block_id = int(block_id)
        self._attempt = int(payload.get("attempt", 1))

        # Rebind keys (may have been unbound by a prior on_leave).
        self.bind_all("<space>", self._on_space)
        self.bind_all("<Control-Return>", self._on_ra_override)

        cfg = getattr(self.app, "config", None)
        if cfg is None:
            self._body.configure(text="(config not loaded)")
            self._start_stream_poll()
            return
        block = cfg.block_by_id(self._block_id)

        counter_text = f"Block {self._block_id} of 4"
        if self._attempt > 1:
            counter_text += f"   ·   re-run (attempt {self._attempt})"
        self._counter.configure(text=counter_text)
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

        self._start_stream_poll()

    def on_leave(self) -> None:
        for seq in ("<space>", "<Control-Return>"):
            try:
                self.unbind_all(seq)
            except tk.TclError:
                pass
        self._cancel_stream_poll()

    # ----- pre-block stream gate -----------------------------------------

    def _start_stream_poll(self) -> None:
        self._cancel_stream_poll()
        self._poll_streams()

    def _cancel_stream_poll(self) -> None:
        if self._poll_after is not None:
            try:
                self.after_cancel(self._poll_after)
            except tk.TclError:
                pass
            self._poll_after = None

    def _poll_streams(self) -> None:
        monitor = getattr(self.app, "monitor", None)
        if monitor is None:
            # No monitor (dev/test) — gate is open.
            self._streams_ok = True
            self._banner.configure(text="")
        else:
            down = [
                h for h in monitor.snapshot()
                if h.status is not StreamStatus.ALIVE
            ]
            if down:
                self._streams_ok = False
                names = ", ".join(
                    (h.spec.description or h.spec.key) for h in down
                )
                self._banner.configure(
                    text=(
                        f"⚠  Stream not ready: {names}\n"
                        "RA: reconnect the headset — SPACE unlocks automatically, "
                        "or press Ctrl+Enter to start anyway."
                    )
                )
            else:
                self._streams_ok = True
                self._banner.configure(text="")
        self._refresh_hint()
        try:
            self._poll_after = self.after(1000, self._poll_streams)
        except tk.TclError:
            self._poll_after = None

    def _refresh_hint(self) -> None:
        if self._streams_ok:
            self._hint.configure(text="Press SPACE when ready", fg="#888888")
        else:
            self._hint.configure(
                text="SPACE disabled until streams are ready", fg=theme.ERROR
            )

    # ----- advance --------------------------------------------------------

    def _on_space(self, _event: tk.Event | None = None) -> None:
        self._advance_to_block(force=False)

    def _on_ra_override(self, _event: tk.Event | None = None) -> None:
        self._advance_to_block(force=True)

    def _advance_to_block(self, *, force: bool) -> None:
        if self._block_id is None:
            return
        if not self._streams_ok and not force:
            # Gated — a required stream is down. The RA can override.
            return
        if not self._streams_ok and force:
            logger.warning(
                "RA override: starting block %s with a stream down", self._block_id
            )
            record_stream_override(self.app, "stream_override", self._block_id, self._attempt)
        for seq in ("<space>", "<Control-Return>"):
            try:
                self.unbind_all(seq)
            except tk.TclError:
                pass
        self._cancel_stream_poll()
        target = "block_artifact" if self._block_id == 4 else "block_breathing"
        self.app.show_stage(target, block_id=self._block_id, attempt=self._attempt)


def _marker_safe(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_.|" else "_" for ch in text)


def record_stream_override(app: object, event: str, block_id: int, attempt: int) -> None:
    """Put an RA decision to proceed with a stream down into the marker stream
    (and therefore the CSV mirror and the XDF), naming the streams that were down.
    Never raises: losing this marker must not block the session."""
    outlet = getattr(app, "outlet", None)
    if outlet is None:
        return
    down = ""
    monitor = getattr(app, "monitor", None)
    try:
        if monitor is not None:
            down = ",".join(
                _marker_safe(h.spec.key) for h in monitor.snapshot()
                if h.status is not StreamStatus.ALIVE
            )
        outlet.push(event, {
            "block": block_id,
            "attempt": attempt,
            "pass": getattr(app, "pass_label", "?"),
            "down": down or "none",
        })
    except Exception:  # noqa: BLE001
        logger.exception("could not record %s marker", event)
