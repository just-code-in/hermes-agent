# Kanban scratch workspaces for Docker workers — design note (Option B)

Status: **proposal for Justin's review. No code.** Written against upstream `main`
`6e69a8933` (2026-09-28); line numbers refer to that commit. The reporting fleet runs
`8c9fe964` plus 18 local patches. Revision 2 fits the design to three pieces already
live on the fleet:

- `container-path-translation.patch`, which carries `tools/container_paths.py`;
- the fail-closed `pre_tool_call` hook `scripts/hermes-hook-kanban-artifact-host-path.py`;
- the `kanban-worker-contract` plugin's `/workspace/kanban/$HERMES_KANBAN_TASK/`
  convention.

## The bug in four steps

1. **Allocation.** The dispatcher creates the scratch dir on the host, at
   `<kanban home>/boards/<board>/workspaces/<task-id>`
   (`hermes_cli/kanban_db_workspace.py:583` `resolve_workspace`). It then pins both
   `HERMES_KANBAN_WORKSPACE` and `TERMINAL_CWD` to that host path
   (`hermes_cli/kanban_db_dispatch.py:2847`, `:2863`).
2. **Execution.** The worker's terminal runs in the profile's long-lived container,
   where that path doesn't exist. `_resolve_config_cwd` (`tools/terminal_tool.py:652`)
   discards it, so `cd $HERMES_KANBAN_WORKSPACE` fails and output lands in `/workspace`.
3. **Completion.** `kanban_complete(artifacts=["/workspace/x.md"])` records a container
   path. `_persist_scratch_completion_artifacts` (`hermes_cli/kanban_db.py:3009`) copies
   only paths under the host scratch root, so it skips this one silently. There's no
   copy, no attachment and no error. Completion then removes the empty scratch dir.
4. **Result.** The board points at a path nothing on the host can open.

Mounting each scratch dir into the container (Option A) is ruled out. Containers are
reused across processes by label (`tools/environments/docker.py:668-686`), and mounts
apply only when a container is created. Justin rejected recreating containers on
2026-08-05.

## Design: allocate inside a mount the container already has

The dispatcher still creates a real host dir, so the board, the DB and cleanup keep
working on host paths. The change is **where**: inside the assignee's existing writable
`/workspace` mount, at the plugin's existing spelling. The same directory has two names:

| Side | Path |
| --- | --- |
| host (DB `workspace_path`) | `<host dir behind /workspace>/kanban/<task-id>` |
| container (worker env) | `/workspace/kanban/<task-id>` |

On the fleet, "the host dir behind `/workspace`" is each profile's
`…/sandboxes/docker/default/workspace`, which every profile mounts read-write.

### Dispatcher changes

In `_dispatch_lane_task` (`hermes_cli/kanban_db_dispatch.py:2016`), before
`resolve_workspace` runs for a `scratch` task with no explicit `workspace_path`:

1. Enter `_worker_profile_scope(profile_home)` (`:2600`), the scope toolset resolution
   already uses. Inside it, `tools.container_paths` reads the **assignee's**
   `TERMINAL_ENV` / `TERMINAL_DOCKER_VOLUMES` and not the dispatching gateway's.
2. `host_root = container_paths.to_host_dir("/workspace")`. This returns a writable
   mount only; read-only mounts are excluded, and a non-Docker backend returns `None`.
3. If `host_root` is set, allocate `host_root/kanban/<task-id>`, **refusing a dir that
   already exists**. Task ids are 32 random bits per board
   (`hermes_cli/kanban_db.py:1086`), so two boards can collide in one profile's
   sandbox; a refusal falls back to step 5 with a warning.
4. Record the host path as `workspace_path`. Build the worker env with
   `HERMES_KANBAN_WORKSPACE` and `TERMINAL_CWD` set to
   `container_paths.to_container_path(host_path)`, which is `/workspace/kanban/<task-id>`.
   The existing `os.path.isdir(workspace)` guard at `:2862` must test the **host** path
   before `TERMINAL_CWD` gets the container spelling.
5. **Fallback, which is today's behaviour byte for byte:** a non-Docker backend, no
   writable `/workspace` mount, or an allocation refusal.

`worktree` and `dir` workspaces are never relocated. They're user-owned paths.

### Completion changes

`kanban_complete` and `kanban_request_review` run in the worker process on the host,
with the assignee's terminal env already bound. In `tools/kanban_tools.py`, in
`_handle_complete` (`:690`) and `_handle_request_review` (`:828`), right after the
artifacts are coerced and **before** `complete_task` or any containment check, each
artifact is translated by its prefix:

| Declared artifact | Result |
| --- | --- |
| Under a **writable** mount, host file exists | Replaced by its host path; a scratch artifact then gets copied to attachments by the existing pipeline |
| Under a writable mount, host file **missing** | **Rejected**: `ArtifactPreservationError`, task stays in flight, scratch kept, same tool error as today |
| Outside every writable mount (host path, name, `/tmp/x` in the container) | Unchanged: upstream's "referenced by name only" tolerance |

That's the hook's rule set, so the hook retires (below).

The translation needs one new function in `tools/container_paths.py`:
`to_host_path(declared) -> str | None`, the file-or-dir sibling of `to_host_dir`. It
maps textually through `container_mount_map()` (writable only, longest prefix first),
after `posixpath.normpath`. It then refuses any result whose resolved path leaves the
mount's host root, so `..` and symlink escapes don't turn into host reads. The
existence check stays in the caller, so "missing" can be a rejection and not a silent
`None`.

### Cleanup changes

