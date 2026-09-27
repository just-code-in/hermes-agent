# Kanban scratch workspaces for Docker workers — design note (Option B)

Status: **proposal, no code yet.** Written against upstream `main` at `6e69a8933`
(2026-09-28). Line numbers refer to that commit.

## The bug

A Kanban worker whose profile runs its terminal in Docker never sees its scratch
workspace, and its deliverables are silently dropped from the board.

1. The dispatcher creates the scratch dir on the host, at
   `<kanban home>/boards/<board>/workspaces/<task-id>`
   (`hermes_cli/kanban_db_workspace.py:583` `resolve_workspace`). It then pins
   `HERMES_KANBAN_WORKSPACE` and `TERMINAL_CWD` to that host path
   (`hermes_cli/kanban_db_dispatch.py:2847`, `:2863`).
2. The worker's terminal and file tools run inside the profile's container. That host
   path is not mounted there, and `_resolve_config_cwd`
   (`tools/terminal_tool.py:652`) discards it as an unusable container cwd. The work
   lands wherever the backend's default cwd points, which is `/workspace` on the
   reporting fleet.
3. `kanban_complete` records the artifact as a container path (`/workspace/x.md`).
   `_persist_scratch_completion_artifacts` (`hermes_cli/kanban_db.py:3009`) copies only
   artifacts that resolve under the host scratch root. It skips this one without a
   copy, an attachment row or an error, and completion then removes the empty scratch
   dir. The board ends up pointing at a path nothing on the host can open.
4. `KANBAN_GUIDANCE` (`agent/prompt_builder.py:269-319`) tells every worker to
   `cd $HERMES_KANBAN_WORKSPACE`, which fails inside the container.

## Why not mount the scratch dir (Option A)

Containers are reused across processes. `DockerEnvironment` attaches to an existing
container by label (`hermes-task-id`, `hermes-profile`, egress fingerprint;
`tools/environments/docker.py:668-686`), and a Kanban worker attaches to its profile's
long-lived container. Bind mounts are fixed when a container is created, so a
per-task mount would mean recreating the shared container for every card. The
reporting fleet rejected that on 2026-08-05: it costs a gateway restart plus
`docker rm -f` on every container, and it adds a second output location that
competes with `/workspace`.

## Proposal

**Put the scratch dir where the container can already see it, then translate paths
at the two boundaries.**

