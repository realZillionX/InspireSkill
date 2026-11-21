# Persistent Assets (`INSPIRE.md`)

Use this guide when maintaining `INSPIRE.md` to share stable remote assets across sessions, agents, and collaborators. For live project owners, budgets, and priorities, run `inspire project`; for shared storage layout and mount isolation, see [`paths.md`](paths.md).

## No Repository Binding

The CLI stores account state under `~/.inspire/` and neither reads nor writes repository-local configuration files. A single repository can run across multiple Projects, Workspaces, and storage pools: every workload command takes explicit scheduling flags, and every file operation uses explicit `/inspire/...` remote paths.

`INSPIRE.md` is an optional, human-readable asset inventory — the CLI does not parse it. Create `INSPIRE.md` only when a repository maintains long-lived remote paths, base images, registered models, or dataset revisions on the platform.

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

Retain an entry in `INSPIRE.md` only while the asset has active or planned consumers, backs reproducible production runs, or is the sole copy of an artifact. Do not treat routine intermediate outputs as permanent assets.

## What Not to Record

- Account configs, passwords, proxy credentials, session tokens, or API keys.
- Live quota availability, workload status, ephemeral proxy URLs, queue positions, or scratch outputs.
- Raw logs, smoke-test transcripts, experiment diaries, or short-term task lists.
- Scheduling parameters that belong to live CLI queries.

## Upkeep

Update `INSPIRE.md` immediately when saving or replacing a base image, moving a canonical path, registering a model, or retiring an asset. When `INSPIRE.md` disagrees with live CLI queries, treat live platform state as authoritative and update the file. Delete one-off staging files as soon as their consumer finishes.
