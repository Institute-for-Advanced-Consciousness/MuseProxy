"""AudioScheduler — sample-anchored marker emission via sounddevice.

The whole study depends on this module being right. The pattern (lifted
from SuperChillsOddball's ``ToneScheduler``):

  1. Pre-render an entire block as one contiguous numpy buffer with
     every cue's WAV inserted at its configured onset sample.
  2. Open a single ``sounddevice.OutputStream`` for the block.
  3. On the first audio callback, capture
     ``t0_lsl = pylsl.local_clock() + (outputBufferDacTime - currentTime)
              - cursor_samples / sample_rate``.
  4. For each cue whose onset sample falls in the current callback
     window, emit its marker with ``timestamp = t0_lsl + onset_sample
     / sample_rate``.

No ``time.sleep`` on the timing-critical path.

Public surface:
  AudioScheduler — owns sample_rate + device handle
    .play_wav(path, on_onset=None) -> threading.Event
    .schedule_paced_breathing(...) -> threading.Event
    .schedule_artifact_block(...) -> threading.Event
    .play_volume_check(...) -> stop() callable
    .stop()  (stops any active stream)

Pure helpers (testable without hardware):
  load_wav_mono(path, sr) -> np.ndarray
  build_paced_buffer(schedule, inhale_wav, exhale_wav, sr) -> np.ndarray
  build_artifact_buffer(schedule, sr) -> np.ndarray
  synth_volume_check_tone(freq_hz, duration_s, sr, amplitude, ramp_s) -> np.ndarray
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from .artifacts import ArtifactSchedule
from .breathing import PacedSchedule

logger = logging.getLogger(__name__)

__all__ = [
    "AudioScheduler",
    "load_wav_mono",
    "build_paced_buffer",
    "build_artifact_buffer",
    "synth_volume_check_tone",
    "AudioPlayback",
]


# ---------------------------------------------------------------------------
# WAV loading
# ---------------------------------------------------------------------------


def load_wav_mono(path: str | Path, sample_rate: int) -> np.ndarray:
    """Load a WAV as mono float32 at ``sample_rate``.

    Resampling is NOT performed — the file must already be at the target
    rate. (We render WAVs at install time at the configured rate; a
    mismatch indicates an install bug we want to catch loudly.)
    """
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if sr != sample_rate:
        raise ValueError(
            f"{path}: sample rate is {sr} Hz; expected {sample_rate} Hz. "
            f"Re-run scripts/generate_audio.py --force."
        )
    if data.ndim == 2:
        # Average to mono.
        data = data.mean(axis=1).astype(np.float32)
    return np.ascontiguousarray(data, dtype=np.float32)


# ---------------------------------------------------------------------------
# Buffer construction (pure)
# ---------------------------------------------------------------------------


def _place_cue(buf: np.ndarray, cue_audio: np.ndarray, onset_sample: int) -> None:
    """Mix ``cue_audio`` into ``buf`` at ``onset_sample`` (clipped at end)."""
    end = onset_sample + len(cue_audio)
    if onset_sample < 0:
        raise ValueError(f"onset_sample < 0: {onset_sample}")
    if end > len(buf):
        # Truncate the cue audio if it would overrun; warn but don't crash —
        # the last cue's window may extend past the block boundary.
        truncated = len(buf) - onset_sample
        if truncated <= 0:
            return
        cue_audio = cue_audio[:truncated]
        end = onset_sample + truncated
    buf[onset_sample:end] += cue_audio


def build_paced_buffer(
    schedule: PacedSchedule,
    *,
    inhale_audio: np.ndarray,
    exhale_audio: np.ndarray,
    sample_rate: int,
) -> np.ndarray:
    """Build a mono float32 buffer with all paced cues placed at their onsets."""
    buf = np.zeros(schedule.total_samples, dtype=np.float32)
    for cue in schedule.cues:
        audio = inhale_audio if cue.kind == "inhale" else exhale_audio
        _place_cue(buf, audio, cue.onset_sample)
    return buf


def build_artifact_buffer(
    schedule: ArtifactSchedule,
    *,
    sample_rate: int,
    audio_for_cue: Callable[[str], np.ndarray],
) -> np.ndarray:
    """Build a mono float32 buffer with all artifact cue WAVs at their onsets.

    ``audio_for_cue(label)`` returns the loaded numpy audio for that label.
    Indirection (rather than reading WAV paths inline) makes it trivial
    to stub for tests.
    """
    buf = np.zeros(schedule.total_samples, dtype=np.float32)
    for cue in schedule.cues:
        audio = audio_for_cue(cue.label)
        _place_cue(buf, audio, cue.onset_sample)
    return buf


def synth_volume_check_tone(
    *,
    frequency_hz: float,
    duration_s: float,
    sample_rate: int,
    amplitude: float = 0.3,
    ramp_s: float = 0.05,
) -> np.ndarray:
    """Pure-numpy 1 kHz (or other) reference tone with cosine on/off ramps."""
    n = int(round(duration_s * sample_rate))
    t = np.arange(n, dtype=np.float64) / sample_rate
    tone = np.sin(2 * np.pi * frequency_hz * t).astype(np.float32)
    ramp_n = max(1, int(round(ramp_s * sample_rate)))
    if 2 * ramp_n < n:
        ramp = 0.5 * (1 - np.cos(np.linspace(0, np.pi, ramp_n, dtype=np.float32)))
        tone[:ramp_n] *= ramp
        tone[-ramp_n:] *= ramp[::-1]
    return (tone * amplitude).astype(np.float32)


# ---------------------------------------------------------------------------
# Playback handle
# ---------------------------------------------------------------------------


class AudioPlayback:
    """Handle for an in-flight audio stream.

    ``done`` is set when the entire buffer has been played out (or
    ``stop()`` was called). ``stop()`` is idempotent.

    The optional ``t0_lsl_event`` is set the first time the audio
    callback fires — at that moment ``t0_lsl`` holds the LSL clock
    timestamp corresponding to sample 0 reaching the DAC. Stages that
    care about the absolute onset time can wait on the event.
    """

    def __init__(self) -> None:
        self.done = threading.Event()
        self.t0_lsl_event = threading.Event()
        self.t0_lsl: float | None = None
        self._stream: Any = None  # sounddevice.OutputStream | None
        self._stopped = False

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._stream is not None:
            try:
                self._stream.stop(ignore_errors=True)
                self._stream.close(ignore_errors=True)
            except Exception:  # noqa: BLE001
                logger.exception("AudioPlayback.stop: stream teardown failed")
        self.done.set()


# ---------------------------------------------------------------------------
# AudioScheduler — owns one stream at a time
# ---------------------------------------------------------------------------


class AudioScheduler:
    """Plays a single buffer at a time, with sample-anchored marker emission.

    Construct once at intake; ``stop()`` on app teardown. Every stage
    that needs audio calls ``play_*()`` (or ``play_buffer()``), gets back
    an ``AudioPlayback`` handle, and either blocks on ``handle.done`` or
    polls it from the Tk thread.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 44100,
        device: int | str | None = None,
        blocksize: int = 1024,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.blocksize = blocksize
        self._active: AudioPlayback | None = None
        self._lock = threading.Lock()

    # ----- raw buffer playback -------------------------------------------

    def play_buffer(
        self,
        buf: np.ndarray,
        *,
        cue_onsets: list[tuple[int, Callable[[float], None]]] | None = None,
        on_first_callback: Callable[[float], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> AudioPlayback:
        """Play ``buf`` (mono float32) through sounddevice.

        ``cue_onsets`` is a list of ``(onset_sample, callback)`` pairs.
        Each callback is invoked exactly once, on the audio thread, with
        the LSL timestamp at which that sample reaches the DAC.

        ``on_first_callback(t0_lsl)`` is invoked once at the start of the
        first audio callback — useful for ``block_start`` markers that
        should be anchored to the buffer's sample 0.

        ``on_finished`` is invoked from the sounddevice ``finished``
        callback, after the entire buffer has played.
        """
        import pylsl
        import sounddevice as sd

        if buf.ndim != 1:
            raise ValueError(f"buf must be 1-D mono; got shape {buf.shape}")
        buf = np.ascontiguousarray(buf, dtype=np.float32)

        # Sort cue_onsets by sample so we can scan them in order.
        cues: list[tuple[int, Callable[[float], None]]] = sorted(
            cue_onsets or [], key=lambda c: c[0]
        )

        playback = AudioPlayback()
        cursor = [0]  # samples written so far
        cue_idx = [0]  # next cue to consider
        t0_holder: dict[str, float] = {}
        # True once the buffer's tail has been written — i.e. playback ran to
        # completion rather than being stopped early (abort / on_leave).
        reached_tail = [False]

        sr = self.sample_rate

        def callback(outdata, frames, time_info, status):
            if status:
                logger.warning("audio callback status: %s", status)

            # Capture t0_lsl on the very first callback.
            if "t0_lsl" not in t0_holder:
                current_time = float(time_info.currentTime)
                dac_time = float(time_info.outputBufferDacTime)
                local = pylsl.local_clock()
                # When the very first sample (cursor=0) reaches the DAC,
                # its LSL timestamp is `local + (dac_time - current_time)`.
                t0_lsl = local + (dac_time - current_time)
                t0_holder["t0_lsl"] = t0_lsl
                playback.t0_lsl = t0_lsl
                playback.t0_lsl_event.set()
                if on_first_callback is not None:
                    try:
                        on_first_callback(t0_lsl)
                    except Exception:  # noqa: BLE001
                        logger.exception("on_first_callback raised")

            start = cursor[0]
            end = start + frames
            chunk = buf[start:end]
            if len(chunk) < frames:
                # Tail: pad with silence, then trigger finish after this callback.
                outdata[: len(chunk), 0] = chunk
                outdata[len(chunk):, 0] = 0.0
                reached_tail[0] = True
                raise sd.CallbackStop
            else:
                outdata[:, 0] = chunk

            # Fire any cue callbacks whose onset_sample falls in [start, end).
            t0_lsl = t0_holder["t0_lsl"]
            while cue_idx[0] < len(cues):
                onset_sample, cb = cues[cue_idx[0]]
                if onset_sample < start:
                    # Late — fire anyway, but flag.
                    cue_ts = t0_lsl + onset_sample / sr
                    try:
                        cb(cue_ts)
                    except Exception:  # noqa: BLE001
                        logger.exception("cue callback raised")
                    cue_idx[0] += 1
                    continue
                if onset_sample < end:
                    cue_ts = t0_lsl + onset_sample / sr
                    try:
                        cb(cue_ts)
                    except Exception:  # noqa: BLE001
                        logger.exception("cue callback raised")
                    cue_idx[0] += 1
                else:
                    break

            cursor[0] = end

        def finished():
            # Natural end: cues in the final (truncated) callback were played
            # but not yet fired — fire them now with their DAC-anchored times.
            # Early stop: the remaining cues never reached the DAC, so writing
            # their markers would put events that never happened into the XDF.
            t0_lsl = t0_holder.get("t0_lsl")
            if t0_lsl is not None and not reached_tail[0] and cue_idx[0] < len(cues):
                logger.warning(
                    "playback stopped early; %d cue(s) never played and were not marked",
                    len(cues) - cue_idx[0],
                )
                cue_idx[0] = len(cues)
            if t0_lsl is not None:
                while cue_idx[0] < len(cues):
                    onset_sample, cb = cues[cue_idx[0]]
                    cue_ts = t0_lsl + onset_sample / sr
                    try:
                        cb(cue_ts)
                    except Exception:  # noqa: BLE001
                        logger.exception("late cue callback raised")
                    cue_idx[0] += 1
            playback.done.set()
            if on_finished is not None:
                try:
                    on_finished()
                except Exception:  # noqa: BLE001
                    logger.exception("on_finished raised")

        stream = sd.OutputStream(
            samplerate=sr,
            channels=1,
            dtype="float32",
            blocksize=self.blocksize,
            device=self.device,
            callback=callback,
            finished_callback=finished,
        )
        playback._stream = stream

        with self._lock:
            if self._active is not None and not self._active.done.is_set():
                self._active.stop()
            self._active = playback

        stream.start()
        return playback

    # ----- convenience: single WAV ---------------------------------------

    def play_wav(
        self,
        path: str | Path,
        *,
        on_onset: Callable[[float], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> AudioPlayback:
        audio = load_wav_mono(path, self.sample_rate)
        return self.play_buffer(
            audio, on_first_callback=on_onset, on_finished=on_finished
        )

    # ----- paced breathing ------------------------------------------------

    def schedule_paced_breathing(
        self,
        *,
        schedule: PacedSchedule,
        inhale_wav: str | Path,
        exhale_wav: str | Path,
        on_inhale: Callable[[int, float], None] | None = None,
        on_exhale: Callable[[int, float], None] | None = None,
        on_block_start: Callable[[float], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> AudioPlayback:
        inhale_audio = load_wav_mono(inhale_wav, self.sample_rate)
        exhale_audio = load_wav_mono(exhale_wav, self.sample_rate)
        buf = build_paced_buffer(
            schedule,
            inhale_audio=inhale_audio,
            exhale_audio=exhale_audio,
            sample_rate=self.sample_rate,
        )
        cue_onsets: list[tuple[int, Callable[[float], None]]] = []
        for cue in schedule.cues:
            cycle = cue.cycle
            if cue.kind == "inhale" and on_inhale is not None:
                cue_onsets.append((cue.onset_sample, lambda ts, c=cycle: on_inhale(c, ts)))
            elif cue.kind == "exhale" and on_exhale is not None:
                cue_onsets.append((cue.onset_sample, lambda ts, c=cycle: on_exhale(c, ts)))
        return self.play_buffer(
            buf,
            cue_onsets=cue_onsets,
            on_first_callback=on_block_start,
            on_finished=on_finished,
        )

    # ----- artifact block -------------------------------------------------

    def schedule_artifact_block(
        self,
        *,
        schedule: ArtifactSchedule,
        on_cue_start: Callable[[str, float], None] | None = None,
        on_cue_end: Callable[[str, float], None] | None = None,
        on_block_start: Callable[[float], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> AudioPlayback:
        # Audio cache so each WAV is loaded once.
        cache: dict[str, np.ndarray] = {}

        def loader(label: str) -> np.ndarray:
            if label not in cache:
                wav = next(c.wav_path for c in schedule.cues if c.label == label)
                cache[label] = load_wav_mono(wav, self.sample_rate)
            return cache[label]

        buf = build_artifact_buffer(
            schedule, sample_rate=self.sample_rate, audio_for_cue=loader
        )

        cue_onsets: list[tuple[int, Callable[[float], None]]] = []
        for cue in schedule.cues:
            label = cue.label
            if on_cue_start is not None:
                cue_onsets.append(
                    (cue.onset_sample, lambda ts, lbl=label: on_cue_start(lbl, ts))
                )
            if not cue.is_state and on_cue_end is not None:
                cue_onsets.append(
                    (cue.end_sample, lambda ts, lbl=label: on_cue_end(lbl, ts))
                )

        return self.play_buffer(
            buf,
            cue_onsets=cue_onsets,
            on_first_callback=on_block_start,
            on_finished=on_finished,
        )

    # ----- volume-check loop ---------------------------------------------

    def play_volume_check(
        self,
        *,
        frequency_hz: float,
        duration_s: float,
        amplitude: float = 0.3,
        ramp_s: float = 0.05,
    ) -> AudioPlayback:
        """Plays the volume-check tone exactly once. Stages call repeatedly
        for the looping behavior (so they can stop on user confirm)."""
        tone = synth_volume_check_tone(
            frequency_hz=frequency_hz,
            duration_s=duration_s,
            sample_rate=self.sample_rate,
            amplitude=amplitude,
            ramp_s=ramp_s,
        )
        return self.play_buffer(tone)

    # ----- shutdown -------------------------------------------------------

    def stop(self) -> None:
        with self._lock:
            if self._active is not None:
                self._active.stop()
                self._active = None
