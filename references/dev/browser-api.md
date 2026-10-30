# Browser API Maintainer Reference

> **Audience:** CLI and SDK maintainers. Do not load this file during normal workload operations — regular CLI usage relies solely on the name-based CLI surface and `--help`.
>
> **Scope:** Covers both the main compute console (`/api/v2` Action gateway on `{base_url}`, Sections 1–10) and the Dataset Hub (`plaza`, Section 11). Both share the same CAS single sign-on (SSO) session layer.

## 1. Sources of Truth & Invariants

| Priority | Source | What It Defines |
| --- | --- | --- |
| 1 | [`browser_api/`](../../cli/inspire/platform/web/browser_api/) | Canonical request construction, response key extraction, and normalization |
| 2 | [`session/`](../../cli/inspire/platform/web/session/), [`plaza/`](../../cli/inspire/platform/web/plaza/) | Per-account isolation, login, cookies, proxies, transport retries, and Dataset Hub client |
| 3 | `cli/tests/test_browser_api_*.py` | Unit tests locking wrapper contracts and output boundaries |
| 4 | Live console SPA network requests | Authoritative proof of whether a platform capability exists and what payload it sends |
| 5 | `GET {base_url}/discovery` | Candidate Action names and request schemas (**a hint, not a contract** — see Section 6) |
| 6 | Click `--help` | Public CLI contract |

**Core invariants:**
- All `/api/v2` JSON Action requests are constructed inside `browser_api/` (instance PTY WebSockets in `job_shell.py` and login bootstrap in `session/auth.py` are the only non-Action console flows).
- All v2 JSON responses are unpacked through `_v2_result()` in `session/envelope.py` (re-exported by `browser_api/core.py`).
- Only verified, reproducible contracts with active or explicitly evaluated consumers belong in this document.

## 2. Request & Response Contract

```http
POST {base_url}/api/v2/{route}?Action={Action}
Cookie: inspire-session=…
Content-Type: application/json
Referer: {base_url}/{console_page_path}

{ …JSON body… }
```

| Aspect | Rule |
| --- | --- |
| Method & Shape | Always `POST` with a JSON body and `?Action={Action}` in the query string (for non-Action WebSocket/proxy routes, see Section 10). |
| Authentication | Requires the `inspire-session` cookie (`x-inspire-client-source` is not required on read paths). |
| `Referer` | Must match the origin and corresponding console page path; constructed per wrapper. |
| Field Casing | Protobuf JSON unmarshaling is case- and underscore-insensitive (`ImageId` / `image_id` / `Image_Id` map to the same field), **except** when the underlying field name itself differs (e.g., `image.UpdateImage` requires `id`, whereas `image_id` is silently ignored). |
| Account Isolation | Every request uses the SSO session and proxy configuration of the selected Account Alias; cross-account commands never fall back to the active default account. |

### Response Envelope

Both successes and business errors return **HTTP 200** with an AWS-style JSON envelope:

```jsonc
{"ResponseMetadata": {…}, "Result": {…}}                                  // Success
{"ResponseMetadata": {"Error": {"Code": "…", "Message": "…"}}}            // Business error
```

- **Unpacking order:** Check `429` → `401/302` → non-JSON content-type → `ResponseMetadata.Error` → `Result` (defaulting missing `Result` to `{}`). Always check `429` before sniffing `Content-Type` because gateway rate-limiting returns an HTML error page.
- **List keys vary per Action:** There is no universal list key (`jobs`, `items`, `list`, `logs`, `events`, `scale_history_items`, `logic_compute_groups`, `node_dimensions`, etc.); declare the expected key explicitly in each wrapper.

| Error `Code` | Meaning | Handling |
| --- | --- | --- |
| `InvalidAction` | Action does not exist on this route | Route itself is alive (a missing route returns HTTP `404 page not found`) |
| `InvalidParameter` | Schema violation (`proto: unknown field "X"`, missing required field, `page or page_size too large`) | Fix request payload |
| `AccessForbidden` |Genuine permission denial **or** missing workspace scoping | Verify workspace scoping placement (Section 5) before concluding lack of permission |
| `ResourceNotFound` | Target object does not exist | Standard on `notebook` / `hpc` / `serving`; note that `train.DeleteJob` returns `AccessForbidden` instead |
| `Conflict` | Invalid state transition (e.g., deleting a running workload or starting an already running one) | `stop` and poll until stopped before deleting |
| `InternalError` / `Throttling` / `ServiceUnavailable` / `SlowDown` / `RequestTimeout` / `TooManyRequests` | Transient server or gateway error | Raised as `TransientAPIError` and retried by the transport layer |

> [!WARNING]
> `InternalError` is ambiguous: it can indicate a genuine server failure, a handler crash after accepting a valid field, or a deterministic validation failure (e.g., `hpc.GetJobLog` time window > 30 days, or `hpc.CreateJobConsole` missing `priority`). Validate known client-side constraints before sending requests so deterministic errors do not burn transient retries.

### Transport Layer & Empty Results

`request_json()` in [`session/__init__.py`](../../cli/inspire/platform/web/session/__init__.py) is the single entrypoint for both `requests` and Playwright channels:
- **Retries:** HTTP `408/425/429/500/502/503/504` and transient envelope codes raise `TransientAPIError` (`ValueError` subclass) and retry up to 3 times (honoring `Retry-After` up to 8s, otherwise exponential backoff with jitter). Keep rate-limit retries in the transport layer, never in individual wrappers.
- **`page_size` clamping:** `_clamped_page_size()` caps `page_size` at `5000` (preserving `-1` for "fetch all"). Exception: `train.ListJobs` rejects `page_size >= 1000` with `InvalidParameter: page or page_size too large`, so `jobs.list_jobs` clamps at `999`.
- **Never conflate request failure with an empty result:** Only a successful response with zero items means "none exist". Wrappers must never swallow `TransientAPIError` or `SessionExpiredError` into `[]`, `None`, or `0`. Fan-out wrappers (such as per-group quota fetches) must track partial completion explicitly (`QuotaCatalog.complete`).

### Session Lifecycle & Login Guard (`session/login_guard.py`)

