"""Tests for artifact lifecycle configuration."""

from __future__ import annotations

from pathlib import Path

from hermes_cli.config_defaults import DEFAULT_CONFIG
from tools.artifact_lifecycle import ArtifactManager


def test_default_config_defines_artifact_lifecycle_policy() -> None:
    """Expose explicit defaults for managed artifact retention."""
    policy = DEFAULT_CONFIG["artifact_lifecycle"]

    assert policy == {
        "enabled": True,
        "orphan_grace_hours": 24,
        "spillover_retention_hours": 24,
        "scratch_retention_hours": 0,
        "max_total_size_mb": 2048,
    }


def test_manager_from_config_uses_terminal_temp_dir(tmp_path: Path) -> None:
    """Use an existing configured terminal root for owned operations."""
    configured_root = tmp_path / "configured-temp"
    configured_root.mkdir()
    config = {
        "terminal": {"temp_dir": str(configured_root)},
        "artifact_lifecycle": DEFAULT_CONFIG["artifact_lifecycle"],
    }

    manager = ArtifactManager.from_config(config, session_id="session-1")

    assert manager.root == configured_root
    assert manager.policy == config["artifact_lifecycle"]


def test_manager_from_config_falls_back_to_hermes_root_for_missing_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Ignore a missing terminal override rather than creating outside it."""
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config = {
        "terminal": {"temp_dir": str(tmp_path / "missing")},
        "artifact_lifecycle": DEFAULT_CONFIG["artifact_lifecycle"],
    }

    manager = ArtifactManager.from_config(config, session_id="session-1")

    assert manager.root == hermes_home / "cache" / "terminal"


def test_manager_from_config_copies_policy_without_sharing_mutable_config(
    tmp_path: Path,
) -> None:
    """Copy policy values so later config mutation cannot alter an operation."""
    policy = dict(DEFAULT_CONFIG["artifact_lifecycle"])
    config = {"terminal": {}, "artifact_lifecycle": policy}

    manager = ArtifactManager.from_config(config, session_id="session-1")
    policy["max_total_size_mb"] = 1

    assert manager.policy["max_total_size_mb"] == 2048
