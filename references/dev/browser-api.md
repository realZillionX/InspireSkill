# Browser API Maintainer Reference

> **Audience:** CLI and SDK maintainers. Do not load this file during normal workload operations — regular CLI usage relies solely on the name-based CLI surface and `--help`.
>
> **Scope:** Covers both the main compute console (`/api/v2` Action gateway on `{base_url}`, Sections 1–10) and the Dataset Hub (`plaza`, Section 11). The compute console uses OAuth device authorization; the separate Dataset Hub protocol uses CAS SSO.

## 1. Sources of Truth & Invariants

| Priority | Source | What It Defines |
| --- | --- | --- |
| 1 | [`browser_api/`](../../cli/inspire/platform/web/browser_api/) | Canonical request construction, response key extraction, and normalization |
| 2 | [`session/`](../../cli/inspire/platform/web/session/), [`plaza/`](../../cli/inspire/platform/web/plaza/) | Per-account isolation, OAuth/CAS login, proxies, transport retries, and Dataset Hub client |
| 3 | `cli/tests/test_browser_api_*.py` | Unit tests checking wrapper behavior and output boundaries |
| 4 | Live console SPA network requests | Authoritative proof of whether a platform capability exists and what payload it sends |
| 5 | `GET {base_url}/discovery` | Candidate Action names and request schemas (**a hint, not proof of supported behavior** — see Section 6) |
| 6 | Click `--help` | Public CLI interface |

**Core invariants:**
- All `/api/v2` JSON Action requests are constructed inside `browser_api/` (except `user.GetUserDetail` / `user.ListAccessibleWorkspaces` during session bootstrap in `session/`, and non-Action instance PTY WebSockets in [`pty_socket.py`](../../cli/inspire/platform/web/pty_socket.py)).
- All v2 JSON responses are unpacked through `_v2_result()` in [`session/envelope.py`](../../cli/inspire/platform/web/session/envelope.py) (re-exported by [`browser_api/core.py`](../../cli/inspire/platform/web/browser_api/core.py)).
- Only verified, reproducible behavior with active or explicitly evaluated consumers belongs in this document.

## 2. Request & Response Contract

```http
POST {base_url}/api/v2/{route}?Action={Action}
Authorization: Bearer …
X-Inspire-Client-Source: inspire-cli/inspireskill-<version>
Content-Type: application/json
Referer: {base_url}/{console_page_path}

{ …JSON body… }
```

| Aspect | Rule |
| --- | --- |
| Method & Shape | Always `POST` with a JSON body and `?Action={Action}` in the query string (for non-Action WebSocket/proxy routes, see Section 10). |
| Authentication | Requires `Authorization: Bearer …` and `X-Inspire-Client-Source: inspire-cli/inspireskill-<version>` (sent only to the configured platform origin). |
| `Referer` | Must match the origin and corresponding console page path; constructed per wrapper. |
| Field Casing | Protobuf JSON unmarshaling is case- and underscore-insensitive (`ImageId` / `image_id` / `Image_Id` map to the same field), **except** when the underlying field name itself differs (e.g., `image.UpdateImage` requires `id`, whereas `image_id` is silently ignored). |
| Account Isolation | Every request uses the OAuth token session and proxy configuration of the selected Account Alias; cross-account commands never fall back to the active default account. |

### Response Envelope

Both successes and business errors return **HTTP 200** with an AWS-style JSON envelope:

```jsonc
{"ResponseMetadata": {…}, "Result": {…}}                                  // Success
{"ResponseMetadata": {"Error": {"Code": "…", "Message": "…"}}}            // Business error
```

- **Unpacking order:** Check `429` → `401/302` → non-JSON content-type → `ResponseMetadata.Error` → `Result` (defaulting missing `Result` to `{}`). Always check `429` before sniffing `Content-Type` because gateway rate-limiting returns an HTML error page.
- **List keys vary per Action:** Declare the expected list key explicitly in each wrapper (`jobs`, `items`, `list`, `logs`, `events`, `scale_history_items`, `logic_compute_groups`, `node_dimensions`, etc.).

| Error `Code` | Meaning | Handling |
| --- | --- | --- |
| `InvalidAction` | Action does not exist on this route | Route itself is alive (a missing route returns HTTP `404 page not found`) |
| `InvalidParameter` | Schema violation (`proto: unknown field "X"`, missing required field, `page or page_size too large`) | Fix request payload |
| `AccessForbidden` | Genuine permission denial **or** missing workspace scoping | Verify workspace scoping placement (Section 5) before concluding lack of permission |
| `ResourceNotFound` | Target object does not exist | Standard on `notebook` / `hpc` / `serving`; note that `train.DeleteJob` returns `AccessForbidden` instead |
| `Conflict` | Invalid state transition (e.g., deleting a running workload or starting an already running one) | `stop` and poll until stopped before deleting |
| `InternalError` / `Throttling` / `ServiceUnavailable` / `SlowDown` / `RequestTimeout` / `TooManyRequests` | Transient server or gateway error | Raised as `TransientAPIError` and retried by the transport layer |

> [!WARNING]
> `InternalError` is ambiguous: it can indicate a genuine server failure, a handler crash after accepting a valid field, or a deterministic validation failure (e.g., `hpc.GetJobLog` time window > 30 days, or `hpc.CreateJobConsole` missing `priority`). Validate known client-side constraints before sending requests so deterministic errors do not burn transient retries.

### Transport Layer & Empty Results

