"""RA-facing stream-recovery screen.

Reached after a block whose ``StreamHealthMonitor`` window report showed
a stream went SILENT or MISSING during the block. The RA reconnects the
headset, watches the live panel confirm every stream is back, then either
re-runs the block (capturing it cleanly) or continues without it.

Re-running routes back into the same block with ``attempt = N + 1``; the
marker grammar tags the re-run (``attempt=N``) so the analyst keeps the
attempt with full stream coverage. ``Continue`` proceeds exactly as a
clean block would have — that routing lives in ``advance_to_next`` so the
between-block stage and this stage agree.
"""

from __future__ import annotations

import logging
import tkinter as tk
from typing import Any

from .. import theme
from ..stream_health import StreamStatus
from ._base import StageFrame

logger = logging.getLogger(__name__)

# Status -> (label, colour) for the two panels.
_OK_GREEN = "#7ccb7c"
_STATUS_TEXT: dict[StreamStatus, tuple[str, str]] = {
    StreamStatus.ALIVE: ("OK", _OK_GREEN),
    StreamStatus.SILENT: ("SILENT", theme.ERROR),
    StreamStatus.MISSING: ("DROPPED", theme.ERROR),
}


def advance_to_next(app: Any, finished_block_id: int) -> None:
    """Route forward as if ``finished_block_id`` had completed cleanly."""
    if finished_block_id < 4:
        app.show_stage("block_instructions", block_id=finished_block_id + 1)
    elif app.pass_label == "A":
        app.show_stage("pass_switch")
    else:
        app.show_stage("completion")


def block_window_report(app: Any, token: float | None) -> dict[str, str]:
    """Worst per-stream health (plain strings) over a block window, or {}.

    Returns an empty dict when no monitor is attached (dev/test) or no
    window was opened — callers treat empty as "no degradation".
    """
    monitor = getattr(app, "monitor", None)
    if monitor is None or token is None:
        return {}
    try:
        return monitor.window_report_values(token)
    except Exception:  # noqa: BLE001
        logger.exception("window_report failed")
        return {}


def _status_from_value(value: str) -> StreamStatus:
    try:
        return StreamStatus(value)
    except ValueError:
        return StreamStatus.MISSING


def _stream_label(key: str) -> str:
    # key is "name|type"; render "name type" (drop the |* type-agnostic marker).
    return key.replace("|*", "").replace("|", " ").strip()


