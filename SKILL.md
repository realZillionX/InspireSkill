---
name: inspire
description: "Use for the Inspire compute platform through the inspire CLI and Python SDK: INSPIRE.md asset upkeep, workspace/project/resource/path selection, Notebook, GPU Job, HPC, Ray, Serving, TensorBoard, Image, Model Registry, Dataset Hub, observation, cleanup, and Browser API maintenance. Use CLI Help for syntax and load only the focused reference."
---

# Inspire Skill

`inspire` is the local CLI (with a companion Python SDK) for the Inspire compute platform. Always treat `inspire --help`, `inspire <group> --help`, and `inspire <group> <subcommand> --help` as the source of truth for commands, flags, defaults, and examples. This document covers platform concepts, authentication, the core execution loop, and reference routing.

## Core Operating Model

### Accounts & Authentication

- **Account selection:** Accounts are referenced by local alias under `~/.inspire/accounts/<name>/`. `inspire account use <name>` sets the persistent default account; passing `--account <name>` on any command overrides it for that invocation only. Each command pins its account at startup and is unaffected by concurrent switches. Web sessions, CAS sessions, SSH bridges, and local caches are isolated per account.
- **OAuth device login (`inspire login`):**
  - `inspire login --account <name>` creates the account profile if missing and starts OAuth device authorization (or re-authenticates an existing account). Without `--account`, it uses the active account, or creates `default` when no accounts exist (if accounts exist with none active, pass `--account` or run `inspire account use`). `inspire account add <name>` creates a new account only and supports `--no-login`.
  - `--base-url` and `--proxy` configure new accounts (ignored with a warning on existing accounts). When no account is active, the target account becomes active unless `--no-use` is passed; `--use` forces activation.
  - The verification URL and device code print immediately to stderr (`--no-browser` disables browser launch; `--json` returns account/token metadata without secret tokens). `inspire init` starts device login if the active account is logged out; other commands never prompt interactively for login.
  - For agents: run `inspire login --account <name>` in the background, relay the printed URL and code to the user immediately, and wait for completion — never retry login in a loop.
- **CAS login for Dataset Hub (`inspire login --cas`):**
  - Catalog commands (`dataset list|show|tags|applications`) require a CAS session stored in `cas_session.json`; `dataset validate` and `--dataset` mounts use the regular OAuth Bearer session.
  - Run `inspire login --cas feishu` (scan the QR code printed to stderr; requires Chromium) or `inspire login --cas sms --phone <number>` (does not require Chromium; bare `--cas` prompts in a TTY). In non-interactive or `--json` mode, `--cas sms --phone <number>` sends the code and exits; complete within 10 minutes on the same account with `--cas sms --phone <number> --sms-code <code>` (phone numbers and codes are never saved).
  - A valid CAS session automatically approves OAuth device login and silently renews expired OAuth refresh tokens.
- **Token refresh & health checks:** Access tokens (~1 hour) and rotating refresh tokens live in `web_session.json` and refresh automatically under the account lock. Session-bound refresh tokens carry a 12-hour idle window and 7-day cap; offline refresh tokens have no fixed expiry and follow server-side idle limits. Run `inspire account check` periodically (e.g., every 6 hours in unattended runs) to keep sessions warm, or pass `--offline` to inspect local state without network calls. Usernames and passwords are never stored or read.
- **Logout (`inspire logout`):** `inspire logout [--account <name>]` (equivalent to `inspire account remove <name>`, `--yes` for non-interactive use) deletes the local account profile, tokens, and bridge/tunnel state while platform workloads keep running.

### Four Execution Planes

Break every task down into four planes before running commands:

| Plane | What It Binds |
| --- | --- |
| Scheduling | `workspace`, `project`, `group`, `quota` (GPU/CPU/Memory), Shared Memory, and `image` — where and at what spec the workload runs (TensorBoard uses a fixed 1 CPU / 2 GiB spec without `quota` or `image`). |
| Remote Files | Absolute shared-storage paths (`/inspire/...`) for code, data, weights, checkpoints, and outputs. Use shell environment variables for shorthand; the CLI stores no path aliases. Official datasets from the Dataset Hub mount read-only via `--dataset`. |
| Workload Type | **Notebook** for interactive debugging, internet staging, and image building; **Job** for fixed-scale background GPU workloads; **HPC** for CPU Slurm batch jobs; **Ray** for elastic workers, long-lived actors, or streaming pipelines; **Serving** for online HTTP model deployments. |
| Observation & Cleanup | **Events** for scheduling and pod startup; **Logs** for program output (on Job, HPC, Ray, and Serving; on Notebook use `exec` or `shell`); **Metrics** and **Instances** for per-unit load and node placement; **Status** for platform state and assigned nodes; **`shell`** for interactive container inspection; **TensorBoard** (`inspire tensorboard`) for training curves. Verify outputs and clean up resources when done. |

### Scheduling & Live State

- Pass `workspace`, `project`, `group`, `quota`, and `image` explicitly when creating compute workloads (`workspace`, `project`, `group`, and `summary-path` for TensorBoard). There are no implicit defaults or saved workload profiles.
- Copy both `group` and `quota` from the same live `<workload> quota` row, and select a `READY` `image` from the target Workspace's image catalog. For GPU Jobs, `--shm-size` cannot exceed the instance memory in `--quota` (see [`references/compute-workloads.md`](references/compute-workloads.md)).
- Live queries are the source of truth for accounts, workspaces, projects, compute groups, quotas, images, and availability. The local SQLite cache only accelerates name and catalog resolution. Manage it with `inspire cache status|refresh|clear` (`--resource <kind>`). `inspire cache refresh` requires an explicit scope (`--resource`, `--workspace`, or `--name`) and is rarely needed in normal operation.
- Address resources by name and local account alias. Disambiguate duplicate names with `--workspace` or `--pick`.
- Discovery lists and batch outputs show up to 20 items by default (`--limit/-n` or `--all` adjusts the bound). Job logs apply line and character budgets with truncation notices. Pass root-level `inspire --json ...` for structured single-document JSON output.

