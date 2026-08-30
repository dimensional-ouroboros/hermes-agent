"""Manage profile-scoped Hermes temporary artifacts."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import uuid
import copy
import psutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable

from hermes_constants import get_hermes_home


_ARTIFACT_KINDS = frozenset(
    {"scratch", "process", "rpc", "staging", "spillover", "worktree", "recovery"}
)
_SENSITIVITIES = frozenset({"public", "internal", "sensitive", "secret-bearing"})
_RETENTION_POLICIES = frozenset({"finalize", "ttl", "manual"})
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_TERMINAL_OUTCOMES = frozenset({"success", "failure", "cancelled", "timed-out"})
DEFAULT_ARTIFACT_POLICY = {
    "enabled": True,
    "orphan_grace_hours": 24,
    "spillover_retention_hours": 24,
    "scratch_retention_hours": 0,
    "max_total_size_mb": 2048,
}


def _utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    """Serialize a datetime as an ISO-8601 UTC string."""
    return _as_utc(value).isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse an optional ISO-8601 timestamp."""
    if not value:
        return None
    return _as_utc(datetime.fromisoformat(value))


def _validate_component(value: str, label: str) -> str:
    """Validate a path component used in an owned namespace."""
    if not value or not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError(f"{label} must contain only letters, numbers, '.', '_' or '-'")
    return value


def _validate_relative_path(value: str) -> Path:
    """Validate a manifest path without traversal or absolute components."""
    candidate = Path(value)
    if (
        not value
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError("relative path must not be absolute or contain traversal")
    return candidate


def _safe_mode(path: Path, mode: int) -> None:
    """Apply a restrictive mode when the platform supports POSIX modes."""
    try:
        path.chmod(mode)
    except OSError:
        pass


def _directory_identity(path: Path) -> tuple[int, int]:
    """Return the device and inode identity of a directory."""
    metadata = path.stat()
    return metadata.st_dev, metadata.st_ino


def _identity_matches(path: Path, identity: object) -> bool:
    """Return whether a real directory still has its allocated identity."""
    if path.is_symlink() or not path.is_dir() or not isinstance(identity, dict):
        return False
    try:
        expected = (int(identity["device"]), int(identity["inode"]))
        return _directory_identity(path) == expected
    except (KeyError, TypeError, ValueError, OSError):
        return False


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Write JSON atomically with restrictive permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_mode(path.parent, stat.S_IRWXU)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
    )
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _safe_mode(path, stat.S_IRUSR | stat.S_IWUSR)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _is_within(path: Path, root: Path) -> bool:
    """Return whether a path resolves beneath a root without escaping it."""
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def _is_lexically_within(path: Path, root: Path) -> bool:
    """Return whether a directory entry is lexically beneath a root."""
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError:
        return False
    return True


