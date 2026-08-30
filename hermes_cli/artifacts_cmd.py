"""CLI commands for inspecting and managing Hermes artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path

from hermes_constants import get_hermes_home
from tools.artifact_lifecycle import ArtifactManager


def _resolved_config() -> dict:
    """Load the resolved Hermes configuration."""
    try:
        from hermes_cli.config import load_config

        return load_config()
    except Exception:
        return {}


def _manager(config: dict | None = None) -> ArtifactManager:
    """Create an artifact manager for the active Hermes profile."""
    config = _resolved_config() if config is None else config
    session_id = os.environ.get("HERMES_SESSION_ID") or "default"
    try:
        return ArtifactManager.from_config(config, session_id=session_id)
    except ValueError:
        return ArtifactManager.from_config(config, session_id="default")


def _tree_bytes(path: Path) -> int:
    """Sum regular-file sizes beneath a durable state path."""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for directory, _directories, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(directory) / name).stat().st_size
            except OSError:
                pass
    return total


def _durable_status(config: dict) -> dict:
    """Return retention metadata for durable stores without reading payloads."""
    home = get_hermes_home()
    sessions = config.get("sessions") or {}
    checkpoints = config.get("checkpoints") or {}
    session_paths = [home / name for name in ("state.db", "state.db-wal", "state.db-shm")]
    durable = {
        "sessions": {
            "path": str(home / "state.db"),
            "bytes": sum(_tree_bytes(path) for path in session_paths),
            "auto_prune": bool(sessions.get("auto_prune", False)),
            "retention_days": sessions.get("retention_days", 90),
            "vacuum_after_prune": bool(sessions.get("vacuum_after_prune", True)),
        },
        "checkpoints": {
            "path": str(home / "checkpoints"),
            "bytes": _tree_bytes(home / "checkpoints"),
            "enabled": bool(checkpoints.get("enabled", False)),
            "auto_prune": bool(checkpoints.get("auto_prune", True)),
            "retention_days": checkpoints.get("retention_days", 7),
            "max_total_size_mb": checkpoints.get("max_total_size_mb", 500),
        },
    }
    for name in ("backups", "state-snapshots", "logs", "memories"):
        durable[name] = {
            "path": str(home / name),
            "bytes": _tree_bytes(home / name),
            "automatic_cleanup": False,
            "owner_managed": True,
        }
    return durable


def _external_status() -> dict:
    """Return known external roots excluded from Hermes cleanup."""
    home = Path.home()
    paths = [
        home / ".cache",
        home / ".local" / "state" / "dim_mcp" / "browser",
        home / ".cache" / "ms-playwright",
        home / ".cache" / "huggingface",
    ]
    return {
        "automatic_cleanup": False,
        "owner_managed": True,
        "paths": [str(path) for path in paths],
    }


def _print_json(payload) -> None:
    """Print one machine-readable JSON payload."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def cmd_artifacts(args) -> int:
    """Dispatch an artifact lifecycle CLI action."""
    action = getattr(args, "artifact_action", None) or "status"
    config = _resolved_config()
    manager = _manager(config)

    if action == "status":
        status = manager.status()
        status["durable"] = _durable_status(config)
        status["external"] = _external_status()
        _print_json(status)
        return 0
    if action == "dry-run":
        _print_json(manager.reap_orphans(dry_run=True))
        return 0
    if action == "reap":
        _print_json(manager.reap_orphans(dry_run=False))
        return 0
    if action == "list":
        _print_json(
            manager.list_operations(
                owner=getattr(args, "owner", None),
                kind=getattr(args, "kind", None),
                status=getattr(args, "status", None),
            )
        )
        return 0
    if action == "promote":
        operation_id = getattr(args, "operation_id", "")
        destination = getattr(args, "destination", "")
        relative_name = getattr(args, "relative_name", None)
        if not operation_id or not destination:
            print("promote requires --operation-id and --destination")
            return 2
        operation = manager.load_operation(operation_id)
        target = operation.promote(destination, relative_name=relative_name)
        operation.finalize("success")
        _print_json({"operation_id": operation_id, "destination": str(target)})
        return 0

    print(f"Unknown artifact action: {action}")
    return 2


def register_parser(subparsers, handler=cmd_artifacts):
    """Register the ``hermes artifacts`` command tree."""
    parser = subparsers.add_parser(
        "artifacts",
        help="Inspect and manage Hermes-owned temporary artifacts",
    )
    actions = parser.add_subparsers(dest="artifact_action")
    actions.add_parser("status", help="Show managed artifact status")
    actions.add_parser("dry-run", help="Preview expired artifact cleanup")
    actions.add_parser("reap", help="Reap expired dead-owner artifacts")

    list_parser = actions.add_parser("list", help="List artifact operation manifests")
    list_parser.add_argument("--owner")
    list_parser.add_argument("--kind")
    list_parser.add_argument("--status")

    promote_parser = actions.add_parser("promote", help="Promote an artifact file")
    promote_parser.add_argument("--operation-id", required=True)
    promote_parser.add_argument("--relative-name")
    promote_parser.add_argument("--destination", required=True)
    parser.set_defaults(func=handler, artifact_action="status")
    return parser
