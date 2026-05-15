"""Tk root + stage controller. Entry point for the MuseProxy GUI runner.

The shell is intentionally thin: experimental logic — block scheduling,
audio playback, marker emission, persistence — lives in the individual
stage modules and the support modules (``audio``, ``markers``,
``persistence``). This file owns:

  * The Tk root and the registry of ``StageFrame`` instances.
  * ``show_stage(name, **payload)``, the single transition point.
  * The thread-safe ``_ui_queue`` (callables enqueued by worker threads,
    drained on the main thread every 50 ms by ``_poll_ui_queue``).
  * Abort handling (Escape key + window-close + an idempotent
    ``abort()`` method).
  * The ``--self-check`` CLI entry.

Pass-aware state lives here too: ``self.pass_label`` (``"A"`` or ``"B"``)
and the per-pass services ``self.outlet``, ``self.writer``,
``self.audio_scheduler`` — re-created at the pass switch.
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import tkinter as tk
from collections.abc import Callable, Iterable
from tkinter import messagebox, ttk
from typing import Any

from . import theme
from .stages._base import StageFrame
from .stages.between_block_settle import BetweenBlockSettleFrame
from .stages.block_artifact import BlockArtifactFrame
from .stages.block_breathing import BlockBreathingFrame
from .stages.block_instructions import BlockInstructionsFrame
from .stages.completion import CompletionFrame
from .stages.intake import IntakeFrame
from .stages.pass_switch import PassSwitchFrame
from .stages.pre_launch import PreLaunchFrame
from .stages.settle import SettleFrame
from .stages.volume_check import VolumeCheckFrame

logger = logging.getLogger(__name__)


__all__ = ["App", "main"]


_STAGE_CLASSES: tuple[type[StageFrame], ...] = (
    IntakeFrame,
    PreLaunchFrame,
    VolumeCheckFrame,
    SettleFrame,
    BlockInstructionsFrame,
    BlockBreathingFrame,
    BlockArtifactFrame,
    BetweenBlockSettleFrame,
    PassSwitchFrame,
    CompletionFrame,
)


# ---------------------------------------------------------------------------
# App / controller
# ---------------------------------------------------------------------------


class App:
    """Owns the Tk root, the stage registry, and per-pass services.

    Service attributes (``outlet``, ``writer``, ``audio_scheduler``) are
    ``None`` until the intake stage opens them, and are re-created at
    the pass switch. Stages are written to tolerate ``None`` services so
    a developer can walk the GUI without LSL/audio for layout review.
    """

    INITIAL_STAGE = "intake"

    # Public surface for stage frames -- set on every instance --------------
    theme_bg: str = theme.CHROME_BG

    def __init__(self, *, headless: bool = False) -> None:
        """Construct the App.

        ``headless`` is for tests: skip the Tk geometry/style setup that
        flakes in headless CI but keep the queue + registry intact. In
        practice the test suite spins up a real Tk via the
        ``shared_app`` fixture; ``headless`` is reserved.
        """
        self.theme_bg = theme.CHROME_BG

        self.root = tk.Tk()
        self.root.title("MuseProxy — IACS Muse Proxy Metrics Study")
        self.root.configure(bg=self.theme_bg)
        self.root.geometry("1280x800")
        self._maximize_window()

        # ttk styles (dark Combobox + Scrollbar to match chrome).
        self.style = ttk.Style(self.root)
        theme.configure_ttk_styles(self.style)

        # Option-DB defaults so any widget we forget to style still
        # blends into the dark chrome.
        self.root.option_add("*Background", theme.CHROME_BG)
        self.root.option_add("*Foreground", theme.TEXT)
        self.root.option_add("*selectBackground", theme.ACCENT_DARK)
        self.root.option_add("*selectForeground", "#FFFFFF")

        # ----- runtime services (None until intake submit) ------------------
        self.outlet: Any = None  # MuseProxyMarkerOutlet | None
        self.writer: Any = None  # PassWriter | None
        self.audio_scheduler: Any = None  # AudioScheduler | None

        # ----- visit state --------------------------------------------------
        self.participant_id: str | None = None
        self.pass_label: str = "A"  # flips to "B" at pass switch
        self.pass_a_device: str | None = None  # "muse2" | "athena"
        self.pass_b_device: str | None = None
        self.intake_payload: dict[str, Any] = {}

        # ----- stage registry ----------------------------------------------
        self._frames: dict[str, StageFrame] = {}
        self._current: str | None = None
        self._register_frames(_STAGE_CLASSES)

        # ----- thread-safe UI update queue ---------------------------------
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._poll_ui_queue()

        # ----- abort hooks --------------------------------------------------
        self.root.bind("<Escape>", self._on_escape)
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

    # ----- window helpers ------------------------------------------------

    def _maximize_window(self) -> None:
        try:
            self.root.state("zoomed")
        except tk.TclError:
            try:
                self.root.attributes("-zoomed", True)
            except tk.TclError:
                pass

    # ----- frame registry ------------------------------------------------

    def _register_frames(self, classes: Iterable[type[StageFrame]]) -> None:
        for cls in classes:
            if cls.NAME == StageFrame.NAME:
                raise ValueError(f"{cls.__name__} did not override NAME")
            if cls.NAME in self._frames:
                raise ValueError(f"duplicate stage NAME: {cls.NAME!r}")
            self._frames[cls.NAME] = cls(self)

    # ----- public API used by stages -------------------------------------

    def show_stage(self, name: str, **payload: Any) -> None:
        """Hide the current frame, on_leave it, pack the named frame, on_enter it."""
        if name not in self._frames:
            raise KeyError(
                f"unknown stage: {name!r}. Known: {sorted(self._frames)}"
            )
        if self._current is not None:
            old = self._frames[self._current]
            try:
                old.on_leave()
            except Exception:  # noqa: BLE001
                logger.exception("stage %s.on_leave raised", self._current)
            old.pack_forget()
        frame = self._frames[name]
        frame.pack(fill="both", expand=True)
        # Set _current BEFORE on_enter so a recursive show_stage call
        # from within on_enter does not get clobbered when this outer
        # call returns. (E.g. a zero-duration settle that auto-advances.)
        self._current = name
        try:
            frame.on_enter(**payload)
        except Exception:
            logger.exception("stage %s.on_enter raised", name)
            raise
        logger.info("stage -> %s (payload keys: %s)", name, sorted(payload))

    @property
    def current_stage(self) -> str | None:
        return self._current

    @property
    def stages(self) -> dict[str, StageFrame]:
        return dict(self._frames)

    # ----- thread-safe UI updates ----------------------------------------

    def enqueue_ui(self, func: Callable[[], None]) -> None:
        """Schedule a callable to run on the Tk main thread.

        Worker threads (audio callbacks, timers) MUST NOT touch Tk
        widgets directly — uv's bundled Tk is not safe across threads.
        Push a closure onto the queue instead.
        """
        self._ui_queue.put(func)

    def _poll_ui_queue(self) -> None:
        """Drain the UI queue once, then reschedule. Runs on the main thread."""
        try:
            while True:
                func = self._ui_queue.get_nowait()
                try:
                    func()
                except tk.TclError:
                    pass  # widget destroyed mid-flight; harmless on shutdown
                except Exception:  # noqa: BLE001
                    logger.exception("ui queue callable raised")
        except queue.Empty:
            pass
        try:
            self.root.after(50, self._poll_ui_queue)
        except tk.TclError:
            pass  # root destroyed during shutdown

    # ----- abort handling ------------------------------------------------

    # Test seam: tests monkeypatch this to skip the Tk modal dialog.
    confirm_abort = staticmethod(
        lambda: messagebox.askyesno(
            "Abort session?",
            "Abort session?\n\nData so far will be saved as a "
            "PARTIAL_session.json for the current pass.",
            icon="warning",
        )
    )

    def _on_escape(self, _event: object = None) -> None:
        if not self.confirm_abort():
            return
        self.abort()

    def _on_window_close(self) -> None:
        if not self.confirm_abort():
            return
        self.abort()

    def abort(self) -> None:
        """Mark the session as aborted, refresh PARTIAL, close outlet, exit.

        Idempotent — safe to call multiple times. Does not raise even if
        services are partially attached or already shut down.
        """
        logger.warning(
            "abort requested at pass=%s stage=%s", self.pass_label, self._current
        )
        if self.outlet is not None:
            try:
                self.outlet.session_abort(pass_label=self.pass_label, stage=self._current or "?")
            except Exception:  # noqa: BLE001
                logger.exception("session_abort marker push failed")
        if self.writer is not None:
            try:
                self.writer.mark_pending_blocks_aborted()
                self.writer.finalize(status="aborted")
            except Exception:  # noqa: BLE001
                logger.exception("writer.finalize(aborted) failed")
        if self.outlet is not None:
            try:
                self.outlet.close()
            except Exception:  # noqa: BLE001
                logger.exception("outlet.close failed")
        self.shutdown()

    # ----- lifecycle -----------------------------------------------------

    def run(self) -> None:
        self.show_stage(self.INITIAL_STAGE)
        self.root.mainloop()

    def shutdown(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="muse-proxy")
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run install-time sanity checks and exit. Returns 0 on success, 1 on failure.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional path to an alternate session_defaults.yaml.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.self_check:
        return _run_self_check(args.config)

    app = App()
    try:
        app.run()
    finally:
        app.shutdown()
    return 0


def _run_self_check(config_path: str | None) -> int:
    """Confirm the install is good. Returns 0 if all PASS, 1 if any FAIL.

    Phase 1 implements the checks that don't depend on config / WAVs.
    Later phases will add the remaining checks.
    """
    failures: list[str] = []

    # 1) Python version. The check is unreachable at runtime — pyproject
    # already pins requires-python>=3.11 — but is useful diagnostic
    # output for sysadmins running --self-check.
    py = sys.version.split()[0]
    print(f"PASS python version                 {py}")

    # 2) Required dependencies import
    dep_failures: list[str] = []
    for name in ("pylsl", "sounddevice", "soundfile", "numpy", "pydantic", "yaml"):
        try:
            __import__(name)
        except ImportError as e:
            dep_failures.append(f"{name}: {e}")
    if dep_failures:
        for d in dep_failures:
            print(f"FAIL dependency import              {d}", file=sys.stderr)
        failures.append("dependency imports")
    else:
        print("PASS all required dependencies importable")

    # 3) Config — load + validate via pydantic.
    from .config import DEFAULT_CONFIG_PATH, load_config
    cfg_path = DEFAULT_CONFIG_PATH if config_path is None else config_path
    from pathlib import Path
    cfg = None
    if not Path(cfg_path).exists():
        print(f"FAIL config file                    {cfg_path}", file=sys.stderr)
        failures.append("config file")
    else:
        try:
            cfg = load_config(cfg_path)
            print(f"PASS config validates               {cfg_path}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL config schema                  {e}", file=sys.stderr)
            failures.append("config schema")

    # 4) WAV assets — check the full manifest declared by config.
    sounds_dir = Path(__file__).resolve().parents[2] / "assets" / "sounds"
    if not sounds_dir.exists():
        print(f"WARN sounds directory missing       {sounds_dir} "
              "(run scripts/generate_audio.py at install time)")
    elif cfg is not None:
        # Reproduce the manifest the renderer writes — common cues +
        # one artifact_{label}.wav per cue.
        expected: list[str] = [
            "volume_check.wav", "in.wav", "out.wav",
            "close_eyes.wav", "open_eyes.wav", "take_a_moment.wav",
        ]
        for cue in cfg.artifact_block.cues:
            expected.append(f"artifact_{cue.label}.wav")
        missing = [name for name in expected
                   if not (sounds_dir / name).exists()]
        if missing:
            print(
                f"FAIL {len(missing)} WAV file(s) missing in {sounds_dir}: "
                f"{', '.join(missing)} (run scripts/generate_audio.py)",
                file=sys.stderr,
            )
            failures.append("WAV manifest")
        else:
            print(
                f"PASS {len(expected)} WAV files present       at {sounds_dir}"
            )
    else:
        wavs = list(sounds_dir.glob("*.wav"))
        if not wavs:
            print(
                f"WARN no WAV files yet               {sounds_dir} "
                "(run scripts/generate_audio.py)"
            )
        else:
            print(f"WARN {len(wavs)} WAV files present (config not loaded, can't verify manifest)")

    # 5) Audio output device discoverable
    try:
        import sounddevice as sd
        info = sd.query_devices(None, "output")
        name = info["name"] if isinstance(info, dict) else str(info)
        print(f"PASS audio output device            {name}")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL audio output device            {e}", file=sys.stderr)
        failures.append("audio device")

    # 6) LSL outlet can be created and a sample pushed
    try:
        from pylsl import StreamInfo, StreamOutlet, cf_string
        info = StreamInfo(
            name="MuseProxySelfCheck",
            type="Markers",
            channel_count=1,
            nominal_srate=0,
            channel_format=cf_string,
            source_id="MuseProxy_self_check",
        )
        outlet = StreamOutlet(info)
        outlet.push_sample(["self_check_ping"])
        del outlet
        print("PASS LSL outlet open + push_sample")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL LSL outlet                     {e}", file=sys.stderr)
        failures.append("LSL outlet")

    # 7) Tk display
    try:
        r = tk.Tk()
        r.withdraw()
        r.destroy()
        print("PASS Tk display available")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL Tk display                     {e}", file=sys.stderr)
        failures.append("Tk display")

    # 8) Data\ directory writable
    data_dir = Path(__file__).resolve().parents[2] / "Data"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".self_check_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(f"PASS Data/ writable                 {data_dir}")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL Data/ writable                 {e}", file=sys.stderr)
        failures.append("Data dir")

    if failures:
        print(f"\nSELF-CHECK FAILED: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nSELF-CHECK OK — ready to launch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