This refines Option B as originally described ("stop creating a host scratch dir,
translate completion paths back"). The dispatcher still creates a real host dir, so
the board, the DB and cleanup keep working on host paths. It creates that dir inside
a writable mount the assignee's container already has, and gives the worker the
container spelling of it.

### 1. Allocation (dispatcher, host side)

In `_dispatch_lane_task` (`hermes_cli/kanban_db_dispatch.py:2016`) / `resolve_workspace`, for a `scratch` task whose **assignee**
profile resolves to the Docker backend with persistent containers:

- Resolve the assignee's mount table inside `_worker_profile_scope(profile_home)`
  (`hermes_cli/kanban_db_dispatch.py:2600`), so the `TERMINAL_*` values and the
  sandbox layout are the assignee's and not the dispatching gateway's.
- Find the host dir backing container `/workspace`, using the same order the media
  translator uses:
  - an explicit **writable** `docker_volumes` entry targeting `/workspace`;
  - otherwise the persistent sandbox layout
    (`<sandbox>/docker/<candidate>/workspace`).

  Only an **existing** dir counts; if the container was never created, fall through.
- Allocate `<that host dir>/kanban/<board>/<task-id>` on the host. The container sees
  the same dir as `/workspace/kanban/<board>/<task-id>`.
- `workspace_path` in the DB is the **host** path. The worker gets the **container**
  path in `HERMES_KANBAN_WORKSPACE` and `TERMINAL_CWD`. `_is_unusable_container_cwd`
  accepts it, since it's absolute and not a host prefix, so the worker's shell and
  file tools start in the right place and `cd $HERMES_KANBAN_WORKSPACE` works.
- **Fallback:** keep today's behaviour exactly when any of these holds:
  - the backend isn't Docker, or containers aren't persistent;
  - `/workspace` is `:ro` or tmpfs, or `docker_mount_cwd_to_workspace` is on (the
    host root then depends on whichever process created the container, which the
    dispatcher can't know);
  - no backing dir exists yet.

  The `<board>` segment keeps two boards' same-named tasks apart, and keeps one board's
  cleanup inside its own subtree.

### 2. Completion (worker process, host side)

`kanban_complete` and `kanban_request_review` run in the worker process on the host,
with the assignee's `TERMINAL_*` env already bound. In
`tools/kanban_tools.py::_handle_complete` / `_handle_request_review`, before
`complete_task`:

- Translate every container-absolute artifact path to its host path through the
  shared mount table (step 3). A path under the task's workspace then resolves under
  the host `workspace_path`, and `_persist_scratch_completion_artifacts` copies it to
  attachments as designed. A path elsewhere under a mount (e.g. `/workspace/reports/x.md`)
  is recorded by its host path, which is strictly better than a container path the host
  can't open.
- An untranslatable path stays as given (today's tolerance for artifacts referenced
  only by name).
- Translation happens **before** the existing containment, size and regular-file
  checks, so a container path gets exactly the verdict its host path would. This is
  the same ordering rule the media translator follows.

### 3. One mount table (a refactor that has to come first)

Upstream already has a correct, profile-scoped container-to-host translator:
`gateway/platforms/base.py:1066` `_translate_docker_container_media_path`, with
`_parse_docker_volume_mounts` (`:941`), `_docker_persistent_sandbox_roots` (`:1011`),
`_default_docker_workspace_host_roots` (`:1025`) and `_cache_dir_container_mounts`
(`:1040`). It already handles:

- longest-prefix matching;
- the synthetic persistent `/workspace` and `/root` mounts;
- the credential-surface carve-out for `/root/.hermes/*`;
- the #109024 profile-scope bug.

`tools/` and `hermes_cli/` must not import `gateway/`, so the Kanban code can't call
that translator where it lives.

The lower-level module already exists on the fork: `tools/container_paths.py`
(commit `2b45630cd`, "feat(tools): add container_paths helper to translate sandbox
bind mounts"). It's the base commit of the filed context-cwd and skill-dir PRs
(`fix/context-cwd-container-translation`, `fix/skill-dir-container-path`). It
already reads through `tools.terminal_scope.terminal_env`, excludes read-only mounts
in the container-to-host direction (`to_host_dir`, `container_mount_map`), and names
the gateway translator as a follow-up to merge in. It does **not** yet know the
synthetic persistent-sandbox mounts (`<sandbox>/docker/<candidate>/workspace` →
`/workspace`, `.../home` → `/root`), which is the case a default-configured Docker
profile hits.

So step 3 is:
1. Build on `tools/container_paths.py`, once it has landed or as a stacked commit.
2. Move the persistent-sandbox and cache-dir mount helpers out of
   `gateway/platforms/base.py` into it, keeping the `/root/.hermes/*` carve-out.
3. Point `_translate_docker_container_media_path` at it, so there's one mount table
   and not three.

That commit has a test-seam risk. Media-translation tests may monkeypatch
`gateway.platforms.base._tenv` and friends. Following "patch where production reads",
the extraction re-points only the tests whose seam actually moved, and nothing else.

`TERMINAL_DOCKER_VOLUMES` may reach the process as a list rendered to JSON, or as a
JSON-encoded string that was passed straight through. `_parse_docker_volume_mounts`
already calls `json.loads` on the raw env value, so both spellings parse; a test should
pin that.

### 4. Cleanup

`_managed_scratch_path_info` (`hermes_cli/kanban_db_workspace.py:72`) gates **both**
the artifact copy and the completion `rmtree`. It must learn the new root, but this is
the security-sensitive part of the change:

- The new managed root is exactly `<backing /workspace dir>/kanban/<board>/`,
  recognised both resolved and lexically, like the existing roots. It never covers
  `/workspace` itself or anything above `kanban/`.
- A path is managed only if the dispatcher allocated it: the DB's `workspace_path`
  equals the path, and its last component is the task id. A card whose
  `workspace_path` was set by hand to something under the sandbox must not become
  `rmtree`-able.

### 5. Guidance text

`KANBAN_GUIDANCE` doesn't change. `cd $HERMES_KANBAN_WORKSPACE` is simply true now.
The reporting fleet's `kanban-worker-contract` plugin can drop its "what a failed
`cd` means" paragraph.

## Non-goals

- Other container backends (Modal, Daytona, Singularity, SSH, Vercel sandbox) keep
  today's behaviour. Their mount models differ, and none has a reported incident.
- `worktree` and `dir` workspaces are untouched: they're user-owned paths, never
  relocated and never removed.
- No change to how containers are created, labelled or reused.

## Tests (behaviour contracts, each shown red on base)

1. **Allocation relation.** For a Docker-persistent assignee (temp `HERMES_HOME`,
   sandbox `workspace` dir present), the worker env's `HERMES_KANBAN_WORKSPACE`,
   translated container-to-host through the assignee's mount table, equals the task's
   `workspace_path`, and that dir exists on the host. Run it A→B→A across two profile
   homes under multiplex, so the dispatcher's own profile never leaks into the
   assignee's table.
2. **Preservation.** A worker completes with `artifacts=["/workspace/kanban/<board>/<id>/report.md"]`.
   The file is copied into the task's attachments, and the recorded artifact is the
   attachment path. On base it's skipped silently.
3. **Fallback.** With a non-Docker backend, or a `:ro` `/workspace`, allocation and env
   are byte-identical to base. This guards the "no behaviour change elsewhere" promise.
4. **Cleanup boundary.** A scratch card whose `workspace_path` was set by hand under the
   sandbox `workspace` dir is never removed on completion.

No real Docker daemon is needed. The mapping is pure filesystem layout with real
imports. The live proof is a card on the reporting fleet's `experiments` board with a
Docker assignee.

## Rollout on the reporting fleet (Mac)

1. Land the extraction (step 3) and the fix as separate commits on
   `fix/kanban-container-scratch-workspace`; cut local patches against the live base.
2. Retire `scripts/hermes-hook-kanban-artifact-host-path.py` (the `pre_tool_call`
   hook) once proof cards complete without it, and shrink the `kanban-worker-contract`
   plugin text.
3. Cut the local patch on top of `container-path-translation.patch`, which carries
   `tools/container_paths.py`, so the two don't edit the same lines.

## Open questions (for Justin and the Mac session)

1. **Order against the open container-path PRs.** PR #124757
   (`fix/media-bare-path-container-translation`) extends the gateway's private
   translator. The context-cwd and skill-dir PRs ship `tools/container_paths.py`. What
   are those two PRs' upstream numbers and states? Step 3 should stack on whichever
   lands `tools/container_paths.py`, and should wait for #124757 so the two don't
   edit `gateway/platforms/base.py` against each other.
2. **Existing upstream work.** Per the fleet rule, search upstream's open PRs and
   issues for Kanban + Docker/container scratch workspaces before anything is filed.
   If one exists, this note becomes a comment there, not a PR.
3. **Observed worker cwd.** The code says a discarded host `TERMINAL_CWD` falls back to
   the Docker default (`/root`, `tools/terminal_tool.py:649`), yet the fleet reports
   files landing in `/workspace`. Does the worker process re-bridge `terminal.cwd` from
   `config.yaml`? Check one live worker's first `pwd` and say which it is. It doesn't
   change the design, but it does change the "before" in the test.
4. **Path convention.** The plugin and hook use `/workspace/kanban/<task-id>`. This
   note proposes `/workspace/kanban/<board>/<task-id>`. Is the extra segment OK for the
   fleet's tooling?
5. **What survives completion.** Today the scratch dir is removed after declared
   artifacts are copied. With the dir inside `/workspace`, is removing undeclared files
   still what you want, or should Docker scratch dirs be kept (like `worktree`) and
   pruned later?