`get_web_session()` returns the cached disk session without pre-validating it (1-hour local TTL).
- **Renewal flow:** On `401` or `3xx` (with `allow_redirects=False`), `_request_json_once()` first attempts a passwordless SSO renewal using existing CAS/Keycloak cookies (verifying that the renewed user ID matches). Only if SSO cookies cannot renew the session does it enter a single credential login guarded by the login circuit breaker, then retries the original request once.
- **Single-submission login guard:** Because repeated failed logins trigger CAS account lockouts and CAPTCHA challenges Vary by `User-Agent`, at most **one** credential submission is permitted per session expiration:
  - Both the `requests` CAS flow and the Playwright Chromium flow inspect the login form before submitting credentials. If `requests` encounters a CAPTCHA form, it falls back to Chromium without submitting; if Chromium also requires a CAPTCHA, it aborts before typing credentials and raises `AuthenticationError`.
  - Once credentials are POSTed, **any** subsequent failure raises `AuthenticationError` and never falls back to a second submission path.
  - Consecutive credential failures record a per-account circuit-breaker file (`web_session.login-block.json`) with escalating cooldowns (`60s → 5m → 15m → 30m`, reset after 1 hour, a successful login, a newer disk session, or a password change).

## 3. Route Names

Gateway URLs use the route slug in `/api/v2/{route}`, which does **not** always match the Service `Name` in `/discovery`:

| Discovery Service `Name` | Gateway Route | Notes |
| --- | --- | --- |
| `inference-serving` | **`inference_serving`** | Hyphenated form returns 404 |
| `model-hub` | **`model-hub`** | Underscore form returns 404 |
| `train` / `hpc` / `ray` / `notebook` / `workspace` / `user` / `project` / `image` / `cluster` | Same as `Name` | — |
| *(Not in discovery)* | **`resource-price`**, **`file`**, **`dataset`** | Plural variants (`files`, `datasets`, etc.) return 404 |

## 4. Pagination & `total` Quirks

| Aspect | Platform Behavior | Wrapper Handling |
| --- | --- | --- |
| Page number key | `PageNumber`, `page_num`, and `page` are interchangeable on most Actions | **`train.ListTensorboards` only accepts PascalCase `PageNumber`** (`page` / `page_num` silently return `[]`) |
| `page_size: -1` | Supported by `workspace.ListLogicComputeGroups`, `image.ListImages`, `project.ListProjects`; returns only 10 rows on `workspace.List*Dimension`; rejected by `model-hub.ListModels` | Only use `-1` on verified Actions; paginate explicitly by `total` elsewhere |
| Omitting `page_size` | `ListLogicComputeGroups` and `hpc.ListSlurmdPodEvent` return `[]` with a non-zero `total` | Always send an explicit `page_size` |
| `total` type | Either `int` (`notebook.ListNotebooks`) or numeric `str` (`hpc.ListJobs` returns `"202"`) | Always coerce via `_coerce_total()`, never `isinstance(total, int)` |
| Unpaginated Actions | `notebook.ListRunIndex` rejects `PageNumber` | Do not send pagination keys |
| Unknown field unmarshaling | Most Actions reject unknown keys with `proto: unknown field`, but `train.CreateTensorboard` and `notebook.SaveMirror` silently drop unknown keys | Test with a known-invalid key first before relying on `unknown field` probes |

## 5. Workspace Scoping

The same `workspace_id` is placed in four different locations depending on the Action:

| Placement | Actions |
| --- | --- |
| Top-level `workspace_id` | `ListJobs` (`train`/`hpc`/`ray`), `notebook.ListNotebooks`, `inference_serving.ListServings`, `model-hub.ListModels`, `workspace.GetWorkspaceQuota`, `workspace.GetWorkspaceComputeResource`, `workspace.GetLogicComputeGroupResource`, `workspace.Get*NodeSpecs`, `dataset.ValidateDataset` |
| Nested `filter.workspace_id` | `workspace.ListNodeDimension` / `ListTaskDimension` / `ListUserDimension`, `workspace.ListLogicComputeGroups`, `project.ListProjects`, `file.*` |
| Top-level `workspace_id` + nested `filter_by` | `notebook.ListNotebooks`, `ray.ListJobs`, `inference_serving.ListServings`, `model-hub.ListModels` (filters go inside `filter_by`, while `workspace_id` stays at top level) |
| PascalCase `WorkspaceId` | `user.GetPermissions`, `user.GetRoutes`, `notebook.GetScheduleConfig` |

### `cluster.*` vs. `workspace.*`

- The 8 dimension/resource Actions shared between `cluster.*` and `workspace.*` (`ListNodeDimension`, `ListTaskDimension`, `ListProjectDimension`, `ListUserDimension`, `ListLogicComputeGroups`, `GetOverviewResourceMetric`, etc.) return `AccessForbidden` for regular users under `cluster.*` and must be called under `workspace.*` with `filter.workspace_id`.
- **Exception:** `cluster.ListNodeEvents` has no `workspace.*` counterpart and is readable by regular users (`inspire resources node-events`).

## 6. Surface Discovery & `scripts/scan_v2_surface.py`

| Source | What It Proves | Limitations |
| --- | --- | --- |
| `GET {base_url}/discovery` | Static list of public Services/Actions and request field schemas (`Version` is a content etag) | Response list keys (`Items`/`TotalCount`) are inaccurate; omits `resource-price`, `file`, `dataset`, and `*Console` write Actions; does not reflect RBAC permissions |
| Console SPA bundles (`scripts/scan_v2_surface.py`) | Lower bound of Actions and REST paths actively called by the web UI | Must recursively follow lazy-loaded JS chunks from the entrypoint and normalize template literals |
| `user.GetMyPermissions` | Returns `{Services: {svc: {Read, Write, Actions: {Action: bool}}}}` for RBAC-gated services | Does not cover `train`, `notebook`, `workspace`, `project`, `ray`, or `hpc` |

Never conclude a capability is missing based solely on `/discovery` — always check `*Console` variants and the SPA bundle first.

## 7. Probing Methodology

When verifying undocumented Actions or fields without mutating state:
1. **Route existence:** Send any Action name. HTTP `404` means the route does not exist; `InvalidAction` proves the route is live.
2. **Action existence:** Send `{}`. `InvalidAction: unknown action: X` means the Action does not exist; any validation error proves the Action exists.
3. **Field existence:** Send `{"candidate_field": "x"}`. `InvalidParameter: proto: unknown field` means the field is not in the schema.
   - **Caveat 1 (`InternalError`):** Always compare against a control request with a known-invalid field (`{"nonexistent_xyz": 1}`) to verify the handler performs strict protobuf unmarshaling.
   - **Caveat 2 (Fake resource IDs mask unknown fields):** Gateway auth middleware checks resource IDs (`notebook_id`, `ray_job_id`, etc.) *before* strict protobuf decoding. Sending a fake ID returns `ResourceNotFound` even when accompanied by unknown fields. Probe either without a resource ID or with a real resource ID owned by the test account.
