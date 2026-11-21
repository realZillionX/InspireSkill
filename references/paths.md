# Remote Paths & Shared Storage

Use this guide for shared storage scopes, storage pools, mount isolation, and remote path rules. For scheduling and quotas, see [`resources.md`](resources.md); for network preparation and internal mirrors, see [`internal-sources.md`](internal-sources.md).

## Paths vs. Scheduling

The CLI stores no path aliases and never infers remote directories from your local Git repository. Scheduling flags (`workspace`, `project`, `group`, `quota`, `image`) control where a workload runs; `--cwd`, SCP source/destination arguments, and `/inspire/...` paths inside your commands control where files live.

Use shell environment variables for shorthand:

```bash
export REMOTE_ROOT=/inspire/qb-ilm2/project/<topic>/<path-user>
inspire notebook exec <notebook> --cwd "$REMOTE_ROOT/repo" "python train.py"
inspire notebook scp <notebook> ./config.yaml "$REMOTE_ROOT/repo/config.yaml"
```

`notebook exec` and `notebook shell` require an absolute remote path for `--cwd`; when omitted, the CLI does not inject `cd` and preserves the remote transport's default working directory. `notebook scp` accepts relative remote paths (resolved by the remote shell) with a warning; prefer absolute `/inspire/...` paths.

## Path Scopes

| Scope | Path Pattern | Typical Use |
| --- | --- | --- |
| Project Personal | `/inspire/<tier>/project/<topic>/<path-user>/...` | Per-project, per-user workspace for code checkouts, scripts, and debug outputs |
| Project Public | `/inspire/<tier>/project/<topic>/public/...` | Shared across project members for datasets, weights, checkpoints, and batch outputs |
| Global Personal | `/inspire/<tier>/global_user/<path-user>/...` | Cross-project personal storage for small utilities and cross-project file staging |
| Global Public | `/inspire/hdd/global_public/...` | Platform-wide shared storage (typically read-only for regular accounts) |

`<path-user>` is the directory name assigned on shared storage and may differ from your login username. Inspect the mounted directories inside a Notebook in the target Project rather than guessing from your login name.

Official datasets mounted with `--dataset` appear read-only at `/inspire/dataset/<dataset-name>/<version-name>` and do not count against project storage quotas (see [`dataset.md`](dataset.md)).

## Storage Pools

| Pool | Project Prefix | Characteristics |
| --- | --- | --- |
| SSD (`gpfs_flash`) | `/inspire/ssd/project/<topic>/` | High-IOPS hot path for active training working sets and frequent checkpoints |
| HDD (`gpfs_hdd`) | `/inspire/hdd/project/<topic>/` | General-purpose capacity; check free space before large writes |
| qb-ilm (`qb_prod_ipfs01`) | `/inspire/qb-ilm/project/<topic>/` | High-capacity pool with near-SSD sequential read throughput |
| qb-ilm2 (`qb_prod_ipfs02`) | `/inspire/qb-ilm2/project/<topic>/` | Newer high-capacity pool with ample free space; preferred for new large datasets |

`global_public` exists only on `hdd`. Use project personal or project public paths when you need `ssd`, `qb-ilm`, or `qb-ilm2` throughput.

## Mount Isolation & Access Modes

- **Per-project fileset isolation:** Every workload mounts only the fileset of its selected Project. Paths belonging to other projects (`/inspire/{hdd,ssd,qb-ilm,qb-ilm2}/project/<other-project>/`) are not mounted inside the container (`No such file or directory`). To copy small files across projects, start a Notebook in each project and stage through `/inspire/<tier>/global_user/<path-user>/...`; for large datasets or checkpoints exceeding personal quota, coordinate with a project administrator.
- **Project public-directory mount mode (`--public-path-mount-mode`):** Supported on Notebook, Job, HPC, Ray, and Serving creation (`public_path_mount_mode` in batch files and SDK `*CreateSpec`, using `PublicPathMountMode`):
  - `0` — read-write (`READ_WRITE`, platform default when omitted)
  - `1` — read-only (`READ_ONLY`)
  - `2` — not mounted (`NOT_MOUNTED`)
  Legacy `--public-path-readonly` / `--no-public-path-readonly` (`public_path_readonly` in batch/SDK) maps to `1` / `0` with a deprecation warning and raises a validation error if combined with a conflicting mode.
- **Project member-directory mount mode (`--project-path-readonly`):** Available on `notebook create` for Project Maintainers to downgrade per-member directories under `/inspire/<tier>/project/<topic>/` to read-only, independent of `--public-path-mount-mode`.
