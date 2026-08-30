"""Tests for the owned Hermes artifact lifecycle."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.artifact_lifecycle import ArtifactManager, managed_artifact_operation


@pytest.fixture
def manager(tmp_path: Path) -> ArtifactManager:
    """Return an artifact manager rooted in the test directory."""
    return ArtifactManager(tmp_path / "artifacts", session_id="session-1")


def test_create_operation_writes_owned_manifest_with_restrictive_permissions(
    manager: ArtifactManager,
) -> None:
    """Create an operation beneath the managed root and write its manifest."""
    operation = manager.create_operation(
        owner="unit-test",
        kind="scratch",
        sensitivity="sensitive",
        retention="finalize",
    )

    assert operation.root == manager.root / "operations" / "session-1" / operation.operation_id
    assert operation.root.is_dir()
    assert stat.S_IMODE(operation.root.stat().st_mode) == 0o700
    assert operation.manifest_path.is_file()
    assert stat.S_IMODE(operation.manifest_path.stat().st_mode) == 0o600

    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))
    assert manifest["operation_id"] == operation.operation_id
    assert manifest["owner"] == "unit-test"
    assert manifest["kind"] == "scratch"
    assert manifest["sensitivity"] == "sensitive"
    assert manifest["status"] == "active"
    assert manifest["cleanup_policy"] == "finalize"


def test_create_operation_records_process_start_identity(
    manager: ArtifactManager,
    monkeypatch,
) -> None:
    """Record the owner start identity when only a PID is supplied."""
    import tools.artifact_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_read_pid_start_time", lambda pid: "start-1")
    operation = manager.create_operation(
        owner="unit-test",
        kind="process",
        sensitivity="internal",
        retention="ttl",
        retention_seconds=60,
        pid=1234,
    )

    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))

    assert manifest["pid_start_time"] == "start-1"


def test_operation_path_rejects_traversal_and_absolute_paths(
    manager: ArtifactManager,
) -> None:
    """Reject paths that can escape the operation root."""
    operation = manager.create_operation(
        owner="unit-test",
        kind="scratch",
        sensitivity="internal",
        retention="finalize",
    )

    with pytest.raises(ValueError, match="operation root|relative path"):
        operation.path("../outside.txt")
    with pytest.raises(ValueError, match="operation root|relative path"):
        operation.path(str(manager.root.parent / "outside.txt"))


def test_register_existing_rejects_paths_outside_operation(
    manager: ArtifactManager,
    tmp_path: Path,
) -> None:
    """Reject registration of a path outside the operation root."""
    operation = manager.create_operation(
        owner="unit-test",
        kind="staging",
        sensitivity="internal",
        retention="finalize",
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive", encoding="utf-8")

    with pytest.raises(ValueError, match="operation root"):
        operation.register_existing(outside)


def test_finalize_removes_owned_files_and_symlinks_without_touching_targets(
    manager: ArtifactManager,
    tmp_path: Path,
) -> None:
    """Finalize an operation by unlinking owned files and emptying directories."""
    operation = manager.create_operation(
        owner="unit-test",
        kind="scratch",
        sensitivity="sensitive",
        retention="finalize",
    )
    payload = operation.path("nested/payload.txt")
    payload.parent.mkdir()
    payload.write_text("payload", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive", encoding="utf-8")
    link = operation.path("nested/outside-link")
    link.symlink_to(outside)

    result = operation.finalize("success")

    assert result["deleted_files"] == 2
    assert result["failures"] == []
    assert not payload.exists()
    assert not link.exists() and not link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "must survive"
    assert not operation.root.exists()

    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "finalized-success"
    assert manifest["outcome"] == "success"


def test_finalize_is_idempotent_and_keeps_manifest(
    manager: ArtifactManager,
) -> None:
    """Allow repeated finalization without deleting lifecycle metadata."""
    operation = manager.create_operation(
        owner="unit-test",
        kind="scratch",
        sensitivity="internal",
        retention="finalize",
    )
    operation.path("payload.txt").write_text("payload", encoding="utf-8")

    first = operation.finalize("failure")
    second = operation.finalize("failure")

    assert first["deleted_files"] == 1
    assert second["deleted_files"] == 0
    assert operation.manifest_path.is_file()
    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "finalized-failure"


def test_promote_moves_owned_file_and_protects_it_from_cleanup(
    manager: ArtifactManager,
    tmp_path: Path,
) -> None:
    """Promote an output before finalization and preserve the destination."""
    operation = manager.create_operation(
        owner="unit-test",
        kind="staging",
        sensitivity="internal",
        retention="finalize",
    )
    source = operation.path("report.md")
    source.write_text("report", encoding="utf-8")
    destination = tmp_path / "durable" / "report.md"

    promoted = operation.promote(destination)
    operation.finalize("success")

    assert promoted == destination
    assert destination.read_text(encoding="utf-8") == "report"
    assert not source.exists()
    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))
    assert manifest["promoted_paths"] == [str(destination)]


def test_promote_rejects_destination_inside_owned_payload_root(
    manager: ArtifactManager,
) -> None:
    """Reject promotion that would remain exposed to operation cleanup."""
    operation = manager.create_operation(
        owner="unit-test",
        kind="staging",
        sensitivity="internal",
        retention="finalize",
    )
    source = operation.path("report.md")
    source.write_text("report", encoding="utf-8")

    with pytest.raises(ValueError, match="payload root"):
        operation.promote(operation.path("promoted.md"), relative_name="report.md")


def test_finalize_quarantines_replaced_operation_root(tmp_path: Path) -> None:
    """Never follow a replaced operation root into an external directory."""
    manager = ArtifactManager(tmp_path / "artifacts", session_id="session-1")
    operation = manager.create_operation(
        owner="unit-test",
        kind="scratch",
        sensitivity="sensitive",
        retention="finalize",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    operation.root.rmdir()
    operation.root.symlink_to(outside, target_is_directory=True)

    result = operation.finalize("success")

    assert result["failures"]
    assert protected.read_text(encoding="utf-8") == "keep"
    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "quarantined"


def test_load_operation_rejects_manifest_relative_path_traversal(
    manager: ArtifactManager,
    tmp_path: Path,
) -> None:
    """Reject shared-operation metadata that escapes by relative traversal."""
    shared = tmp_path / "shared"
    shared.mkdir()
    operation = manager.create_operation(
        owner="unit-test",
        kind="spillover",
        sensitivity="sensitive",
        retention="ttl",
        retention_seconds=60,
        payload_root=shared,
    )
    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))
    manifest["relative_paths"] = ["../outside.txt"]
    operation.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="relative path"):
        manager.load_operation(operation.operation_id)


def test_promote_does_not_overwrite_existing_destination(
    manager: ArtifactManager,
    tmp_path: Path,
) -> None:
    """Preserve an existing durable file when promotion collides."""
    operation = manager.create_operation(
        owner="unit-test",
        kind="staging",
        sensitivity="internal",
        retention="finalize",
    )
    source = operation.path("report.md")
    source.write_text("new", encoding="utf-8")
    destination = tmp_path / "report.md"
    destination.write_text("old", encoding="utf-8")

    with pytest.raises(FileExistsError):
        operation.promote(destination, relative_name="report.md")

    assert source.read_text(encoding="utf-8") == "new"
    assert destination.read_text(encoding="utf-8") == "old"


def test_finalize_retention_reaps_dead_owner_after_orphan_grace(tmp_path: Path) -> None:
    """Reap a finalize-retention operation orphaned by a crashed owner."""
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    manager = ArtifactManager(tmp_path / "artifacts", now=lambda: now)
    manager.policy["orphan_grace_hours"] = 1
    operation = manager.create_operation(
        owner="unit-test",
        kind="staging",
        sensitivity="sensitive",
        retention="finalize",
        pid=1234,
        pid_start_time="start-1",
    )
    payload = operation.path("payload.txt")
    payload.write_text("delete", encoding="utf-8")

    result = manager.reap_orphans(
        now=now + timedelta(hours=1, seconds=1),
        is_pid_alive=lambda pid, start_time: False,
        dry_run=False,
    )

    assert result["reaped"] == 1
    assert not payload.exists()


def test_disabled_policy_rejects_new_operations(manager: ArtifactManager) -> None:
    """Reject allocation when lifecycle management is explicitly disabled."""
    manager.policy["enabled"] = False

    with pytest.raises(RuntimeError, match="disabled"):
        manager.create_operation(
            owner="unit-test",
            kind="scratch",
            sensitivity="internal",
            retention="finalize",
        )


def test_policy_derives_spillover_ttl_and_enforces_quota(tmp_path: Path) -> None:
    """Apply configured spillover retention and total-size limits."""
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    manager = ArtifactManager(tmp_path / "artifacts", now=lambda: now)
    manager.policy["spillover_retention_hours"] = 2
    manager.policy["max_total_size_mb"] = 1
    operation = manager.create_operation(
        owner="unit-test",
        kind="spillover",
        sensitivity="sensitive",
        retention="ttl",
    )
    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))
    assert manifest["expires_at"] == (now + timedelta(hours=2)).isoformat()
    payload = operation.path("payload.txt")
    payload.write_bytes(b"x" * (1024 * 1024 + 1))

    with pytest.raises(ValueError, match="quota"):
        manager.create_operation(
            owner="second",
            kind="spillover",
            sensitivity="internal",
            retention="manual",
        )


def test_reap_orphans_removes_expired_dead_operations(
    tmp_path: Path,
) -> None:
    """Reap an expired operation only after its owner is known to be dead."""
    root = tmp_path / "artifacts"
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    manager = ArtifactManager(root, session_id="session-1", now=lambda: now)
    operation = manager.create_operation(
        owner="unit-test",
        kind="process",
        sensitivity="internal",
        retention="ttl",
        retention_seconds=60,
        pid=1234,
        pid_start_time="start-1",
    )
    operation.path("output.log").write_text("output", encoding="utf-8")

    result = manager.reap_orphans(
        now=now + timedelta(seconds=61),
        is_pid_alive=lambda pid, start_time: False,
        dry_run=False,
    )

    assert result["reaped"] == 1
    assert result["failures"] == []
    assert not operation.root.exists()
    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "reaped"


def test_reap_orphans_keeps_expired_operation_with_live_owner(
    tmp_path: Path,
) -> None:
    """Keep an expired operation while its recorded owner is still alive."""
    root = tmp_path / "artifacts"
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    manager = ArtifactManager(root, session_id="session-1", now=lambda: now)
    operation = manager.create_operation(
        owner="unit-test",
        kind="process",
        sensitivity="internal",
        retention="ttl",
        retention_seconds=60,
        pid=os.getpid(),
        pid_start_time="start-1",
    )
    payload = operation.path("output.log")
    payload.write_text("output", encoding="utf-8")

    result = manager.reap_orphans(
        now=now + timedelta(seconds=61),
        is_pid_alive=lambda pid, start_time: True,
        dry_run=False,
    )

    assert result["reaped"] == 0
    assert result["candidates"] == [operation.operation_id]
    assert payload.exists()


def test_shared_payload_root_reaps_only_registered_file(tmp_path: Path) -> None:
    """Reap one registered file without sweeping a shared payload namespace."""
    root = tmp_path / "artifacts"
    shared = tmp_path / "spillover"
    shared.mkdir()
    other = shared / "other.txt"
    other.write_text("keep", encoding="utf-8")
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    manager = ArtifactManager(root, session_id="session-1", now=lambda: now)
    operation = manager.create_operation(
        owner="tool-result-storage",
        kind="spillover",
        sensitivity="sensitive",
        retention="ttl",
        retention_seconds=60,
        pid=1234,
        pid_start_time="start-1",
        payload_root=shared,
    )
    owned = operation.path("owned.txt")
    owned.write_text("delete", encoding="utf-8")
    operation.register_existing(owned)

    result = manager.reap_orphans(
        now=now + timedelta(seconds=61),
        is_pid_alive=lambda pid, start_time: False,
        dry_run=False,
    )

    assert result["reaped"] == 1
    assert not owned.exists()
    assert other.exists()
    assert shared.exists()
    assert not any((root / "operations").iterdir())


def test_managed_artifact_operation_finalizes_on_context_exit(tmp_path: Path) -> None:
    """Finalize a short-lived operation when its context exits normally."""
    manager = ArtifactManager(tmp_path / "artifacts", session_id="session-1")

    with managed_artifact_operation(
        manager=manager,
        owner="test-context",
        kind="staging",
        sensitivity="internal",
        retention="finalize",
    ) as operation:
        payload = operation.path("payload.txt")
        payload.write_text("payload", encoding="utf-8")

    assert not payload.exists()
    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "finalized-success"


def test_managed_artifact_operation_retains_manual_payload_on_context_exit(
    tmp_path: Path,
) -> None:
    """Keep a manually retained payload after a context exits normally."""
    manager = ArtifactManager(tmp_path / "artifacts", session_id="session-1")

    with managed_artifact_operation(
        manager=manager,
        owner="test-context",
        kind="staging",
        sensitivity="internal",
        retention="manual",
    ) as operation:
        payload = operation.path("payload.txt")
        payload.write_text("retain", encoding="utf-8")

    assert payload.read_text(encoding="utf-8") == "retain"
    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "retained"


def test_status_counts_shared_payload_files_once(tmp_path: Path) -> None:
    """Count each shared payload once rather than once per manifest."""
    root = tmp_path / "artifacts"
    shared = tmp_path / "spillover"
    shared.mkdir()
    manager = ArtifactManager(root, session_id="session-1")
    for name, content in (("one.txt", "one"), ("two.txt", "two")):
        operation = manager.create_operation(
            owner="tool-result-storage",
            kind="spillover",
            sensitivity="sensitive",
            retention="manual",
            payload_root=shared,
        )
        payload = operation.path(name)
        payload.write_text(content, encoding="utf-8")
        operation.register_existing(payload)

    status = manager.status()

    assert status["payload_bytes"] == 6


def test_register_existing_preserves_internal_symlink_target(tmp_path: Path) -> None:
    """Remove a registered symlink without removing its internal target."""
    root = tmp_path / "artifacts"
    shared = tmp_path / "spillover"
    shared.mkdir()
    target = shared / "target.txt"
    target.write_text("keep", encoding="utf-8")
    link = shared / "link.txt"
    link.symlink_to(target)
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    manager = ArtifactManager(root, session_id="session-1", now=lambda: now)
    operation = manager.create_operation(
        owner="tool-result-storage",
        kind="spillover",
        sensitivity="sensitive",
        retention="ttl",
        retention_seconds=60,
        pid=1234,
        payload_root=shared,
    )
    operation.register_existing(link)

    result = manager.reap_orphans(
        now=now + timedelta(seconds=61),
        is_pid_alive=lambda pid, start_time: False,
        dry_run=False,
    )

    assert result["reaped"] == 1
    assert not link.exists() and not link.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"


def test_pid_liveness_rejects_reused_process_identity(monkeypatch) -> None:
    """Treat a live PID with a different start identity as dead."""
    import tools.artifact_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(lifecycle, "_read_pid_start_time", lambda pid: "new-start")

    assert lifecycle._pid_is_alive(1234, "old-start") is False
    assert lifecycle._pid_is_alive(1234, "new-start") is True


def test_pid_liveness_treats_permission_denial_as_live(monkeypatch) -> None:
    """Protect an artifact when process liveness cannot be inspected."""
    import tools.artifact_lifecycle as lifecycle

    def deny(pid):
        raise PermissionError

    monkeypatch.setattr(lifecycle.psutil, "pid_exists", deny)

    assert lifecycle._pid_is_alive(1234, None) is True


def test_pid_liveness_treats_unreadable_start_identity_as_live(monkeypatch) -> None:
    """Retain an artifact when PID identity cannot be verified."""
    import tools.artifact_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(lifecycle, "_read_pid_start_time", lambda pid: None)

    assert lifecycle._pid_is_alive(1234, "unknown-start") is True


def test_load_operation_rejects_manifest_payload_root_escape(
    manager: ArtifactManager,
    tmp_path: Path,
) -> None:
    """Reject a manifest that redirects shared cleanup to another root."""
    operation = manager.create_operation(
        owner="unit-test",
        kind="spillover",
        sensitivity="sensitive",
        retention="ttl",
        retention_seconds=60,
        payload_root=manager.root,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))
    manifest["payload_root"] = str(outside)
    operation.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="payload root"):
        manager.load_operation(operation.operation_id)


def test_status_reports_owner_bytes_largest_oldest_and_cleanup_failures(
    manager: ArtifactManager,
) -> None:
    """Report lifecycle ownership and payload health without reading contents."""
    operation = manager.create_operation(
        owner="unit-test",
        kind="scratch",
        sensitivity="internal",
        retention="finalize",
    )
    operation.path("payload.txt").write_text("payload", encoding="utf-8")

    status = manager.status()

    assert status["by_owner"]["unit-test"]["bytes"] == len("payload")
    assert status["by_kind_bytes"]["scratch"] == len("payload")
    assert status["oldest_artifact"]["operation_id"] == operation.operation_id
    assert status["largest_artifact"]["operation_id"] == operation.operation_id
    assert status["cleanup_failures"] == []
    assert status["quarantined"] == []


def test_status_reports_expired_dead_owner_candidate(tmp_path: Path) -> None:
    """Expose an expired dead-owner operation without deleting it."""
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    manager = ArtifactManager(tmp_path / "artifacts", now=lambda: now)
    operation = manager.create_operation(
        owner="unit-test",
        kind="process",
        sensitivity="internal",
        retention="ttl",
        retention_seconds=60,
        pid=1234,
        pid_start_time="start-1",
    )

    manifest = json.loads(operation.manifest_path.read_text(encoding="utf-8"))
    manifest["expires_at"] = "2020-01-01T00:00:00+00:00"
    operation.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    status = manager.status()

    assert status["expired_orphan_candidates"] == [operation.operation_id]
    assert operation.manifest_path.is_file()