`_managed_scratch_path_info` (`hermes_cli/kanban_db_workspace.py:72`) gates both the
artifact copy and the completion `rmtree`, so it's the security-sensitive edit. A
relocated scratch dir counts as managed only if all three hold:

- it is exactly `<writable /workspace host root>/kanban/<task-id>`, checked both
  resolved and lexically, like the existing roots;
- its task-id component equals the task's own id;
- it equals the DB `workspace_path`.

A card whose `workspace_path` was hand-set to anything else under the sandbox is never
removed. `/workspace` and `/workspace/kanban` themselves are never managed.

### `KANBAN_GUIDANCE` changes: none

With the container spelling in `$HERMES_KANBAN_WORKSPACE`, the existing text
(`agent/prompt_builder.py:269-330`) is accurate as written: "`cd $HERMES_KANBAN_WORKSPACE`
first" works, and "Files must exist at completion" is now enforced for container paths
too. Leaving it byte-identical also avoids churning a system-prompt constant.

### How `docker_volumes` is read

Both spellings already parse:

- a YAML list is rendered with `json.dumps` by the env bridge (`gateway/run.py:2029`)
  and by `_config_terminal_value`;
- a JSON-encoded string passes through unchanged.

`_volume_specs` then calls `json.loads`, and both give a list. A test pins both forms.
A plain non-JSON string, such as a single bare `a:b`, still yields no mounts. That's
today's behaviour, and the fleet no longer uses it.

## What it retires on the fleet

| Piece | Fate | Why |
| --- | --- | --- |
| `scripts/hermes-hook-kanban-artifact-host-path.py` (`pre_tool_call`, fail-closed) | Retire after proof cards pass without it | Its reverse mapping and its reject-if-missing rule move into `kanban_complete` itself |
| `kanban-worker-contract` plugin | Retire, or keep only non-workspace prose | Its job is explaining a failed `cd` and pointing at `/workspace/kanban/$HERMES_KANBAN_TASK/`; the env var now **is** that path |
| `container-path-translation.patch` | Stays, gains `to_host_path` | It's the dependency; it shrinks when its upstream PR lands |
| `logs/kanban-artifact-hook.jsonl` | Stops growing | The tool error and the task's event log carry rejections |

Retire the hook and plugin one at a time, each after a proof card, never in the same
step as deploying the fix.

## Non-goals

- Upstream's default Docker layout, where no `docker_volumes` entry claims `/workspace`
  and the persistent sandbox is bound implicitly. Covering it means teaching
  `container_paths` the synthetic mounts that
  `gateway/platforms/base.py:1011-1040` computes for media delivery. That's worth doing
  before an upstream PR, but it isn't needed on the fleet, where every profile mounts
  `/workspace` explicitly.
- Other container backends (Modal, Daytona, Singularity, SSH, Vercel sandbox).
- Any change to how containers are created, labelled or reused.

## Tests: red on base, green with the fix

Behaviour contracts with real imports against temp homes, and no Docker daemon, since
the mapping is filesystem layout.

1. **Allocation relation, A→B→A under multiplex.** Profile B has Docker and a writable
   `<tmp>/b-ws:/workspace` volume; profile A is local. Dispatch B's card, then A's,
   then B's again. For each B card, `to_host_path(env["HERMES_KANBAN_WORKSPACE"])`
   under B's scope equals the task's `workspace_path`, and that dir exists. A's env is
   identical to base.
   *Red on base:* B's env carries the host path, which B's container can't open.
2. **Completion translation**, parametrized over the three table rows above:
   - `/workspace/kanban/<id>/report.md` existing → the recorded artifact is an
     attachment path, and the file was copied. *Red on base:* skipped silently.
   - `/workspace/kanban/<id>/missing.md` → `ArtifactPreservationError`, and the task
     is still running. *Red on base:* accepted.
   - `notes.md` → recorded unchanged. Green on both, as a guard.
3. **Cleanup boundary.** A scratch card whose `workspace_path` was hand-set to
   `<b-ws>/kanban/other-dir` is never removed on completion. Green on base too (base
   never manages that root); it guards the new managed-root rule.

Plus one line in `tests/tools/test_container_paths.py` that feeds `docker_volumes` as
a YAML list and as a JSON-encoded string and gets the same mount map.

**Live proof on the fleet:** a card on `experiments` assigned to a Docker profile. The
worker's first `pwd` is `/workspace/kanban/<id>`, the report shows up as a card
attachment, and `kanban-artifact-hook.jsonl` records nothing, because the hook has been
disabled for that proof.

## Delivery

- Branch `fix/kanban-container-scratch-workspace`, stacked on the fork commit that adds
  `tools/container_paths.py` (`2b45630cd`).
- Three commits: `to_host_path` + parse test; dispatcher + cleanup; completion.
- Local patch cut against `8c9fe964` on top of `container-path-translation.patch`, with
  `check-patches.sh` green.
- Upstream: nothing is filed until Justin approves, and only after a search of open
  PRs and issues on Kanban + Docker/container scratch workspaces. If one exists, this
  becomes a comment there.

## Questions for Justin

1. **Remove undeclared files on completion?** Today the scratch dir is removed after
   declared artifacts are copied. It now lives inside the profile's `/workspace`, so a
   worker's other output (logs, intermediate data) disappears with it, as it does
   today. Keep that, or keep Docker scratch dirs like `worktree` and prune them later?
2. **Plugin retirement.** Does `kanban-worker-contract` carry anything besides the
   workspace explanation? If so, it shrinks rather than retires.
3. **Upstream order.** Which upstream PR carries `tools/container_paths.py`, and is it
   still open? Fix 5 can't be proposed upstream before it lands, or it has to be
   stacked on it.
