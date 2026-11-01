---
name: inspire
description: "Use for the Inspire compute platform through the inspire CLI: INSPIRE.md asset upkeep, workspace/project/resource/path selection, Notebook, GPU Job, HPC, Ray, Serving, TensorBoard, Image, Model Registry, observation, cleanup, and Inspire CLI Browser API maintenance. Use CLI Help for syntax and load only the focused reference."
---

# Inspire Skill

`inspire` is the local CLI for the Inspire compute platform. Always treat `inspire --help`, `inspire <group> --help`, and `inspire <group> <subcommand> --help` as the source of truth for commands, flags, defaults, and examples. This document covers platform concepts, project conventions, the core execution loop, and reference routing.

## Core Operating Model

### Accounts
`inspire account use <name>` sets the persistent default account. Passing `--account <name>` on any command overrides the account for that invocation only. Accounts are referenced by local alias. Once a command starts, its account is pinned and unaffected by concurrent changes to the default account. Switching accounts preserves each account's web session, SSH connections, and local caches.

### Four Execution Planes

Break every task down into four planes before running commands:

| Plane | What It Binds |
| --- | --- |
| Scheduling | `workspace`, `project`, `group`, `quota`, GPU/CPU/Memory/Shared Memory, and `image` — where and at what spec the workload runs. |
| Remote Files | Absolute shared-storage paths (`/inspire/...`) for code, data, weights, checkpoints, and outputs. Use shell environment variables for shorthand; the CLI does not store path aliases. Official datasets from the Dataset Hub are mounted read-only via `--dataset`. |
| Workload Type | **Notebook** for interactive debugging and environment setup; **Job** for fixed-scale background GPU workloads; **HPC** for CPU Slurm batch jobs; **Ray** for elastic workers, long-running daemons, or streaming pipelines; **Serving** for online HTTP model deployments. |
| Observation & Cleanup | **Events** for scheduling/controller state; **Logs** for program output (available on Job, HPC, Ray, and Serving; on Notebook use `exec` or `shell`); **Metrics** and **Instances** for per-unit load and placement; **Status** for platform state and assigned nodes; **`shell`** for interactive container inspection (available on Job, HPC, Ray, and Serving); **TensorBoard** (`inspire tensorboard`) for training curves. Verify outputs and clean up resources when done. |

### Scheduling & Live State
- Always pass `workspace`, `project`, `group`, `quota`, and `image` explicitly when creating workloads. There are no implicit defaults or saved workload profiles.
- Copy both `group` and `quota` from the same live quota row, and select `image` from the live image catalog. For GPU Jobs, `--shm-size` cannot exceed the instance memory in the selected quota (see [`references/compute-workloads.md`](references/compute-workloads.md)).
- Live queries are the source of truth for accounts, workspaces, projects, compute groups, quotas, images, and availability. The local cache only accelerates name and catalog resolution. Manage it with `inspire cache status|refresh|clear` (all accept `--resource <kind>`). `inspire cache refresh` requires an explicit scope (`--resource`, `--workspace`, or `--name`) and is rarely needed in normal operation.
- The CLI addresses resources by name and alias. Disambiguate duplicate names with `--workspace` or `--pick`.
- Discovery lists and batch outputs show up to 20 items by default; use `--limit/-n` or `--all` to adjust. Job logs apply line and character budgets with truncation notices. Pass root-level `inspire --json ...` when structured single-document JSON output is needed.

### Workspaces & Network Boundaries
- `CPU资源空间` (CPU workspace) and `分布式训练空间` (distributed GPU training workspace) are the default public workspaces available to all users:
  - Use `CPU资源空间` for CPU Notebooks, outbound internet downloads, dependency preparation, CPU HPC jobs, and CPU Ray jobs.
  - Use `分布式训练空间` for GPU Notebooks, GPU Jobs, multi-node training, Serving, and GPU observation.
- Other workspaces (project-dedicated workspaces, specialized hardware partitions, etc.) must be explicitly specified by the user. Do not select them automatically just because they appear in a listing.
- `分布式训练空间` typically has no outbound internet access. Download external repositories, weights, and datasets from `CPU资源空间` onto shared storage or bake dependencies into an image before running GPU workloads (see [`references/internal-sources.md`](references/internal-sources.md)).

