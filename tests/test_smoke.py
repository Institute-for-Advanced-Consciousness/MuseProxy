"""Phase 1 smoke tests — every module imports cleanly, theme is sane.

These are the minimum bar that has to stay green every phase.
"""

from __future__ import annotations

import importlib

import pytest

CORE_MODULES = [
    "muse_proxy",
    "muse_proxy.app",
    "muse_proxy.audio",
    "muse_proxy.artifacts",
    "muse_proxy.breathing",
    "muse_proxy.config",
    "muse_proxy.markers",
    "muse_proxy.persistence",
    "muse_proxy.theme",
]

STAGE_MODULES = [
    "muse_proxy.stages",
    "muse_proxy.stages._base",
    "muse_proxy.stages.between_block_settle",
    "muse_proxy.stages.block_artifact",
    "muse_proxy.stages.block_breathing",
    "muse_proxy.stages.block_instructions",
    "muse_proxy.stages.completion",
    "muse_proxy.stages.intake",
    "muse_proxy.stages.pass_switch",
    "muse_proxy.stages.pre_launch",
    "muse_proxy.stages.settle",
    "muse_proxy.stages.volume_check",
]


@pytest.mark.parametrize("module_name", CORE_MODULES + STAGE_MODULES)
def test_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)


def test_version_exposed() -> None:
    import muse_proxy

    assert isinstance(muse_proxy.__version__, str)
    assert muse_proxy.__version__


def test_theme_constants() -> None:
    from muse_proxy import theme

    assert theme.PURPLE.lower() == "#5d5179"
    assert theme.WHITE.lower() == "#ffffff"
    assert theme.BLACK.lower() == "#000000"
    # Style names should be distinct.
    style_names = {
        theme.STYLE_PRIMARY_BUTTON,
        theme.STYLE_DISABLED_BUTTON,
        theme.STYLE_ABORT_BUTTON,
        theme.STYLE_SECONDARY_BUTTON,
    }
    assert len(style_names) == 4


def test_default_config_path_points_into_config_dir() -> None:
    from muse_proxy.config import DEFAULT_CONFIG_PATH

    assert DEFAULT_CONFIG_PATH.name == "session_defaults.yaml"
    assert DEFAULT_CONFIG_PATH.parent.name == "config"
    assert DEFAULT_CONFIG_PATH.exists(), (
        f"expected default config at {DEFAULT_CONFIG_PATH}"
    )


def test_stage_names_unique_and_match_class_names() -> None:
    """Every StageFrame subclass must declare a unique non-default NAME."""
    from muse_proxy.stages._base import StageFrame
    from muse_proxy.stages.between_block_settle import BetweenBlockSettleFrame
    from muse_proxy.stages.block_artifact import BlockArtifactFrame
    from muse_proxy.stages.block_breathing import BlockBreathingFrame
    from muse_proxy.stages.block_instructions import BlockInstructionsFrame
    from muse_proxy.stages.completion import CompletionFrame
    from muse_proxy.stages.intake import IntakeFrame
    from muse_proxy.stages.pass_switch import PassSwitchFrame
    from muse_proxy.stages.pre_launch import PreLaunchFrame
    from muse_proxy.stages.settle import SettleFrame
    from muse_proxy.stages.volume_check import VolumeCheckFrame

    all_frames = [
        IntakeFrame, PreLaunchFrame, VolumeCheckFrame, SettleFrame,
        BlockInstructionsFrame, BlockBreathingFrame, BlockArtifactFrame,
        BetweenBlockSettleFrame, PassSwitchFrame, CompletionFrame,
    ]
    names = [cls.NAME for cls in all_frames]
    assert StageFrame.NAME not in names, "a frame forgot to override NAME"
    assert len(set(names)) == len(names), f"duplicate stage NAMEs: {names}"