### Workspaces & Network Boundaries

- `CPU资源空间` (CPU workspace) and `分布式训练空间` (distributed GPU training workspace) are the two default public Workspaces available to all users:
  - Use `CPU资源空间` for CPU Notebooks, outbound internet downloads, dependency preparation, CPU HPC jobs, and CPU Ray jobs.
  - Use `分布式训练空间` for GPU Notebooks, GPU Jobs, multi-node training, Serving deployments, and GPU observation.
- Select other Workspaces (project-dedicated workspaces, specialized hardware partitions, etc.) only when explicitly requested by the user — never pick them automatically just because they appear in a listing.
- `分布式训练空间` typically has no outbound internet access. Stage external repositories, weights, and datasets from `CPU资源空间` onto shared storage or bake dependencies into an image before launching GPU workloads (see [`references/internal-sources.md`](references/internal-sources.md)).

### Restricted H100/H200 Notebooks

Notebooks on `H100` or `H200` GPUs execute over `JupyterTerminal` rather than SSH. `JupyterTerminal` `exec` binds remote stdin to `/dev/null` and rejects local stdin forwarding: never pass `--stdin` or `--bash-stdin`, pipe local data, or redirect local files into `inspire notebook exec` on restricted Notebooks (or before transport is known). Stage scripts and input files on `/inspire/...` first, or use `inspire notebook shell` for interactive TTY sessions. SSH-backed Notebooks support `exec --stdin` and `scp` normally (see [`references/notebook.md`](references/notebook.md)).

## Persistent Asset Contract (`INSPIRE.md`)

The CLI neither reads nor writes repository-local configuration files and never binds a repository to a single project. Select the target Project, Workspace, remote paths, and image explicitly for each task.

Maintain an optional `INSPIRE.md` in a downstream repository only when stable remote paths, base images, registered models, dataset versions, or long-lived infrastructure need to be shared across sessions and collaborators. Scope each entry by Project and Workspace, and never store credentials, transient status, or raw logs in `INSPIRE.md` (see [`references/assets.md`](references/assets.md)).

## Standard Execution Loop

1. If `INSPIRE.md` exists, read the entries matching the target Project and Workspace.
2. Load the single most relevant reference from the index below (load a second only when crossing domain boundaries).
3. Check `--help` for exact command syntax and flags.
4. Query live state for account, workspace, project, compute group, quota, image, and availability.
5. Prepare shared paths, code, and environment; run `--dry-run` or a small probe before scaling out.
6. Submit the workload.
7. Observe progress via `events`, `logs`, `metrics`, `instances`, `status`, or `tensorboard`.
8. Verify application health and output integrity; record any new durable assets in `INSPIRE.md`.
9. Stop running workloads before deleting them (`stop` then `delete`); clean up finished temporary workloads unless downstream consumers still need them.

## Reference Index

Always use CLI `--help` for command syntax. Load only the reference needed for the current task:

### Platform Operations

| Task or Decision | Reference |
| --- | --- |
| Login, logout, account initialization, proxy checks, updates, or uninstall | `inspire login --help`, `inspire logout --help`, `inspire init --help`, `inspire account --help`, `inspire update --help`, `inspire uninstall --help` |
| `INSPIRE.md` conventions, project metadata (`inspire project`), and asset lifecycle | [`references/assets.md`](references/assets.md) |
| Workspaces, compute groups, quotas, live availability, usage attribution, and priority rules | [`references/resources.md`](references/resources.md) |
| Task handoffs, Compute Group selection, and shared quotas across concurrent sessions | [`references/agent-ops.md`](references/agent-ops.md) |
| Shared storage scopes, storage pools, mount isolation, and remote paths | [`references/paths.md`](references/paths.md) |
| Dataset Hub search, official dataset mounting (`--dataset`), versions, and access requests | [`references/dataset.md`](references/dataset.md) |
| Outbound internet staging, internal package mirrors, runtime dependency setup, and image baking | [`references/internal-sources.md`](references/internal-sources.md) |
| Notebook creation, account-scoped connections, `exec` / `shell` / `scp`, HTTP proxy URLs, and image saving | [`references/notebook.md`](references/notebook.md) |
| GPU Job, CPU HPC, Ray, Serving, API keys (`INF_API_KEY`), TensorBoard, batch submission, and observation | [`references/compute-workloads.md`](references/compute-workloads.md) |
| Multi-stage project workflows across CPU staging, data processing, GPU training, and deployment | [`references/workflows.md`](references/workflows.md) |
| Image selection, external Docker registration and sync, visibility levels, and cleanup | [`references/image.md`](references/image.md) |
| Model Registry, model versions, deployment sizing (`deploy-config`), and Serving boundaries | [`references/model.md`](references/model.md) |
| Python SDK (`InspireClient`, `InspireAsyncClient`, `Accounts`, typed models, remote exec, file transfer) | [`references/sdk.md`](references/sdk.md) |

### CLI Maintenance

Load only when maintaining the CLI's Browser API wrappers, auditing frontend request contracts, or inspecting raw protocol behavior:

| Topic | Reference |
| --- | --- |
| `/api/v2` request/response envelopes, OAuth/CAS session lifecycle, pagination, workspace scoping, probing methodology, per-route Action tables, creation payload contracts, and Dataset Hub (`plaza`) protocol | [`references/dev/browser-api.md`](references/dev/browser-api.md) |