`request_json()` in [`session/__init__.py`](../../cli/inspire/platform/web/session/__init__.py) is the single entrypoint for both `requests` and Playwright channels:
- **Retries:** HTTP `408/425/429/500/502/503/504` and transient envelope codes raise `TransientAPIError` (`ValueError` subclass) and retry up to 3 times (honoring `Retry-After` up to 8s in session requests, otherwise exponential backoff with jitter). Keep rate-limit retries in the transport layer, never in individual wrappers.
- **`page_size` clamping:** `_clamped_page_size()` caps generic requests at `5000` (preserving `-1` for "fetch all"). `train.ListJobs` follows the console window: clamp size to `[1, 5000]`, page to `[1, floor(5000 / size)]`. Scanners stop before requesting beyond that window and never repeatedly fetch a clamped final page. Because `train.ListJobs` filters rows by `created_by` while returning a platform-wide `total`, short or empty pages establish completion; CLI history scans use 999-row pages and report truncation explicitly when filling the 5,000-row window.
- **Never conflate request failure with an empty result:** Only a successful response with zero items means "none exist". Wrappers must never swallow `TransientAPIError` or `SessionExpiredError` into `[]`, `None`, or `0`. Fan-out wrappers (such as per-group quota fetches) track partial completion explicitly (`QuotaCatalog.complete`).

### OAuth Session Lifecycle

- **Grant & token types:** Device login and silent CAS renewal request `openid profile email offline_access` and store tokens in `web_session.json` (`0600` on POSIX, current-user ACL on Windows). Access tokens last ~1 hour and refresh automatically within 60 seconds of expiry under the per-account file lock (rereading disk first and atomically persisting rotated tokens via `_save_existing_account`).
  - **Session-bound refresh tokens (`typ: "Refresh"`):** Observed on `inspire-studio` (`inf-internal` realm) with a 12-hour rolling idle timeout (`refresh_expires_in: 43200`) and a 7-day absolute session limit (`GET /realms/inf-internal/account/sessions`). Running an online `inspire account check` every 6 hours refreshes access tokens before idle expiry. Once `refresh_expires_at` passes locally, refresh skips OAuth and attempts silent CAS renewal directly.
  - **Offline refresh tokens (`typ: "Offline"`):** Survive browser/CAS SSO expiry and carry no fixed deadline (`refresh_expires_in: 0` or `null`/absent, no JWT `exp`), remaining subject to server-side idle timeout and revocation. `WebSession.refresh_offline` is persisted (and recovered from unverified JWT `typ == "Offline"` if missing on disk). Its local 30-day `refresh_expires_at` is purely informational: offline refresh is always attempted even past that timestamp, falling back to CAS on rejection, transient/network failure, or malformed response.
  - **Lifetime validation:** Token lifetimes must be finite numbers (never booleans): positive for access tokens and session-bound refresh tokens; absent, `null`, or non-negative finite for offline refresh tokens.
- **Refresh fallback & error classification:**
  - `get_web_session()` never prompts interactively; HTTP `401` or auth redirects refresh once and retry once. Notebook `GetNotebookAccessUrl` sessions use Jupyter's own cookies without forwarding platform Bearer tokens.
  - OAuth refresh runs first whenever eligible; if it fails (or a session refresh token is expired), silent device authorization runs when a valid `cas_session.json` is present. Either path succeeding preserves API access.
  - HTTP `400/401` with `invalid_grant`, `invalid_token`, `invalid_client`, or `unauthorized_client` raises `RefreshRejectedError(AuthenticationError)` carrying HTTP status and an allowlisted RFC 6749/6750 error code (`refresh_rejected` for unknown codes; raw server text is never echoed). HTTP `408/425/429/5xx` or `temporarily_unavailable` raises `RefreshUnavailableError(TransientAPIError)` (`unavailable` when unknown).
  - If both OAuth refresh and CAS fallback fail, network errors re-raise unchanged (with CAS failure chained) and transient errors re-raise as `RefreshUnavailableError` — never misclassified as login-required errors. Transport and SDK layers map these to retryable `TransportError` before inspecting chained CAS causes, and plaza CLI handling preserves `TransportError` rather than converting it to `SessionExpiredError`.
- **Diagnostics (`account check` & `AccountCheck`):**
  - `login --json` exposes `refresh_kind`; `account check`, `account list`, and SDK `AccountCheck` expose `oauth.refresh_kind` (`"offline"` or `"session"`), with `refresh_expires_at: null` for offline tokens (`state: "offline"` when the access token is expired locally).
  - Only `RefreshRejectedError` sets `oauth.state = "rejected"` (with `oauth.refresh_error`). Transient or network failures preserve the local token state and attach `oauth.refresh_error`, `oauth.refresh_retryable: true`, and `oauth.refresh_status` (HTTP status or `null`); CLI `account check` exits with API error code `13` (vs. `11` for authentication rejection) while still emitting JSON and effective proxy diagnostics.
  - Local `inspire logout` deletes the account directory without calling a server revocation endpoint; server-side session revocation lives in the Keycloak account console.

## 3. Route Names

Gateway URLs use `/api/v2/{route}`, which does **not** always match the Service `Name` in `/discovery`:

| Discovery Service `Name` | Gateway Route | Notes |
| --- | --- | --- |
| `inference-serving` | **`inference_serving`** | Hyphenated form returns 404 |
| `model-hub` | **`model-hub`** | Underscore form returns 404 |
| `train` / `hpc` / `ray` / `notebook` / `workspace` / `user` / `project` / `image` / `cluster` | Same as `Name` | — |
| *(Not in discovery)* | **`model_plaza`**, **`resource-price`**, **`file`**, **`dataset`** | `model_plaza` uses an underscore; plural variants (`files`, `datasets`, etc.) return 404 |

## 4. Pagination & `total` Quirks

