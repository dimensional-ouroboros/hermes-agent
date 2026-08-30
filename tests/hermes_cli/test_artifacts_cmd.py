"""Tests for the ``hermes artifacts`` CLI."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools.artifact_lifecycle import ArtifactManager


@pytest.fixture
def artifact_home(tmp_path: Path, monkeypatch) -> Path:
    """Point Hermes artifact commands at an isolated profile home."""
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _args(action: str, **kwargs) -> Namespace:
    """Build CLI arguments for one artifact action."""
    return Namespace(artifact_action=action, **kwargs)


def _expire(operation) -> None:
    """Make a test operation eligible for immediate reaping."""
    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))
    manifest["expires_at"] = "2020-01-01T00:00:00+00:00"
    operation.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_status_prints_manager_summary_as_json(artifact_home: Path, capsys) -> None:
    """Print metadata-only artifact status as JSON."""
    manager = ArtifactManager(artifact_home / "cache" / "terminal", session_id="session-1")
    manager.create_operation(
        owner="test",
        kind="scratch",
        sensitivity="internal",
        retention="finalize",
    )

    from hermes_cli.artifacts_cmd import cmd_artifacts

    assert cmd_artifacts(_args("status")) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["manifest_count"] == 1
    assert output["by_kind"] == {"scratch": 1}
    assert output["durable"]["sessions"]["auto_prune"] is False
    assert output["durable"]["checkpoints"]["auto_prune"] is True
    assert output["durable"]["backups"]["automatic_cleanup"] is False
    assert output["durable"]["logs"]["automatic_cleanup"] is False
    assert output["external"]["automatic_cleanup"] is False


def test_dry_run_lists_expired_dead_operation_without_deleting(
    artifact_home: Path,
    capsys,
) -> None:
    """Preview an expired operation without changing its payload."""
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    manager = ArtifactManager(
        artifact_home / "cache" / "terminal",
        session_id="session-1",
        now=lambda: now,
    )
    operation = manager.create_operation(
        owner="test",
        kind="process",
        sensitivity="internal",
        retention="ttl",
        retention_seconds=1,
        pid=1234,
        pid_start_time="start-1",
    )
    payload = operation.path("output.log")
    payload.write_text("keep during preview", encoding="utf-8")
    _expire(operation)

    from hermes_cli.artifacts_cmd import cmd_artifacts

    assert cmd_artifacts(_args("dry-run")) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["candidates"] == [operation.operation_id]
    assert output["reaped"] == 0
    assert payload.exists()


def test_reap_deletes_expired_dead_operation(artifact_home: Path, capsys) -> None:
    """Reap an expired operation through the explicit CLI action."""
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    manager = ArtifactManager(
        artifact_home / "cache" / "terminal",
        session_id="session-1",
        now=lambda: now,
    )
    operation = manager.create_operation(
        owner="test",
        kind="process",
        sensitivity="internal",
        retention="ttl",
        retention_seconds=1,
        pid=1234,
        pid_start_time="start-1",
    )
    payload = operation.path("output.log")
    payload.write_text("delete", encoding="utf-8")
    _expire(operation)

    from hermes_cli.artifacts_cmd import cmd_artifacts

    assert cmd_artifacts(_args("reap")) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["reaped"] == 1
    assert not payload.exists()


def test_list_filters_operations_by_owner(artifact_home: Path, capsys) -> None:
    """List only manifests belonging to the requested owner."""
    manager = ArtifactManager(artifact_home / "cache" / "terminal", session_id="session-1")
    owned = manager.create_operation(
        owner="owner-a",
        kind="staging",
        sensitivity="internal",
        retention="manual",
    )
    manager.create_operation(
        owner="owner-b",
        kind="staging",
        sensitivity="internal",
        retention="manual",
    )

    from hermes_cli.artifacts_cmd import cmd_artifacts

    assert cmd_artifacts(_args("list", owner="owner-a")) == 0
    output = json.loads(capsys.readouterr().out)
    assert [item["operation_id"] for item in output] == [owned.operation_id]


def test_parser_registers_all_artifact_actions() -> None:
    """Expose artifact actions through the top-level Hermes parser."""
    from hermes_cli._parser import build_top_level_parser
    from hermes_cli.artifacts_cmd import cmd_artifacts, register_parser

    parser, subparsers, _chat = build_top_level_parser()
    register_parser(subparsers, cmd_artifacts)

    status = parser.parse_args(["artifacts"])
    dry_run = parser.parse_args(["artifacts", "dry-run"])
    reap = parser.parse_args(["artifacts", "reap"])
    listing = parser.parse_args(["artifacts", "list", "--owner", "test"])
    promote = parser.parse_args(
        [
            "artifacts",
            "promote",
            "--operation-id",
            "op-1",
            "--relative-name",
            "report.md",
            "--destination",
            "/tmp/report.md",
        ]
    )

    assert status.artifact_action == "status"
    assert dry_run.artifact_action == "dry-run"
    assert reap.artifact_action == "reap"
    assert listing.owner == "test"
    assert promote.operation_id == "op-1"
    assert promote.relative_name == "report.md"


def test_promote_moves_explicit_operation_file(artifact_home: Path, capsys) -> None:
    """Promote an operation file through the explicit CLI action."""
    manager = ArtifactManager(artifact_home / "cache" / "terminal", session_id="session-1")
    operation = manager.create_operation(
        owner="test",
        kind="staging",
        sensitivity="internal",
        retention="finalize",
    )
    source = operation.path("report.md")
    source.write_text("report", encoding="utf-8")
    destination = artifact_home / "reports" / "report.md"

    from hermes_cli.artifacts_cmd import cmd_artifacts

    assert cmd_artifacts(
        _args(
            "promote",
            operation_id=operation.operation_id,
            relative_name="report.md",
            destination=str(destination),
        )
    ) == 0
    assert json.loads(capsys.readouterr().out)["destination"] == str(destination)
    assert destination.read_text(encoding="utf-8") == "report"
    assert not source.exists()