4. **Write semantics & round-trip:** Only trust a write field if the resulting resource state actually changes and `Get*` (or container inspection) reflects the written value.

## 8. Action Reference by Route

Every entry below is called via `POST {base_url}/api/v2/{route}?Action={Action}` and unpacked from `Result`. `†` indicates an Action absent from `/discovery`; `‡` indicates an entire route absent from `/discovery`.

| Route | Domain | Action Count | Primary CLI Groups |
| --- | --- | --- | --- |
| [`train`](#81-train--distributed-training--tensorboard) | GPU Jobs & TensorBoard | 16 | `job`, `tensorboard` |
| [`hpc`](#82-hpc--cpu-slurm-batch) | CPU Slurm Batch | 11 | `hpc` |
| [`ray`](#83-ray--elastic-compute) | Ray Clusters | 12 | `ray` |
| [`notebook`](#84-notebook--interactive-modeling) | Notebooks & Image Snapshots | 18 | `notebook`, `image` |
| [`inference_serving`](#85-inference_serving--model-serving) | Online Model Serving | 19 | `serving` |
| [`workspace`](#86-workspace--cluster--workspace-resources) | Compute Groups, Nodes, Quotas, Usage | 9 (+1 `cluster`) | `resources`, `<workload> quota`, `create` |
| [`user`](#87-user--account--api-keys) | Identity, Permissions & API Keys | 7 | `account`, owner-filtered lists |
| [`project`](#88-project--projects) | Projects & Budgets | 4 | `project`, `create` |
| [`image`](#89-image--image-catalog) | Image Registry | 5 | `image` |
| [`model-hub`](#810-model-hub--model-registry) | Model Registry | 14 | `model`, `serving create` |
| [`resource-price`](#811-resource-price--quota-catalog-) ‡ | Per-Group Quota & Price Catalog | 1 | `<workload> quota`, `create` |
| [`file`](#812-file--storage-discovery-) ‡ | Storage Pools & Directories | 2 | *(No active CLI consumer)* |
| [`dataset`](#813-dataset--dataset-mount-validation-) ‡ | Official Dataset Validation | 1 | `dataset validate`, `--dataset` |

---

### 8.1 `train` — Distributed Training & TensorBoard

Referer: `/jobs/distributedTraining` (detail: `/jobs/distributedTrainingDetail/{job_id}`).

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `CreateJobConsole` † | See [Section 9](#9-creation-payload-contracts) | `{job_id, sub_code, sub_msg}` | `job create`, `job batch` |
| `GetTrainScheduleConfig` | `{workspace_id}` | `train_enable_specified_nodes` | `job create --specified-node`, `job batch` |
| `GetJob` | `{job_id}` | `name`, `status`, `command`, `framework_config[]`, `dataset_info[]`, `node_infos[]`, `specified_nodes[]`, `exclude_nodes[]`, `project_id`, `workspace_id`, `logic_compute_group_name`, `created_at`, `finished_at` | `job status` / `command` / `logs` / `metrics` / `wait` |
| `ListJobs` | `{workspace_id, page_num, page_size, created_by, status?, keyword?}` or batch `{workspace_id, job_ids[]}` ([Section 8.15](#815-batch-reads--listjobs--listjobevents)) | `{jobs[], total}` (`total` is `int`) | `job list`, `job status`, Name Resolver, `cache refresh` |
| `ListJobInstances` | `{job_id, page_num, page_size}` | `{items[], total}` | `job instances` / `shell` / `logs` / `events` |
| `ListJobEvents` | `{PageNumber\|page_num, page_size, filter:{object_type, object_ids[]}}` ([Section 8.15](#815-batch-reads--listjobs--listjobevents)) | `{events[], total}` | `job events` |
| `GetJobLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[], total}` | `job logs` |
| `StopJob` | `{job_id}` | `{code, message}` | `job stop` |
| `DeleteJob` | `{job_id}` | — | `job delete` |
| `GetTaskMetric` | See [Section 8.14](#814-metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `job metrics` |
| `ListTensorboards` | `{workspace_id, created_by, PageNumber, page_size, status?, keyword?}` | `{items[], total}` | `tensorboard list`, Name Resolver |
| `GetTensorboard` | `{tb_id}` | `name`, `status`, `tb_summary_path`, `url`, `job_id`, `job_name`, `auto_stop_time_ms`, `running_time_ms`, `project_name`, `logic_compute_group_name` | `tensorboard status` / `tags` / `scalars`, wait polling |
| `CreateTensorboard` † | `{name, workspace_id, project_id, logic_compute_group_id, tb_summary_path, auto_stop_time_ms, job_id?}` | Always `{}` | `tensorboard create` |
| `StartTensorboard` † | `{tb_id}` | — | `tensorboard start` |
| `StopTensorboard` † | `{tb_id}` | — | `tensorboard stop` |
| `DeleteTensorboard` † | `{tb_id}` | — | `tensorboard delete` |

**Job notes:**
- `ListJobEvents` serves both controller-level (`filter.object_type: "job"`) and pod-level (`filter.object_type: "instance"`, `object_ids` chunked at 200 pod names) events. Do not use `ListJobInstanceEvents` (its `total` is hardcoded to `"0"`).
- `GetJobLog` requires string epoch-ms timestamps and rejects windows wider than 30 days.
- `DeleteJob` requires the job to be stopped first (`Conflict` if running) and returns `AccessForbidden` (not `ResourceNotFound`) when the job ID does not exist.
- `node_infos[]` holds assigned runtime nodes (cleared once stopped), whereas `specified_nodes[]` and `exclude_nodes[]` echo creation constraints. Per-rank node mapping comes from `ListJobInstances`.
- `job list --workspace all` fans out across visible Workspaces in 8-way concurrent round-robin pages.

**TensorBoard notes:**
- `CreateTensorboard` requires `auto_stop_time_ms` as a **string** (max 72h; missing field error misleadingly says `tensorboard_auto_stop_time_ms is required`). Its unmarshaller ignores unknown fields and returns `{}` on success, so the CLI resolves the newly created board by name via `ListTensorboards`.
- `GetTensorboard` on an unknown ID returns `InvalidParameter: 用户不存在。` (treat as not found, not an account error).
- `ListTensorboards` requires PascalCase `PageNumber`; `job_id` filter is ignored by the server and filtered client-side.
- `GetTensorboard.url` points to a live TensorBoard web app authenticated by `inspire-session`, exposing `data/runs`, `data/plugin/scalars/tags`, and `data/plugin/scalars/scalars?run=&tag=` (used by `tensorboard tags` and `scalars`).

---

### 8.2 `hpc` — CPU Slurm Batch

Referer: `/jobs/highPerformanceComputing` (detail: `/jobs/hpcDetail/{job_id}`).

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `CreateJobConsole` † | See [Section 9](#9-creation-payload-contracts) | `{job_id, sub_code, sub_msg}` | `hpc create`, `hpc batch` |
| `GetJob` | `{job_id}` | `{job_name, status, sbatch_script{}, slurm_cluster_spec{}, nodes[], steps, description, ttl_after_job_finish_seconds, dataset_info[], project_id, workspace_id, …}` | `hpc status`, `hpc metrics` |
| `ListJobs` | `{workspace_id, page_num, page_size, created_by, status?}` or batch `{workspace_id, job_ids[]}` | `{jobs[]\|items[], total}` (`total` is `str`) | `hpc list`, `hpc status`, Name Resolver |
| `ListJobEvents` | `{pageNum: -1, pageSize: 200, filter:{object_ids:[job_id], object_type:"HPC_JOB"}, sorter:[{field:"last_timestamp", sort:"ascend"}]}` | `{events[]\|items[]\|list[]}` | `hpc events` |
| `ListJobInstances` | `{jobId, page_num, page_size}` | `{items[]\|list[], total}` | `hpc instances` |
| `ListSlurmdPodEvent` | `{instance_id, page_size, PageNumber}` | `{events[], total}` (`str`) | `hpc events --instance` |
| `GetJobLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[], total}` (`int`) | `hpc logs` |
| `GetHpcScheduleConfig` | `{workspace_id}` | `{enable_auto_stop, auto_stop_ruleset, enable_max_running_time, max_running_time_days/hours/minutes, predef_node_spec}` or `null` | `resources policy` |
| `StopJob` | `{job_id}` | `{job_id, sub_code, sub_msg}` | `hpc stop` |
| `DeleteJob` | `{job_id}` | `{job_id, sub_code, sub_msg}` | `hpc delete` |
| `GetTaskMetric` | See [Section 8.14](#814-metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `hpc metrics` |

**HPC notes:**
- Casing inconsistencies: `ListJobInstances` requires camelCase `jobId`; `ListJobEvents` requires `pageNum`/`pageSize`; list rows populate `job_name` (never `name`).
- `GetJob.steps` (`completed/total`, e.g., `1/1` vs `0/0`) is statically parsed from `srun` invocations in `entrypoint` and is the only field that distinguishes a job that ran `srun` steps from one that did not.
- `GetJobLog` and `ListSlurmdPodEvent` require namespaced `<ns>/<pod>` identifiers. `GetJobLog` rejects windows > 30 days (`HPC_LOG_MAX_WINDOW_MS`), ignores `PageNumber`, and returns the **oldest** `page_size` lines (so tailing requires fetching the window and slicing client-side). `ListSlurmdPodEvent` emits one duplicate row per occurrence without a `count` field (deduplicated client-side).
- Jobs in `SUCCEEDED_RETAINING` or `FAILED_RETAINING` reject `DeleteJob` with `Conflict`, and `StopJob` is a no-op on retaining jobs — wait for the platform to transition them to `SUCCEEDED` / `FAILED`.

---

### 8.3 `ray` — Elastic Compute

Referer: `/jobs/ray`.

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `CreateJob` | See [Section 9](#9-creation-payload-contracts) | `{ray_job_id, sub_code, sub_msg}` | `ray create`, `ray batch` |
| `GetJob` | `{ray_job_id}` | `status`, `head_node{}`, `worker_groups[]`, `entrypoint`, `creator{}`, `priority_name` | `ray status`, `ray metrics` |
| `ListJobs` | `{workspace_id, page_num, page_size, filter_by:{user_id:[…]}}` | `{items[], total}` (`str`) | `ray list`, Name Resolver |
| `ListJobCreators` | `{workspace_id}` | `{items[]}` | — |
| `ListJobEvents` | `{ray_job_id, page_num, page_size, sorter:[{field:"last_timestamp", sort}], filter:{object_ids[]}?}` | `{items[], total}` | `ray events`, `--instance` |
| `ListJobInstances` | `{ray_job_id, page_num, page_size}` | `{items[], total}` | `ray instances` |
| `ListJobScalingHistories` | `{ray_job_id, page_num, page_size, worker_group_name?}` | `{items[], total}` (`str`) | `ray scaling` |
| `GetJobLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[], total}` (`int`) | `ray logs` |
| `StartJob` | `{ray_job_id}` | `{ray_job{}}` | `ray start` |
| `StopJob` | `{ray_job_id}` | `{ray_job{}}` | `ray stop` |
| `DeleteJob` | `{ray_job_id}` | `{ray_job{}}` | `ray delete` |
| `GetTaskMetric` | See [Section 8.14](#814-metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `ray metrics` |

**Ray notes:**
- Every Action uses `ray_job_id` except `GetJobLog`, which scopes solely via `filter.podNames` (rejecting empty `podNames` client-side).
- `ListJobEvents` returns both `"job"` and `"instance"` events in a single call; use monotonic event `id` as a tiebreaker for identical 1-second timestamps.
- Invalid state transitions on `StartJob` / `StopJob` return `InternalError: RayJob status not allow <verb>` rather than `Conflict` (do not retry as transient).
- `UpdateJob` only allows mutating `name` and `description` on stopped jobs (worker group min/max replicas are immutable after creation) and is intentionally not wrapped.

---

### 8.4 `notebook` — Interactive Modeling

Referer: `/jobs/interactiveModeling`.

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `CreateNotebook` | See [Section 9](#9-creation-payload-contracts) | `{notebook_id, sub_code, sub_msg}` | `notebook create`, `notebook batch` |
| `GetNotebook` | `{notebook_id}` | `status`, `sub_status`, `workspace{}`, `project{}`, `logic_compute_group{}`, `quota{}`, `node{}`, `extra_info{}`, `dataset_info[]` | `notebook status` / `metrics` / `exec` / `shell` / `ssh` / `proxy-url`, `wait` |
| `ListNotebooks` | `{workspace_id, page, page_size, filter_by:{keyword, user_id[], logic_compute_group_id[], status[], mirror_url[]}, order_by:[{field:"created_at", order:"desc"}]}` | `{list[], total}` (`int`) | `notebook list`, Name Resolver |
| `ListNotebookCreators` † | `{workspace_id}` | `{list[], total}` | — |
| `ListNotebookEvents` | `{notebook_id, page, page_size}` | `{list[]\|events[], total}` | `notebook events`, `create --wait` |
| `ListNotebookLifecycles` | `{notebook_id, page, page_size, start_time?, end_time?}` | `{list[]}` | — |
| `ListRunIndex` | `{notebook_id}` | `{list[{index, start_time, end_time}]}` | `notebook lifecycle` |
| `StartNotebook` | `{notebook_id}` | `{notebook_id, sub_code, sub_msg}` | `notebook start` |
| `StopNotebook` | `{notebook_id}` | `{notebook_id, sub_code, sub_msg}` | `notebook stop` |
| `DeleteNotebook` | `{notebook_id}` | `{notebook_id, sub_code, sub_msg}` | `notebook delete` |
| `SaveNotebookImage` | `{notebook_id, name, version, description, flatten}` | Always `{}` | `notebook save-image` |
| `EstimateSaveMirrorSize` | `{notebook_id}` | `{active_snapshot_size}` | `notebook save-image`, `--dry-run` |
| `CancelSaveMirror` | `{notebook_id}` | — | `notebook cancel-save-image` |
| `CheckNotebook` † | `{name, workspace_id}` | `{notebook_id, sub_code, sub_msg}` if taken; `null` if free | Pre-creation duplicate name check |
| `GetNotebookAccessUrl` | `{notebook_id}` | `{jupyter_url, vscode_url}` | `notebook proxy-url` / `exec` / `shell`, SSH bridge |
| `GetRealtimeNotebookMetric` | `{notebook_id}` | `{resource_metric_list[]}` | `notebook metrics --now` |
| `GetScheduleConfig` | `{WorkspaceId}` | Workspace schedule policies + JSON-encoded quota menus (`quota`, `predef_train_spec`, `rayjob_quota`, `serving_quota`) | `resources policy`, `<workload> quota` Priority column, creation priority precheck |
| `GetTaskMetric` | See [Section 8.14](#814-metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `notebook metrics` |

**Notebook notes:**
- `GetNotebook.node` is not cleared to `{}` when `STOPPED`; instead `node.name` becomes `""` and `node.status` becomes `"UNKNOWN_NODE_STATUS"`.
- `GetRealtimeNotebookMetric` returns cluster-wide totals if `notebook_id` is empty — always validate a non-empty `notebook_id` before calling.
- `GetNotebookAccessUrl` returns the raw `jupyter_url` used for headless `JupyterTerminal` REST/WebSocket calls (`GET` for `_xsrf` cookie, `POST`/`DELETE api/terminals` with `X-XSRFToken`).
- `SaveNotebookImage` only supports 2-level `accessible` (`1` private, `2` public) and returns `{}`; the CLI resolves the saved image from `image.ListImages` and applies 3-level visibility (`private` / `project` / `public`) via `image.UpdateImage`.
- `GetScheduleConfig` is the superset of all workload schedule configs and provides `allowed_priority_levels` keyed by `quota_id`.
- `CreateNotebook` always sends `allow_ssh: true` so the Notebook proxy exposes the container's `rtunnel` port.

---

### 8.5 `inference_serving` — Model Serving

Referer: `/jobs/modelDeployment`. Route uses an **underscore** (`inference_serving`).

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `CreateServingConsole` † | See [Section 9](#9-creation-payload-contracts) | `{inference_serving_id, sub_code, sub_msg}` | `serving create`, `serving batch` |
| `ListServings` | `{workspace_id, page, page_size, filter_by:{my_serving:true, keyword?, project_id[]?, status[]?, inference_serving_type[]?}}` | `{inference_servings[], total}` | `serving list`, Name Resolver |
| `GetServing` | `{inference_serving_id}` | `status`, `replicas`, `node_num_per_replica`, `model_id`, `model_version`, `mirror_id`, `port`, `command`, `resource_spec_price{}`, `extra_info{node_names[], service}` | `serving status` / `api` / `metrics` |
| `ListServingVersions` | `{inference_serving_id}` | `{inference_servings[]\|list[], total}` | `serving versions` |
| `ListServingInstances` | `{inference_serving_id, page, page_size}` | `{groups[{items[]}], total}` | `serving instances` / `logs` / `events --instance` |
| `ListServingEvents` | `{page, page_size, filter:{object_type, object_ids[]}}` | `{events[]\|items[]\|list[]}` | `serving events`, `--instance` |
| `ListServingScaleHistory` | `{inference_serving_id, page, page_size}` | `{scale_history_items[], total}` (`str`) | `serving scale-history` |
| `GetServingLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[], total}` | `serving logs` |
| `GetServingScheduleConfig` | `{workspace_id}` | `{enable_auto_stop, items[{auto_stop_ruleset, gpu_count_min, gpu_count_max}]}` | `resources policy` |
| `GetServingApiMetric` | `{inference_serving_id, metric_types[], time_range:{start_timestamp, end_timestamp, interval_second}}` | `{metric_groups[]}` | `serving api-metrics` |
| `GetInferenceServingTerms` | `{inference_serving_id}` | `{terms[]}` | — |
| `GetServingConfigByWorkspaceId` | `{workspace_id}` | `{configs{}}` | `serving configs` |
| `GetInferenceServingUserProjectList` | `{workspace_id}` | `{projects[], users[]}` | Project/user resolution for `create` |
| `StartServing` | `{inference_serving_id}` | `{inference_serving_id, sub_code, sub_msg}` | `serving start` |
| `StopServing` | `{inference_serving_id}` | `{inference_serving_id}` | `serving stop` |
| `ScaleServing` | `{inference_serving_id, replica}` | — | `serving scale` |
| `RollbackServing` | `{inference_serving_id, version}` | `{inference_serving_id, sub_code, sub_msg}` | `serving rollback` |
| `DeleteServing` | `{inference_serving_id}` | `{inference_serving_id}` | `serving delete` |
| `GetTaskMetric` | See [Section 8.14](#814-metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `serving metrics` |

**Serving notes:**
- Always use `CreateServingConsole` (not OpenAPI `CreateServing`). `ScaleServing` takes singular `replica`.
- `ListServingInstances` nests instances inside `groups[].items[]` (one group per replica), and instance `name` is namespaced (`<project>/<pod>`).
- `ListServingEvents` uses `object_type: "INFERENCE_SERVING"` for deployment events and `"INFERENCE_SERVING_INSTANCE"` (with namespaced pod IDs) for pod events such as `Unhealthy`.
- `GetServingApiMetric` accepts multiple `metric_types` in one request (`QPS`, `SUCCESS_QPS`, `FAIL_QPS`, `SUCCESS_RATE`, `FAIL_RATE`, `REQUEST_COUNT`, `LATENCY`, `TTFT`/`_P50`/`_P95`/`_P99`, `TTLT`/`_P50`/`_P95`/`_P99`, `INPUT_TOKENS`, `OUTPUT_TOKENS`).
- `UpdateServing` is intentionally not wrapped because it only works in `FAILED`/`STOPPED` state, requires a flattened `resource_spec_price` incompatible with `GetServing`'s nested response, and overwrites omitted fields.

---

### 8.6 `workspace` — Cluster & Workspace Resources

Referer: `/jobs/distributedTraining` (`/cluster/nodeList` for `cluster.ListNodeEvents`).

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `ListLogicComputeGroups` | `{page_size: -1, page_num: 1, filter:{workspace_id}}` | `{logic_compute_groups[], total}` | `resources availability`, `<workload> quota`, group resolution |
| `ListNodeDimension` | `{filter:{workspace_id, logic_compute_group_id}, PageNumber, page_size}` | `{node_dimensions[], total}` | `resources availability` (full-node columns) |
| `ListTaskDimension` | `{filter:{workspace_id, logic_compute_group_id?, user_id?, project_id?}, PageNumber, page_size}` | `{task_dimensions[], total}` | `resources usage`, low-priority attribution in `availability` |
| `ListUserDimension` | `{filter:{workspace_id, user_id?}, PageNumber, page_size}` | `{user_dimensions[], total}` | `resources usage --mine` |
| `GetLogicComputeGroupNodeSpecs` | `{workspace_id, logic_compute_group_id}` | `{node_specs[]}` | `resources availability` |
| `GetWorkspaceNodeSpecs` | `{workspace_id}` | `{node_specs[]}` | `resources availability` |
| `GetLogicComputeGroupResource` | `{workspace_id, logic_compute_group_id}` | `{logic_resouces{}, gpu_type_stats[], runtime_attributes[]}` | `resources availability` |
| `GetWorkspaceQuota` | `{workspace_id}` | `{gpu_high_running, gpu_high_running_used, cpu_*, memory_*, is_fair_workspace, …}` | — |
| `GetWorkspaceComputeResource` | `{workspace_id}` | `{logic_resouces{cpu_total, cpu_used, memory_gi_total, memory_gi_used, gpu_total, gpu_used, gpu_low_priority_used}}` | — |
| `cluster.ListNodeEvents` | `{PageNumber, page_size, filter:{node_names[], from?}, sorter:[{field:"last_timestamp", sort}]}` | `{events[], total}` | `inspire resources node-events` |

**Workspace & dimension notes:**
- **Platform typo:** `GetLogicComputeGroupResource` and `GetWorkspaceComputeResource` spell the response key `logic_resouces` (missing the second `r`).
- `ListLogicComputeGroups` uses `logic_compute_group_id` (not `id`) and returns `support_job_type_list` as a **JSON-encoded string** (`interactive_modeling`, `hpc_job`, `ray_job`, `distributed_training`, `tensorboard`, `inference_serving_customize`, `inference_serving_exclusive`).
- `ListNodeDimension`, `ListTaskDimension`, and `ListUserDimension` require nested `filter.workspace_id`, default to 10 rows unless `page_size: 5000` is passed, and must be paginated until all `total` unique IDs are collected.
- A physical 8-GPU node counts as `Free Nodes` only when all six conditions hold simultaneously: `status == "READY"`, `tasks_associated.count == 0`, `gpu.used == 0`, no `cordon_type`, not `is_maint`, and `resource_pool != "fault"`. It counts toward `High Pri Nodes` when every associated task in `TaskDimension` has a known low submission priority.

---

### 8.7 `user` — Account & API Keys

| Action | Request Keys | Response (`Result`) | Consumer |
| --- | --- | --- | --- |
| `GetUserDetail` | `{}` | `{id, name, name_en, email, avatar_url, global_role, created_at, extra_info{}}` | Current-user filtering, `account check`, login bootstrap |
| `GetPermissions` † | `{WorkspaceId}` | `{permissions[]}` | `account permissions` |
| `GetRoutes` † | `{WorkspaceId}` | `{routes[{name, routes[{path, name, is_fair_workspace}]}]}` | Workspace enumeration (`WorkspaceId: "default"` during bootstrap) and `is_fair_workspace` detection |
| `GetMyAPIList` | `{}` | `{items[{key_id, name, created_at, value}]}` (`value` stripped by wrapper) | `account api-key list` |
| `GenerateAPIKey` | `{key_name}` | Verified by re-reading `GetMyAPIList` | `account api-key create` |
| `GetAPIKeyPlaintext` | `{api_key_id}` | `{value}` | `account api-key export`, `account api-key run`, SDK `api_keys.plaintext` |
| `DeleteAPIKey` | `{api_key_id}` | Verified by re-reading `GetMyAPIList` | `account api-key delete` |

---

### 8.8 `project` — Projects

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `ListProjects` † | `{page, page_size, filter:{workspace_id?, check_admin?}}` | `{items[], total}` | `project list`, project resolution |
| `GetProjectDetail` † | `{ProjectId}` | `{budget, children_budget, remain_budget, used_budget, created_at, en_name, description, priority, owner…}` | `project detail` |
| `GetProjectOwners` † | `{}` | `{items[{id, name, login_name, …}]}` | `project owners` |
| `GetProjectForPage` | `{page, page_size, filter{}}` | `{items[], total}` | — |

---

### 8.9 `image` — Image Catalog

Referer: `/jobs/interactiveModeling`.

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `ListImages` | `{page: 0, page_size: -1, filter:{…}}` | `{images[], total}` | `image list`, image resolution, `cache refresh` |
| `GetImageById` | `{ImageId}` | `image_id`, `name`, `address`, `framework`, `version`, `source`, `visibility`, `status`, `description`, `created_at` | `image detail`, readiness polling |
| `CreateImage` † | `{name, version, registry_hint:{workspace_id}, visibility, add_method, description}` | `{image{image_id, …}}` | `image register` (`add_method: 0` for `docker push`) |
| `UpdateImage` † | `{id, visibility?, description?}` | — | `image set-visibility` (requires bare `id`, not `image_id`) |
| `DeleteImage` | `{image_id}` | — | `image delete` |

---

### 8.10 `model-hub` — Model Registry

Referer: `/jobs/modelService?spaceId={workspace_id}`. Route uses a **hyphen** (`model-hub`).

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `ListModels` | `{workspace_id, page, page_size, filter_by:{user_id, keyword?, project_id[]?, model_type[]?}}` | `{list[], total}` | `model list`, `serving create` model resolution |
| `GetModelDetail` | `{model_id}` | `{model{}, user_name, project_name, user_avatar}` | `model status` |
| `ListModelVersions` | `{model_id}` | `{list[], total, next_version}` | `model versions`, `model status` |
| `ListModelRelatedServings` | `{model_id, version, page, page_size}` | `{serving[], total}` | `model status` / `model delete` precheck |
| `GetHasModelPendingServing` | `{model_id, version?}` | `{has_pending_serving}` | `model status` / `model delete` precheck |
| `GetRecommendedConfig` | `{model_id, version}` | `{min_node_count, min_gpu_count_per_node, min_cpu_count_per_node, min_memory_size_gib_per_node}` | `model deploy-config` |
| `CheckModelVLLMCompatible` | `{model_id, version, inference_serving_type}` | `{is_vllm_compatible}` | `model deploy-config` |
| `GetModelVLLMCompatibleData` | `{model_id, inference_serving_type?}` | `{data:[{version, is_vllm_compatible}]}` | `model status`, `model versions` |
| `CreateModel` † | `{name, project_id, workspace_id, model_source_path, model_source_type, model_type[], tags[], description}` | `{model_id}` | `model register` (`model_source_type: 1`) |
| `DeleteModel` † | `{model_id}` | — | `model delete` |

*(Note: `ListModelVersions` elements spell the active serving count `running_infrence_serving` and carry a dead `is_vllm_compatible` field; always query `GetModelVLLMCompatibleData` or `CheckModelVLLMCompatible` for live vLLM compatibility.)*

---

### 8.11 `resource-price` — Quota Catalog ‡

Referer: `/jobs/interactiveModeling`. Resolves `-q gpu,cpu,mem` into `quota_id` and `resource_spec_price`:

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `GetLogicComputeGroupResourceSpecPrices` | `{workspace_id, logic_compute_group_id, schedule_config_type}` | `{lcg_resource_spec_prices[{quota_id, gpu_count, cpu_count, memory_size_gib, gpu_info{}, cpu_info{}, total_price_per_hour, …}]}` | `<workload> quota`, `create` quota resolution |

`schedule_config_type` takes `SCHEDULE_CONFIG_TYPE_` + `DSW` (Notebook), `TRAIN` (Job), `HPC`, `RAY_JOB`, or `SERVE`. Joined against `notebook.GetScheduleConfig` by `quota_id` to attach `allowed_priority_levels`.

---

### 8.12 `file` — Storage Discovery ‡

Referer: `/jobs/files?spaceId={workspace_id}`.

| Action | Request Keys | Response (`Result`) | Status |
| --- | --- | --- | --- |
| `GetSystemStorageTypeList` | `{filter:{workspace_id}}` | `{system_storages[{name, cluster_id}]}` | Wrapped, no active CLI consumer |
| `GetDirList` | `{filter:{workspace_id, system_storage_type, name, cluster_id?}}` | `{files[{directory}]}` | Wrapped, no active CLI consumer |
| `GetSftpgoConnectionInfo` † | `{storage_name}` | `{address, webdav_port, auth}` | **Intentionally not wrapped** (`auth` is a plaintext base64 `user:password` credential for WebDAV) |
| `CreateCopy` / `ListFileCopyTasks` † | — | — | **Intentionally not wrapped** (submits human-reviewed approval tickets rather than immediate file copies) |

---

### 8.13 `dataset` — Dataset Mount Validation ‡

Referer: `/jobs/interactiveModeling?spaceId={workspace_id}`.

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `ValidateDataset` | `{datasets:[{dataset_id, version_id}], workspace_id}` | `{datasets_result:[{dataset_id, version_id, success, path, error_message}]}` | `dataset validate`, `notebook`/`job`/`hpc create --dataset` |

Both `dataset_id` and `version_id` take **string codes** (e.g., `"pixabay-81k"` and `"v0"`), never numeric Dataset Hub IDs. Error codes: `2000 数据集不存在`, `2001 版本不存在`, `2005 无访问权限`. Returned `path` is the internal storage path passed into `dataset_info[].path` on creation; the container mount point is always `/inspire/dataset/<dataset_code>/<version_code>`. Match batch results by `(dataset_id, version_id)` key, not list order.

---

### 8.14 Metrics — `GetTaskMetric`

Called per service route (`POST /api/v2/{notebook|train|hpc|ray|inference_serving}?Action=GetTaskMetric`):

```jsonc
{
  "filter": {"logic_compute_group_id": "lcg-…", "task_id": "…", "task_type": "…"},
  "metric_types": ["gpu_usage_rate"],
  "time_range": {"start_timestamp": 1700000000, "end_timestamp": 1700003600, "interval_second": 60}
}
```

- `task_type` values: `interactive_modeling` (`notebook`), `distributed_training` (`job`), `hpc_job` (`hpc`), `ray_job` (`ray`), `inference_serving` (`serving`).
- **One metric per request:** Passing multiple `metric_types` only returns the first metric, so the wrapper fans out per metric (`gpu_usage_rate`, `gpu_memory_usage_rate`, `cpu_usage_rate`, `memory_usage_rate`, `disk_io_read`, `disk_io_write`, `network_tcp_ip_io_read`, `network_tcp_ip_io_write`).
- **Platform typo:** Response key is spelled `time_seris_metric_groups` (wrapper also accepts `time_series_metric_groups`).
- **Do not use `GetTaskMetricBatch`:** It drops per-Pod `group_name` labels, returns empty disk I/O and `InternalError` on network I/O, and disagrees with single-task aggregation values.

---

### 8.15 Batch Reads — `ListJobs` & `ListJobEvents`

`train` and `hpc` support batch ID queries up to **20 IDs per request** (deduplicate before chunking):
- `ListJobs` with `{workspace_id, job_ids: [...]}` silently drops unknown IDs (diff against requested IDs to find missing jobs).
- `ListJobEvents` with `filter: {object_type, object_ids: [...]}` fails the **entire batch** (`InvalidParameter: job <id> not found`) if any single ID is missing; strip the offending ID and retry the remaining batch. (Pod-level `object_type: "instance"` events use a separate 200-item chunk limit.)

---

## 9. Creation Payload Contracts

| Field | `notebook.CreateNotebook` | `train.CreateJobConsole` † | `hpc.CreateJobConsole` † | `ray.CreateJob` | `inference_serving.CreateServingConsole` † |
| --- | --- | --- | --- | --- | --- |
| `dataset_info` | Accepted | Accepted | Accepted | **Rejected** | **Rejected** |
| `envs` (`[{name, value}]`) | **Rejected** | Accepted (not echoed by `GetJob`) | **Rejected** | **Rejected** | **Rejected** |
| `description` | **Rejected** | Accepted | Accepted | Accepted | Accepted |
| `is_publicpath_readonly` | Accepted | Accepted | Accepted | Accepted | Accepted |
| `is_projectuserspath_readonly` | Accepted (Maintainer only) | **Rejected** | **Rejected** | — | — |

**Key payload rules:**
- **Omit unset optional fields:** Optional flags (such as `is_publicpath_readonly` or `is_projectuserspath_readonly`) must be omitted from the JSON body when not explicitly set.
- **HPC runtime limits & priority:** `job_max_time` (`"D-HH:MM:SS"`) and `max_running_time_days`/`_hours`/`_minutes` live inside `sbatch_script`. Top-level `priority` is required (missing `priority` triggers `InternalError`). Never send `sbatch_script.memory_per_node` (broken in the platform script generator); always send `memory_per_cpu`.
- **Durations as strings:** `train.CreateJobConsole` expects `max_running_time_ms`, `reserve_on_fail_ms`, and `reserve_on_success_ms` as **string** integers.
- **Images:** Pass registry URLs (`image` / `mirror_url`) or `mirror_id` as required by each Action, never display names.

---

## 10. Non-Action `/api/v2` Endpoints (WebSocket PTY & Notebook Proxy)

### Instance PTY WebSockets (`job_shell.py`)

| Workload | Route | Workload ID Query Param | Instance Query Param |
| --- | --- | --- | --- |
| `job` | `/api/v2/train_job/remote_cmd` | `job_id` | `instance_name` |
| `hpc` | `/api/v2/hpc_jobs/instances/exec` | `job_id` | `instance_id` |
| `ray` | `/api/v2/ray_job/instances/exec` | `job_id` | `instance_id` |
| `serving` | `/api/v2/inference_servings/instances/exec` | **`inference_serving_id`** | `instance_id` |

### Notebook Lab Redirect & Reverse Proxy

`GET {base_url}/api/v2/notebook/lab/{notebook_id}` returns `301` with a token-bearing gateway URL (`https://<gateway>/ws-…/project-…/user-…/jupyter/<notebook>/<token>/lab`).
- Container port proxying (`/proxy/{port}/`) works **only** on that token-bearing gateway URL (used by `notebook proxy-url` and by `rtunnel` SSH bridging on port 22222).
- Always scrub the embedded gateway token in logs and errors via `redact_proxy_url` in `rtunnel.py`.

---

## 11. Dataset Hub (`plaza/`)

The Dataset Hub (`PLAZA_BASE_URL` in [`plaza/core.py`](../../cli/inspire/platform/web/plaza/core.py)) is a separate service sharing CAS SSO (`CAS_BASE_URL`), implemented in [`plaza/`](../../cli/inspire/platform/web/plaza/).

### Two Identifier Sets
- **`datasetCode` / `versionCode`:** User-facing slugs displayed by the CLI and required by `dataset.ValidateDataset` and `--dataset`.
- **`datasetId` / `versionId`:** Internal numeric IDs used only inside `plaza/` calls (`findDatasets`); never expose them or pass them to mount APIs.

### Three-Step CAS Handshake (Pure HTTP)
1. `GET {CAS_BASE_URL}/cas/login?service=<urlencode(PLAZA_BASE_URL + "/")>` with the session's `CASTGC` cookie (`allow_redirects=False`) → `302` with `?ticket=ST-…`.
2. `POST {PLAZA_BASE_URL}/api/base/login` with `{"ticket": "…", "service": PLAZA_BASE_URL + "/"}` → issues `datasets-session` cookie and `userInfo`.
3. Subsequent requests send `datasets-session` (cached in memory per `(account, session_created_at)`).

### Envelope & Wrapped Endpoints

Responses use `{"code": 0, "data": {…}, "msg": "…"}`. Note that `code: 7` is returned for both HTTP 200 record-not-found errors and HTTP 401 unauthenticated errors — trigger session re-handshake **only on HTTP 401**.

| Endpoint | Method | Parameters | Response (`data`) | CLI Consumer |
| --- | --- | --- | --- | --- |
| `/api/datasets/getDatasetsList` | `GET` | `page`, `pageSize`, `keyword?`, `tags?` (omit `tags` when empty; comma-separated numeric `tagId`s) | `{list[], total, page, pageSize}` | `dataset list`, code-to-ID resolver |
| `/api/datasets/findDatasets` | `POST` | `{datasetId}` | Dataset detail + `versions[]` | `dataset show` |
| `/api/datasetTags/getDatasetTagsList` | `GET` | `pageSize=999` | `{list[]}` (`categoryId`: `0/1` text, `2` image, `3` audio, `4` video, `5` multimodal) | `dataset tags`, `--tag` resolver |
| `/api/datasetApplyApprove/getDatasetApplyList` | `GET` | `page`, `pageSize`, `keyword?` | `{list[], total}` (`state`: `0` pending, `1` approved, `2` rejected, `-1` withdrawn — note `0` is falsy) | `dataset applications` |
| `/api/datasetApplyApprove/getDatasetApproveList` | `GET` | `page`, `pageSize`, `keyword?`, `role?` | `{list[], total}` | `dataset applications --to-approve` |
| `/api/datasetApplyApprove/intoApproveById` | `GET` | `id` | Single application detail including `datasetCode` | `dataset applications <name>` |

Write endpoints (`datasetApply`, `datasetApprove`, `createDatasetUserRole`, dataset creation/mutation) are intentionally not wrapped.

---

## 12. Output Boundaries & Change Acceptance Checklist

1. **No raw payload leakage:** Unpack via `_v2_result()`, declare list keys explicitly, and project human and `--json` output through explicit allowlists.
2. **Name-only public identity:** Never expose opaque handles (`ws-`, `project-`, `lcg-`, `quota_id`, `mirror_id`, `notebook_id`, `job_id`, or embedded IDs in error strings) in CLI output.
3. **Explicit credential/endpoint exceptions:** Only `notebook proxy-url` (`preserve_raw={"url"}`), `serving list/status/create/api` (validated HTTP(S) `endpoint`), and explicit `account api-key export --stdout` / `run` may cross the redaction boundary.
4. **Distinguish errors from empty catalogs:** Never turn transport/auth failures into `[]` or `0`, and cover failure paths with unit tests.
5. **Synchronize docs and tests:** Update Click `--help`, `cli/tests/test_browser_api_*.py`, and the tables in this file together.