| Aspect | Platform Behavior | Wrapper Handling |
| --- | --- | --- |
| Page number key | `PageNumber`, `page_num`, and `page` are interchangeable on most Actions | **`train.ListTensorboards` only accepts PascalCase `PageNumber`** (`page` / `page_num` silently return `[]`) |
| `page_size: -1` | Supported by `workspace.ListLogicComputeGroups`, `image.ListImages`, `project.ListProjects`; returns only 10 rows on `workspace.List*Dimension`; rejected by `model-hub.ListModels` | Only use `-1` on verified Actions; paginate explicitly by `total` elsewhere |
| Omitting `page_size` | `ListLogicComputeGroups` and `hpc.ListSlurmdPodEvent` return `[]` with a non-zero `total` | Always send an explicit `page_size` |
| `total` type | Either `int` (`notebook.ListNotebooks`) or numeric `str` (`hpc.ListJobs` returns `"202"`) | Always coerce via `_coerce_total()`, never `isinstance(total, int)` |
| Unpaginated Actions | `notebook.ListRunIndex` rejects `PageNumber` | Do not send pagination keys |
| Unknown field unmarshaling | Most Actions reject unknown keys with `proto: unknown field`, while `train.CreateTensorboard` and `notebook.SaveNotebookImage` ignore unknown keys | Test with a known-invalid key first before relying on `unknown field` probes |

## 5. Workspace Scoping

`workspace_id` is placed in five different shapes depending on the Action:

| Placement | Actions |
| --- | --- |
| Unscoped (`{}`) | `user.ListAccessibleWorkspaces` |
| Top-level `workspace_id` | `ListJobs` (`train`/`hpc`/`ray`), `notebook.ListNotebooks`, `inference_serving.ListServings`, `model-hub.ListModels`, `workspace.GetWorkspaceQuota`, `workspace.GetWorkspaceComputeResource`, `workspace.GetLogicComputeGroupResource`, `workspace.Get*NodeSpecs`, `dataset.ValidateDataset` |
| Nested `filter.workspace_id` | `workspace.ListNodeDimension` / `ListTaskDimension` / `ListUserDimension`, `workspace.ListLogicComputeGroups`, `project.ListProjects`, `model_plaza.GetModelListInPlaza`, `file.*` |
| Top-level `workspace_id` + nested `filter_by` | `notebook.ListNotebooks`, `ray.ListJobs`, `inference_serving.ListServings`, `model-hub.ListModels` (filters go inside `filter_by`, while `workspace_id` stays at top level) |
| PascalCase `WorkspaceId` / camelCase `workspaceId` | `user.GetPermissions` (`WorkspaceId`), `notebook.GetScheduleConfig` (`WorkspaceId`), `image.SyncImage` (`workspaceId`) |

Workspace discovery calls `user.ListAccessibleWorkspaces` with `{}` and parses `Result.items` (`workspace_id`, `name`, `is_fair_workspace`) via [`session/workspaces.py`](../../cli/inspire/platform/web/session/workspaces.py). Missing or malformed `items` raises an error so cache refresh never mistakes a broken response for an empty catalog (`items: []` is a valid empty result).

### `cluster.*` vs. `workspace.*`

- Dimension and resource Actions shared between `cluster.*` and `workspace.*` (`ListNodeDimension`, `ListTaskDimension`, `ListProjectDimension`, `ListUserDimension`, `ListLogicComputeGroups`, `GetOverviewResourceMetric`, etc.) return `AccessForbidden` for regular users under `cluster.*` and must be called under `workspace.*` with `filter.workspace_id`.
- **Exception:** `cluster.ListNodeEvents` has no `workspace.*` counterpart and is readable by regular users (`inspire resources node-events`).

## 6. Surface Discovery & Contract Verification

| Source | What It Proves | Limitations |
| --- | --- | --- |
| `GET {base_url}/discovery` | Static list of public Services/Actions and request field schemas (`Version` is a content etag) | Response list keys (`Items`/`TotalCount`) are inaccurate; omits `model_plaza`, `resource-price`, `file`, `dataset`, and `*Console` write Actions; does not reflect RBAC permissions |
| Console SPA bundles (`scripts/scan_v2_surface.py`) | Lower bound of Actions and REST paths called by the web UI | Must recursively follow lazy-loaded JS chunks from the entrypoint and normalize template literals |
| `user.GetMyPermissions` | Returns `{Services: {svc: {Read, Write, Actions: {Action: bool}}}}` for RBAC-gated services | Does not cover `train`, `notebook`, `workspace`, `project`, `ray`, or `hpc` |

Never conclude a capability is missing based solely on `/discovery` — always check `*Console` variants and the SPA bundle first.

### Offline Bundle Field Evidence (`scan_v2_surface.py --bundles`)

Scan downloaded JS bundles offline from `cli/` without loading credentials or making network calls:

```bash
uv run python ../scripts/scan_v2_surface.py \
  --bundles /path/to/sanitized-bundles \
  --fields is_publicpath_readonly,publicpath_mount_mode \
  --json /tmp/request-field-evidence.json
```

`--bundles` reads immediate `*.js` files in the directory (rejecting empty directories and `--probe`). `--fields` adds a `field_evidence` report in both offline and live modes, classifying each requested identifier's `token_presence` (`present` or `no token evidence`) with `request_field_evidence: "not linked to a request body"` (identifier matching cannot distinguish submit payloads from read/compatibility code). Output includes only field names and classifications — never source excerpts, matched values, or bundle filenames.

### Request Contract Snapshots (`compare_request_contracts.py`)

