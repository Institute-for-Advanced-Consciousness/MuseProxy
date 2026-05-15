"""Render every audio asset MuseProxy needs.

Idempotent. Called by INSTALL.bat after ``uv sync`` and reusable as
``uv run python scripts/generate_audio.py [--force]``.

Steps:
  1. Read ``config/session_defaults.yaml`` for the cue list, TTS voice,
     audio sample rate, and silence-trim threshold.
  2. For each cue (close_eyes, open_eyes, take_a_moment, in, out, and
     every ``artifact_block.cues[].label``): render the spoken text to
     MP3 via ``edge-tts``, decode + trim leading/trailing silence + write
     as 1-channel ``.wav`` at the configured sample rate.
  3. Synthesize ``volume_check.wav`` directly from numpy — a sinusoid
     with cosine ramps. No TTS for this one.
  4. Print a manifest and warn if any breathing cue exceeds 400 ms.

Skipped files: anything that already exists with a non-zero size,
unless ``--force`` is passed. Requires internet for edge-tts; the
runtime is fully offline.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

# Add the parent of this file's parent to sys.path so we can import muse_proxy
# without requiring an editable install.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from muse_proxy.audio import synth_volume_check_tone  # noqa: E402
from muse_proxy.config import REPO_ROOT, load_config  # noqa: E402

ASSETS_DIR = REPO_ROOT / "assets" / "sounds"

# Spoken cue labels common to breathing blocks + bookends.
COMMON_CUES: dict[str, str] = {
    "in": "In",
    "out": "Out",
    "close_eyes": "Close your eyes",
    "open_eyes": "All done. Open your eyes.",
    "take_a_moment": "Take a moment.",
}

# Cues that MUST fit within the fast paced-breathing phase (2.5 s) with
# headroom. We warn at 400 ms.
BREATHING_CUE_LABELS = ("in", "out")


# ---------------------------------------------------------------------------
# TTS rendering
# ---------------------------------------------------------------------------


def render_tts_to_wav(
    *,
    text: str,
    voice: str,
    out_path: Path,
    sample_rate: int,
    silence_trim_threshold: float = 0.01,
) -> float:
    """Render ``text`` via edge-tts and save as mono WAV at ``sample_rate``.

    Returns the duration of the saved WAV in seconds.
    """
    import edge_tts

    # 1) Render to MP3 bytes.
    async def _render() -> bytes:
        comm = edge_tts.Communicate(text, voice)
        buf = io.BytesIO()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    mp3_bytes = asyncio.run(_render())
    if not mp3_bytes:
        raise RuntimeError(f"edge-tts returned 0 bytes for {text!r}")

    # 2) Decode MP3 -> mono float32 at sample_rate. soundfile can read
    #    MP3 in modern installs but we go through a temp file for safety.
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(mp3_bytes)
        mp3_path = Path(f.name)
    try:
        data, sr = sf.read(str(mp3_path), dtype="float32", always_2d=False)
    finally:
        try:
            mp3_path.unlink()
        except OSError:
            pass

    if data.ndim == 2:
        data = data.mean(axis=1).astype(np.float32)

    if sr != sample_rate:
        # Linear resample. (Good enough — the cue audio is speech, not music.)
        data = _resample_linear(data, src_sr=sr, dst_sr=sample_rate)

    # 3) Trim silence at both ends.
    data = _trim_silence(data, threshold=silence_trim_threshold)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), data, sample_rate, subtype="PCM_16")
    return len(data) / sample_rate


def _resample_linear(data: np.ndarray, *, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return data
    n_dst = int(round(len(data) * dst_sr / src_sr))
    t_src = np.linspace(0.0, len(data) / src_sr, num=len(data), endpoint=False)
    t_dst = np.linspace(0.0, len(data) / src_sr, num=n_dst, endpoint=False)
    return np.interp(t_dst, t_src, data).astype(np.float32)


def _trim_silence(data: np.ndarray, threshold: float) -> np.ndarray:
    above = np.abs(data) > threshold
    if not above.any():
        return data
    first = int(np.argmax(above))
    last = len(data) - int(np.argmax(above[::-1]))
    return data[first:last]


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def expected_wavs(cfg) -> dict[str, str]:
    """Map of basename → spoken text. Used for both rendering and audit."""
    mapping: dict[str, str] = {}
    for label, spoken in COMMON_CUES.items():
        mapping[f"{label}.wav"] = spoken
    for cue in cfg.artifact_block.cues:
        mapping[f"artifact_{cue.label}.wav"] = cue.spoken
    return mapping


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="generate_audio")
    parser.add_argument(
        "--force", action="store_true", help="Regenerate even if WAV exists."
    )
    parser.add_argument(
        "--config", default=None, help="Optional alternate session_defaults.yaml."
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    sr = cfg.audio.sample_rate
    voice = cfg.audio.tts_voice
    trim = cfg.audio.silence_trim_threshold

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    manifest: list[tuple[str, float | None, str]] = []

    # --- 1) Volume check tone (no TTS) ---------------------------------
    vc_path = ASSETS_DIR / "volume_check.wav"
    if args.force or not _exists_nonempty(vc_path):
        vc = cfg.audio.volume_check
        tone = synth_volume_check_tone(
            frequency_hz=vc.frequency_hz,
            duration_s=vc.duration_s,
            sample_rate=sr,
            amplitude=vc.amplitude,
            ramp_s=vc.ramp_s,
        )
        sf.write(str(vc_path), tone, sr, subtype="PCM_16")
        manifest.append(("volume_check.wav", vc.duration_s, "1 kHz reference tone"))
    else:
        info = sf.info(str(vc_path))
        manifest.append(("volume_check.wav", info.duration, "(skipped — already present)"))

    # --- 2) TTS cues ---------------------------------------------------
    wavs = expected_wavs(cfg)
    for basename, spoken in wavs.items():
        out_path = ASSETS_DIR / basename
        if (not args.force) and _exists_nonempty(out_path):
            try:
                dur = sf.info(str(out_path)).duration
            except Exception:  # noqa: BLE001
                dur = None
            manifest.append((basename, dur, "(skipped — already present)"))
            continue
        try:
            dur = render_tts_to_wav(
                text=spoken,
                voice=voice,
                out_path=out_path,
                sample_rate=sr,
                silence_trim_threshold=trim,
            )
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {basename:<40} {e}", file=sys.stderr)
            return 1
        manifest.append((basename, dur, spoken))

    # --- 3) Print manifest --------------------------------------------
    print()
    print(f"{'file':<40}{'duration':>12}  spoken")
    print(f"{'-' * 40}{'-' * 12}  {'-' * 40}")
    warnings: list[str] = []
    for basename, dur, spoken in manifest:
        dur_s = f"{dur:.3f}s" if dur is not None else "?"
        print(f"{basename:<40}{dur_s:>12}  {spoken}")
        # Warn if a breathing cue is over 400 ms.
        label_root = basename.removesuffix(".wav")
        if label_root in BREATHING_CUE_LABELS and dur is not None and dur > 0.4:
            warnings.append(
                f"WARN  {basename} is {dur:.3f}s — must be <= 0.400s to fit "
                f"in 2.5s paced phase"
            )
    for w in warnings:
        print(w, file=sys.stderr)

    print()
    print(f"Wrote {len(manifest)} WAV(s) to {ASSETS_DIR}")
    return 0 if not warnings else 0  # warnings don't fail INSTALL


def _exists_nonempty(p: Path) -> bool:
    try:
        return p.exists() and p.stat().st_size > 0
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
