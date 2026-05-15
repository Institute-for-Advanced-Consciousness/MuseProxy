"""YAML loader + pydantic models for ``config/session_defaults.yaml``.

The full schema is defined here; pydantic enforces required fields and
type invariants at load time. Operators may edit between sessions, never
mid-session.

``REPO_ROOT`` is the directory containing ``pyproject.toml``;
``DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "session_defaults.yaml"``.

Public surface:
  load_config(path: str | Path | None = None) -> Config
  Config (pydantic root model)
  next_participant_id(data_root, cfg) -> str
  REPO_ROOT, DEFAULT_CONFIG_PATH
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH: Path = REPO_ROOT / "config" / "session_defaults.yaml"


# ---------------------------------------------------------------------------
# pydantic models — one per top-level YAML section
# ---------------------------------------------------------------------------


class _StrictModel(BaseModel):
    """Forbid unknown keys so YAML typos fail loudly."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ParticipantConfig(_StrictModel):
    auto_suggest_next_id: bool = True
    id_prefix: str = "P"
    id_digits: int = Field(default=3, ge=1, le=10)


class LSLConfig(_StrictModel):
    outlet_name: str
    outlet_type: str = "Markers"
    source_id_template: str = "MuseProxy"
    channel_count: int = Field(default=1, ge=1)
    nominal_srate: float = 0.0
    channel_format: str = "string"


class StreamRef(_StrictModel):
    name: str
    description: str


class PreLaunchStreamsConfig(_StrictModel):
    always_required: list[StreamRef]
    muse_2: list[StreamRef]
    muse_athena: list[StreamRef]


class PreLaunchManualConfirmations(_StrictModel):
    pass_a: list[str]
    pass_b: list[str]


class VolumeCheckConfig(_StrictModel):
    frequency_hz: float = Field(ge=20.0, le=20000.0)
    duration_s: float = Field(gt=0.0)
    amplitude: float = Field(ge=0.0, le=1.0)
    ramp_s: float = Field(ge=0.0)


class AudioConfig(_StrictModel):
    device_name_substring: str | None = None
    sample_rate: int = Field(default=44100, gt=0)
    channels: int = Field(default=1, ge=1, le=2)
    tts_voice: str = "en-US-AriaNeural"
    silence_trim_threshold: float = Field(default=0.01, ge=0.0, lt=1.0)
    volume_check: VolumeCheckConfig


class SettleConfig(_StrictModel):
    initial_pass_a_default_min: float = Field(ge=0.0)
    initial_pass_b_default_min: float = Field(ge=0.0)
    inter_pass_break_default_min: float = Field(ge=0.0)
    between_block_self_paced: bool = True


class BlockConfig(_StrictModel):
    id: int = Field(ge=1)
    label: str
    duration_s: float = Field(gt=0.0)
    paced: bool
    instruction: str
    rate_hz: float | None = None
    inhale_duration_s: float | None = None
    exhale_duration_s: float | None = None

    @model_validator(mode="after")
    def _check_paced_fields(self) -> BlockConfig:
        if self.paced:
            if self.rate_hz is None or self.rate_hz <= 0:
                raise ValueError(f"block {self.id} is paced but rate_hz is invalid")
            if self.inhale_duration_s is None or self.inhale_duration_s <= 0:
                raise ValueError(f"block {self.id} is paced but inhale_duration_s is invalid")
            if self.exhale_duration_s is None or self.exhale_duration_s <= 0:
                raise ValueError(f"block {self.id} is paced but exhale_duration_s is invalid")
            cycle = self.inhale_duration_s + self.exhale_duration_s
            expected = 1.0 / self.rate_hz
            if abs(cycle - expected) > 1e-3:
                raise ValueError(
                    f"block {self.id}: inhale+exhale={cycle}s but 1/rate_hz={expected}s"
                )
        return self


class ArtifactCueConfig(_StrictModel):
    start_s: float = Field(ge=0.0)
    label: str
    spoken: str
    window_s: float = Field(gt=0.0)
    state: bool = False

    @model_validator(mode="after")
    def _check_label(self) -> ArtifactCueConfig:
        # Marker grammar reserves ':' and '='.
        if ":" in self.label or "=" in self.label:
            raise ValueError(f"artifact cue label may not contain ':' or '=': {self.label!r}")
        return self


class ArtifactBlockConfig(_StrictModel):
    cues: list[ArtifactCueConfig]

    @model_validator(mode="after")
    def _check_cues(self) -> ArtifactBlockConfig:
        if not self.cues:
            raise ValueError("artifact_block.cues is empty")
        seen: set[str] = set()
        for cue in self.cues:
            if cue.label in seen:
                raise ValueError(f"duplicate artifact cue label: {cue.label}")
            seen.add(cue.label)
        # Onsets must be monotonically non-decreasing.
        for prev, nxt in zip(self.cues[:-1], self.cues[1:], strict=True):
            if nxt.start_s < prev.start_s:
                raise ValueError(
                    f"artifact cues not in chronological order: "
                    f"{prev.label}@{prev.start_s}s, then {nxt.label}@{nxt.start_s}s"
                )
        return self