### Restricted H100/H200 Notebooks
Notebooks on `H100` or `H200` GPUs run commands over `JupyterTerminal` rather than SSH. `JupyterTerminal` `exec` binds remote stdin to `/dev/null` and does not support local stdin forwarding: never pass `--stdin` / `--bash-stdin`, pipe local data, or redirect local files into `inspire notebook exec` on restricted notebooks (or before transport is confirmed). Stage scripts and input files on `/inspire/...` first, or use `inspire notebook shell` for interactive TTY sessions. SSH-backed notebooks support `exec --stdin` normally (see [`references/notebook.md`](references/notebook.md)).

## Persistent Asset Contract (`INSPIRE.md`)

The CLI does not use repo-local `./.inspire/` configs or bind a repository to a single project. Select the target project, workspace, paths, and image explicitly for each task.

Maintain an optional `INSPIRE.md` in a repository only when stable remote paths, base images, models, dataset revisions, or long-lived infrastructure need to be shared across sessions and collaborators. Scope each entry by Project and Workspace; never store credentials, transient status, or raw logs in `INSPIRE.md` (see [`references/assets.md`](references/assets.md)).

## Standard Execution Loop

1. If `INSPIRE.md` exists, read the entries relevant to the current Project and Workspace.
2. Load the single most relevant reference from the index below (load a second only when crossing domain boundaries).
3. Check `--help` for the exact command syntax and flags.
4. Query live state for account, workspace, project, compute group, quota, image, and resource availability.
5. Prepare shared paths, code, and environment; run `--dry-run` or a short probe for complex setups.
6. Submit the workload.
7. Observe progress via `events`, `logs`, `metrics`, `instances`, `status`, or `tensorboard`.
8. Verify application health and output integrity; record any new long-lived assets in `INSPIRE.md`.
9. Stop running workloads before deleting them (`stop` then `delete`); delete finished temporary workloads unless downstream consumers still need them.

## Reference Index

Always use CLI `--help` for command syntax. Load only the reference needed for the current task:

### Platform Operations

| Task or Decision | Reference |
| --- | --- |
| Account initialization, updates, uninstall, or local proxy checks | `inspire init --help`, `inspire account --help`, `inspire update --help`, `inspire uninstall --help` |
| `INSPIRE.md` conventions, project metadata (`inspire project`), and asset lifecycle | [`references/assets.md`](references/assets.md) |
| Workspaces, compute groups, quotas, live availability, usage attribution, and priority rules | [`references/resources.md`](references/resources.md) |
| Cross-session task ledger, zone-selection discipline, and shared-quota etiquette for repeated agent operation | [`references/agent-ops.md`](references/agent-ops.md) |
| Shared storage scopes, storage tiers, mount isolation, and remote paths | [`references/paths.md`](references/paths.md) |
| Dataset Hub search, official dataset mounting (`--dataset`), versions, and access requests | [`references/dataset.md`](references/dataset.md) |
| Outbound internet staging, internal package mirrors, dependency setup, and image baking | [`references/internal-sources.md`](references/internal-sources.md) |
| Notebook creation, account-scoped connections, `exec` / `shell` / `scp`, HTTP proxy URLs, and image saving | [`references/notebook.md`](references/notebook.md) |
| GPU Job, CPU HPC, Ray, Serving, API keys (`INF_API_KEY`), TensorBoard, batch submission, and observation | [`references/compute-workloads.md`](references/compute-workloads.md) |
| Multi-stage project workflows across CPU staging, data processing, GPU training, and deployment | [`references/workflows.md`](references/workflows.md) |
| Image selection, external registration, visibility levels, and cleanup | [`references/image.md`](references/image.md) |
| Model Registry, model versions, deployment sizing (`deploy-config`), and Serving boundaries | [`references/model.md`](references/model.md) |
| Python SDK (`InspireClient`, `InspireAsyncClient`, `Accounts`, typed models, remote exec, file transfer) | [`references/sdk.md`](references/sdk.md) |

### CLI Maintenance

Load only when maintaining the CLI's Browser API wrappers, auditing frontend request contracts, or inspecting raw protocol behavior:

| Topic | Reference |
| --- | --- |
| `/api/v2` request/response envelopes, session & login guard, pagination, workspace scoping, probing methodology, per-route Action tables, creation payload contracts, and Dataset Hub (`plaza`) protocol | [`references/dev/browser-api.md`](references/dev/browser-api.md) |