[`contracts/public-path-mount.json`](contracts/public-path-mount.json) records verified field types and enum values across the five workload creation Actions (`"type": "absent"` marks verified exclusion; unreviewed fields are omitted). Compare two snapshots or validate a request body offline from the repository root:

```bash
python3 scripts/compare_request_contracts.py references/dev/contracts/public-path-mount.json reviewed.json
python3 scripts/compare_request_contracts.py references/dev/contracts/public-path-mount.json request.json --request hpc.CreateJobConsole
```

Snapshot diffs report additions, removals, type/enum changes, and single-addition/single-removal rename candidates; `--request` exits `1` on excluded fields, wrong types, or out-of-range enum values while allowing omitted optional fields. SDK and CLI unit tests validate outgoing create payloads against the same snapshot.

### Manual Maintainer Write Smoke Checklist

Write probes are never automated. Before running an authorized live smoke test:
1. Use a dedicated local test account with explicit approval for one create attempt; never commit credentials, cookies, raw responses, or resource IDs.
2. Pin `workspace`, `project`, `group`, minimal `quota`, `image`, a unique disposable name, replica/worker bounds (`1` replica, autoscaling disabled), and a short wall-time cap (e.g., 5 minutes).
3. Review `--help` and `<workload> create --dry-run` first, and prepare the exact `stop` and `delete` commands before submitting.
4. Inspect the created resource, then stop and delete it inside a `finally` path regardless of assertion outcome, confirming terminal state.
5. If submission times out or returns no ID, reconcile the unique name via read-only list queries before taking further action — never blindly resubmit or guess renamed fields.

## 7. Probing Methodology

When verifying undocumented Actions or fields without mutating state:
1. **Route existence:** Send any Action name. HTTP `404` means the route does not exist; `InvalidAction` proves the route is live.
2. **Action existence:** Send `{}`. `InvalidAction: unknown action: X` means the Action does not exist; any validation error proves the Action exists.
3. **Field existence:** Send `{"candidate_field": "x"}`. `InvalidParameter: proto: unknown field` means the field is not in the schema.
   - **Caveat 1 (`InternalError`):** Always compare against a control request with a known-invalid field (`{"nonexistent_xyz": 1}`) to verify the handler performs strict protobuf unmarshaling.
   - **Caveat 2 (Fake resource IDs mask unknown fields):** Gateway auth middleware checks resource IDs (`notebook_id`, `ray_job_id`, etc.) *before* strict protobuf decoding. Sending a fake ID returns `ResourceNotFound` even when accompanied by unknown fields. Probe either without a resource ID or with a real resource ID owned by the test account.
4. **Write semantics & round-trip:** Trust a write field only when the resulting resource state changes and `Get*` (or container inspection) reflects the written value.

## 8. Action Reference by Route

Every entry below is called via `POST {base_url}/api/v2/{route}?Action={Action}` and unpacked from `Result`. `†` indicates an Action absent from `/discovery`; `‡` indicates an entire route absent from `/discovery`.