class UIConfig(_StrictModel):
    color_purple: str = "#5d5179"
    color_white: str = "#ffffff"
    color_black: str = "#000000"
    color_gray_light: str = "#e0e0e0"
    color_gray_mid: str = "#888888"
    font_family: str = "Arial"


class Config(_StrictModel):
    """Top-level config model, matches ``config/session_defaults.yaml``."""

    participant: ParticipantConfig
    lsl: LSLConfig
    pre_launch_streams: PreLaunchStreamsConfig
    pre_launch_manual_confirmations: PreLaunchManualConfirmations
    audio: AudioConfig
    settle: SettleConfig
    blocks: list[BlockConfig]
    artifact_block: ArtifactBlockConfig
    ui: UIConfig
    data_dir: str

    @model_validator(mode="after")
    def _check_blocks(self) -> Config:
        if len(self.blocks) != 4:
            raise ValueError(f"expected exactly 4 blocks, got {len(self.blocks)}")
        ids = [b.id for b in self.blocks]
        if ids != [1, 2, 3, 4]:
            raise ValueError(f"block ids must be 1,2,3,4 in order; got {ids}")
        # Block 4 must be the artifact block (paced=False) but its label is "artifact"
        if self.blocks[-1].label != "artifact":
            raise ValueError(f"block 4 must have label 'artifact', got {self.blocks[-1].label!r}")
        return self

    def streams_for_device(self, device: str) -> list[StreamRef]:
        """Return the expected pre-launch stream list for the given device.

        device is the canonical short name: ``muse2`` or ``athena``.
        """
        d = _canon_device(device)
        if d == "muse2":
            return list(self.pre_launch_streams.always_required) + list(
                self.pre_launch_streams.muse_2
            )
        if d == "athena":
            return list(self.pre_launch_streams.always_required) + list(
                self.pre_launch_streams.muse_athena
            )
        raise ValueError(f"unknown device: {device!r}")

    def block_by_id(self, block_id: int) -> BlockConfig:
        for b in self.blocks:
            if b.id == block_id:
                return b
        raise KeyError(f"no block with id={block_id}")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_config(path: str | Path | None = None) -> Config:
    """Parse + validate the YAML, return a Config instance.

    Raises:
      FileNotFoundError if the YAML file is absent.
      pydantic.ValidationError if any required field is missing or any
      invariant fails. The error message names the bad field.
    """
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)


# ---------------------------------------------------------------------------
# Participant ID auto-suggestion
# ---------------------------------------------------------------------------


def next_participant_id(
    data_root: str | Path, prefix: str = "P", digits: int = 3
) -> str:
    """Scan ``data_root`` for ``{prefix}{NNN}`` subdirs and return the next.

    Returns ``{prefix}{(max+1):0{digits}d}`` if any exist; ``{prefix}001``
    otherwise. Non-matching subdirs are ignored.
    """
    root = Path(data_root)
    if not root.exists():
        return f"{prefix}{'1'.zfill(digits)}"
    pattern = re.compile(rf"^{re.escape(prefix)}(\d{{{digits},}})$")
    max_n = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        m = pattern.match(child.name)
        if m:
            try:
                n = int(m.group(1))
            except ValueError:
                continue
            if n > max_n:
                max_n = n
    return f"{prefix}{str(max_n + 1).zfill(digits)}"


# ---------------------------------------------------------------------------
# Device-name canonicalisation
# ---------------------------------------------------------------------------

_DEVICE_ALIASES = {
    "muse2": "muse2",
    "muse 2": "muse2",
    "muse_2": "muse2",
    "athena": "athena",
    "muse athena": "athena",
    "muse_athena": "athena",
}


def _canon_device(device: str) -> str:
    return _DEVICE_ALIASES[device.strip().lower()]


def canon_device(device: str) -> str:
    """Public canonicaliser. Maps Muse-2 / Muse 2 / muse2 → ``muse2``;
    Athena / Muse Athena → ``athena``. Raises KeyError on unknown."""
    return _canon_device(device)


__all__ = [
    "REPO_ROOT",
    "DEFAULT_CONFIG_PATH",
    "Config",
    "ParticipantConfig",
    "LSLConfig",
    "StreamRef",
    "PreLaunchStreamsConfig",
    "PreLaunchManualConfirmations",
    "VolumeCheckConfig",
    "AudioConfig",
    "SettleConfig",
    "BlockConfig",
    "ArtifactCueConfig",
    "ArtifactBlockConfig",
    "UIConfig",
    "load_config",
    "next_participant_id",
    "canon_device",
]
