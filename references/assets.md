# Persistent Assets (`INSPIRE.md`)

Use this guide when maintaining `INSPIRE.md` to share stable remote assets across sessions, agents, and collaborators. For live project owners, budgets, and priorities, run `inspire project`; for shared storage layout and mount isolation, see [`paths.md`](paths.md).

## No Repository Binding

The CLI stores account configuration under `~/.inspire/`. Current operational workflows do not read or write repo-local `./.inspire/` configuration, although `inspire update` may detect and clean up retired repo-local directories. A single repository can run across multiple Projects, Workspaces, and storage pools: every workload command takes explicit `workspace`, `project`, `group`, `quota`, and `image` flags, and every file operation uses explicit remote paths.

`INSPIRE.md` is an optional, human-readable asset inventory — the CLI does not parse it. Only create `INSPIRE.md` when a repository actually maintains long-lived remote paths, base images, models, or datasets on the platform.

## What to Record

| Category | Details |
| --- | --- |
| Scope | Target Project, Workspace, and storage pool for each asset |
| Canonical Remote Paths | Absolute `/inspire/...` paths for code checkouts, shared datasets, weights, checkpoints, and deliverables |
| Code Sync | Git remote, target branch/commit, role of each remote checkout, and sync workflow |
| Long-Lived Infrastructure | Persistent Notebooks, Serving endpoints, or build environments with active or planned consumers |
| Asset Identity | Base images, model/dataset revisions, tokenizers, manifests, build recipes, and downstream consumers |
| Reproducibility | Exact data, image, launch arguments, and validation checkpoints needed to reproduce production runs |
| Lifecycle & Retention | Active consumers, references, replacements, and whether an asset is the sole copy before deletion |

Keep an asset in `INSPIRE.md` only if it has active or planned consumers, is referenced by reproducible runs, or is the sole copy of an artifact. Do not treat every large or expensive intermediate output as permanent.

## What Not to Record

- Account configs, passwords, proxy credentials, session tokens, or API keys.
- Live quota availability, workload status, ephemeral URLs, queue positions, or benchmark scratch output.
- Raw logs, smoke-test transcripts, experiment diaries, or short-term task lists.
- Scheduling parameters that should be queried live from the CLI.

## Upkeep

Update `INSPIRE.md` immediately when saving or replacing a base image, moving a canonical path, registering a model, or retiring an asset. If `INSPIRE.md` disagrees with live CLI queries, treat live platform state as authoritative and update the file. Delete one-off staging files as soon as their job finishes.
