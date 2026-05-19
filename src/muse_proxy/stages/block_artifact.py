"""Block 4 runner — artifact-creation cues.

Drives ``AudioScheduler.schedule_artifact_block`` with the cue list
from ``config.artifact_block.cues``. Per-cue start/end markers are
sample-anchored to the audio onset.
"""

from __future__ import annotations

import logging
import time
import tkinter as tk
from typing import Any

import pylsl

from .. import theme
from ..artifacts import build_artifact_schedule
from ..config import REPO_ROOT
from ._base import StageFrame
from .stream_recovery import block_window_report

logger = logging.getLogger(__name__)


class BlockArtifactFrame(StageFrame):
    NAME = "block_artifact"

    def build(self) -> None:
        self.configure(bg=theme.TASK_BG)

        # Pinned bottom: timer.
        self._timer_label = tk.Label(
            self, text="--:--",
            bg=theme.TASK_BG, fg="#666666", font=theme.FONT_BODY_BOLD,
        )
        self._timer_label.pack(side="bottom", pady=(0, 30))

        # Top: small counter line.
        tk.Frame(self, bg=theme.TASK_BG, height=40).pack(side="top")
        self._heading = tk.Label(
            self, text="Block 4 of 4  ·  Artifact creation",
            bg=theme.TASK_BG, fg="#888888", font=theme.FONT_SUBHEADING,
        )
        self._heading.pack(side="top", pady=(0, 6))

        # Center: cue label fills.
        self._cue_label = tk.Label(
            self, text="Listen for the prompts…",
            bg=theme.TASK_BG, fg=theme.TASK_TEXT, font=theme.FONT_TASK_MED,
            wraplength=900, justify="center",
        )
        self._cue_label.pack(side="top", expand=True)
        self.register_wrappable(self._cue_label, ratio=0.7, min_px=400)

        self._block_id = 4
        self._block_start_lsl: float | None = None
        self._block_start_monotonic: float | None = None
        self._duration_planned_s: float = 90.0
        self._events: list[dict[str, Any]] = []
        self._block_record: Any = None
        self._playback: Any = None
        self._tick_after: str | None = None
        self._closed = False
        self._attempt: int = 1
        self._health_token: float | None = None
        self._health_report: dict[str, str] = {}

    def on_enter(self, **payload: Any) -> None:
        cfg = getattr(self.app, "config", None)
        if cfg is None:
            self._heading.configure(text="(config not loaded)")
            return
        block = cfg.block_by_id(4)
        self._duration_planned_s = block.duration_s
        self._attempt = int(payload.get("attempt", 1))
        self._events = []
        self._closed = False
        # Reset playback state so a Pass B run doesn't see a stale handle
        # from Pass A.
        self._playback = None
        self._block_record = None
        self._block_start_lsl = None
        self._block_start_monotonic = None
        self._health_token = None
        self._health_report = {}

        # Pre-roll: emit block_start at the moment audio playback starts.
        self._launch()

    def on_leave(self) -> None:
        self._closed = True
        self._cancel_tick()
        try:
            if self._playback is not None:
                self._playback.stop()
        except Exception:  # noqa: BLE001
            logger.exception("playback stop failed")

    def _launch(self) -> None:
        sched = self.app.audio_scheduler
        cfg = self.app.config
        outlet = self.app.outlet
        pass_label = self.app.pass_label
        attempt = self._attempt

        sound_dir = REPO_ROOT / "assets" / "sounds"

        if sched is None:
            self._cue_label.configure(
                text="(no audio device — block skipped)"
            )
            # Even without audio, record a block marker so analysts can find the gap.
            self._block_start_lsl = pylsl.local_clock()
            self._health_token = self._block_start_lsl
            if outlet is not None:
                outlet.block_start(block_id=4, label="artifact",
                                   pass_label=pass_label,
                                   duration_planned_s=self._duration_planned_s,
                                   attempt=attempt)
            self._block_start_monotonic = time.monotonic()
            self._tick()
            return

        try:
            schedule = build_artifact_schedule(
                cfg.artifact_block.cues,
                cue_wav_dir=sound_dir,
                sample_rate=sched.sample_rate,
                require_wav_files=True,
            )
        except FileNotFoundError as e:
            logger.exception("missing artifact WAV")
            self._cue_label.configure(text=f"Missing audio asset:\n{e}")
            return

        def on_block_start(t0_lsl: float) -> None:
            self._block_start_lsl = t0_lsl
            self._block_start_monotonic = time.monotonic()
            # The stream-health window for this block opens at block start.
            self._health_token = t0_lsl
            if outlet is not None:
                payload = {"block": 4, "label": "artifact", "pass": pass_label,
                           "duration_planned_s": self._duration_planned_s}
                if attempt > 1:
                    payload["attempt"] = attempt
                try:
                    outlet.push("block_start", payload, lsl_timestamp=t0_lsl)
                except Exception:  # noqa: BLE001
                    logger.exception("block_start marker failed")
            self._events.append(
                {"label": "block_start", "kind": "marker", "lsl_ts": t0_lsl,
                 "payload": {"block": 4, "label": "artifact"}}
            )
            # Begin GUI timer.
            self.app.enqueue_ui(self._tick)
            # Block-level writer record (LSL-anchored).
            if self.app.writer is not None:
                self._block_record = self.app.writer.start_block(
                    4, "artifact", self._duration_planned_s, t0_lsl, attempt=attempt
                )

        def on_cue_start(label: str, lsl_ts: float) -> None:
            if outlet is not None:
                try:
                    outlet.artifact(label=label, kind="start",
                                    pass_label=pass_label, lsl_timestamp=lsl_ts,
                                    attempt=attempt)
                except Exception:  # noqa: BLE001
                    logger.exception("artifact start marker failed")
            self._events.append(
                {"label": f"artifact:{label}:start", "kind": "marker",
                 "lsl_ts": lsl_ts, "payload": {"block": 4}}
            )
            text = label.replace("_", " ").title()
            self.app.enqueue_ui(
                lambda t=text: self._cue_label.configure(
                    text=t, fg=theme.ACCENT_LIGHT, font=theme.FONT_TASK_MED,
                )
            )

        def on_cue_end(label: str, lsl_ts: float) -> None:
            if outlet is not None:
                try:
                    outlet.artifact(label=label, kind="end",
                                    pass_label=pass_label, lsl_timestamp=lsl_ts,
                                    attempt=attempt)
                except Exception:  # noqa: BLE001
                    logger.exception("artifact end marker failed")
            self._events.append(
                {"label": f"artifact:{label}:end", "kind": "marker",
                 "lsl_ts": lsl_ts, "payload": {"block": 4}}
            )

        def on_finished() -> None:
            self.app.enqueue_ui(self._end_block)

        try:
            self._playback = sched.schedule_artifact_block(
                schedule=schedule,
                on_block_start=on_block_start,
                on_cue_start=on_cue_start,
                on_cue_end=on_cue_end,
                on_finished=on_finished,
            )
        except Exception:  # noqa: BLE001
            logger.exception("schedule_artifact_block failed")
            self._end_block()

    # ----- timer ----------------------------------------------------------

    def _tick(self) -> None:
        if self._closed or self._block_start_monotonic is None:
            return
        elapsed = time.monotonic() - self._block_start_monotonic
        remaining = max(0.0, self._duration_planned_s - elapsed)
        m, s = divmod(int(remaining), 60)
        self._timer_label.configure(text=f"{m:02d}:{s:02d}")
        if remaining <= 0:
            return
        self._tick_after = self.after(250, self._tick)

    def _cancel_tick(self) -> None:
        if self._tick_after is not None:
            try:
                self.after_cancel(self._tick_after)
            except tk.TclError:
                pass
            self._tick_after = None

    # ----- end + advance --------------------------------------------------

    def _end_block(self) -> None:
        if self._closed:
            return
        end_lsl = pylsl.local_clock()
        outlet = self.app.outlet
        writer = self.app.writer
        if self._block_start_lsl is None:
            self._block_start_lsl = end_lsl
        # Worst per-stream health observed across this block window.
        self._health_report = block_window_report(self.app, self._health_token)
        if outlet is not None:
            try:
                outlet.block_end(
                    block_id=4, label="artifact",
                    pass_label=self.app.pass_label,
                    duration_actual_s=end_lsl - self._block_start_lsl,
                    attempt=self._attempt,
                )
            except Exception:  # noqa: BLE001
                logger.exception("block_end marker failed")
        if writer is not None and self._block_record is not None:
            try:
                self._block_record.stream_health = self._health_report or None
                writer.end_block(
                    self._block_record, end_lsl, events=self._events,
                    status="completed",
                )
            except Exception:  # noqa: BLE001
                logger.exception("writer.end_block failed")
        self._cancel_tick()
        self._cue_label.configure(text="Done.")
        self._timer_label.configure(text="00:00")
        # After block 4, advance to between_block_settle which routes to
        # pass_switch (Pass A) or completion (Pass B) — or to stream_recovery
        # if a stream dropped during the block.
        self.app.show_stage(
            "between_block_settle", after_block=4,
            attempt=self._attempt, health_report=self._health_report,
        )
