# Artifact Lifecycle

Hermes owns several different kinds of filesystem state. Temporary execution files
must not be mixed with durable conversations, rollback state, user outputs, or
vendor-owned caches.

## Managed root

The local terminal backend uses `terminal.temp_dir` as its session temporary root.
When the setting is empty, Hermes uses:

```text
$HERMES_HOME/cache/terminal/
```

A configured `terminal.temp_dir` must be an existing absolute path. Hermes does
not globally age-prune a user-selected path. It only cleans operation directories
that Hermes created and registered there.

Managed operations use separate namespaces:

```text
cache/terminal/
├── operations/<session-id>/<operation-id>/
├── manifests/<operation-id>.json
└── spillover/<result-file>
```

The manifest directory stores lifecycle metadata, not prompt bodies, credentials,
cookies, raw environment values, or full tool results.

## Artifact classes

| Class | Meaning | Default lifecycle |
|---|---|---|
| `scratch` | Short-lived working files | Delete when the operation finalizes |
| `process` | Process logs, PID/exit files, shell snapshots | Delete after process teardown |
| `rpc` | Code-execution request/response files | Delete after the owner exits |
| `staging` | Archive, import, conversion, and generator staging | Delete after success or failure |
| `spillover` | Full output retained after inline truncation | Delete by age/size policy |
| `worktree` | Git worktree associated with an agent/task | Git-aware finalization only |
| `checkpoint` | Rollback shadow Git state | Owned by `hermes checkpoints` |
| `backup` | Profile/update/curator rollback copies | Owned by the relevant backup policy |
| `durable` | User-requested report or output | Requires explicit promotion |
| `external` | Browser, model, package, or user cache | Never swept by Hermes |

## Operation states

```text
allocated → active → finalized-success
                    → finalized-failure
                    → cancelled
                    → timed-out
                    → orphaned → reaped | quarantined
```

An operation can be deleted only when Hermes created or explicitly registered the
path, the path remains below the owned root, and the operation is finalized or its
lease has expired. Live process ownership is checked with PID and process-start
identity where available.

## Python API

Use `ArtifactManager` for operations that need a durable lifecycle record:

```python
from tools.artifact_lifecycle import managed_artifact_operation

with managed_artifact_operation(
    owner="example-component",
    kind="staging",
    sensitivity="sensitive",
) as operation:
    input_path = operation.path("input.dat")
    input_path.write_bytes(payload)
```

The context finalizes on normal completion and on exceptions. For longer-running
operations, retain the operation object and call `heartbeat()` periodically.
Use `promote()` when an output becomes a durable user-visible file. Use
`mark_retained()` for an artifact that must remain available for manual recovery.

All cleanup is path-safe and file-oriented:

- traversal and symlink escapes are rejected;
- symlinks are unlinked without following their targets;
- files are unlinked individually;
- directories are removed only after they are empty;
- failed cleanup is reported and not widened to a broader deletion.

## CLI

Inspect managed operations without reading payload contents:

```bash
hermes artifacts status
hermes artifacts list
hermes artifacts list --owner execute_code
hermes artifacts dry-run
hermes artifacts reap
```

`dry-run` is read-only. `reap` removes only expired, dead-owner operations with
valid manifests. Explicit promotion is available for a single operation file:

```bash
hermes artifacts promote \
  --operation-id <id> \
  --relative-name report.md \
  --destination ~/reports/report.md
```

The CLI reports managed temporary state separately from sessions, checkpoints,
backups, logs, memories, and external caches. Status also reports owner and
class byte totals, active and expired-orphan candidates, the oldest and largest
operation, cleanup failures, promoted/retained operations, and the known
external roots excluded from automatic cleanup.

## Integration rules

### Terminal and code execution

Terminal shell snapshots, cwd markers, background process groups, code-execution
staging, session kernels, and RPC files must use an operation root. Linux
session kernels use an abstract Unix socket to avoid filesystem pathname limits.
macOS uses a filesystem socket inside the operation root because macOS does not
support Linux abstract sockets; Windows uses loopback TCP.

Remote backends have their own filesystem. They receive an operation identifier
and use a bounded backend-local cleanup routine. A host-side manager must never
assume that a remote path is readable on the host.

### Spillover

Oversized tool results are stored in the manager-owned spillover namespace with
an expiry. The in-context result contains a preview and a path reference. The
full result is treated as sensitive and is redacted before persistence where the
producer supplies raw tool output that may contain credentials.

### Delegation

Each accepted async delegation receives an artifact operation identity. The
identity is returned in the dispatch handle, persisted with the durable delegation
record, and included in completion/recovery events. Child reports remain
non-durable until explicitly promoted.

### Worktrees

Worktrees are not ordinary scratch directories. Hermes preserves dirty trees,
unique unpushed commits, and in-use trees. Clean, fully merged trees may be
removed with Git's worktree command. The artifact manifest records the repository,
branch, task, owner, and worktree path, but generic artifact cleanup never removes
a Git worktree.

### Profiles and external roots

Use `get_hermes_home()` for profile-scoped state. Browser profiles, Playwright
binaries, Hugging Face models, uv caches, and other vendor state are external
artifacts. Hermes may report them, but must not remove them through scratch
cleanup.

## Skill and plugin policy

Skills and plugins that create temporary files must declare an artifact policy and
use Hermes-managed operations or the operation environment variables:

```yaml
artifact_policy:
  kinds: [scratch, staging, worktree]
  cleanup: finalize
  persistent_outputs: explicit-promotion
  external_state: preserve
```

For a skill, place this mapping in `SKILL.md` frontmatter. The install scanner
requires it when executable guidance contains a likely file-writing API such as
`write_text`, `write_bytes`, or a temporary-file allocator. The policy is
metadata only; it must not contain credentials or prompt contents.

New executable guidance is rejected when it teaches unmanaged patterns such as:

```text
mktemp -d
TemporaryDirectory() without an owned directory
hardcoded /tmp or /run/user paths
git worktree add ... /tmp/...
raw recursive deletion of scratch directories
```

Rejected legacy examples may remain only when clearly labelled as rejected and
non-executable. The `disk-cleanup` compatibility plugin uses manifests and the
shared bounded remover; it does not infer ownership from filenames.

## Retention configuration

The artifact policy is separate from durable session/checkpoint retention:

```yaml
artifact_lifecycle:
  enabled: true
  orphan_grace_hours: 24
  spillover_retention_hours: 24
  scratch_retention_hours: 0
  max_total_size_mb: 2048
```

Existing session and checkpoint settings retain their own controls:

```yaml
sessions:
  auto_prune: true
  retention_days: 90
  vacuum_after_prune: true

checkpoints:
  auto_prune: true
  retention_days: 7
```

Active sessions are never pruned solely because they are old. Checkpoint orphan
removal remains an explicit checkpoint-manager decision because a missing project
may be an unmounted external volume.

## Enforcement limits

`TMPDIR`, `terminal.temp_dir`, and child environment variables control cooperative
libraries. A local unrestricted shell can still write an absolute path outside the
operation root. Hermes therefore combines:

1. internal producer migration to `ArtifactManager`;
2. skill/plugin install-time policy checks;
3. pre-tool warnings or blocks for obvious unmanaged creation commands;
4. operation-scoped child environment variables;
5. container or sandbox backends for mandatory filesystem containment.

A shell-command regex is not a complete local filesystem sandbox.