class StreamRecoveryFrame(StageFrame):
    NAME = "stream_recovery"

    def build(self) -> None:
        self.configure(bg=theme.CHROME_BG)

        # ----- Bottom: action buttons -----------------------------------
        btn_row = tk.Frame(self, bg=theme.CHROME_BG)
        btn_row.pack(side="bottom", pady=(6, 26))
        self._rerun_btn = theme.primary_button(
            btn_row, "Re-run block", self._on_rerun,
            width=24, font=theme.FONT_BUTTON_LARGE, padx=18, pady=12,
        )
        self._rerun_btn.pack(side="left", padx=(0, 14))
        self._continue_btn = theme.secondary_button(
            btn_row, "Continue anyway", self._on_continue,
            width=24, font=theme.FONT_BUTTON_LARGE, padx=18, pady=12,
        )
        self._continue_btn.pack(side="left")
        self._hint = theme.muted(self, "")
        self._hint.pack(side="bottom", pady=(0, 6))

        # ----- Top: heading + explanation -------------------------------
        tk.Frame(self, bg=theme.CHROME_BG, height=36).pack(side="top")
        self._heading = theme.heading(self, "Stream dropout")
        self._heading.pack(side="top", pady=(0, 6))
        self._subheading = theme.body(
            self, "", fg=theme.TEXT_MUTED, wraplength=1000, justify="center",
        )
        self._subheading.configure(anchor="center")
        self._subheading.pack(side="top", pady=(0, 18))
        self.register_wrappable(self._subheading, ratio=0.7, min_px=520)

        # ----- Middle: during-block report + live status ----------------
        body = tk.Frame(self, bg=theme.CHROME_BG)
        body.pack(side="top", fill="both", expand=True, padx=60, pady=(0, 8))
        body.grid_columnconfigure(0, weight=1, uniform="cols")
        body.grid_columnconfigure(1, weight=1, uniform="cols")
        body.grid_rowconfigure(0, weight=1)
        self._during_panel = theme.panel(body, padx=18, pady=14)
        self._during_panel.grid(row=0, column=0, padx=(0, 10), sticky="nsew")
        self._live_panel = theme.panel(body, padx=18, pady=14)
        self._live_panel.grid(row=0, column=1, padx=(10, 0), sticky="nsew")

        # ----- state ----------------------------------------------------
        self._block_id: int | None = None
        self._attempt: int = 1
        self._report: dict[str, str] = {}
        self._live_rows: dict[str, tk.Label] = {}
        self._poll_after: str | None = None

    def on_enter(self, **payload: Any) -> None:
        self._block_id = int(payload.get("block_id", 0))
        self._attempt = int(payload.get("attempt", 1))
        self._report = dict(payload.get("report", {}) or {})

        cfg = getattr(self.app, "config", None)
        label = ""
        if cfg is not None and self._block_id:
            try:
                label = cfg.block_by_id(self._block_id).label
            except KeyError:
                label = ""

        self._heading.configure(text=f"Stream dropout — Block {self._block_id}")
        self._rerun_btn.configure(text=f"Re-run block {self._block_id}")
        nxt = "the next block" if (self._block_id or 0) < 4 else "finish the pass"
        self._continue_btn.configure(
            text=("Continue to next block" if (self._block_id or 0) < 4
                  else "Continue — finish pass")
        )
        self._subheading.configure(
            text=(
                f"A data stream stopped while block {self._block_id}"
                f"{f' ({label})' if label else ''} was running. Reconnect the "
                "headset in your Muse-streaming software, wait for every stream "
                "below to read OK, then re-run the block to capture it cleanly "
                f"— or continue to {nxt} without it."
            )
        )
        self._build_during_panel()
        self._build_live_panel()
        self._poll()

    def on_leave(self) -> None:
        if self._poll_after is not None:
            try:
                self.after_cancel(self._poll_after)
            except tk.TclError:
                pass
            self._poll_after = None

    # ----- panels ---------------------------------------------------------

    def _build_during_panel(self) -> None:
        for child in self._during_panel.winfo_children():
            child.destroy()
        theme.panel_header(
            self._during_panel, f"DURING BLOCK {self._block_id}"
        ).pack(fill="x", pady=(0, 8))
        if not self._report:
            tk.Label(
                self._during_panel, text="(no stream report)",
                font=theme.FONT_BODY, fg=theme.TEXT_MUTED, bg=theme.SURFACE,
            ).pack(anchor="w")
            return
        for key, value in sorted(self._report.items()):
            status = _status_from_value(value)
            txt, color = _STATUS_TEXT.get(status, ("?", theme.TEXT_MUTED))
            row = tk.Frame(self._during_panel, bg=theme.SURFACE)
            row.pack(fill="x", pady=2)
            tk.Label(
                row, text=_stream_label(key), font=theme.FONT_BODY,
                fg=theme.TEXT, bg=theme.SURFACE, anchor="w",
            ).pack(side="left")
            tk.Label(
                row, text=txt, font=theme.FONT_BODY_BOLD,
                fg=color, bg=theme.SURFACE, anchor="e",
            ).pack(side="right")

    def _build_live_panel(self) -> None:
        for child in self._live_panel.winfo_children():
            child.destroy()
        self._live_rows = {}
        theme.panel_header(self._live_panel, "LIVE NOW").pack(fill="x", pady=(0, 8))
        monitor = getattr(self.app, "monitor", None)
        snap = monitor.snapshot() if monitor is not None else []
        if not snap:
            tk.Label(
                self._live_panel, text="(stream monitor unavailable)",
                font=theme.FONT_BODY, fg=theme.TEXT_MUTED, bg=theme.SURFACE,
            ).pack(anchor="w")
            return
        for health in snap:
            row = tk.Frame(self._live_panel, bg=theme.SURFACE)
            row.pack(fill="x", pady=2)
            tk.Label(
                row, text=health.spec.description or _stream_label(health.spec.key),
                font=theme.FONT_BODY, fg=theme.TEXT, bg=theme.SURFACE, anchor="w",
            ).pack(side="left")
            value = tk.Label(
                row, text="…", font=theme.FONT_BODY_BOLD,
                fg=theme.TEXT_MUTED, bg=theme.SURFACE, anchor="e",
            )
            value.pack(side="right")
            self._live_rows[health.spec.key] = value

    def _poll(self) -> None:
        monitor = getattr(self.app, "monitor", None)
        all_ok = True
        if monitor is not None:
            for health in monitor.snapshot():
                txt, color = _STATUS_TEXT.get(health.status, ("?", theme.TEXT_MUTED))
                label = self._live_rows.get(health.spec.key)
                if label is not None:
                    try:
                        label.configure(text=txt, fg=color)
                    except tk.TclError:
                        pass
                if health.status is not StreamStatus.ALIVE:
                    all_ok = False
        self._set_rerun_enabled(all_ok)
        try:
            self._poll_after = self.after(1000, self._poll)
        except tk.TclError:
            self._poll_after = None

    def _set_rerun_enabled(self, enabled: bool) -> None:
        if enabled:
            self._rerun_btn.configure(state="normal", **theme.PRIMARY_BUTTON)
            self._hint.configure(
                text="All streams OK — re-run the block to capture it cleanly."
            )
        else:
            self._rerun_btn.configure(state="disabled", **theme.DISABLED_BUTTON)
            self._hint.configure(
                text="Re-run unlocks once every stream reads OK. "
                     "Continue is always available."
            )

    # ----- actions --------------------------------------------------------

    def _on_rerun(self) -> None:
        if str(self._rerun_btn.cget("state")) == "disabled":
            return
        if self._block_id is None:
            return
        self.on_leave()
        logger.info("RA re-running block %s as attempt %s",
                    self._block_id, self._attempt + 1)
        self.app.show_stage(
            "block_instructions", block_id=self._block_id, attempt=self._attempt + 1
        )

    def _on_continue(self) -> None:
        if self._block_id is None:
            return
        self.on_leave()
        logger.info("RA continuing past block %s despite stream dropout",
                    self._block_id)
        advance_to_next(self.app, self._block_id)
