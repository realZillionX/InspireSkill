# Ray jobs (弹性计算) — Browser API surface

The Web UI's **弹性计算** menu at `/jobs/ray?spaceId=<ws>` is a hybrid
`head + worker` Ray-cluster submitter. Typical use case (per user
@精益): CPU decode + GPU inference streaming pipelines where elastic
worker scaling avoids blowing out intermediate storage. There is **no
OpenAPI equivalent** — all endpoints live under `/api/v1/ray_job/*` and
require a web-session cookie with `Referer: /jobs/ray`.

The frontend route uses the `ray-icon` sidebar icon; the backend proto
namespace is `ray_job`.

## Endpoint map (confirmed 2026-04-23)

| Endpoint | Method | Purpose | Payload |
| --- | --- | --- | --- |
| `/api/v1/ray_job/list` | POST | List jobs in a workspace (paged) | `{workspace_id, filter_by: {user_id: [...]}?, page_num, page_size}` |
| `/api/v1/ray_job/users` | POST | List users with Ray jobs (for filter dropdown) | `{workspace_id}` |
| `/api/v1/ray_job/detail` | POST | Full job detail (head + worker specs, instance ranges, status) | `{ray_job_id}` |
| `/api/v1/ray_job/stop` | POST | Stop a running job | `{ray_job_id}` |
| `/api/v1/ray_job/delete` | POST | Permanently remove a job record | `{ray_job_id}` |
| `/api/v1/ray_job/create` | POST | Submit a new job (schema **not yet wrapped** — see below) | nested, see §"Create schema (incomplete)" |

**Field naming**: the proto schema accepts `ray_job_id` as the identifier
for detail / stop / delete. Probing `id` and `job_id` both return
`proto: unknown field "..."`; `ray_job_id` is the only accepted name.

**Non-endpoints** (404 during probing): `events`, `logs`, `status`,
`configs/workspace/<ws>`, `resource_specs/<ws>`, `specs`. The detail
payload carries status + timestamps inline, so a separate status endpoint
isn't needed.

## Create schema (incomplete)

The modal at `/jobs/ray?spaceId=...` → 新建 renders this field set:

- 任务名称 (`name`)
- 所属项目 (`project_id`)
- 执行命令 + 内置环境变量 (`command`, `env`)
- **Head**: 任务镜像 · 计算类型组 · 计算资源规格 · 共享内存（GB）
- **Worker group(s)**: 名称 · 任务镜像 · 最小节点数 · 最大节点数 · 计算类型组 · 计算资源规格 · 共享内存（GB）
  - The min/max instance span is the "弹性" dimension
- 任务优先级 (`priority_level`)

The top-level create body rejects a flat `image` field
(`proto: (line 1:...): unknown field "image"`), so `image` must be nested
under `head` / `worker_spec`. Likely proto shape (patterned on
`train_job`/`hpc_job`, not yet confirmed end-to-end):

```jsonc
{
  "name": "...",
  "project_id": "project-...",
  "workspace_id": "ws-...",
  "command": "srun ...",
  "priority_level": "NORMAL",      // or LOW / HIGH?
  "head": {
    "image": "...",                // nested, key name TBD
    "image_type": "SOURCE_PRIVATE",
    "logic_compute_group_id": "lcg-...",
    "spec_id": "...",              // predef_quota_id
    "shm_size_gib": 8
  },
  "workers": [
    {
      "name": "cpu-decode",
      "image": "...",
      "logic_compute_group_id": "lcg-...",
      "spec_id": "...",
      "shm_size_gib": 8,
      "min_instances": 1,
      "max_instances": 8
    }
  ]
}
```

To finalise this, capture the submit POST body by filling + submitting
the form with Playwright network recording on (see
`/tmp/capture_ray_create_form.py` in the investigation artefacts). Don't
guess fields blindly — the proto parser fails fast on unknown fields,
so a single successful submit from the UI gives you the authoritative
shape.

## Related dropdown endpoints (for future `ray create`)

The create modal pre-fetches:

- `POST /api/v1/logic_compute_groups/list` — compute-group picker
- `POST /api/v1/project/list_v2` — project picker
- `POST /api/v1/image/list` — image picker
- `GET  /api/v1/image/brands` — brand filter for image picker

All four are already wrapped (see `browser_api/projects.py`,
`browser_api/images.py`, `browser_api/availability.py`), so `ray create`
won't need new dropdown wrappers — only the `ray_job/create` POST.

## What the CLI wraps today

See `inspire ray --help` (and `cli/inspire/cli/commands/ray/`):

- `inspire ray list` — with `-A` / `--created-by` filters
- `inspire ray status <ray_job_id>` — summary text + `--json` full payload
- `inspire ray stop <ray_job_id>`
- `inspire ray delete <ray_job_id> [--yes]`

Create intentionally absent — run from the Web UI for now; flip the
switch once someone captures the full submit payload.