| Route | Domain | Wrapped Actions | Primary CLI Groups |
| --- | --- | --- | --- |
| [`train`](#81-train--distributed-training--tensorboard) | GPU Jobs & TensorBoard | 16 | `job`, `tensorboard` |
| [`hpc`](#82-hpc--cpu-slurm-batch) | CPU Slurm Batch | 11 | `hpc` |
| [`ray`](#83-ray--elastic-compute) | Ray Clusters | 12 | `ray` |
| [`notebook`](#84-notebook--interactive-modeling) | Notebooks & Image Snapshots | 18 | `notebook`, `image` |
| [`inference_serving`](#85-inference_serving--model-serving) (+ `model_plaza` ‡) | Online Model Serving & Plaza Selector | 19 (+1 `model_plaza`) | `serving` |
| [`workspace`](#86-workspace--cluster--workspace-resources) (+ `cluster`) | Compute Groups, Nodes, Quotas, Usage | 7 (+1 `cluster`) | `resources`, `<workload> quota`, `create` |
| [`user`](#87-user--account--api-keys) | Identity, Workspaces, Permissions & API Keys | 7 | `account`, `login`, owner-filtered lists |
| [`project`](#88-project--projects) | Projects & Budgets | 5 | `project`, `create` |
| [`image`](#89-image--image-catalog) | Image Registry | 6 | `image` |
| [`model-hub`](#810-model-hub--model-registry) | Model Registry | 14 | `model`, `serving create` |
| [`resource-price`](#811-resource-price--quota-catalog-) ‡ | Per-Group Quota & Price Catalog | 1 | `<workload> quota`, `create` |
| [`file`](#812-file--storage-discovery-) ‡ | Storage Pools & Directories | 0 (documented, not wrapped) | — |
| [`dataset`](#813-dataset--dataset-mount-validation-) ‡ | Official Dataset Validation | 1 | `dataset validate`, `--dataset` |

---

### 8.1 `train` — Distributed Training & TensorBoard

Referer: `/jobs/distributedTraining` (detail: `/jobs/distributedTrainingDetail/{job_id}`).

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `CreateJobConsole` † | See [Section 9](#9-creation-payload-fields) | `{job_id, sub_code, sub_msg}` | `job create`, `job batch` |
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
- `job list --workspace all` fans out across visible Workspaces with up to 8 independent concurrent workspace readers.

**TensorBoard notes:**
- `CreateTensorboard` requires `auto_stop_time_ms` as a **string** (max 72h; missing field error misleadingly says `tensorboard_auto_stop_time_ms is required`). Its unmarshaller ignores unknown fields and returns `{}` on success, so the CLI resolves the newly created board by name via `ListTensorboards`.
- `GetTensorboard` on an unknown ID returns `InvalidParameter: 用户不存在。` (treat as not found, not an account error).
- `ListTensorboards` requires PascalCase `PageNumber`; `job_id` filter is ignored by the server and filtered client-side.
- `GetTensorboard.url` points to a live TensorBoard web app exposing `data/runs`, `data/plugin/scalars/tags`, and `data/plugin/scalars/scalars?run=&tag=` (used by `tensorboard tags` and `scalars`).

---

### 8.2 `hpc` — CPU Slurm Batch

Referer: `/jobs/highPerformanceComputing` (detail: `/jobs/hpcDetail/{job_id}`).

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `CreateJobConsole` † | See [Section 9](#9-creation-payload-fields) | `{job_id, sub_code, sub_msg}` | `hpc create`, `hpc batch` |
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
- `GetJob.steps` (`completed/total`, e.g., `1/1` vs `0/0`) is statically parsed from `srun` invocations in `entrypoint` and distinguishes a job that ran `srun` steps from one that did not.
- `GetJobLog` and `ListSlurmdPodEvent` require namespaced `<ns>/<pod>` identifiers. `GetJobLog` rejects windows > 30 days (`HPC_LOG_MAX_WINDOW_MS`), ignores `PageNumber`, and returns the **oldest** `page_size` lines (tailing fetches the window and slices client-side). `ListSlurmdPodEvent` emits one duplicate row per occurrence without a `count` field (deduplicated client-side).
- Jobs in `SUCCEEDED_RETAINING` or `FAILED_RETAINING` reject `DeleteJob` with `Conflict`, and `StopJob` is a no-op on retaining jobs — wait for transition to `SUCCEEDED` / `FAILED`.

---

### 8.3 `ray` — Elastic Compute

Referer: `/jobs/ray`.

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `CreateJob` | See [Section 9](#9-creation-payload-fields) | `{ray_job_id, sub_code, sub_msg}` | `ray create`, `ray batch` |
| `GetJob` | `{ray_job_id}` | `status`, `head_node{}`, `worker_groups[]`, `entrypoint`, `creator{}`, `priority_name` | `ray status`, `ray metrics` |
| `ListJobs` | `{workspace_id, page_num, page_size, filter_by:{user_id:[…]}}` | `{items[], total}` (`str`) | `ray list`, Name Resolver |
| `ListJobCreators` | `{workspace_id}` | `{items[]}` | Wrapped helper |
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
- `UpdateJob` only mutates `name` and `description` on stopped jobs (worker group min/max replicas are immutable after creation) and is intentionally not wrapped.

---

### 8.4 `notebook` — Interactive Modeling

Referer: `/jobs/interactiveModeling`.

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `CreateNotebook` | See [Section 9](#9-creation-payload-fields) | `{notebook_id, sub_code, sub_msg}` | `notebook create`, `notebook batch` |
| `GetNotebook` | `{notebook_id}` | `status`, `sub_status`, `workspace{}`, `project{}`, `logic_compute_group{}`, `quota{}`, `node{}`, `extra_info{}`, `dataset_info[]` | `notebook status` / `metrics` / `exec` / `shell` / `ssh` / `proxy-url`, `wait` |
| `ListNotebooks` | `{workspace_id, page, page_size, filter_by:{keyword, user_id[], logic_compute_group_id[], status[], mirror_url[]}, order_by:[{field:"created_at", order:"desc"}]}` | `{list[], total}` (`int`) | `notebook list`, Name Resolver |
| `ListNotebookCreators` † | `{workspace_id}` | `{list[], total}` | Wrapped helper |
| `ListNotebookEvents` | `{notebook_id, page, page_size}` | `{list[]\|events[], total}` | `notebook events`, `create --wait` |
| `ListNotebookLifecycles` | `{notebook_id, page, page_size, start_time?, end_time?}` | `{list[]}` | Wrapped helper |
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

Referer: `/jobs/modelDeployment`. Route uses an **underscore** (`inference_serving`), with published plaza model lookup on **`model_plaza`** ‡.

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `CreateServingConsole` † | See [Section 9](#9-creation-payload-fields) | `{inference_serving_id, sub_code, sub_msg}` | `serving create`, `serving batch` |
| `ListServings` | `{workspace_id, page, page_size, filter_by:{my_serving:true, keyword?, project_id[]?, status[]?, inference_serving_type[]?}}` | `{inference_servings[], total}` | `serving list`, Name Resolver |
| `GetServing` | `{inference_serving_id}` | `status`, `replicas`, `node_num_per_replica`, `model_id`, `model_version`, `mirror_id`, `port_configs[]` (legacy `port` fallback), `command`, `resource_spec_price{}`, `extra_info{node_names[], service}` | `serving status` / `api` / `metrics` |
| `ListServingVersions` | `{inference_serving_id}` | `{inference_servings[]\|list[], total}` | `serving versions` |
| `ListServingInstances` | `{inference_serving_id, page, page_size}` | `{groups[{items[]}], total}` | `serving instances` / `logs` / `events --instance` |
| `ListServingEvents` | `{page, page_size, filter:{object_type, object_ids[]}}` | `{events[]\|items[]\|list[]}` | `serving events`, `--instance` |
| `ListServingScaleHistory` | `{inference_serving_id, page, page_size}` | `{scale_history_items[], total}` (`str`) | `serving scale-history` |
| `GetServingLog` | `{page_size, filter:{podNames[], start_timestamp_ms, end_timestamp_ms}}` | `{logs[], total}` | `serving logs` |
| `GetServingScheduleConfig` | `{workspace_id}` | `{enable_auto_stop, items[{auto_stop_ruleset, gpu_count_min, gpu_count_max}]}` | `resources policy` |
| `GetServingApiMetric` | `{inference_serving_id, metric_types[], time_range:{start_timestamp, end_timestamp, interval_second}}` | `{metric_groups[]}` | `serving api-metrics` |
| `GetInferenceServingTerms` | `{inference_serving_id}` | `{terms[]}` | Wrapped helper |
| `GetServingConfigByWorkspaceId` | `{workspace_id}` | `{configs{}}` | `serving configs` |
| `GetInferenceServingUserProjectList` | `{workspace_id}` | `{projects[], users[]}` | Project/user resolution for `create` |
| `StartServing` | `{inference_serving_id}` | `{inference_serving_id, sub_code, sub_msg}` | `serving start` |
| `StopServing` | `{inference_serving_id}` | `{inference_serving_id}` | `serving stop` |
| `ScaleServing` | `{inference_serving_id, replica}` | — | `serving scale` |
| `RollbackServing` | `{inference_serving_id, version}` | `{inference_serving_id, sub_code, sub_msg}` | `serving rollback` |
| `DeleteServing` | `{inference_serving_id}` | `{inference_serving_id}` | `serving delete` |
| `GetTaskMetric` | See [Section 8.14](#814-metrics--gettaskmetric) | `{time_seris_metric_groups[]}` | `serving metrics` |
| `model_plaza.GetModelListInPlaza` ‡ | `{page, page_size, filter:{workspace_id}}` | `{items[], total_count}` | `serving create --model-source plaza`, `serving batch`, SDK `servings` |

**Serving notes:**
- Always use `CreateServingConsole` (not OpenAPI `CreateServing`). `ScaleServing` takes singular `replica`.
- `CreateServingConsole` sends `port_configs: [{port, type, path}]` rather than scalar `port`: required HTTP uses `PORT_TYPE_HTTP` with `/`, and optional metrics uses `PORT_TYPE_METRICS` with `/metrics` (legacy scalar `port` inputs are translated by the wrapper into the HTTP `port_configs` entry).
- For Model Plaza deployments, `CreateServingConsole` sends `model_source: "MODEL_PLAZA"` with `model_id` resolved from `model_plaza.GetModelListInPlaza` (filtering `PUBLISHED` entries across the complete workspace catalog via `list_plaza_models`) and omits `model_version` (explicit plaza versions are rejected client-side; this QZ Model Plaza endpoint uses Bearer auth and is distinct from the CAS-authenticated Dataset Hub).
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
| `GetWorkspaceQuota` | `{workspace_id}` | `{gpu_high_running, gpu_high_running_used, cpu_*, memory_*, is_fair_workspace, …}` | *(Not wrapped)* |
| `GetWorkspaceComputeResource` | `{workspace_id}` | `{logic_resouces{cpu_total, cpu_used, memory_gi_total, memory_gi_used, gpu_total, gpu_used, gpu_low_priority_used}}` | *(Not wrapped)* |
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
| `ListAccessibleWorkspaces` | `{}` | `{items[{workspace_id, name, workspace_type, is_fair_workspace, is_task_priority}]}` | Workspace enumeration, login bootstrap, and `is_fair_workspace` detection |
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
| `GetProjectBudgetUsageOverview` † | `{project_id, workspace_id?}` | `{used, remain, train, inference, storage, private_workspace}` (thousands-separated strings) | `project detail` |
| `GetProjectOwners` † | `{}` | `{items[{id, name, login_name, …}]}` | `project owners` |
| `GetProjectForPage` | `{page, page_size, filter{}}` | `{items[], total}` | Wrapped helper |

---

### 8.9 `image` — Image Catalog

Referer: `/jobs/interactiveModeling`.

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `ListImages` | `{page: 0, page_size: -1, filter:{…}}` | `{images[], total}` | `image list`, image resolution, `cache refresh` |
| `GetImageById` | `{ImageId}` | `image_id`, `name`, `address`, `framework`, `version`, `source`, `visibility`, `status`, `description`, `created_at` | `image detail`, readiness polling |
| `CreateImage` † | `{name, version, registry_hint:{workspace_id}, visibility, add_method, description}` | `{image{image_id, …}}` | `image register` (`add_method: "DIRECT_PUSH"`) |
| `SyncImage` † | `{workspaceId, imageName, imageVersion}` | — | `image sync` after `docker push` |
| `UpdateImage` † | `{id, visibility?, description?}` | — | `image set-visibility` (requires bare `id`, not `image_id`) |
| `DeleteImage` | `{image_id}` | — | `image delete` |

**Image notes:**
- `CreateImage` with `add_method: "DIRECT_PUSH"` creates a slot in `PENDING_SYNC` (numeric `add_method: 2` maps to `IMAGE_ADDRESS` and is not equivalent). After `docker push`, `SyncImage` (note camelCase `workspaceId`, `imageName`, `imageVersion`) transitions the uploaded image to `SUCCESS`.

---

### 8.10 `model-hub` — Model Registry

Referer: `/jobs/modelService?spaceId={workspace_id}`. Route uses a **hyphen** (`model-hub`).

| Action | Request Keys | Response (`Result`) | CLI Consumer |
| --- | --- | --- | --- |
| `ListModels` | `{workspace_id, page, page_size, filter_by:{user_id, keyword?, project_id[]?, model_type[]?}}` | `{list[], total}` | `model list`, `serving create` model resolution |
| `GetModelDetail` | `{model_id}` | `{model{}, user_name, project_name, user_avatar}` | `model status` |
| `ListModelVersions` | `{model_id}` | `{list[], total, next_version}` | `model versions`, `model status` |
| `ListModelVersionOptions` | `{model_id}` | `{list[], total}` (compact version list) | Wrapped helper (`list_model_versions`) |
| `ListModelRelatedServings` | `{model_id, version, page, page_size}` | `{serving[], total}` | `model status` / `model delete` precheck |
| `GetHasModelPendingServing` | `{model_id, version?}` | `{has_pending_serving}` | `model status` / `model delete` precheck |
| `GetRecommendedConfig` | `{model_id, version}` | `{min_node_count, min_gpu_count_per_node, min_cpu_count_per_node, min_memory_size_gib_per_node}` | `model deploy-config` |
| `CheckModelVLLMCompatible` | `{model_id, version, inference_serving_type}` | `{is_vllm_compatible}` | `model deploy-config` |
| `GetModelVLLMCompatibleData` | `{model_id, inference_serving_type?}` | `{data:[{version, is_vllm_compatible}]}` | `model status`, `model versions` |
| `GetModelPublishPrefill` | `{model_id, version}` | Publish form prefill metadata | Wrapped helper |
| `GetModelPublishStatus` | `{model_id, version}` | Model Plaza publish status | Wrapped helper |
| `ListModelCreators` | `{project_id}` | `{list[]\|items[], total}` | Wrapped helper |
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

Referer: `/jobs/files?spaceId={workspace_id}`. Documented platform route; none of its Actions are wrapped in `browser_api/`:

| Action | Request Keys | Response (`Result`) | Status |
| --- | --- | --- | --- |
| `GetSystemStorageTypeList` | `{filter:{workspace_id}}` | `{system_storages[{name, cluster_id}]}` | Not wrapped |
| `GetDirList` | `{filter:{workspace_id, system_storage_type, name, cluster_id?}}` | `{files[{directory}]}` | Not wrapped |
| `GetSftpgoConnectionInfo` † | `{storage_name}` | `{address, webdav_port, auth}` | **Intentionally not wrapped** (`auth` returns a plaintext base64 `user:password` WebDAV credential) |
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
- **Partial defaults:** CLI default metric queries pass an `errors` collector to the shared wrapper and retain successful responses. Explicit metric selectors and SDK calls remain strict; authentication errors and all-metric failures always propagate.
- **One metric per request:** Passing multiple `metric_types` returns only the first metric, so the wrapper fans out per metric (`gpu_usage_rate`, `gpu_memory_usage_rate`, `cpu_usage_rate`, `memory_usage_rate`, `disk_io_read`, `disk_io_write`, `network_tcp_ip_io_read`, `network_tcp_ip_io_write`).
- **Platform typo:** Response key is spelled `time_seris_metric_groups` (wrapper also accepts `time_series_metric_groups`).
- **Do not use `GetTaskMetricBatch`:** It drops per-Pod `group_name` labels, returns empty disk I/O and `InternalError` on network I/O, and disagrees with single-task aggregation values.

---

### 8.15 Batch Reads — `ListJobs` & `ListJobEvents`

`train` and `hpc` support batch ID queries up to **20 IDs per request** (deduplicate before chunking):
- `ListJobs` with `{workspace_id, job_ids: [...]}` silently drops unknown IDs (diff against requested IDs to find missing jobs).
- `ListJobEvents` with `filter: {object_type, object_ids: [...]}` fails the **entire batch** (`InvalidParameter: job <id> not found`) if any single ID is missing; strip the offending ID and retry the remaining batch. (Pod-level `object_type: "instance"` events use a separate 200-item chunk limit.)

---

## 9. Creation Payload Fields

The matrix below describes the wrapper surface across the five compute workload creation Actions:

| Field | `notebook.CreateNotebook` | `train.CreateJobConsole` † | `hpc.CreateJobConsole` † | `ray.CreateJob` | `inference_serving.CreateServingConsole` † |
| --- | --- | --- | --- | --- | --- |
| `dataset_info` | Exposed | Exposed | Exposed | Not exposed | Not exposed |
| `envs` (`[{name, value}]`) | Not exposed | Exposed (not echoed by `GetJob`) | Not exposed | Not exposed | Not exposed |
| `description` | Not exposed | Exposed | Exposed | Exposed | Exposed |
| `publicpath_mount_mode` (`0`, `1`, `2`) | Exposed | Exposed | Exposed | Exposed | Exposed |
| `is_projectuserspath_readonly` | Exposed (Maintainer only) | Not exposed | Not exposed | — | — |

**Key payload rules:**
- **Public-path mount contract (`publicpath_mount_mode`):** Top-level integer enum (`0` read-write, `1` read-only, `2` not mounted), exposed via CLI `--public-path-mount-mode` and SDK/batch `public_path_mount_mode` (`PublicPathMountMode`). Omitted when neither option is specified so the platform applies its default `0`. Never send `is_publicpath_readonly` on creation (`hpc.CreateJobConsole` rejects it as an unknown field); deprecated `public_path_readonly=True/False` maps to `1/0` with a single `DeprecationWarning`, and detail readers fall back to `publicpath_mount_mode ?? (is_publicpath_readonly ? 1 : 0)`.
- **Omit unset optional fields:** Optional fields (such as `publicpath_mount_mode` or `is_projectuserspath_readonly`) are omitted from the JSON body when unset.
- **HPC runtime limits & priority:** `job_max_time` (`"D-HH:MM:SS"`) and `max_running_time_days`/`_hours`/`_minutes` live inside `sbatch_script`. Top-level `priority` is required (missing `priority` triggers `InternalError`). Never send `sbatch_script.memory_per_node` (broken in the platform script generator); always send `memory_per_cpu`.
- **Durations as strings:** `train.CreateJobConsole` expects `max_running_time_ms`, `reserve_on_fail_ms`, and `reserve_on_success_ms` as **string** integers.
- **Images:** Pass registry URLs (`image` / `mirror_url`) or `mirror_id` as required by each Action, never display names.

### Workload Creation Name Rules

Single-create, batch-expanded, and `--dry-run` names are validated locally in [`workload_names.py`](../../cli/inspire/services/utils/workload_names.py) before platform resolution or submission (`ValidationError` / exit 12; names are never silently truncated or normalized):

| Workload | Max Length | Full-Match Pattern | Console Form Validator Evidence |
| --- | --- | --- | --- |
| Notebook | 32 | `[a-z][a-z0-9-]{0,31}` | `interactiveModel`: `name:"name"`, `max:32`, `/^[a-z][a-z0-9-]{0,31}$/g`, `maxLength:32` |
| Ray (and worker groups) | 32 | `[a-z][a-z0-9-]{0,31}` | `rayService`: task and worker `name:"name"`, `max:32`, same regex, `maxLength:32` |
| Serving | 32 | `[a-z][a-z0-9.-]*` | Serving form: `/^[a-z][a-z0-9-.]*$/`, `M.length>32`, `maxLength:32` |
| Job / HPC | 255 | `[a-zA-Z0-9\u4e00-\u9fa5._-]{1,255}` | `distributed` and `hpc` pattern map + task-name input `maxLength:255` |
| TensorBoard | 255 | `[a-zA-Z0-9\u4e00-\u9fa5._-]{1,255}` | TensorBoard create form: same regex validator, `maxLength:255` (the 64-char limit in `create-sandbox-key-form` is unrelated despite sharing a translation key) |

---

## 10. Non-Action `/api/v2` Endpoints (WebSocket PTY & Notebook Proxy)

### Instance PTY WebSockets (`pty_socket.py`)

Implemented in [`pty_socket.py`](../../cli/inspire/platform/web/pty_socket.py) (used by CLI [`job_shell.py`](../../cli/inspire/cli/utils/job_shell.py) and SDK [`remote_exec.py`](../../cli/inspire/sdk/remote_exec.py)):

| Workload | Route | Workload ID Query Param | Instance Query Param |
| --- | --- | --- | --- |
| `job` | `/api/v2/train_job/remote_cmd` | `job_id` | `instance_name` |
| `hpc` | `/api/v2/hpc_jobs/instances/exec` | `job_id` | `instance_id` |
| `ray` | `/api/v2/ray_job/instances/exec` | `job_id` | `instance_id` |
| `serving` | `/api/v2/inference_servings/instances/exec` | **`inference_serving_id`** | `instance_id` |

### Notebook Lab Redirect & Reverse Proxy

`GET {base_url}/api/v2/notebook/lab/{notebook_id}` returns `301` with a token-bearing gateway URL (`https://<gateway>/ws-…/project-…/user-…/jupyter/<notebook>/<token>/lab`).
- Container port proxying (`/proxy/{port}/`) works **only** on that token-bearing gateway URL (used by `notebook proxy-url` and by `rtunnel` SSH bridging on port 22222).
- Always scrub the embedded gateway token in logs and errors via `redact_proxy_url` in [`browser_api/rtunnel.py`](../../cli/inspire/platform/web/browser_api/rtunnel.py).

---

## 11. Dataset Hub (`plaza/`)

The Dataset Hub (`PLAZA_BASE_URL` in [`plaza/core.py`](../../cli/inspire/platform/web/plaza/core.py)) is a separate service sharing CAS SSO (`CAS_BASE_URL`), authenticated via per-account `cas_session.json` (`inspire login --cas feishu` or `inspire login --cas sms`). CAS cookies are scoped to `cas.sii.edu.cn` and saved atomically with private permissions; SMS pending state expires after 10 minutes without persisting phone numbers or codes, and Feishu QR login uses headless Chromium to intercept the CAS redirect before the plaza SPA consumes the ticket.

### Two Identifier Sets
- **`datasetCode` / `versionCode`:** User-facing slugs displayed by the CLI and required by `dataset.ValidateDataset` and `--dataset`.
- **`datasetId` / `versionId`:** Internal numeric IDs used only inside `plaza/` calls (`findDatasets`); never expose them or pass them to mount APIs.

### Three-Step CAS Handshake (Pure HTTP)
1. `GET {CAS_BASE_URL}/cas/login?service=<urlencode(PLAZA_BASE_URL + "/")>` with the account's stored CAS `CASTGC` cookie (`allow_redirects=False`) → `302` with `?ticket=ST-…`.
2. `POST {PLAZA_BASE_URL}/api/base/login` with `{"ticket": "…", "service": PLAZA_BASE_URL + "/"}` → issues `datasets-session` cookie and `userInfo`.
3. Subsequent requests send `datasets-session` (cached in memory per `(account, session_created_at)`). A rejected plaza cookie is re-minted once; if CAS refuses a service ticket, raise `PlazaUnavailable` immediately with a `login --cas` hint without deleting caches or refreshing OAuth tokens.

CAS also auto-approves OAuth device authorization by following the verification URI to its Keycloak CAS broker and submitting the consent form (`code`, `accept=Yes`, supporting both embedded `kcContext` and HTML forms), validating every redirect and form origin against the configured platform, Keycloak, and CAS hosts.

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
4. **Distinguish errors from empty catalogs:** Never turn transport or authentication failures into `[]` or `0`, and cover failure paths with unit tests.
5. **Synchronize docs and tests:** Update Click `--help`, `cli/tests/test_browser_api_*.py`, and the tables in this file together.