def _read_pid_start_time(pid: int) -> str | None:
    """Read the Linux process-start identity for a PID."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat_line.rsplit(")", 1)[1].split()
        return fields[19]
    except (IndexError, OSError, UnicodeDecodeError):
        return None


def _pid_is_alive(pid: int, start_time: str | None) -> bool:
    """Return whether a process ID and optional start identity are alive."""
    if pid <= 0:
        return False
    try:
        if not psutil.pid_exists(pid):
            return False
    except Exception:
        # Permission denied means the PID may be alive but owned by another
        # user. Reaping in that state is less safe than retaining the artifact.
        return True
    if start_time is None:
        return True
    current_start_time = _read_pid_start_time(pid)
    return current_start_time is None or current_start_time == start_time


def _iter_owned_entries(root: Path) -> Iterable[Path]:
    """Yield files and symlinks beneath an owned root without following links."""
    for directory, directories, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            yield Path(directory) / name
        for name in directories:
            entry = Path(directory) / name
            if entry.is_symlink():
                yield entry


def remove_owned_tree(root: Path | str, *, allowed_root: Path | str | None = None) -> dict:
    """Remove one owned tree by unlinking files and empty directories.

    Args:
        root: Directory or file to remove.
        allowed_root: Optional containment boundary for the root.

    Returns:
        A dictionary containing deletion counts and cleanup failures.
    """
    target = Path(root).expanduser()
    boundary = Path(allowed_root).expanduser() if allowed_root is not None else None
    if boundary is not None and not _is_within(target, boundary):
        return {
            "deleted_files": 0,
            "deleted_directories": 0,
            "failures": [f"{target}: outside allowed root {boundary}"],
        }
    if not target.exists() and not target.is_symlink():
        return {"deleted_files": 0, "deleted_directories": 0, "failures": []}
    deleted_files = 0
    deleted_directories = 0
    failures: list[str] = []
    if target.is_symlink() or target.is_file():
        try:
            target.unlink()
            return {"deleted_files": 1, "deleted_directories": 0, "failures": []}
        except OSError as exc:
            return {"deleted_files": 0, "deleted_directories": 0, "failures": [f"{target}: {exc}"]}
    for entry in _iter_owned_entries(target):
        try:
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
                deleted_files += 1
        except OSError as exc:
            failures.append(f"{entry}: {exc}")
    for directory, directories, _files in os.walk(target, topdown=False, followlinks=False):
        for name in directories:
            entry = Path(directory) / name
            try:
                if entry.is_symlink():
                    entry.unlink()
                    deleted_files += 1
                else:
                    entry.rmdir()
                    deleted_directories += 1
            except OSError as exc:
                failures.append(f"{entry}: {exc}")
    try:
        target.rmdir()
        deleted_directories += 1
    except OSError as exc:
        failures.append(f"{target}: {exc}")
    return {
        "deleted_files": deleted_files,
        "deleted_directories": deleted_directories,
        "failures": failures,
    }


@dataclass
class ArtifactOperation:
    """Represent one manager-owned artifact operation."""

    manager: "ArtifactManager"
    operation_id: str
    root: Path
    control_root: Path
    manifest_path: Path
    metadata: dict

    def path(self, relative_name: str) -> Path:
        """Return an owned path beneath this operation root."""
        candidate = _validate_relative_path(relative_name)
        resolved = (self.root / candidate).resolve(strict=False)
        if not _is_within(resolved, self.root):
            raise ValueError("path must remain beneath the operation root")
        return self.root / candidate

    def register_existing(self, path: Path | str) -> Path:
        """Register an existing path beneath this operation root."""
        candidate = Path(path).expanduser().absolute()
        if not _is_within(candidate, self.root):
            raise ValueError("registered path must remain beneath the operation root")
        if not candidate.exists() and not candidate.is_symlink():
            raise FileNotFoundError(candidate)
        # Keep the lexical path in the manifest. Resolving an internal
        # symlink here would cause cleanup to unlink its target instead of the
        # registered directory entry.
        relative = str(candidate.relative_to(self.root.absolute()))
        _validate_relative_path(relative)
        paths = self.metadata.setdefault("relative_paths", [])
        if relative not in paths:
            paths.append(relative)
            paths.sort()
            self._save()
        return candidate

    def heartbeat(self) -> None:
        """Refresh the operation lease timestamp."""
        self.metadata["last_heartbeat_at"] = _timestamp(self.manager.now())
        self._save()

    def annotate(self, **fields: object) -> None:
        """Add non-sensitive lifecycle metadata to the operation manifest."""
        self.metadata.update(fields)
        self._save()

    def promote(self, destination: Path | str, relative_name: str | None = None) -> Path:
        """Move an owned file to a durable destination."""
        if relative_name is None:
            candidates = [
                entry
                for entry in _iter_owned_entries(self.root)
                if entry.is_file() and not entry.is_symlink()
            ]
            if len(candidates) != 1:
                raise ValueError("relative_name is required unless the operation has one file")
            source = candidates[0]
        else:
            source = self.path(relative_name)
        if not source.is_file() or source.is_symlink():
            raise ValueError("only regular files can be promoted")
        target = Path(destination).expanduser()
        if _is_within(target, self.root):
            raise ValueError("promotion destination must be outside the payload root")
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        promoted = self.metadata.setdefault("promoted_paths", [])
        if str(target) not in promoted:
            promoted.append(str(target))
            promoted.sort()
        self._save()
        return target

    def finalize(self, outcome: str) -> dict:
        """Finalize the operation and remove its owned payload."""
        if outcome not in _TERMINAL_OUTCOMES:
            raise ValueError(f"unsupported operation outcome: {outcome}")
        return self._cleanup(f"finalized-{outcome}", outcome=outcome)

    def _reap(self) -> dict:
        """Reap an expired operation owned by a dead process."""
        return self._cleanup("reaped", outcome="orphan-reaped")

    def _cleanup(self, status: str, *, outcome: str) -> dict:
        """Remove operation payloads and persist the terminal manifest state."""
        if self.metadata.get("status") not in {"active", "orphaned"}:
            return {"deleted_files": 0, "deleted_directories": 0, "failures": []}
        deleted_files = 0
        deleted_directories = 0
        failures: list[str] = []
        shared_root = bool(self.metadata.get("shared_payload_root"))
        root_present = self.root.exists() or self.root.is_symlink()
        root_intact = _identity_matches(
            self.root, self.metadata.get("payload_root_identity")
        )
        if root_present and not root_intact:
            failures.append(f"{self.root}: allocated payload root was replaced")
        pinned_fd = None
        cleanup_root = self.root
        if root_present and root_intact and not sys.platform.startswith("win"):
            try:
                pinned_fd = os.open(
                    self.root,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                cleanup_root = Path(f"/proc/self/fd/{pinned_fd}")
            except OSError as exc:
                failures.append(f"{self.root}: could not pin payload root: {exc}")
                root_intact = False
        if root_present and root_intact:
            if shared_root:
                try:
                    entries = [
                        cleanup_root / _validate_relative_path(relative)
                        for relative in self.metadata.get("relative_paths", [])
                    ]
                except (TypeError, ValueError) as exc:
                    entries = []
                    failures.append(f"{self.root}: invalid manifest relative path: {exc}")
            else:
                entries = list(_iter_owned_entries(cleanup_root))
            for entry in entries:
                try:
                    if entry.is_symlink():
                        if not _is_lexically_within(entry, cleanup_root):
                            raise ValueError("entry escaped the operation root")
                        entry.unlink()
                        deleted_files += 1
                    elif entry.is_file():
                        if not _is_within(entry, cleanup_root):
                            raise ValueError("entry escaped the operation root")
                        entry.unlink()
                        deleted_files += 1
                except (OSError, ValueError) as exc:
                    failures.append(f"{entry}: {exc}")
            if not shared_root:
                for directory, directories, _files in os.walk(
                    cleanup_root, topdown=False, followlinks=False
                ):
                    for name in directories:
                        entry = Path(directory) / name
                        try:
                            if entry.is_symlink():
                                entry.unlink()
                                deleted_files += 1
                            else:
                                entry.rmdir()
                                deleted_directories += 1
                        except OSError as exc:
                            failures.append(f"{entry}: {exc}")
                try:
                    if _identity_matches(
                        self.root, self.metadata.get("payload_root_identity")
                    ):
                        self.root.rmdir()
                        deleted_directories += 1
                    else:
                        failures.append(f"{self.root}: allocated payload root changed")
                except OSError as exc:
                    failures.append(f"{self.root}: {exc}")
        if pinned_fd is not None:
            try:
                os.close(pinned_fd)
            except OSError:
                pass
        if shared_root and self.control_root.exists():
            if not _identity_matches(
                self.control_root, self.metadata.get("control_root_identity")
            ):
                failures.append(f"{self.control_root}: control root was replaced")
            else:
                try:
                    self.control_root.rmdir()
                    deleted_directories += 1
                except OSError as exc:
                    failures.append(f"{self.control_root}: {exc}")
        operation_parent = self.control_root.parent
        if operation_parent != self.manager.operations_root and operation_parent.exists():
            try:
                if not any(operation_parent.iterdir()):
                    operation_parent.rmdir()
                    deleted_directories += 1
            except OSError as exc:
                failures.append(f"{operation_parent}: {exc}")
        terminal_status = status if not failures else "quarantined"
        self.metadata["status"] = terminal_status
        self.metadata["outcome"] = outcome
        self.metadata["finalized_at"] = _timestamp(self.manager.now())
        self.metadata["cleanup"] = {
            "deleted_files": deleted_files,
            "deleted_directories": deleted_directories,
            "failures": failures,
        }
        self._save()
        return {
            "deleted_files": deleted_files,
            "deleted_directories": deleted_directories,
            "failures": failures,
        }

    def _save(self) -> None:
        """Persist the operation manifest."""
        _write_json_atomic(self.manifest_path, self.metadata)


class ArtifactManager:
    """Manage profile-scoped Hermes temporary artifact operations."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        session_id: str = "default",
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Initialize an artifact manager.

        Args:
            root: Managed artifact root. Defaults to the Hermes terminal cache.
            session_id: Session namespace for newly created operations.
            now: Clock used for timestamps and deterministic tests.
        """
        self.root = Path(root) if root is not None else get_hermes_home() / "cache" / "terminal"
        self.root = self.root.expanduser().absolute()
        if self.root.is_symlink():
            raise ValueError("managed artifact root must not be a symlink")
        self.session_id = _validate_component(session_id, "session_id")
        self.now = now
        self.policy = copy.deepcopy(DEFAULT_ARTIFACT_POLICY)
        self.root.mkdir(parents=True, exist_ok=True)
        _safe_mode(self.root, stat.S_IRWXU)
        self.operations_root = self.root / "operations"
        self.manifests_root = self.root / "manifests"
        self.operations_root.mkdir(parents=True, exist_ok=True)
        self.manifests_root.mkdir(parents=True, exist_ok=True)
        _safe_mode(self.operations_root, stat.S_IRWXU)
        _safe_mode(self.manifests_root, stat.S_IRWXU)

    @classmethod
    def from_config(
        cls,
        config: dict | None = None,
        *,
        session_id: str = "default",
        now: Callable[[], datetime] = _utc_now,
    ) -> "ArtifactManager":
        """Create a manager from a resolved Hermes configuration.

        Args:
            config: Resolved Hermes configuration mapping.
            session_id: Session namespace for newly created operations.
            now: Clock used for timestamps and deterministic tests.

        Returns:
            A manager using the configured or profile-managed temp root.
        """
        resolved_config = config or {}
        terminal = resolved_config.get("terminal") or {}
        configured_root = terminal.get("temp_dir")
        root = None
        if isinstance(configured_root, str):
            candidate = Path(configured_root).expanduser()
            if candidate.is_absolute() and candidate.is_dir():
                root = candidate
        manager = cls(root, session_id=session_id, now=now)
        configured_policy = resolved_config.get("artifact_lifecycle") or {}
        if isinstance(configured_policy, dict):
            manager.policy = copy.deepcopy(DEFAULT_ARTIFACT_POLICY)
            manager.policy.update(copy.deepcopy(configured_policy))
        return manager

    def create_operation(
        self,
        *,
        owner: str,
        kind: str,
        sensitivity: str,
        retention: str,
        retention_seconds: int | None = None,
        pid: int | None = None,
        pid_start_time: str | None = None,
        payload_root: Path | str | None = None,
    ) -> ArtifactOperation:
        """Create and register a manager-owned operation.

        Args:
            owner: Component that owns the operation.
            kind: Artifact lifecycle class.
            sensitivity: Data sensitivity classification.
            retention: Cleanup policy.
            retention_seconds: TTL for ``ttl`` operations.
            pid: Owning process ID, when applicable.
            pid_start_time: Owning process start identity, when available.
            payload_root: Explicit shared namespace for registered payloads.

        Returns:
            A newly allocated artifact operation.

        Raises:
            ValueError: If a lifecycle field is invalid.
        """
        if not self.policy.get("enabled", True):
            raise RuntimeError("artifact lifecycle is disabled")
        if not owner or not owner.strip():
            raise ValueError("owner is required")
        if kind not in _ARTIFACT_KINDS:
            raise ValueError(f"unsupported artifact kind: {kind}")
        if sensitivity not in _SENSITIVITIES:
            raise ValueError(f"unsupported sensitivity: {sensitivity}")
        if retention not in _RETENTION_POLICIES:
            raise ValueError(f"unsupported retention policy: {retention}")
        if retention == "ttl" and retention_seconds is None:
            policy_key = {
                "spillover": "spillover_retention_hours",
                "scratch": "scratch_retention_hours",
            }.get(kind, "orphan_grace_hours")
            try:
                retention_seconds = int(float(self.policy[policy_key]) * 3600)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("configured TTL is invalid") from exc
        if retention == "ttl" and (retention_seconds is None or retention_seconds <= 0):
            raise ValueError("ttl operations require positive retention_seconds")
        if retention != "ttl" and retention_seconds is not None:
            raise ValueError("retention_seconds is only valid for ttl operations")
        if pid is not None and pid_start_time is None:
            try:
                pid_start_time = _read_pid_start_time(int(pid))
            except (TypeError, ValueError):
                pid_start_time = None

        try:
            quota_mb = float(self.policy.get("max_total_size_mb", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("configured artifact quota is invalid") from exc
        if quota_mb > 0 and self.status()["payload_bytes"] >= quota_mb * 1024 * 1024:
            raise ValueError("artifact lifecycle quota exceeded")

        shared_payload_root = payload_root is not None
        if shared_payload_root:
            payload_path = Path(payload_root).expanduser()
            if not payload_path.is_absolute() or not payload_path.is_dir():
                raise ValueError("payload_root must be an existing absolute directory")
            if payload_path.is_symlink():
                raise ValueError("payload_root must not be a symlink")
        else:
            payload_path = None

        operation_id = uuid.uuid4().hex
        control_root = self.operations_root / self.session_id / operation_id
        control_root.mkdir(parents=True, exist_ok=False)
        _safe_mode(control_root.parent, stat.S_IRWXU)
        _safe_mode(control_root, stat.S_IRWXU)
        if payload_path is None:
            payload_path = control_root
        payload_identity = _directory_identity(payload_path)
        control_identity = _directory_identity(control_root)
        created_at = self.now()
        expires_at = (
            created_at + timedelta(seconds=retention_seconds)
            if retention_seconds is not None
            else None
        )
        try:
            orphan_grace_seconds = int(
                float(self.policy.get("orphan_grace_hours", 24)) * 3600
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("configured orphan grace is invalid") from exc
        if orphan_grace_seconds <= 0:
            raise ValueError("configured orphan grace must be positive")
        orphan_expires_at = created_at + timedelta(seconds=orphan_grace_seconds)
        metadata = {
            "artifact_id": operation_id,
            "profile": os.environ.get("HERMES_PROFILE", "default"),
            "session_id": self.session_id,
            "operation_id": operation_id,
            "owner": owner.strip(),
            "kind": kind,
            "sensitivity": sensitivity,
            "created_at": _timestamp(created_at),
            "expires_at": _timestamp(expires_at) if expires_at else None,
            "orphan_expires_at": _timestamp(orphan_expires_at),
            "last_heartbeat_at": _timestamp(created_at),
            "pid": pid,
            "pid_start_time": pid_start_time,
            "status": "active",
            "cleanup_policy": retention,
            "payload_root": str(payload_path) if shared_payload_root else None,
            "payload_root_identity": {
                "device": payload_identity[0],
                "inode": payload_identity[1],
            },
            "control_root_identity": {
                "device": control_identity[0],
                "inode": control_identity[1],
            },
            "shared_payload_root": shared_payload_root,
            "relative_paths": [],
            "promoted_paths": [],
        }
        manifest_path = self.manifests_root / f"{operation_id}.json"
        operation = ArtifactOperation(
            manager=self,
            operation_id=operation_id,
            root=payload_path,
            control_root=control_root,
            manifest_path=manifest_path,
            metadata=metadata,
        )
        operation._save()
        return operation

    def load_operation(self, operation_id: str) -> ArtifactOperation:
        """Load a registered operation from its manifest."""
        _validate_component(operation_id, "operation_id")
        manifest_path = self.manifests_root / f"{operation_id}.json"
        if manifest_path.is_symlink():
            raise ValueError("manifest must not be a symlink")
        metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
        if metadata.get("operation_id") != operation_id:
            raise ValueError("manifest operation_id does not match its filename")
        session_id = _validate_component(metadata["session_id"], "session_id")
        relative_paths = metadata.get("relative_paths")
        if not isinstance(relative_paths, list):
            raise ValueError("manifest relative paths are invalid")
        for relative in relative_paths:
            if not isinstance(relative, str):
                raise ValueError("manifest relative path is invalid")
            _validate_relative_path(relative)
        control_root = self.operations_root / session_id / operation_id
        if metadata.get("shared_payload_root"):
            payload_value = metadata.get("payload_root")
            if not isinstance(payload_value, str) or not payload_value:
                raise ValueError("manifest payload root is invalid")
            root = Path(payload_value).expanduser()
            if not root.is_absolute() or root.is_symlink():
                raise ValueError("manifest payload root is invalid")
            if not root.is_dir():
                raise ValueError("manifest payload root is invalid")
            identity = metadata.get("payload_root_identity")
            try:
                expected_identity = (int(identity["device"]), int(identity["inode"]))
                actual_identity = _directory_identity(root)
            except (KeyError, TypeError, ValueError, OSError) as exc:
                raise ValueError("manifest payload root identity is invalid") from exc
            if actual_identity != expected_identity:
                raise ValueError("manifest payload root does not match allocation")
        else:
            root = control_root
        return ArtifactOperation(
            manager=self,
            operation_id=operation_id,
            root=root,
            control_root=control_root,
            manifest_path=manifest_path,
            metadata=metadata,
        )

    def reap_orphans(
        self,
        *,
        now: datetime | None = None,
        is_pid_alive: Callable[[int, str | None], bool] = _pid_is_alive,
        dry_run: bool = True,
    ) -> dict:
        """Reap expired operations whose owners are no longer alive.

        Args:
            now: Comparison time. Defaults to the manager clock.
            is_pid_alive: Process liveness callback accepting PID and start identity.
            dry_run: Return candidates without removing payloads when true.

        Returns:
            A dictionary containing candidate, reaped, and failure counts.
        """
        comparison_time = _as_utc(now or self.now())
        candidates: list[str] = []
        reaped = 0
        failures: list[str] = []
        if not self.manifests_root.exists():
            return {"candidates": candidates, "reaped": reaped, "failures": failures}
        for manifest_path in sorted(self.manifests_root.glob("*.json")):
            try:
                metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
                if metadata.get("status") != "active":
                    continue
                expires_at = _parse_timestamp(
                    metadata.get("expires_at") or metadata.get("orphan_expires_at")
                )
                if expires_at is None or expires_at > comparison_time:
                    continue
                operation_id = _validate_component(metadata["operation_id"], "operation_id")
                candidates.append(operation_id)
                pid = metadata.get("pid")
                if pid is not None and is_pid_alive(int(pid), metadata.get("pid_start_time")):
                    continue
                if dry_run:
                    continue
                operation = self.load_operation(operation_id)
                operation.metadata["status"] = "orphaned"
                result = operation._reap()
                if result["failures"]:
                    failures.extend(result["failures"])
                elif operation.metadata.get("status") == "reaped":
                    reaped += 1
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                failures.append(f"{manifest_path}: {exc}")
        return {"candidates": candidates, "reaped": reaped, "failures": failures}

    def status(self) -> dict:
        """Return a metadata-only summary of managed operations."""
        counts: dict[str, int] = {}
        by_owner: dict[str, dict[str, int]] = {}
        by_kind_bytes: dict[str, int] = {}
        active = []
        total_bytes = 0
        seen_payloads: set[str] = set()
        oldest_artifact = None
        largest_artifact = None
        cleanup_failures: list[str] = []
        quarantined: list[str] = []
        promoted: list[str] = []
        retained: list[str] = []
        expired_orphan_candidates: list[str] = []
        comparison_time = _as_utc(self.now())
        manifests = list(self.manifests_root.glob("*.json")) if self.manifests_root.exists() else []
        for manifest_path in manifests:
            try:
                metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            kind = str(metadata.get("kind", "unknown"))
            counts[kind] = counts.get(kind, 0) + 1
            owner = str(metadata.get("owner", "unknown"))
            owner_summary = by_owner.setdefault(owner, {"operations": 0, "bytes": 0})
            owner_summary["operations"] += 1
            if metadata.get("status") == "active":
                active.append(metadata.get("operation_id"))
                expires_at = _parse_timestamp(
                    metadata.get("expires_at") or metadata.get("orphan_expires_at")
                )
                if expires_at is not None and expires_at <= comparison_time:
                    pid = metadata.get("pid")
                    owner_alive = False
                    if pid is not None:
                        try:
                            owner_alive = _pid_is_alive(
                                int(pid), metadata.get("pid_start_time")
                            )
                        except (TypeError, ValueError):
                            owner_alive = False
                    if not owner_alive and metadata.get("operation_id"):
                        expired_orphan_candidates.append(str(metadata["operation_id"]))
            operation_id = metadata.get("operation_id")
            if metadata.get("status") == "quarantined" and operation_id:
                quarantined.append(str(operation_id))
            if metadata.get("promoted_paths") and operation_id:
                promoted.append(str(operation_id))
            if metadata.get("status") == "retained" and operation_id:
                retained.append(str(operation_id))
            cleanup = metadata.get("cleanup")
            if isinstance(cleanup, dict):
                cleanup_failures.extend(str(item) for item in cleanup.get("failures", []))
            session_id = metadata.get("session_id")
            if session_id and operation_id:
                operation_root = (
                    Path(metadata["payload_root"])
                    if metadata.get("shared_payload_root")
                    else self.operations_root / session_id / operation_id
                )
                entries = (
                    (operation_root / relative for relative in metadata.get("relative_paths", []))
                    if metadata.get("shared_payload_root")
                    else _iter_owned_entries(operation_root)
                )
                operation_bytes = 0
                for entry in entries if operation_root.exists() else ():
                    try:
                        if _is_lexically_within(entry, operation_root):
                            size = entry.lstat().st_size
                            operation_bytes += size
                            entry_key = str(entry.absolute())
                            if entry_key not in seen_payloads:
                                seen_payloads.add(entry_key)
                                total_bytes += size
                    except OSError:
                        pass
                owner_summary["bytes"] += operation_bytes
                by_kind_bytes[kind] = by_kind_bytes.get(kind, 0) + operation_bytes
                artifact = {
                    "operation_id": str(operation_id),
                    "owner": owner,
                    "kind": kind,
                    "created_at": metadata.get("created_at"),
                    "bytes": operation_bytes,
                }
                if oldest_artifact is None or (
                    str(artifact["created_at"]),
                    str(artifact["operation_id"]),
                ) < (
                    str(oldest_artifact["created_at"]),
                    str(oldest_artifact["operation_id"]),
                ):
                    oldest_artifact = artifact
                if largest_artifact is None or (
                    operation_bytes,
                    str(artifact["created_at"]),
                    str(artifact["operation_id"]),
                ) > (
                    int(largest_artifact["bytes"]),
                    str(largest_artifact["created_at"]),
                    str(largest_artifact["operation_id"]),
                ):
                    largest_artifact = artifact
        return {
            "root": str(self.root),
            "manifest_count": len(manifests),
            "by_kind": counts,
            "by_kind_bytes": by_kind_bytes,
            "by_owner": by_owner,
            "active_operations": sorted(item for item in active if item),
            "expired_orphan_candidates": sorted(expired_orphan_candidates),
            "payload_bytes": total_bytes,
            "oldest_artifact": oldest_artifact,
            "largest_artifact": largest_artifact,
            "cleanup_failures": cleanup_failures,
            "quarantined": sorted(quarantined),
            "promoted": sorted(promoted),
            "retained": sorted(retained),
            "external_roots_excluded": True,
        }

    def mark_retained(self, operation: ArtifactOperation, reason: str) -> None:
        """Mark an operation retained without deleting its payload."""
        if operation.metadata.get("status") != "active":
            return
        operation.metadata["status"] = "retained"
        operation.metadata["outcome"] = reason
        operation.metadata["finalized_at"] = _timestamp(self.now())
        operation._save()
        self._remove_control_shell(operation)

    def mark_finalized(self, operation: ArtifactOperation, outcome: str) -> None:
        """Record an externally completed operation without deleting payloads."""
        if operation.metadata.get("status") != "active":
            return
        operation.metadata["status"] = f"finalized-{outcome}"
        operation.metadata["outcome"] = outcome
        operation.metadata["finalized_at"] = _timestamp(self.now())
        operation.metadata["cleanup"] = {
            "deleted_files": 0,
            "deleted_directories": 0,
            "failures": [],
        }
        operation._save()
        self._remove_control_shell(operation)

    def _remove_control_shell(self, operation: ArtifactOperation) -> None:
        """Remove an empty private control directory after external cleanup."""
        try:
            operation.control_root.rmdir()
        except OSError:
            return
        parent = operation.control_root.parent
        try:
            if parent != self.operations_root and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

    def list_operations(
        self,
        *,
        owner: str | None = None,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """Return metadata for operations matching optional filters.

        Args:
            owner: Optional owner filter.
            kind: Optional artifact-kind filter.
            status: Optional lifecycle-status filter.

        Returns:
            Sorted metadata dictionaries without payload contents.
        """
        operations = []
        if not self.manifests_root.exists():
            return operations
        for manifest_path in sorted(self.manifests_root.glob("*.json")):
            try:
                metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if owner is not None and metadata.get("owner") != owner:
                continue
            if kind is not None and metadata.get("kind") != kind:
                continue
            if status is not None and metadata.get("status") != status:
                continue
            operations.append(metadata)
        return operations


@contextmanager
def managed_artifact_operation(
    *,
    owner: str,
    kind: str,
    sensitivity: str,
    retention: str = "finalize",
    retention_seconds: int | None = None,
    manager: ArtifactManager | None = None,
    root: Path | str | None = None,
    session_id: str = "default",
):
    """Yield an operation and apply its retention policy on exit.

    Args:
        owner: Component that owns the operation.
        kind: Artifact lifecycle class.
        sensitivity: Data sensitivity classification.
        retention: Cleanup policy.
        retention_seconds: TTL for ``ttl`` operations.
        manager: Existing manager to use, when available.
        root: Managed root used when no manager is supplied.
        session_id: Session namespace used when no manager is supplied.

    Yields:
        The newly created artifact operation.
    """
    if manager is None:
        try:
            from hermes_cli.config import load_config

            active_manager = ArtifactManager.from_config(
                load_config(), session_id=session_id
            )
        except Exception:
            active_manager = ArtifactManager(root, session_id=session_id)
    else:
        active_manager = manager
    operation = active_manager.create_operation(
        owner=owner,
        kind=kind,
        sensitivity=sensitivity,
        retention=retention,
        retention_seconds=retention_seconds,
        pid=os.getpid(),
    )
    try:
        yield operation
    except BaseException:
        if retention == "manual":
            active_manager.mark_retained(operation, "context-failure")
        else:
            operation.finalize("failure")
        raise
    else:
        if retention == "finalize":
            operation.finalize("success")
        elif retention == "manual":
            active_manager.mark_retained(operation, "manual-context")
