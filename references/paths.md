# Remote Paths & Shared Storage

Use this guide for shared storage scopes, storage pools, mount isolation, and remote path rules. For scheduling and quotas, see [`resources.md`](resources.md); for network preparation and internal mirrors, see [`internal-sources.md`](internal-sources.md).

## Paths vs. Scheduling

The CLI does not store path aliases or infer remote directories from your local Git repository. Scheduling flags (`workspace`, `project`, `group`, `quota`, `image`) control where a workload runs; `--cwd`, SCP source/destination arguments, and `/inspire/...` paths inside your commands control where files live.

Use shell environment variables when you want shorthand:

```bash
export REMOTE_ROOT=/inspire/qb-ilm2/project/<topic>/<path-user>
inspire notebook exec <notebook> --cwd "$REMOTE_ROOT/repo" "python train.py"
inspire notebook scp <notebook> ./config.yaml "$REMOTE_ROOT/repo/config.yaml"
```

`notebook exec` and `notebook shell` require an absolute remote path for `--cwd`; when omitted, the CLI does not inject `cd` and leaves the remote runtime's default working directory untouched. `notebook scp` accepts relative remote paths but warns that their resolution depends on the remote shell.

## Path Scopes

| Scope | Path Pattern | Typical Use |
| --- | --- | --- |
| Project Personal | `/inspire/<tier>/project/<topic>/<path-user>/...` | Per-project, per-user workspace for code checkouts, scripts, and debug outputs |
| Project Public | `/inspire/<tier>/project/<topic>/public/...` | Shared across project members for datasets, weights, checkpoints, and batch outputs |
| Global Personal | `/inspire/<tier>/global_user/<path-user>/...` | Cross-project personal storage for small utilities and cross-project file staging |
| Global Public | `/inspire/hdd/global_public/...` | Platform-wide shared storage (typically read-only for regular accounts) |

`<path-user>` is the directory name assigned on shared storage and may differ from your login username. Check the mounted directories inside a Notebook in the target project rather than guessing from your login name.

Official datasets mounted with `--dataset` appear at `/inspire/dataset/<dataset-name>/<version-name>`. They are read-only and do not count against project storage quotas (see [`dataset.md`](dataset.md)).

## Storage Pools

| Pool | Project Prefix | Characteristics |
| --- | --- | --- |
| SSD (`gpfs_flash`) | `/inspire/ssd/project/<topic>/` | High-IOPS hot path for active training working sets and frequent checkpoints |
| HDD (`gpfs_hdd`) | `/inspire/hdd/project/<topic>/` | General-purpose capacity; check free space before large writes |
| qb-ilm (`qb_prod_ipfs01`) | `/inspire/qb-ilm/project/<topic>/` | High-capacity pool with near-SSD sequential read throughput |
| qb-ilm2 (`qb_prod_ipfs02`) | `/inspire/qb-ilm2/project/<topic>/` | Newer high-capacity pool with ample free space; preferred for new large datasets |

`global_public` exists only on `hdd`. Use project personal or project public paths when you need `ssd`, `qb-ilm`, or `qb-ilm2` throughput.

## Mount Isolation

Project public-directory mounting is configurable when creating Notebook, training, HPC,
Ray, and Serving workloads. `public_path_mount_mode` in SDK/batch specs (or the CLI's
`--public-path-mount-mode`) selects `0` read-write, `1` read-only, or `2` not mounted.
Omission lets the platform apply its default of `0`. This setting concerns the project's
public directory; the Notebook project-user-path option remains separate.

Each workload instance only mounts the fileset of its own Project. Paths belonging to other projects (`/inspire/{hdd,ssd,qb-ilm,qb-ilm2}/project/<other-project>/`) are not mounted and will return `No such file or directory`.

To copy small files between projects, start a Notebook in each project and stage the files through `/inspire/<tier>/global_user/<path-user>/...`. For large datasets or full checkpoints that exceed personal quota, contact a project administrator.
