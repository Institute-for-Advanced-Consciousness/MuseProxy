"""Generic runner for breathing blocks (1, 2, 3).

Block 1 (baseline, ``paced=False``): no audio cues; emits ``block_start``,
runs a 5-minute timer, plays pre/post TTS for close_eyes/open_eyes,
emits ``block_end``.

Blocks 2/3 (paced): drives
``AudioScheduler.schedule_paced_breathing`` with the block's
``rate_hz`` / ``inhale_duration_s`` / ``exhale_duration_s``.
"""

from __future__ import annotations

import logging
import time
import tkinter as tk
from typing import Any

import pylsl

from .. import theme
from ..breathing import build_paced_schedule
from ..config import REPO_ROOT
from ._base import StageFrame
from .stream_recovery import block_window_report

logger = logging.getLogger(__name__)


class BlockBreathingFrame(StageFrame):
    NAME = "block_breathing"

    def build(self) -> None:
        self.configure(bg=theme.TASK_BG)

        # Pinned bottom: small timer.
        self._timer_label = tk.Label(
            self, text="--:--",
            bg=theme.TASK_BG, fg="#666666", font=theme.FONT_BODY_BOLD,
        )
        self._timer_label.pack(side="bottom", pady=(0, 30))

        # Top: small block-counter label.
        tk.Frame(self, bg=theme.TASK_BG, height=40).pack(side="top")
        self._heading = tk.Label(
            self, text="",
            bg=theme.TASK_BG, fg="#888888", font=theme.FONT_SUBHEADING,
        )
        self._heading.pack(side="top", pady=(0, 4))

        # Center: huge cue label that fills.
        self._cue_label = tk.Label(
            self, text="",
            bg=theme.TASK_BG, fg=theme.TASK_TEXT, font=theme.FONT_TASK,
            justify="center",
        )
        self._cue_label.pack(side="top", expand=True)

        # Internal state.
        self._block_id: int | None = None
        self._block_label: str | None = None
        self._duration_planned_s: float = 0.0
        self._block_start_lsl: float | None = None
        self._block_start_monotonic: float | None = None
        self._events: list[dict[str, Any]] = []
        self._block_record: Any = None
        self._tick_after: str | None = None
        self._playback: Any = None
        self._tts_handle: Any = None
        self._closed = False
        self._attempt: int = 1
        self._health_token: float | None = None
        self._health_report: dict[str, str] = {}

    def on_enter(self, **payload: Any) -> None:
        block_id = payload.get("block_id")
        if block_id is None:
            raise ValueError("block_breathing requires block_id payload")
        self._block_id = int(block_id)
        self._attempt = int(payload.get("attempt", 1))
        self._closed = False
        self._events = []
        # Reset playback / handle / record state so prior blocks don't
        # interfere (e.g. silent timer checking "is playback active?").
        self._playback = None
        self._tts_handle = None
        self._block_record = None
        self._block_start_lsl = None
        self._block_start_monotonic = None
        self._health_token = None
        self._health_report = {}

        cfg = getattr(self.app, "config", None)
        if cfg is None:
            self._heading.configure(text="(config not loaded)")
            return
        block = cfg.block_by_id(self._block_id)
        self._block_label = block.label
        self._duration_planned_s = block.duration_s
        friendly = {
            "baseline": "Natural breathing",
            "paced_0p1Hz": "Slow paced breathing",
            "paced_0p2Hz": "Fast paced breathing",
        }.get(block.label, block.label)
        self._heading.configure(text=f"Block {self._block_id} of 4  ·  {friendly}")

        if block.paced:
            self._cue_label.configure(text="•", fg="#444444")
        else:
            self._cue_label.configure(
                text="Breathe naturally\nEyes closed",
                fg=theme.TASK_TEXT, font=theme.FONT_TASK_MED,
            )

        # Play "Close your eyes" TTS first; then start the block on its
        # finished callback.
        self._play_pre_tts()

    def on_leave(self) -> None:
        self._closed = True
        self._cancel_tick()
        try:
            if self._playback is not None:
                self._playback.stop()
        except Exception:  # noqa: BLE001
            logger.exception("playback stop failed")
        try:
            if self._tts_handle is not None:
                self._tts_handle.stop()
        except Exception:  # noqa: BLE001
            logger.exception("tts stop failed")

    # ----- TTS bookends ---------------------------------------------------

    def _play_pre_tts(self) -> None:
        sched = self.app.audio_scheduler
        wav = REPO_ROOT / "assets" / "sounds" / "close_eyes.wav"
        if sched is None or not wav.exists():
            self._start_block_run()
            return
        outlet = self.app.outlet

        def on_first(t0_lsl: float) -> None:
            if outlet is not None:
                try:
                    outlet.tts(clip="close_eyes", kind="start", lsl_timestamp=t0_lsl)
                except Exception:  # noqa: BLE001
                    logger.exception("tts:close_eyes:start failed")

        def on_done() -> None:
            if outlet is not None:
                try:
                    outlet.tts(clip="close_eyes", kind="end")
                except Exception:  # noqa: BLE001
                    logger.exception("tts:close_eyes:end failed")
            self.app.enqueue_ui(self._start_block_run)

        try:
            self._tts_handle = sched.play_wav(wav, on_onset=on_first, on_finished=on_done)
        except Exception:  # noqa: BLE001
            logger.exception("close_eyes TTS playback failed")
            self._start_block_run()

    def _play_post_tts_then_advance(self) -> None:
        sched = self.app.audio_scheduler
        wav = REPO_ROOT / "assets" / "sounds" / "open_eyes.wav"
        if sched is None or not wav.exists():
            self._advance()
            return
        outlet = self.app.outlet

        def on_first(t0_lsl: float) -> None:
            if outlet is not None:
                try:
                    outlet.tts(clip="open_eyes", kind="start", lsl_timestamp=t0_lsl)
                except Exception:  # noqa: BLE001
                    logger.exception("tts:open_eyes:start failed")

        def on_done() -> None:
            if outlet is not None:
                try:
                    outlet.tts(clip="open_eyes", kind="end")
                except Exception:  # noqa: BLE001
                    logger.exception("tts:open_eyes:end failed")
            self.app.enqueue_ui(self._advance)

        try:
            self._tts_handle = sched.play_wav(wav, on_onset=on_first, on_finished=on_done)
        except Exception:  # noqa: BLE001
            logger.exception("open_eyes TTS playback failed")
            self._advance()

    # ----- block run ------------------------------------------------------

    def _start_block_run(self) -> None:
        if self._closed:
            return
        cfg = getattr(self.app, "config", None)
        if cfg is None or self._block_id is None:
            return
        block = cfg.block_by_id(self._block_id)

        self._block_start_lsl = pylsl.local_clock()
        self._block_start_monotonic = time.monotonic()
        # The stream-health window for this block opens at block start.
        self._health_token = self._block_start_lsl

        outlet = self.app.outlet
        writer = self.app.writer
        if writer is not None:
            self._block_record = writer.start_block(
                self._block_id, block.label, block.duration_s,
                self._block_start_lsl, attempt=self._attempt,
            )
        if outlet is not None:
            try:
                outlet.block_start(
                    block_id=self._block_id,
                    label=block.label,
                    pass_label=self.app.pass_label,
                    duration_planned_s=block.duration_s,
                    attempt=self._attempt,
                )
                self._events.append(
                    {"label": "block_start", "kind": "marker",
                     "lsl_ts": self._block_start_lsl,
                     "payload": {"block": self._block_id, "label": block.label}}
                )
            except Exception:  # noqa: BLE001
                logger.exception("block_start marker failed")

        if block.paced:
            self._launch_paced(block)
        else:
            # Block 1: silent timer.
            self._cue_label.configure(text="Breathe normally\nEyes closed")
            self._tick()

    def _launch_paced(self, block: Any) -> None:
        sched = self.app.audio_scheduler
        if sched is None:
            # No audio — still record cycle markers by best-effort timing.
            self._cue_label.configure(text="(no audio device — silent timing)")
            self._tick()
            return

        outlet = self.app.outlet
        pass_label = self.app.pass_label
        block_id = self._block_id
        attempt = self._attempt

        schedule = build_paced_schedule(
            rate_hz=block.rate_hz,
            inhale_duration_s=block.inhale_duration_s,
            exhale_duration_s=block.exhale_duration_s,
            duration_s=block.duration_s,
            sample_rate=sched.sample_rate,
        )

        inhale_wav = REPO_ROOT / "assets" / "sounds" / "in.wav"
        exhale_wav = REPO_ROOT / "assets" / "sounds" / "out.wav"

        def on_inhale(cycle: int, lsl_ts: float) -> None:
            if outlet is not None:
                try:
                    outlet.breath("inhale", block_id=block_id, cycle=cycle,
                                  pass_label=pass_label, lsl_timestamp=lsl_ts,
                                  attempt=attempt)
                except Exception:  # noqa: BLE001
                    logger.exception("breath:inhale marker failed")
            self._events.append(
                {"label": "breath:inhale", "kind": "marker", "lsl_ts": lsl_ts,
                 "payload": {"block": block_id, "cycle": cycle}}
            )
            self.app.enqueue_ui(
                lambda: self._cue_label.configure(
                    text="BREATHE IN",
                    fg=theme.ACCENT_LIGHT,
                    font=theme.FONT_TASK,
                )
            )

        def on_exhale(cycle: int, lsl_ts: float) -> None:
            if outlet is not None:
                try:
                    outlet.breath("exhale", block_id=block_id, cycle=cycle,
                                  pass_label=pass_label, lsl_timestamp=lsl_ts,
                                  attempt=attempt)
                except Exception:  # noqa: BLE001
                    logger.exception("breath:exhale marker failed")
            self._events.append(
                {"label": "breath:exhale", "kind": "marker", "lsl_ts": lsl_ts,
                 "payload": {"block": block_id, "cycle": cycle}}
            )
            self.app.enqueue_ui(
                lambda: self._cue_label.configure(
                    text="BREATHE OUT",
                    fg=theme.TEXT_ACCENT,
                    font=theme.FONT_TASK,
                )
            )

        def on_finished() -> None:
            self.app.enqueue_ui(self._end_block)

        try:
            self._playback = sched.schedule_paced_breathing(
                schedule=schedule,
                inhale_wav=inhale_wav,
                exhale_wav=exhale_wav,
                on_inhale=on_inhale,
                on_exhale=on_exhale,
                on_finished=on_finished,
            )
        except Exception:  # noqa: BLE001
            logger.exception("schedule_paced_breathing failed")
            self._end_block()
            return

        self._tick()

    # ----- timer ----------------------------------------------------------

    def _tick(self) -> None:
        if self._closed or self._block_start_monotonic is None:
            return
        elapsed = time.monotonic() - self._block_start_monotonic
        remaining = max(0.0, self._duration_planned_s - elapsed)
        m, s = divmod(int(remaining), 60)
        self._timer_label.configure(text=f"{m:02d}:{s:02d}")
        if remaining <= 0:
            # For paced blocks the audio's on_finished will end the block.
            # For silent blocks, end now.
            if self._playback is None:
                self._end_block()
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
        if self._block_start_lsl is None or self._block_id is None or self._block_label is None:
            return
        end_lsl = pylsl.local_clock()
        outlet = self.app.outlet
        writer = self.app.writer
        # Worst per-stream health observed across this block window.
        self._health_report = block_window_report(self.app, self._health_token)
        if outlet is not None:
            try:
                outlet.block_end(
                    block_id=self._block_id,
                    label=self._block_label,
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
        self._cue_label.configure(text="")
        self._timer_label.configure(text="00:00")
        # Post-TTS, then advance.
        self._play_post_tts_then_advance()

    def _advance(self) -> None:
        if self._closed:
            return
        if self._block_id is None:
            return
        self.app.show_stage(
            "between_block_settle",
            after_block=self._block_id,
            attempt=self._attempt,
            health_report=self._health_report,
        )
