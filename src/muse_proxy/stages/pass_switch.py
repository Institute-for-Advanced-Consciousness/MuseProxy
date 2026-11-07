"""Inter-pass device swap workflow.

Closes the Pass A LSL outlet, finalizes Pass A's SessionWriter, presents
the RA-facing device-swap checklist (stop LR, swap headset, start NEW
LR recording, verify signal), runs the optional inter-pass break timer,
opens a fresh Pass B outlet with a new ``source_id``, and advances to
the Pass B pre_launch screen.

See PROJECT_PLAN.md § 3.
"""

from __future__ import annotations

import logging
import time
import tkinter as tk
from typing import Any

import pylsl

from .. import theme
from ..config import REPO_ROOT
from ._base import StageFrame

logger = logging.getLogger(__name__)


class PassSwitchFrame(StageFrame):
    NAME = "pass_switch"

    def build(self) -> None:
        self.configure(bg=theme.CHROME_BG)

        # ----- Bottom: error + Continue button --------------------------
        self._error_var = tk.StringVar(value="")
        tk.Label(
            self, textvariable=self._error_var,
            font=theme.FONT_LABEL, fg=theme.ERROR, bg=theme.CHROME_BG,
        ).pack(side="bottom", pady=(0, 4))
        self._continue_btn = theme.primary_button(
            self, "Continue to Pass B pre-launch →", self._on_continue,
            width=32, font=theme.FONT_BUTTON_LARGE, padx=20, pady=12,
            state="disabled",
        )
        self._continue_btn.pack(side="bottom", pady=(6, 18))

        # ----- Top: heading + steps line --------------------------------
        theme.heading(self, "Pass Switch").pack(side="top", pady=(20, 4))
        self._subheading = theme.body(
            self, "", wraplength=1100, justify="center",
        )
        self._subheading.configure(anchor="center", fg=theme.TEXT_MUTED)
        self._subheading.pack(side="top", pady=(0, 6))
        self.register_wrappable(self._subheading, ratio=0.78, min_px=600)

        # Pass A per-block stream-coverage summary.
        self._coverage_label = tk.Label(
            self, text="", font=theme.FONT_LABEL, fg=theme.TEXT_MUTED,
            bg=theme.CHROME_BG, justify="center",
        )
        self._coverage_label.pack(side="top", pady=(0, 12))

        # ----- Middle: two columns (checklist + break) ------------------
        body = tk.Frame(self, bg=theme.CHROME_BG)
        body.pack(side="top", fill="both", expand=True, padx=40, pady=(0, 8))
        body.grid_columnconfigure(0, weight=2, uniform="cols")
        body.grid_columnconfigure(1, weight=1, uniform="cols")
        body.grid_rowconfigure(0, weight=1)

        self._checklist_panel = theme.panel(body, padx=18, pady=14)
        self._checklist_panel.grid(row=0, column=0, padx=(0, 10), sticky="nsew")

        self._break_panel = theme.panel(body, padx=18, pady=14)
        self._break_panel.grid(row=0, column=1, padx=(10, 0), sticky="nsew")
        theme.panel_header(self._break_panel, "PARTICIPANT BREAK").pack(
            fill="x", pady=(0, 8)
        )
        tk.Label(
            self._break_panel,
            text="Optional. Skippable.",
            font=theme.FONT_LABEL, fg=theme.TEXT_MUTED, bg=theme.SURFACE,
        ).pack(pady=(0, 6))
        self._break_timer_label = tk.Label(
            self._break_panel, text="00:00",
            bg=theme.SURFACE, fg=theme.ACCENT_LIGHT, font=theme.FONT_TIMER,
        )
        self._break_timer_label.pack(pady=(8, 14))
        self._skip_break_btn = theme.secondary_button(
            self._break_panel, "Skip break", self._on_skip_break, width=14,
        )
        self._skip_break_btn.pack(pady=(0, 4))

        # State.
        self._checklist_vars: list[tk.BooleanVar] = []
        self._break_started: bool = False
        self._break_start_monotonic: float | None = None
        self._break_planned_s: float = 0.0
        self._break_tick_after: str | None = None

    def on_enter(self, **payload: Any) -> None:
        cfg = getattr(self.app, "config", None)
        if cfg is None:
            self._subheading.configure(text="(config not loaded)")
            return

        next_device = self.app.pass_b_device or "?"
        self._subheading.configure(
            text=(
                f"Pass A complete. Now swap to the {_pretty(next_device)} for Pass B.  "
                f"(1) Stop Lab Recorder.  (2) Swap headset.  (3) Verify signal.  "
                f"(4) Start a NEW Lab Recorder XDF with a Pass B filename.  "
                f"(5) Tick the boxes below."
            )
        )

        # Capture Pass A's per-block stream coverage before finalizing —
        # finalize nulls the writer.
        pass_a_blocks = (
            list(self.app.writer.blocks) if self.app.writer is not None else []
        )

        # Finalize Pass A *now* if not already done. (idempotent)
        if self.app.outlet is not None or self.app.writer is not None:
            self._finalize_pass_a()
        self._render_coverage(pass_a_blocks)

        # Build checklist.
        for child in self._checklist_panel.winfo_children():
            child.destroy()
        self._checklist_vars.clear()

        theme.panel_header(self._checklist_panel, "DEVICE SWAP CHECKLIST").pack(
            fill="x", pady=(0, 4)
        )
        tk.Label(
            self._checklist_panel,
            text="Tick each step as you complete it.",
            font=theme.FONT_LABEL, fg=theme.TEXT_MUTED, bg=theme.SURFACE,
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        for line in cfg.pre_launch_manual_confirmations.pass_b:
            v = tk.BooleanVar(value=False)
            v.trace_add("write", lambda *_: self._refresh_continue())
            self._checklist_vars.append(v)
            cb = theme.dark_checkbutton(
                self._checklist_panel, line, v,
                command=self._refresh_continue, wraplength=500,
            )
            cb.pack(fill="x", anchor="w", pady=2)
            self.register_wrappable(cb, ratio=0.50, min_px=300)

        # Optional break.
        self._break_planned_s = cfg.settle.inter_pass_break_default_min * 60.0
        self._break_started = True
        self._break_start_monotonic = time.monotonic()
        self._tick_break()

        self._refresh_continue()
        self._error_var.set("")

    def on_leave(self) -> None:
        self._cancel_break_tick()

    def _render_coverage(self, blocks: list[Any]) -> None:
        from ..stream_health import block_health_summary

        lines = block_health_summary(blocks)
        if not lines:
            self._coverage_label.configure(text="")
            return
        any_bad = any("DEGRADED" in ln for ln in lines)
        header = (
            "Pass A stream coverage — review before continuing:"
            if any_bad
            else "Pass A stream coverage — all blocks OK:"
        )
        self._coverage_label.configure(
            text=header + "\n" + "\n".join(f"  {ln}" for ln in lines),
            fg=(theme.ERROR if any_bad else theme.TEXT_MUTED),
        )

    def _finalize_pass_a(self) -> None:
        if self.app.monitor is not None:
            try:
                self.app.monitor.stop()
            except Exception:  # noqa: BLE001
                logger.exception("monitor.stop failed")
            self.app.monitor = None
        outlet = self.app.outlet
        writer = self.app.writer
        if outlet is not None:
            try:
                outlet.pass_end(pass_label="A")
            except Exception:  # noqa: BLE001
                logger.exception("pass_end(A) marker failed")
            try:
                outlet.close()
            except Exception:  # noqa: BLE001
                logger.exception("outlet.close failed")
        if writer is not None:
            try:
                writer.finalize(status="completed")
            except Exception:  # noqa: BLE001
                logger.exception("writer.finalize(A) failed")
        self.app.outlet = None
        self.app.writer = None

    def _tick_break(self) -> None:
        if not self._break_started or self._break_start_monotonic is None:
            return
        elapsed = time.monotonic() - self._break_start_monotonic
        remaining = max(0.0, self._break_planned_s - elapsed)
        m, s = divmod(int(remaining), 60)
        self._break_timer_label.configure(text=f"{m:02d}:{s:02d}")
        if remaining <= 0:
            self._break_started = False
            self._break_timer_label.configure(text="00:00", fg=theme.TEXT_MUTED)
            return
        self._break_tick_after = self.after(250, self._tick_break)

    def _cancel_break_tick(self) -> None:
        if self._break_tick_after is not None:
            try:
                self.after_cancel(self._break_tick_after)
            except tk.TclError:
                pass
            self._break_tick_after = None

    def _on_skip_break(self) -> None:
        self._break_started = False
        self._cancel_break_tick()
        self._break_timer_label.configure(text="00:00", fg=theme.TEXT_MUTED)

    def _refresh_continue(self) -> None:
        ok = bool(self._checklist_vars) and all(v.get() for v in self._checklist_vars)
        if ok:
            self._continue_btn.configure(state="normal", **theme.PRIMARY_BUTTON)
        else:
            self._continue_btn.configure(state="disabled", **theme.DISABLED_BUTTON)

    def _on_continue(self) -> None:
        if str(self._continue_btn.cget("state")) == "disabled":
            return
        cfg = getattr(self.app, "config", None)
        if cfg is None:
            return
        try:
            self._open_pass_b()
        except Exception as e:  # noqa: BLE001
            logger.exception("open pass B failed")
            self._error_var.set(f"Pass B open failed: {e}")
            return

        self.app.show_stage("pre_launch")

    def _open_pass_b(self) -> None:
        from ..markers import MuseProxyMarkerOutlet, build_source_id
        from ..persistence import PassWriter

        cfg = self.app.config  # type: ignore[attr-defined]
        pid = self.app.participant_id
        if pid is None:
            raise RuntimeError("participant_id is not set")
        device = self.app.pass_b_device
        if device is None:
            raise RuntimeError("pass_b_device is not set")

        self.app.pass_label = "B"

        data_root = REPO_ROOT / cfg.data_dir
        date_str = self.app.intake_payload.get("date", "")

        writer = PassWriter(
            data_root, pid, "B", device, config_snapshot=cfg.model_dump()
        )
        self.app.writer = writer

        src = build_source_id(cfg.lsl.source_id_template, "B", pid, date_str)
        outlet = MuseProxyMarkerOutlet(
            outlet_name=cfg.lsl.outlet_name,
            outlet_type=cfg.lsl.outlet_type,
            source_id=src,
            csv_path=writer.markers_csv_path,
        )
        self.app.outlet = outlet

        start_lsl = pylsl.local_clock()
        writer.start_pass(start_lsl)
        outlet.session_start(pid=pid, date_yyyymmdd=date_str)
        outlet.device_marker(pass_label="A", device=self.app.pass_a_device or "?")
        outlet.device_marker(pass_label="B", device=device)
        outlet.pass_start(pass_label="B", device=device)
        intake_marker_kv = {
            k: v for k, v in self.app.intake_payload.items()
            if k not in ("ra_notes", "pid", "date")
        }
        outlet.intake(**intake_marker_kv)

        # Fresh stream-health monitor for Pass B's device.
        try:
            from ..stream_health import StreamHealthMonitor, specs_from_stream_refs

            specs = specs_from_stream_refs(cfg.streams_for_device(device))
            monitor = StreamHealthMonitor(specs)
            monitor.start()
            self.app.monitor = monitor
        except Exception:  # noqa: BLE001
            logger.exception("pass B stream-health monitor init failed")
            self.app.monitor = None


def _pretty(device: str) -> str:
    return {"muse-2": "Muse-2", "muse-athena": "Muse-Athena"}.get(device, device)
