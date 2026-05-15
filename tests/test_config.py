"""Tests for muse_proxy.config — YAML loader + pydantic invariants."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from muse_proxy.config import (
    DEFAULT_CONFIG_PATH,
    REPO_ROOT,
    Config,
    canon_device,
    load_config,
    next_participant_id,
)

# ---------------------------------------------------------------------------
# load_config — default YAML
# ---------------------------------------------------------------------------


def test_default_config_loads() -> None:
    cfg = load_config()
    assert isinstance(cfg, Config)


def test_default_config_top_level_sections_present() -> None:
    cfg = load_config()
    assert cfg.participant.id_prefix == "P"
    assert cfg.lsl.outlet_name == "MuseProxyMarkers"
    assert cfg.audio.sample_rate == 44100
    assert len(cfg.blocks) == 4
    assert len(cfg.artifact_block.cues) >= 1
    assert cfg.data_dir == "Data"


def test_default_config_blocks_ordered_1_to_4() -> None:
    cfg = load_config()
    assert [b.id for b in cfg.blocks] == [1, 2, 3, 4]
    assert cfg.blocks[0].label == "baseline"
    assert cfg.blocks[1].label == "paced_0p1Hz"
    assert cfg.blocks[2].label == "paced_0p2Hz"
    assert cfg.blocks[3].label == "artifact"


def test_default_config_paced_blocks_consistent() -> None:
    cfg = load_config()
    b2 = cfg.block_by_id(2)
    assert b2.paced is True
    assert b2.rate_hz == pytest.approx(0.1)
    assert b2.inhale_duration_s == pytest.approx(5.0)
    assert b2.exhale_duration_s == pytest.approx(5.0)
    b3 = cfg.block_by_id(3)
    assert b3.rate_hz == pytest.approx(0.2)
    assert b3.inhale_duration_s == pytest.approx(2.5)


def test_default_artifact_cues_distinct_labels() -> None:
    cfg = load_config()
    labels = [c.label for c in cfg.artifact_block.cues]
    assert len(labels) == len(set(labels))


def test_default_artifact_cues_chronological() -> None:
    cfg = load_config()
    onsets = [c.start_s for c in cfg.artifact_block.cues]
    assert onsets == sorted(onsets)


def test_default_config_path_lives_inside_repo() -> None:
    assert DEFAULT_CONFIG_PATH.is_relative_to(REPO_ROOT)


# ---------------------------------------------------------------------------
# load_config — failure modes
# ---------------------------------------------------------------------------


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_load_config_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    yaml_text = (DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
                 + "\nunknown_key: 42\n")
    p = tmp_path / "bad.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_load_config_rejects_missing_required_field(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("participant:\n  id_prefix: P\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_block_paced_with_bad_cycle_rejected(tmp_path: Path) -> None:
    # inhale + exhale != 1/rate_hz
    base = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    bad = base.replace("inhale_duration_s: 5.0", "inhale_duration_s: 6.0", 1)
    p = tmp_path / "bad.yaml"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_artifact_cue_label_with_reserved_char_rejected(tmp_path: Path) -> None:
    base = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    bad = base.replace('label: "eyes_open"', 'label: "eyes:open"', 1)
    p = tmp_path / "bad.yaml"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_artifact_cues_out_of_order_rejected(tmp_path: Path) -> None:
    base = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    # Push the first cue to start_s: 999
    bad = base.replace("start_s: 0.0", "start_s: 999.0", 1)
    p = tmp_path / "bad.yaml"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_volume_check_amplitude_must_be_le_1(tmp_path: Path) -> None:
    base = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    bad = base.replace("amplitude: 0.3", "amplitude: 1.5", 1)
    p = tmp_path / "bad.yaml"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_must_have_exactly_4_blocks(tmp_path: Path) -> None:
    base = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    # Lop off the last block (4 artifact). Crude — relies on YAML layout.
    idx = base.find("  - id: 4")
    bad = base[:idx]
    p = tmp_path / "bad.yaml"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


# ---------------------------------------------------------------------------
# streams_for_device + canon_device
# ---------------------------------------------------------------------------


def test_streams_for_muse2_includes_always_required() -> None:
    cfg = load_config()
    streams = cfg.streams_for_device("muse2")
    names = [s.name for s in streams]
    assert "AIM" in names
    assert "Muse" in names
    assert "MuseProxyMarkers" in names


def test_streams_for_athena_includes_athena_substring() -> None:
    cfg = load_config()
    names = [s.name for s in cfg.streams_for_device("athena")]
    assert "Athena" in names
    assert "AIM" in names


def test_streams_for_device_unknown_raises() -> None:
    cfg = load_config()
    with pytest.raises((KeyError, ValueError)):
        cfg.streams_for_device("magnetoencephalograph")


@pytest.mark.parametrize(
    "raw,canon",
    [
        ("muse2", "muse2"),
        ("Muse 2", "muse2"),
        ("muse_2", "muse2"),
        ("athena", "athena"),
        ("Muse Athena", "athena"),
        ("muse_athena", "athena"),
        ("MUSE 2", "muse2"),
    ],
)
def test_canon_device(raw: str, canon: str) -> None:
    assert canon_device(raw) == canon


def test_canon_device_unknown_raises() -> None:
    with pytest.raises(KeyError):
        canon_device("Emotiv Insight")


# ---------------------------------------------------------------------------
# next_participant_id
# ---------------------------------------------------------------------------


def test_next_participant_id_empty_root(tmp_path: Path) -> None:
    assert next_participant_id(tmp_path) == "P001"


def test_next_participant_id_nonexistent_root(tmp_path: Path) -> None:
    assert next_participant_id(tmp_path / "does-not-exist") == "P001"


def test_next_participant_id_finds_max(tmp_path: Path) -> None:
    (tmp_path / "P001").mkdir()
    (tmp_path / "P002").mkdir()
    (tmp_path / "P017").mkdir()
    (tmp_path / "Q003").mkdir()  # different prefix - ignored
    (tmp_path / "P003_old").mkdir()  # not a perfect match - ignored
    (tmp_path / "notes.txt").write_text("ignore me")
    assert next_participant_id(tmp_path) == "P018"


def test_next_participant_id_custom_prefix(tmp_path: Path) -> None:
    (tmp_path / "S005").mkdir()
    assert next_participant_id(tmp_path, prefix="S") == "S006"


def test_next_participant_id_custom_digits(tmp_path: Path) -> None:
    (tmp_path / "P0001").mkdir()
    (tmp_path / "P0042").mkdir()
    assert next_participant_id(tmp_path, digits=4) == "P0043"


# ---------------------------------------------------------------------------
# Sanity — config_snapshot is YAML-serialisable
# ---------------------------------------------------------------------------


def test_config_round_trips_through_model_dump() -> None:
    cfg = load_config()
    dumped = cfg.model_dump()
    assert isinstance(dumped, dict)
    # Round-trips: dumped data should re-validate.
    Config.model_validate(dumped)


def test_minimal_inline_config_validates() -> None:
    yaml_text = textwrap.dedent(
        """\
        participant:
          id_prefix: P
          id_digits: 3
        lsl:
          outlet_name: M
        pre_launch_streams:
          always_required:
            - {name: AIM, description: x}
          muse_2:
            - {name: Muse, description: x}
          muse_athena:
            - {name: Athena, description: x}
        pre_launch_manual_confirmations:
          pass_a: [a]
          pass_b: [b]
        audio:
          sample_rate: 44100
          volume_check:
            frequency_hz: 1000
            duration_s: 3.0
            amplitude: 0.3
            ramp_s: 0.05
        settle:
          initial_pass_a_default_min: 5
          initial_pass_b_default_min: 2
          inter_pass_break_default_min: 5
        blocks:
          - {id: 1, label: baseline, duration_s: 300, paced: false, instruction: "x"}
          - {id: 2, label: paced_0p1Hz, duration_s: 300, paced: true, rate_hz: 0.1, inhale_duration_s: 5.0, exhale_duration_s: 5.0, instruction: "x"}
          - {id: 3, label: paced_0p2Hz, duration_s: 300, paced: true, rate_hz: 0.2, inhale_duration_s: 2.5, exhale_duration_s: 2.5, instruction: "x"}
          - {id: 4, label: artifact, duration_s: 90, paced: false, instruction: "x"}
        artifact_block:
          cues:
            - {start_s: 0.0, label: eyes_open, spoken: "Open", window_s: 3.0, state: true}
        ui: {}
        data_dir: Data
        """
    )
    import yaml as _yaml
    cfg = Config.model_validate(_yaml.safe_load(yaml_text))
    assert cfg.lsl.outlet_name == "M"
