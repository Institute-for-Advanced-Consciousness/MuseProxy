"""Intake stage — RA-facing visit metadata form.

Full field set per PROJECT_PLAN.md § 8. On submit:
  1. Validates required fields.
  2. Creates a Pass A LSL outlet (new source_id) and a PassWriter for
     Pass A. Also writes intake.json at the visit root.
  3. Emits ``session_start`` + ``pass_start:pass=A`` + the compound
     ``intake:...`` marker on the Pass A outlet.
  4. Advances to pre_launch.

Layout: pinned heading at top, pinned Begin button + error label at
bottom, four panels in a 2×2 grid filling the middle.
"""

from __future__ import annotations

import logging
import tkinter as tk
from datetime import date
from tkinter import ttk
from typing import Any

from .. import theme
from ..config import REPO_ROOT, canon_device, load_config, next_participant_id
from ._base import StageFrame

logger = logging.getLogger(__name__)


_DEVICE_CHOICES = ("Muse 2", "Muse Athena")
_SEX_CHOICES = ("M", "F", "Other", "Prefer not to say")
_HANDEDNESS_CHOICES = ("R", "L", "Ambi")
_YN_CHOICES = ("Y", "N")


class IntakeFrame(StageFrame):
    NAME = "intake"

    def build(self) -> None:
        self._config = None
        self.configure(bg=theme.CHROME_BG)

        # ----- Bottom strip (pinned): error label + Begin button --------
        self._error_var = tk.StringVar(value="")
        tk.Label(
            self,
            textvariable=self._error_var,
            font=theme.FONT_LABEL,
            fg=theme.ERROR,
            bg=theme.CHROME_BG,
        ).pack(side="bottom", pady=(0, 6))

        self._submit_btn = theme.primary_button(
            self, "Begin Session →", self._on_submit,
            width=26, font=theme.FONT_BUTTON_LARGE, padx=20, pady=12,
        )
        self._submit_btn.pack(side="bottom", pady=(6, 18))

        # ----- Top strip: heading + subhead -----------------------------
        theme.heading(self, "Intake — Visit Metadata").pack(side="top", pady=(18, 2))
        theme.muted(
            self,
            "All fields required unless noted. RA notes are saved to JSON "
            "and never emitted as markers.",
        ).pack(side="top", pady=(0, 14))

        # ----- Middle: 2×2 grid of panels -------------------------------
        body = tk.Frame(self, bg=theme.CHROME_BG)
        body.pack(side="top", fill="both", expand=True, padx=28, pady=(0, 8))
        body.grid_columnconfigure(0, weight=1, uniform="cols")
        body.grid_columnconfigure(1, weight=1, uniform="cols")
        body.grid_rowconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)

        # All form variables.
        self._vars: dict[str, tk.Variable] = {}

        self._build_participant_panel(body).grid(
            row=0, column=0, padx=(0, 8), pady=(0, 8), sticky="nsew"
        )
        self._build_demographics_panel(body).grid(
            row=0, column=1, padx=(8, 0), pady=(0, 8), sticky="nsew"
        )
        self._build_lifestyle_panel(body).grid(
            row=1, column=0, padx=(0, 8), pady=(8, 0), sticky="nsew"
        )
        self._build_state_panel(body).grid(
            row=1, column=1, padx=(8, 0), pady=(8, 0), sticky="nsew"
        )

    # ----- panel builders -------------------------------------------------

    def _build_participant_panel(self, parent: tk.Misc) -> tk.Frame:
        p = theme.panel(parent)
        theme.panel_header(p, "PARTICIPANT & VISIT").grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8)
        )

        # PID
        self._vars["pid"] = tk.StringVar()
        self._row(p, 1, "Participant ID", theme.dark_entry(p, self._vars["pid"], width=14))

        # Date (auto, read-only)
        self._vars["date"] = tk.StringVar(value=date.today().strftime("%Y%m%d"))
        self._row(
            p, 2, "Date (auto)",
            theme.dark_entry(p, self._vars["date"], width=14, readonly=True),
        )

        # Pass A device
        v_pa = tk.StringVar(value=_DEVICE_CHOICES[0])
        v_pb = tk.StringVar(value=_DEVICE_CHOICES[1])
        self._vars["pass_A_device"] = v_pa
        self._vars["pass_B_device"] = v_pb

        cmb_a = ttk.Combobox(
            p, textvariable=v_pa, values=_DEVICE_CHOICES,
            state="readonly", style="Dark.TCombobox", width=16,
        )
        self._row(p, 3, "Pass A device", cmb_a)
        cmb_b = ttk.Combobox(
            p, textvariable=v_pb, values=_DEVICE_CHOICES,
            state="readonly", style="Dark.TCombobox", width=16,
        )
        cmb_b.configure(state="disabled")  # auto-filled
        self._row(p, 4, "Pass B device (auto)", cmb_b)

        def _on_pa_change(*_args: Any) -> None:
            choice = v_pa.get()
            other = _DEVICE_CHOICES[1] if choice == _DEVICE_CHOICES[0] else _DEVICE_CHOICES[0]
            v_pb.set(other)

        v_pa.trace_add("write", _on_pa_change)

        p.grid_columnconfigure(1, weight=1)
        return p

    def _build_demographics_panel(self, parent: tk.Misc) -> tk.Frame:
        p = theme.panel(parent)
        theme.panel_header(p, "DEMOGRAPHICS & HEALTH").grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8)
        )

        self._vars["age"] = tk.StringVar()
        self._row(p, 1, "Age", theme.dark_entry(p, self._vars["age"], width=10))

        self._vars["sex"] = tk.StringVar(value=_SEX_CHOICES[0])
        self._row(p, 2, "Sex", ttk.Combobox(
            p, textvariable=self._vars["sex"], values=_SEX_CHOICES,
            state="readonly", style="Dark.TCombobox", width=18,
        ))

        self._vars["handedness"] = tk.StringVar(value=_HANDEDNESS_CHOICES[0])
        self._row(p, 3, "Handedness", ttk.Combobox(
            p, textvariable=self._vars["handedness"], values=_HANDEDNESS_CHOICES,
            state="readonly", style="Dark.TCombobox", width=18,
        ))

        self._vars["glasses"] = tk.StringVar(value="N")
        self._row(p, 4, "Glasses / contacts", self._yn_combo(p, self._vars["glasses"]))

        self._vars["dental_work"] = tk.StringVar(value="N")
        self._row(p, 5, "Recent dental work (1mo)", self._yn_combo(p, self._vars["dental_work"]))

        self._vars["facial_pain"] = tk.StringVar(value="N")
        self._row(p, 6, "Current facial / jaw pain", self._yn_combo(p, self._vars["facial_pain"]))

        p.grid_columnconfigure(1, weight=1)
        return p

    def _build_lifestyle_panel(self, parent: tk.Misc) -> tk.Frame:
        p = theme.panel(parent)
        theme.panel_header(p, "LIFESTYLE (last 24h)").grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8)
        )

        self._vars["caffeine_hours_ago"] = tk.StringVar(value="99")
        self._row(p, 1, "Hours since caffeine (99=none)",
                  theme.dark_entry(p, self._vars["caffeine_hours_ago"], width=10))

        self._vars["last_meal_hours_ago"] = tk.StringVar()
        self._row(p, 2, "Hours since last meal",
                  theme.dark_entry(p, self._vars["last_meal_hours_ago"], width=10))

        self._vars["sleep_hours"] = tk.StringVar()
        self._row(p, 3, "Sleep hours last night",
                  theme.dark_entry(p, self._vars["sleep_hours"], width=10))

        self._vars["recent_exercise"] = tk.StringVar(value="N")
        self._row(p, 4, "Vigorous exercise (last 2h)",
                  self._yn_combo(p, self._vars["recent_exercise"]))

        p.grid_columnconfigure(1, weight=1)
        return p

    def _build_state_panel(self, parent: tk.Misc) -> tk.Frame:
        p = theme.panel(parent)
        theme.panel_header(p, "ARRIVAL STATE & COMFORT").grid(
            row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8)
        )

        # Stress slider + live readout
        self._vars["stress_self_report"] = tk.IntVar(value=0)
        theme.field_label(p, "Self-reported stress (0–10)").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=(2, 0)
        )
        stress_val = tk.Label(
            p, text="0", font=theme.FONT_BODY_BOLD,
            fg=theme.ACCENT_LIGHT, bg=theme.SURFACE, width=2,
        )
        stress_val.grid(row=1, column=2, sticky="e", pady=(2, 0))
        scale = tk.Scale(
            p, from_=0, to=10, orient="horizontal",
            variable=self._vars["stress_self_report"],
            bg=theme.SURFACE, fg=theme.TEXT,
            troughcolor=theme.SURFACE_RAISED,
            activebackground=theme.ACCENT_LIGHT,
            highlightthickness=0, borderwidth=0,
            sliderrelief="flat", sliderlength=20,
            showvalue=False,
            command=lambda v: stress_val.configure(text=str(int(float(v)))),
        )
        scale.grid(row=1, column=1, sticky="ew", padx=4, pady=(2, 0))

        # Trip stress
        self._vars["trip_stress"] = tk.StringVar(value="N")
        theme.field_label(p, "Stressful trip in / parking").grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=4
        )
        self._yn_combo(p, self._vars["trip_stress"]).grid(
            row=2, column=1, columnspan=2, sticky="w", pady=4
        )

        # Trip settle minutes
        self._vars["trip_settle_minutes"] = tk.StringVar(value="0")
        theme.field_label(p, "Settle minutes after trip").grid(
            row=3, column=0, sticky="w", padx=(0, 10), pady=4
        )
        theme.dark_entry(p, self._vars["trip_settle_minutes"], width=8).grid(
            row=3, column=1, columnspan=2, sticky="w", pady=4
        )

        # Hydration / warm
        self._vars["hydration"] = tk.StringVar(value="Y")
        theme.field_label(p, "Water offered & accepted").grid(
            row=4, column=0, sticky="w", padx=(0, 10), pady=4
        )
        self._yn_combo(p, self._vars["hydration"]).grid(
            row=4, column=1, columnspan=2, sticky="w", pady=4
        )

        self._vars["warm"] = tk.StringVar(value="Y")
        theme.field_label(p, "Participant warm enough").grid(
            row=5, column=0, sticky="w", padx=(0, 10), pady=4
        )
        self._yn_combo(p, self._vars["warm"]).grid(
            row=5, column=1, columnspan=2, sticky="w", pady=4
        )

        # RA notes
        theme.field_label(p, "RA notes (free text, not in markers)").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(10, 2)
        )
        self._notes_text = theme.dark_text(p, height=3, width=42)
        self._notes_text.grid(
            row=7, column=0, columnspan=3, sticky="nsew", pady=(0, 0)
        )

        p.grid_columnconfigure(1, weight=1)
        p.grid_rowconfigure(7, weight=1)
        return p

    # ----- helpers --------------------------------------------------------

    def _row(self, parent: tk.Misc, row: int, label: str, widget: tk.Widget) -> None:
        theme.field_label(parent, label).grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=4
        )
        widget.grid(row=row, column=1, sticky="w", pady=4)

    def _yn_combo(self, parent: tk.Misc, var: tk.StringVar) -> ttk.Combobox:
        return ttk.Combobox(
            parent, textvariable=var, values=_YN_CHOICES,
            state="readonly", style="Dark.TCombobox", width=6,
        )

    # ----- on_enter -------------------------------------------------------

    def on_enter(self, **payload: Any) -> None:
        if self._config is None:
            try:
                self._config = load_config()
            except Exception as e:  # noqa: BLE001
                logger.exception("config load failed")
                self._error_var.set(f"Failed to load config: {e}")
                return
        self.app.config = self._config  # type: ignore[attr-defined]
        if self._config.participant.auto_suggest_next_id and not self._vars["pid"].get():
            data_root = REPO_ROOT / self._config.data_dir
            suggested = next_participant_id(
                data_root,
                prefix=self._config.participant.id_prefix,
                digits=self._config.participant.id_digits,
            )
            self._vars["pid"].set(suggested)
        self._error_var.set("")

    # ----- submission -----------------------------------------------------

    def _on_submit(self) -> None:
        try:
            payload = self._collect()
        except _IntakeError as e:
            self._error_var.set(str(e))
            return

        try:
            self._open_services(payload)
        except Exception as e:  # noqa: BLE001
            logger.exception("service open failed")
            self._error_var.set(f"Failed to open services: {e}")
            return

        self.app.show_stage("pre_launch")

    def _collect(self) -> dict[str, Any]:
        pid = self._vars["pid"].get().strip()
        if not pid:
            raise _IntakeError("Participant ID is required.")

        def _to_float(key: str, label: str) -> float:
            raw = self._vars[key].get().strip()
            if raw == "":
                raise _IntakeError(f"{label} is required.")
            try:
                return float(raw)
            except ValueError as e:
                raise _IntakeError(f"{label}: not a number ({raw!r}).") from e

        def _to_int(key: str, label: str) -> int:
            raw = self._vars[key].get()
            if isinstance(raw, int):
                return raw
            raw = str(raw).strip()
            if raw == "":
                raise _IntakeError(f"{label} is required.")
            try:
                return int(raw)
            except ValueError as e:
                raise _IntakeError(f"{label}: not an integer ({raw!r}).") from e

        out = {
            "pid": pid,
            "date": self._vars["date"].get().strip(),
            "pass_A_device": canon_device(self._vars["pass_A_device"].get()),
            "pass_B_device": canon_device(self._vars["pass_B_device"].get()),
            "age": _to_int("age", "Age"),
            "sex": self._vars["sex"].get(),
            "handedness": self._vars["handedness"].get(),
            "glasses": self._vars["glasses"].get(),
            "dental_work": self._vars["dental_work"].get(),
            "facial_pain": self._vars["facial_pain"].get(),
            "caffeine_hours_ago": _to_float("caffeine_hours_ago", "Caffeine hours"),
            "last_meal_hours_ago": _to_float("last_meal_hours_ago", "Last-meal hours"),
            "sleep_hours": _to_float("sleep_hours", "Sleep hours"),
            "recent_exercise": self._vars["recent_exercise"].get(),
            "stress_self_report": int(self._vars["stress_self_report"].get()),
            "trip_stress": self._vars["trip_stress"].get(),
            "trip_settle_minutes": _to_int("trip_settle_minutes", "Trip settle minutes"),
            "hydration": self._vars["hydration"].get(),
            "warm": self._vars["warm"].get(),
            "ra_notes": self._notes_text.get("1.0", "end").strip(),
        }

        if out["pass_A_device"] == out["pass_B_device"]:
            raise _IntakeError("Pass A and Pass B must use different devices.")

        return out

    def _open_services(self, intake: dict[str, Any]) -> None:
        """Create the Pass A outlet + writer, write intake.json, emit markers."""
        from ..audio import AudioScheduler
        from ..markers import MuseProxyMarkerOutlet, build_source_id
        from ..persistence import IntakeWriter, PassWriter

        cfg = self._config
        if cfg is None:  # safety
            raise RuntimeError("config not loaded")

        pid = intake["pid"]
        date_str = intake["date"]
        pass_a_device = intake["pass_A_device"]

        # Stash on app for downstream stages.
        self.app.participant_id = pid
        self.app.pass_label = "A"
        self.app.pass_a_device = pass_a_device
        self.app.pass_b_device = intake["pass_B_device"]
        self.app.intake_payload = intake

        data_root = REPO_ROOT / cfg.data_dir

        # 1) intake.json — visit-level.
        IntakeWriter(data_root, pid).write(intake)

        # 2) Pass A writer.
        writer = PassWriter(
            data_root, pid, "A", pass_a_device, config_snapshot=cfg.model_dump()
        )
        self.app.writer = writer

        # 3) Pass A outlet + CSV mirror.
        src = build_source_id(cfg.lsl.source_id_template, "A", pid, date_str)
        outlet = MuseProxyMarkerOutlet(
            outlet_name=cfg.lsl.outlet_name,
            outlet_type=cfg.lsl.outlet_type,
            source_id=src,
            csv_path=writer.markers_csv_path,
        )
        self.app.outlet = outlet

        # 4) Emit session/pass/device + intake markers.
        import pylsl

        start_lsl = pylsl.local_clock()
        writer.start_pass(start_lsl)
        outlet.session_start(pid=pid, date_yyyymmdd=date_str)
        outlet.device_marker(pass_label="A", device=pass_a_device)
        outlet.device_marker(pass_label="B", device=intake["pass_B_device"])
        outlet.pass_start(pass_label="A", device=pass_a_device)
        intake_marker_kv = {
            k: v for k, v in intake.items()
            if k not in ("ra_notes", "pid", "date")
        }
        outlet.intake(**intake_marker_kv)

        # 5) Audio scheduler (reusable across stages).
        try:
            device_arg: int | str | None = cfg.audio.device_name_substring
            self.app.audio_scheduler = AudioScheduler(
                sample_rate=cfg.audio.sample_rate, device=device_arg
            )
        except Exception:  # noqa: BLE001
            logger.exception("AudioScheduler init failed; continuing without audio")
            self.app.audio_scheduler = None


class _IntakeError(Exception):
    pass
