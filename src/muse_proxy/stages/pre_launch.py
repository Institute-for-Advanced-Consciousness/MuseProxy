"""Pre-launch checklist (Jazzo-style).

Per-pass checklist. Start button stays disabled until every checkbox in
``config.pre_launch_manual_confirmations.pass_{a,b}`` is ticked AND
every required stream substring in
``config.pre_launch_streams.always_required + .muse_{2,athena}`` has
been visually confirmed by the RA (one tick per stream).

This stage does NOT query LSL directly — the RA confirms what they see
in Lab Recorder.
"""

from __future__ import annotations

import tkinter as tk
from typing import Any

from .. import theme
from ._base import StageFrame


class PreLaunchFrame(StageFrame):
    NAME = "pre_launch"

    def build(self) -> None:
        self._stream_vars: list[tk.BooleanVar] = []
        self._confirmation_vars: list[tk.BooleanVar] = []
        self.configure(bg=theme.CHROME_BG)

        # ----- Bottom: pinned Start button ------------------------------
        self._start_btn = theme.primary_button(
            self, "Start →", self._on_start,
            width=26, font=theme.FONT_BUTTON_LARGE, padx=20, pady=12,
            state="disabled",
        )
        self._start_btn.pack(side="bottom", pady=(6, 18))
        self._hint = theme.muted(
            self,
            "Tick every item — both panels — to enable Start.",
        )
        self._hint.pack(side="bottom", pady=(0, 6))

        # ----- Top: heading + subhead -----------------------------------
        self._heading = theme.heading(self, "Pre-launch")
        self._heading.pack(side="top", pady=(18, 4))
        self._subheading = theme.body(
            self, "", wraplength=1000, justify="center",
        )
        self._subheading.configure(fg=theme.TEXT_MUTED, anchor="center")
        self._subheading.pack(side="top", pady=(0, 16))
        self.register_wrappable(self._subheading, ratio=0.78, min_px=600)

        # ----- Middle: two panels side-by-side --------------------------
        body = tk.Frame(self, bg=theme.CHROME_BG)
        body.pack(side="top", fill="both", expand=True, padx=40, pady=(0, 8))
        body.grid_columnconfigure(0, weight=1, uniform="cols")
        body.grid_columnconfigure(1, weight=1, uniform="cols")
        body.grid_rowconfigure(0, weight=1)

        self._streams_panel = theme.panel(body, padx=18, pady=14)
        self._streams_panel.grid(row=0, column=0, padx=(0, 10), sticky="nsew")

        self._confirms_panel = theme.panel(body, padx=18, pady=14)
        self._confirms_panel.grid(row=0, column=1, padx=(10, 0), sticky="nsew")

    def on_enter(self, **payload: Any) -> None:
        cfg = getattr(self.app, "config", None)
        if cfg is None:
            return

        pass_label = self.app.pass_label
        device = self.app.pass_a_device if pass_label == "A" else self.app.pass_b_device
        if device is None:
            self._subheading.configure(text="Internal error: no device set.")
            return

        self._heading.configure(
            text=f"Pre-launch — Pass {pass_label}  ·  {_pretty(device)}"
        )
        if pass_label == "A":
            self._subheading.configure(
                text=("Confirm streams + manual items before Start. "
                      "The first XDF of this visit begins recording now.")
            )
        else:
            self._subheading.configure(
                text=("Confirm Pass B streams + a NEW Lab Recorder XDF is "
                      "actively recording before clicking Start.")
            )

        # Rebuild left panel: stream visibility checks.
        for child in self._streams_panel.winfo_children():
            child.destroy()
        self._stream_vars.clear()

        theme.panel_header(self._streams_panel, "STREAMS VISIBLE IN LAB RECORDER").pack(
            fill="x", pady=(0, 4)
        )
        tk.Label(
            self._streams_panel,
            text="Tick each stream as you see it appear.",
            font=theme.FONT_LABEL, fg=theme.TEXT_MUTED, bg=theme.SURFACE,
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        for stream in cfg.streams_for_device(device):
            v = tk.BooleanVar(value=False)
            v.trace_add("write", lambda *_: self._refresh_start())
            self._stream_vars.append(v)
            text = f"{stream.name}  —  {stream.description}"
            cb = theme.dark_checkbutton(
                self._streams_panel, text, v,
                command=self._refresh_start, wraplength=380,
            )
            cb.pack(fill="x", anchor="w", pady=2)
            self.register_wrappable(cb, ratio=0.42, min_px=260)

        # Rebuild right panel: manual confirmations.
        for child in self._confirms_panel.winfo_children():
            child.destroy()
        self._confirmation_vars.clear()

        theme.panel_header(self._confirms_panel, "MANUAL CONFIRMATIONS").pack(
            fill="x", pady=(0, 4)
        )
        tk.Label(
            self._confirms_panel,
            text="Tick each step as you complete it.",
            font=theme.FONT_LABEL, fg=theme.TEXT_MUTED, bg=theme.SURFACE,
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        items = (
            cfg.pre_launch_manual_confirmations.pass_a
            if pass_label == "A"
            else cfg.pre_launch_manual_confirmations.pass_b
        )
        for line in items:
            v = tk.BooleanVar(value=False)
            v.trace_add("write", lambda *_: self._refresh_start())
            self._confirmation_vars.append(v)
            cb = theme.dark_checkbutton(
                self._confirms_panel, line, v,
                command=self._refresh_start, wraplength=380,
            )
            cb.pack(fill="x", anchor="w", pady=2)
            self.register_wrappable(cb, ratio=0.42, min_px=260)

        self._refresh_start()

    def _refresh_start(self) -> None:
        all_ticked = all(v.get() for v in self._stream_vars + self._confirmation_vars)
        if all_ticked:
            self._start_btn.configure(state="normal", **theme.PRIMARY_BUTTON)
            self._hint.configure(text="All checks complete. Click Start.")
        else:
            self._start_btn.configure(state="disabled", **theme.DISABLED_BUTTON)
            self._hint.configure(
                text="Tick every item — both panels — to enable Start."
            )

    def _on_start(self) -> None:
        if str(self._start_btn.cget("state")) == "disabled":
            return
        self.app.show_stage("volume_check")


def _pretty(device: str) -> str:
    return {"muse-2": "Muse-2", "muse-athena": "Muse-Athena"}.get(device, device)
