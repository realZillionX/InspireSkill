# Python SDK Reference

The `inspire-skill` package includes a typed Python SDK alongside the `inspire` CLI:
- **`InspireClient`** — synchronous client for scripts, CLI tools, and synchronous workers.
- **`InspireAsyncClient`** — native `asyncio` client for async applications, agent runtimes, and web services.
- **`Accounts`** — standalone synchronous helper for managing local account profiles under `~/.inspire/accounts/<alias>/`.

All three are importable from `inspire` or `inspire.sdk` and share the same resource models, typed references, exceptions, and platform services.

## 1. Quickstart

Install from PyPI (`pip install inspire-skill`) or from a local checkout (`uv pip install -e ./cli`). Runtime dependencies (`requests[socks]`, `httpx[socks]`, `greenlet`, etc.) are installed automatically.

**Synchronous (`InspireClient`):**

```python
import sys
from inspire import InspireClient

def main(account: str, workspace: str) -> None:
    with InspireClient(account=account) as client:
        client.login()
        ws = client.workspaces.get(workspace)
        for job in client.jobs.iter(ws.ref, max_items=40):
            print(job.name, job.status)

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

**Asynchronous (`InspireAsyncClient`):**

```python
import asyncio
import sys
from inspire import InspireAsyncClient

async def main(account: str, workspace: str) -> None:
    async with InspireAsyncClient(account=account) as client:
        await client.login()
        jobs, notebooks = await asyncio.gather(
            client.jobs.list(workspace, limit=5),
            client.notebooks.list(workspace, limit=5),
        )
        print("Jobs:", [job.name for job in jobs.items])
        print("Notebooks:", [nb.name for nb in notebooks.items])
        async for job in client.jobs.iter(workspace, max_items=10):
            print(job.name, job.status)

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
```

## 2. Accounts & Sessions

### Local Account Management (`Accounts`)

`Accounts` manages `~/.inspire/accounts/<alias>/` directly without constructing a client or making network calls (`InspireClient.accounts` and `InspireAsyncClient.accounts` expose the same synchronous class):

| Method | Description |
| --- | --- |
| `Accounts.list()` | Return a sorted tuple of configured account aliases |
| `Accounts.current()` | Return the saved default account alias on disk (`None` if unset) |
| `Accounts.exists(name)` | Return whether the account alias exists |
| `Accounts.config_path(name)` | Return the `Path` to the account's `config.toml` |
| `Accounts.add(name, *, base_url=DEFAULT_BASE_URL, proxy=None, use=None, overwrite=False)` | Create an account profile; activates it when no account is active unless `use=False`; `use=True` forces activation (`overwrite=True` replaces the account directory and its caches) |
| `Accounts.use(name)` | Set the persistent default account on disk |
| `Accounts.rename(old, new)` | Rename an account alias and migrate its Notebook target caches |
| `Accounts.remove(name)` | **Clear tokens under the refresh lock and delete the entire local account directory without confirmation** (equivalent to CLI `logout --yes` / `account remove --yes`, including bridge/tunnel state; platform workloads keep running) |

```python
from inspire import Accounts, InspireClient

Accounts.add("research", use=False)
# Authorize this profile first via CLI: inspire login --account research
with InspireClient(account="research") as client:
    identity = client.login()
    result = client.init()
    print(result.config_path, result.changed, result.warnings)
```

### Client Construction, `login()`, and `init()`

- **Constructors:**
  - `InspireClient(account=None, *, username=None, password=None, base_url=None, proxy=None, allow_browser=False, timeout=30, operation_timeout=120, catalog_ttl=60, catalog_disk_cache=False)`
  - `InspireAsyncClient(account=None, *, username=None, password=None, base_url=None, proxy=None, allow_browser=False, timeout=30, operation_timeout=120, catalog_ttl=60, catalog_disk_cache=False, concurrency=None)`
  - Clients load the named `account` (or the saved active account when `account=None`). Constructing a client never changes the saved default account. Deprecated `username` / `password` arguments on `Accounts.add`, client constructors, and `from_credentials` emit `DeprecationWarning` and are ignored. Create and authorize accounts via `inspire login --account <name>` (`inspire account add <name>` also creates a profile).
  - `InspireClient` pins its account at construction time; `InspireAsyncClient` loads local account config on entering `async with` or on its first method call (accessing `.account` or `.base_url` beforehand raises `ConfigurationError`).
- **`login(*, force=False) -> AccountInfo`** (async: `await client.login(force=False)`):
  - Authorize the account beforehand with `inspire login --account <alias>`; the SDK never triggers interactive device login.
  - Cached OAuth tokens in `web_session.json` refresh automatically under a per-account file lock with a disk reread, persisting rotated refresh tokens atomically.
  - `AccountCheck.oauth["refresh_kind"]` distinguishes `"offline"` refresh tokens (no fixed expiry, `refresh_expires_at` is `None`, subject to server-side idle timeout and revocation; the local 30-day estimate never blocks refresh) from `"session"` refresh tokens (rolling 12-hour idle timeout and 7-day session cap). When refresh credentials expire or are rejected, a valid stored CAS session (`cas_session.json`) silently renews authorization; otherwise run `inspire login`. Displayed identity comes from `WebSession.login_username`.
  - `allow_browser=True` enables Playwright-backed requests for non-login operations. Device authorization never requires Chromium.
- **`init(*, force=False) -> InitResult`** (async: `await client.init(force=False)`):
  - Uses an existing OAuth session, verifies at least one accessible Workspace, and atomically writes normalized account config (`force=False` preserves custom TOML sections while stripping deprecated `[auth]` fields; `force=True` rebuilds from template). Interactive login prompts and `ssh-keygen` remain CLI-only (`inspire init`).

## 3. Concurrency, Cancellation & Lifecycle

- **Thread and task safety:**
  - Use each synchronous `InspireClient` within a single thread and process (close with `with` or `client.close()`).
  - Share a single `InspireAsyncClient` across concurrent tasks on the same event loop (`asyncio.gather(...)` overlaps native async HTTP, WebSocket, and SSH/SCP subprocess I/O; close with `async with` or `await client.close()`).
  - The `concurrency` parameter on `InspireAsyncClient` is deprecated (positive integers emit `DeprecationWarning` as a no-op; non-positive values raise `ValidationError`). Use `asyncio.Semaphore` for application-level concurrency throttling.
- **Local I/O offload pool:** `InspireAsyncClient` offloads blocking local disk, SQLite, file-lock, and certificate operations to a per-client thread pool capped at **4 workers** (network concurrency is unrestricted). Initial client setup, direct `Accounts` calls, synchronous `on_output` callbacks, and in-memory payload parsing run on the event loop thread.
- **Stream iteration:** `iter`, `follow_events`, `follow_logs`, and `exec_stream` are async iterators on `InspireAsyncClient` — wrap them in `contextlib.aclosing(...)` when breaking early so underlying connections close deterministically.
- **Bounded multi-ref status:** `await client.ray.status(refs)` and `await client.servings.status(refs)` cap concurrent in-flight reads at **8** per call (`jobs.status` and `hpc.status` use server-side batch endpoints in chunks of 20).
- **Cancellation:** Canceling an async task aborts in-flight network/WebSocket waits and terminates local SSH/SCP subprocesses while allowing any active local file/SQLite lock operation to finish cleanly. **Canceling a local Python call or `wait()` never stops a remote workload on the platform.**

## 4. Resource References, Pagination & Handles

### Pagination & Disambiguation

- Collection methods follow `list(workspace, *, ...filters, limit=20, cursor=None)` and `iter(workspace, *, ...filters, max_items=None)`.
- `Page` exposes `.items`, `.next_cursor`, and `.total`. Pass `cursor=page.next_cursor` with the same filters to fetch the next page.
- Workload listings and name scans enforce a 5,000-row ceiling (`page_num × page_size <= 5000` and max 100 pages); exceeding this raises `ResolutionIncompleteError` (narrow with `status=` / `keyword=` or pass a typed `Ref`).
- `servings.plan` and `servings.create` scan up to 100 pages of 100 filtered models when resolving a registered model name to catch cross-page duplicates; incomplete catalogs raise `ResolutionIncompleteError`.
- When a name matches multiple resources, `AmbiguousResourceError` is raised with `.candidates` containing the full resource model objects (e.g., `Job`, `Image`, `ModelInfo`). Select the desired candidate and pass `candidate.ref` to subsequent calls.
- Typed `Ref` objects (`JobRef`, `NotebookRef`, etc.) are frozen, serializable via `.to_dict()` / `XRef.from_dict()`, and validate account, base URL, and workspace consistency.

### Status Normalization

Both the CLI and SDK `.view` projections normalize workload statuses to uppercase (`UNKNOWN` when empty or unrecognized):
- **Jobs:** `job_running` → `RUNNING`, `CREATING` / `job_creating` → `PENDING`, `STOPPED` / `job_stopped` → `CANCELLED`.
- **HPC, Ray, Serving, Notebook, TensorBoard:** Normalized to uppercase while preserving `STOPPED` (TensorBoard strips the `tb_status_` prefix).

### Submission Handles & `await handle`

Creating a workload or image returns a handle containing `.ref`:
- On `InspireClient`, call `client.<facade>.wait(handle.ref)` explicitly.
- On `InspireAsyncClient`, `create`, `save_image`, and `images.register` return awaitable `Async*Handle` subclasses:

| Async Call | Returned Handle | `await handle` Equivalent |
| --- | --- | --- |
| `jobs.create` | `AsyncJobHandle` | `await client.jobs.wait(handle.ref)` |
| `hpc.create` | `AsyncHPCJobHandle` | `await client.hpc.wait(handle.ref)` |
| `ray.create` | `AsyncRayJobHandle` | `await client.ray.wait(handle.ref)` |
| `servings.create` | `AsyncServingHandle` | `await client.servings.wait(handle.ref)` |
| `tensorboards.create` | `AsyncTensorboardHandle` | `await client.tensorboards.wait(handle.ref)` |
| `notebooks.create` | `AsyncNotebookHandle` | `await client.notebooks.wait(handle.ref)` |
| `notebooks.save_image` | `AsyncImageSaveHandle` | `await client.notebooks.wait_image_ready(handle)` |
| `images.register` | `AsyncImageRegisterHandle` | `await client.images.wait_ready(handle.ref)` |

```python
finished = await handle
finished = await handle.wait(timeout=7200, raise_on_failure=True)
finished_all = await asyncio.gather(*handles)
```

- **Important:** Workload `wait()` defaults to `raise_on_failure=False`, returning the finished resource object even if its status is `FAILED` or `CANCELLED`. Pass `raise_on_failure=True` to raise `JobFailedError`, `HPCJobFailedError`, `RayJobFailedError`, `NotebookFailedError`, `ServingFailedError`, or `TensorboardFailedError` (carrying `.job`, `.notebook`, `.serving`, or `.tensorboard`).
- Use `handle.future()` when passing handles to `asyncio.wait(...)` or `asyncio.as_completed(...)` on Python 3.11+.
- To reattach a serialized `Ref` into an awaitable handle on `InspireAsyncClient` without making a network call, call `await client.<facade>.bind_ref(ref)` (or `await client.notebooks.bind_image_ref(image_ref, notebook=notebook_ref)`).

### Catalog Caching (`catalog_disk_cache`)

- By default (`catalog_disk_cache=False`), each client uses an in-memory snapshot cache with `catalog_ttl=60` seconds (`0` disables caching).
- Setting `catalog_disk_cache=True` shares the per-account SQLite index (`resource-index.sqlite3`) with the CLI for `workspaces`, `projects`, `compute_groups`, `images`, and `prices` (`quota-*`), plus `sdk-catalog-v1.json` for `priority_levels`, `fair_scheduling`, and `current_user`.
- `client.cache.stats()` (async: `await client.cache.stats()`) returns `hits`, `misses`, `entries` (plus `shared_hits` when disk cache is enabled); `client.cache.clear()` clears the cache.

## 5. Observation Models

Nine observation methods return frozen dataclass models (or tuples/`Page`s of them), all exported from `inspire` and `inspire.sdk`:

| Method | Return Type |
| --- | --- |
| `servings.versions` | `tuple[ServingVersion, ...]` |
| `servings.scale_history` | `Page[ServingScaleHistoryEntry]` |
| `servings.configs` | `ServingConfigs` (contains `tuple[ServingConfigItem, ...]`) |
| `servings.api` | `ServingInvocationInfo` (inherits `ServingInvocationCredentials`) |
| `servings.api_metrics` | `ServingAPIMetrics` (`ServingAPIMetricTimeRange` + `tuple[ServingAPIMetricSeries, ...]`) |
| `tensorboards.tags` | `TensorboardTags` |
| `tensorboards.scalars` | `TensorboardScalars` (`tuple[TensorboardScalarSeries, ...]` + `tuple[TensorboardScalarPoint, ...]`) |
| `ray.scaling` | `tuple[RayScalingEvent, ...]` |
| `notebooks.lifecycle` | `tuple[NotebookRun, ...]` |

```python
for version in client.servings.versions(serving_ref):
    print(version.version, version.status)
for series in client.tensorboards.scalars(board_ref, points=10).series:
    print(series.run, series.tag, series.last_value)
    for point in series.points:
        print(point.step, point.value)
```

## 6. Facade Method Reference

`InspireClient` and `InspireAsyncClient` share the resource facades below. On `InspireAsyncClient`, regular methods are coroutines (`await ...`), `iter` / `follow_events` / `follow_logs` are async iterators (`async for ...`), and workloads with `exec` also provide `exec_stream(...) -> AsyncIterator[str]`.

### workspaces

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `workspaces.get(ref)` | `Resource[WorkspaceRef]` | `account context` (name lookup) |
| `workspaces.list(*, limit=20, cursor=None)` | `Page[Resource[WorkspaceRef]]` | `account context` |

### projects

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `projects.detail(ref, *, workspace=None)` | `ProjectDetail` | `project detail` |
| `projects.get(ref, *, workspace=None)` | `ProjectInfo` | `project list` (name lookup) |
| `projects.list(workspace=None, *, limit=20, cursor=None)` | `Page[ProjectInfo]` | `project list` |
| `projects.owners()` | `tuple[ProjectOwner, ...]` | `project owners` |

### compute_groups

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `compute_groups.get(ref, *, workspace=None)` | `Resource[ComputeGroupRef]` | `account context` (compute group lookup) |
| `compute_groups.list(workspace, *, limit=20, cursor=None)` | `Page[Resource[ComputeGroupRef]]` | `account context` |

### images

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `images.delete(ref, *, workspace=None)` | `None` | `image delete` |
| `images.detail(ref, *, workspace=None)` | `ImageDetail` | `image detail` |
| `images.get(ref, *, workspace=None)` | `Image` | `image list` (name lookup) |
| `images.list(workspace, *, source=None, keyword=None, limit=20, cursor=None)` | `Page[Image]` | `image list` |
| `images.register(name, *, workspace, version=None, description=None, visibility=None, operation_id=None)` | `ImageRegisterHandle` | `image register` |
| `images.set_visibility(ref, *, visibility, workspace=None)` | `None` | `image set-visibility` |
| `images.sync(ref, *, workspace=None)` | `None` | `image sync` |
| `images.wait_ready(ref, *, timeout=600, poll_interval=5, workspace=None)` | `CustomImageInfo` | `image sync --wait` (after sync) |

For external Docker images, call `images.register` (which reserves a `PENDING_SYNC` slot), push the image to the returned registry address, then call `images.sync(handle.ref)` followed by `images.wait_ready(handle.ref)`.

### datasets

Catalog methods (`applications`, `get`, `list`, `tags`) require a stored CAS session (`inspire login --cas feishu --account <name>` or `--cas sms --phone <number>`). `datasets.validate` uses the regular OAuth Bearer session.

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `datasets.applications(name=None, *, to_approve=False, keyword=None, limit=20, cursor=None)` | `Page[DatasetApplication]` | `dataset applications` |
| `datasets.get(ref)` | `DatasetDetail` | `dataset show` |
| `datasets.list(keyword=None, *, tag=None, limit=20, cursor=None)` | `Page[DatasetInfo]` | `dataset list` |
| `datasets.tags()` | `tuple[DatasetTag, ...]` | `dataset tags` |
| `datasets.validate(specs, *, workspace)` | `tuple[DatasetValidation, ...]` | `dataset validate` |

### models

Read and lookup methods on `models` accept a specific Workspace (`str | WorkspaceRef`) or `"all"` for cross-workspace resolution (`register` requires a single target Workspace).

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `models.delete(ref, *, force=False, workspace=None, project=None)` | `None` | `model delete` |
| `models.deploy_config(ref, *, workspace=None, project=None, version=None)` | `ModelDeployConfig` | `model deploy-config` |
| `models.detail(ref, *, workspace=None, project=None)` | `ModelStatus` | `model status` |
| `models.get(ref, *, workspace=None, project=None)` | `ModelInfo` | `model list` (name lookup) |
| `models.list(workspace, *, project=None, keyword=None, limit=20, cursor=None)` | `Page[ModelInfo]` | `model list` |
| `models.register(name, *, source_path, workspace, project, type=None, tag=None, description=None, operation_id=None)` | `ModelRegisterHandle` | `model register` |
| `models.status(refs, *, workspace=None, project=None)` | `tuple[ModelInfo, ...]` | `model list` (per-ref status) |
| `models.versions(ref, *, workspace=None, project=None)` | `tuple[ModelVersion, ...]` | `model versions` |

### resources

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `resources.availability(workspace, *, group=None, include_cpu=False)` | `tuple[ResourceAvailability, ...]` | `resources availability` |
| `resources.node_events(nodes, *, since=None, type=None, reason=None, limit=None, from_component=None)` | `EventResult` | `resources node-events` |
| `resources.policy(workspace, *, workload=None)` | `tuple[WorkloadSchedulePolicy, ...]` | `resources policy` |
| `resources.usage(workspace, *, project=None, user=None, task=None, group=None, mine=False, details=False, limit=None)` | `ResourceUsage` | `resources usage` |

### account_info

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `account_info.check()` | `AccountCheck` | `account check` |
| `account_info.context(*, limit=None)` | `AccountContext` | `account context` |
| `account_info.current()` | `AccountInfo` | `account current` |
| `account_info.permissions(workspace=None)` | `tuple[Permission, ...]` | `account permissions` (`workspace=None` or `"all"` queries all workspaces) |

`account_info.check()` validates local config and live OAuth authentication (reading local `cas` cache metadata without making network requests to CAS):
- Includes `ok`, `identity`, `session_created_at`, `issues`, `oauth`, `cas`, and `refreshed`.
- When live token refresh is explicitly rejected by the server, `oauth["state"] == "rejected"` and `oauth["refresh_error"]` holds an allowlisted OAuth error code.
- When a network or transient error occurs during session validation or refresh, `account_info.check()` returns `AccountCheck(ok=False, ...)` with `oauth["refresh_retryable"] == True`, `oauth["refresh_error"]`, and `oauth["refresh_status"]` (HTTP status or `None` when no HTTP response arrived) instead of demanding re-login. Across other SDK methods, transport session acquisition and refresh map network and transient failures to `TransportError(..., retryable=True)`, prioritizing active network failures over chained CAS causes.

### api_keys

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `api_keys.create(name)` | `APIKeyInfo` | `account api-key create` |
| `api_keys.delete(ref)` | `None` | `account api-key delete` |
| `api_keys.get(ref)` | `APIKeyInfo` | `account api-key list` (name lookup) |
| `api_keys.list(*, limit=20, cursor=None)` | `Page[APIKeyInfo]` | `account api-key list` |
| `api_keys.plaintext(ref)` | `str` | `account api-key export --stdout` |

### jobs

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `jobs.command(ref, *, workspace=None)` | `str` | `job command` |
| `jobs.create(spec, *, operation_id=None)` | `JobHandle` | `job create` |
| `jobs.delete(ref, *, workspace=None)` | `None` | `job delete` |
| `jobs.events(ref, *, workspace=None, type=None, reason=None, instance=None, workload_level=False, limit=100)` | `EventResult` | `job events` |
| `jobs.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `job shell` (non-interactive) |
| `jobs.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]` / `AsyncIterator[EventResult]` | `job events --follow` |
| `jobs.follow_logs(ref, *, interval=2, **filters)` | `Iterator[LogResult]` / `AsyncIterator[LogResult]` | `job logs --follow` |
| `jobs.get(ref, *, workspace=None)` | `Job` | `job status` |
| `jobs.instance_names(ref, *, workspace=None)` | `tuple[str, ...]` | `job instances` |
| `jobs.instances(ref, *, workspace=None)` | `tuple[Instance, ...]` | `job instances` |
| `jobs.iter(workspace, *, status=None, keyword=None, max_items=None)` | `Iterator[Job]` / `AsyncIterator[Job]` | `job list --all` |
| `jobs.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `Page[Job]` | `job list` |
| `jobs.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=100, max_chars=None)` | `LogResult` | `job logs` |
| `jobs.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval='1m', group=None)` | `tuple[MetricGroup, ...]` | `job metrics` |
| `jobs.plan(spec)` | `JobPlan` | `job create --dry-run` |
| `jobs.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `job quota` |
| `jobs.status(refs, *, workspace=None, allow_partial=False)` | `tuple[Job, ...]` or `tuple[StatusResult[Job], ...]` | `job status` |
| `jobs.stop(ref, *, workspace=None)` | `None` | `job stop` |
| `jobs.wait(ref, *, workspace=None, timeout=3600, poll_interval=10, raise_on_failure=False)` | `Job` | `job wait` |

#### Partial Batch Status (`allow_partial=True` on `jobs.status` and `hpc.status`)

By default (`allow_partial=False`), `jobs.status(refs)` and `hpc.status(refs)` return `tuple[Job, ...]` / `tuple[HPCJob, ...]` and raise `ResourceNotFoundError` if any resolved reference is omitted from the batch response.

Pass `allow_partial=True` to receive one frozen `StatusResult[Job]` / `StatusResult[HPCJob]` per input in the exact input order (preserving duplicate references, without issuing per-item fallback requests):
- `.ref` and `.name`: the requested reference and name (`StatusResult.to_dict()` serializes public name, outcome, sanitized snapshot, and error message without opaque IDs).
- `.outcome == "status"`: `.value` holds the `Job` / `HPCJob` snapshot; `.error` is `None`.
- `.outcome == "missing"`: the batch response omitted the item; both `.value` and `.error` are `None`.
- `.outcome == "error"`: workspace ownership validation failed on the returned item; `.error` holds `ValidationError` and `.value` is `None`.

Pre-batch reference/name resolution and whole-batch transport, authentication, permission, or rate-limit errors still raise normally.

```python
results = client.jobs.status(refs, allow_partial=True)
for item in results:
    if item.outcome == "status":
        print(item.name, item.value.status)
    elif item.outcome == "missing":
        print(item.name, "missing or no longer visible")
    else:
        print(item.name, item.error)
```

Like `HPCJob`, `RayJob`, and `Serving`, `Job` exposes `.view` (the sanitized public projection matching `inspire --json job status`, also returned by `.to_dict()`) and `.raw` (the full platform payload). `instances()` on `jobs`, `hpc`, `ray`, and `servings` returns frozen `Instance` objects where `.label` is the human-readable instance label and `.handle` is the opaque namespaced pod identifier (hidden from `repr()`); both `exec(instance=...)` and `logs(instance=...)` accept either `.label` or `.handle`.

### notebooks

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `notebooks.cancel_save_image(ref, *, workspace=None)` | `bool` | `notebook cancel-save-image` |
| `notebooks.create(spec, *, operation_id=None)` | `NotebookHandle` | `notebook create` |
| `notebooks.delete(ref, *, workspace=None)` | `None` | `notebook delete` |
| `notebooks.download(ref, *, local, remote, workspace=None, transport='auto', recursive=False, overwrite=True, timeout=120, max_bytes=16777216)` | `TransferResult` | `notebook scp -d` (plus Jupyter Contents API) |
| `notebooks.estimate_image_size(ref, *, workspace=None)` | `NotebookImageSizeEstimate` | `notebook save-image --dry-run` |
| `notebooks.events(ref, *, keyword=None, limit=100, workspace=None)` | `EventResult` | `notebook events` |
| `notebooks.exec(ref, *, command, workspace=None, cwd=None, env=None, timeout=120, transport='auto', on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `notebook exec` |
| `notebooks.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]` / `AsyncIterator[EventResult]` | `notebook events --follow` |
| `notebooks.get(ref, *, workspace=None)` | `Notebook` | `notebook status` |
| `notebooks.iter(workspace, *, status=None, keyword=None, max_items=None)` | `Iterator[Notebook]` / `AsyncIterator[Notebook]` | `notebook list --all` |
| `notebooks.lifecycle(ref, *, limit=None, workspace=None)` | `tuple[NotebookRun, ...]` | `notebook lifecycle` |
| `notebooks.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `Page[Notebook]` | `notebook list` |
| `notebooks.metrics(ref, *, metric='core', window='1h', start=None, end=None, interval=None, group=None, workspace=None)` | `tuple[MetricGroup, ...]` | `notebook metrics` |
| `notebooks.plan(spec)` | `NotebookPlan` | SDK single-create dry-run (`notebook batch --dry-run` in CLI) |
| `notebooks.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `notebook quota` |
| `notebooks.realtime_metrics(ref, *, workspace=None)` | `tuple[NotebookResourceSnapshot, ...]` | `notebook metrics --now` |
| `notebooks.save_image(ref, *, name, version=None, description=None, visibility=None, flatten=False, workspace=None)` | `ImageSaveHandle` | `notebook save-image` |
| `notebooks.start(ref, *, workspace=None)` | `None` | `notebook start` |
| `notebooks.status(refs, *, workspace=None)` | `tuple[Notebook, ...]` | `notebook status` |
| `notebooks.stop(ref, *, workspace=None)` | `None` | `notebook stop` |
| `notebooks.upload(ref, *, local, remote, workspace=None, transport='auto', recursive=False, overwrite=True, timeout=120, max_bytes=16777216)` | `TransferResult` | `notebook scp` (plus Jupyter Contents API) |
| `notebooks.wait(ref, *, timeout=600, poll_interval=5, target='RUNNING', raise_on_failure=False, workspace=None)` | `Notebook` | `notebook create --wait` |
| `notebooks.wait_image_ready(ref, *, timeout=600, poll_interval=5, workspace=None)` | `CustomImageInfo` | `notebook save-image --wait` |

### hpc

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `hpc.create(spec, *, operation_id=None)` | `HPCJobHandle` | `hpc create` |
| `hpc.delete(ref, *, workspace=None)` | `None` | `hpc delete` |
| `hpc.events(ref, *, workspace=None, reason=None, instance=None, workload_level=False, limit=100)` | `EventResult` | `hpc events` |
| `hpc.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `hpc shell` (non-interactive) |
| `hpc.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]` / `AsyncIterator[EventResult]` | `hpc events --follow` |
| `hpc.get(ref, *, workspace=None)` | `HPCJob` | `hpc status` |
| `hpc.instance_names(ref, *, workspace=None)` | `tuple[str, ...]` | `hpc instances` |
| `hpc.instances(ref, *, workspace=None)` | `tuple[Instance, ...]` | `hpc instances` |
| `hpc.iter(workspace, *, status=None, keyword=None, max_items=None)` | `Iterator[HPCJob]` / `AsyncIterator[HPCJob]` | `hpc list --all` |
| `hpc.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `Page[HPCJob]` | `hpc list` |
| `hpc.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=None)` | `LogResult` | `hpc logs` |
| `hpc.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval=None, group=None)` | `tuple[MetricGroup, ...]` | `hpc metrics` |
| `hpc.plan(spec)` | `HPCJobPlan` | `hpc create --dry-run` |
| `hpc.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `hpc quota` |
| `hpc.status(refs, *, workspace=None, allow_partial=False)` | `tuple[HPCJob, ...]` or `tuple[StatusResult[HPCJob], ...]` | `hpc status` |
| `hpc.stop(ref, *, workspace=None)` | `None` | `hpc stop` |
| `hpc.wait(ref, *, timeout=3600, poll_interval=10, raise_on_failure=False, workspace=None)` | `HPCJob` | `hpc status` (poll until terminal) |

### ray

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `ray.create(spec, *, operation_id=None)` | `RayJobHandle` | `ray create` |
| `ray.delete(ref, *, workspace=None)` | `None` | `ray delete` |
| `ray.events(ref, *, workspace=None, type=None, reason=None, instance=None, workload_level=False, limit=100)` | `EventResult` | `ray events` |
| `ray.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `ray shell` (non-interactive) |
| `ray.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]` / `AsyncIterator[EventResult]` | `ray events --follow` |
| `ray.get(ref, *, workspace=None)` | `RayJob` | `ray status` |
| `ray.instance_names(ref, *, workspace=None)` | `tuple[str, ...]` | `ray instances` |
| `ray.instances(ref, *, workspace=None)` | `tuple[Instance, ...]` | `ray instances` |
| `ray.iter(workspace, *, status=None, keyword=None, max_items=None)` | `Iterator[RayJob]` / `AsyncIterator[RayJob]` | `ray list --all` |
| `ray.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `Page[RayJob]` | `ray list` |
| `ray.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=None)` | `LogResult` | `ray logs` |
| `ray.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval=None, group=None)` | `tuple[MetricGroup, ...]` | `ray metrics` |
| `ray.plan(spec)` | `RayJobPlan` | `ray create --dry-run` |
| `ray.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `ray quota` |
| `ray.scaling(ref, *, group=None, limit=None, workspace=None)` | `tuple[RayScalingEvent, ...]` | `ray scaling` |
| `ray.start(ref, *, workspace=None)` | `None` | `ray start` |
| `ray.status(refs, *, workspace=None)` | `tuple[RayJob, ...]` | `ray status` |
| `ray.stop(ref, *, workspace=None)` | `None` | `ray stop` |
| `ray.wait(ref, *, timeout=3600, poll_interval=10, raise_on_failure=False, workspace=None)` | `RayJob` | `ray status` (poll until terminal) |

### servings

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `servings.api(ref, *, affinity_key=None, workspace=None)` | `ServingInvocationInfo` | `serving api` |
| `servings.api_metrics(ref, *, metric=None, window='1h', interval=None, workspace=None)` | `ServingAPIMetrics` | `serving api-metrics` |
| `servings.configs(workspace)` | `ServingConfigs` | `serving configs` |
| `servings.create(spec, *, operation_id=None)` | `ServingHandle` | `serving create` |
| `servings.delete(ref, *, workspace=None)` | `None` | `serving delete` |
| `servings.events(ref, *, workspace=None, type=None, reason=None, instance=None, workload_level=False, limit=100)` | `EventResult` | `serving events` |
| `servings.exec(ref, *, command, workspace=None, instance=None, cwd=None, env=None, timeout=120, on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `serving shell` (non-interactive) |
| `servings.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]` / `AsyncIterator[EventResult]` | `serving events --follow` |
| `servings.get(ref, *, workspace=None)` | `Serving` | `serving status` |
| `servings.instance_names(ref, *, workspace=None)` | `tuple[str, ...]` | `serving instances` |
| `servings.instances(ref, *, workspace=None)` | `tuple[Instance, ...]` | `serving instances` |
| `servings.iter(workspace, *, project=None, status=None, keyword=None, max_items=None)` | `Iterator[Serving]` / `AsyncIterator[Serving]` | `serving list --all` |
| `servings.list(workspace, *, project=None, status=None, keyword=None, limit=20, cursor=None)` | `Page[Serving]` | `serving list` |
| `servings.logs(ref, *, workspace=None, instance=None, window=None, start=None, end=None, tail=None, head=None, limit=None)` | `LogResult` | `serving logs` |
| `servings.metrics(ref, *, workspace=None, metric='core', window='1h', start=None, end=None, interval=None, group=None)` | `tuple[MetricGroup, ...]` | `serving metrics` |
| `servings.plan(spec)` | `ServingPlan` | `serving create --dry-run` |
| `servings.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `serving quota` |
| `servings.rollback(ref, *, version, workspace=None)` | `None` | `serving rollback` |
| `servings.scale(ref, *, replicas, workspace=None)` | `None` | `serving scale` |
| `servings.scale_history(ref, *, workspace=None, limit=20, cursor=None)` | `Page[ServingScaleHistoryEntry]` | `serving scale-history` |
| `servings.start(ref, *, workspace=None)` | `None` | `serving start` |
| `servings.status(refs, *, workspace=None)` | `tuple[Serving, ...]` | `serving status` |
| `servings.stop(ref, *, workspace=None)` | `None` | `serving stop` |
| `servings.versions(ref, *, workspace=None)` | `tuple[ServingVersion, ...]` | `serving versions` |
| `servings.wait(ref, *, timeout=3600, poll_interval=10, raise_on_failure=False, workspace=None, target='RUNNING')` | `Serving` | `serving start` / `create` (wait for target state) |

**Ports & plaza models on `ServingCreateSpec`:**
- **Structured ports (`port` & `port_configs`):** `port` sets the HTTP listening port shorthand. To add an optional metrics port (or pass structured ports directly), set `port=None` (or keep `port` matching the HTTP entry) and pass `port_configs`:
  ```python
  port_configs = [
      {"port": 30000, "type": "PORT_TYPE_HTTP", "path": "/"},
      {"port": 30001, "type": "PORT_TYPE_METRICS", "path": "/metrics"},
  ]
  ```
  The outgoing payload sends `port_configs` only; `Serving` and `ServingVersion` read HTTP and metrics ports from `port_configs` (falling back to legacy `port`).
- **Model Plaza (`model_source="plaza"`):** Pass `model_source="plaza"` (`"MODEL_PLAZA"` is also accepted; `"registered"` or `None` selects registered models), pass the published model name as `model` (registry `ModelRef` objects are rejected for plaza source), and leave `model_version=None` (`ServingPlan.model_version` is `None`; explicit plaza versions raise `ValidationError`). Plaza names resolve by case-sensitive exact match (ignoring surrounding whitespace) against the full live workspace plaza catalog without registering or copying weights.

### tensorboards

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `tensorboards.create(spec, *, operation_id=None)` | `TensorboardHandle` | `tensorboard create` |
| `tensorboards.delete(ref, *, workspace=None)` | `None` | `tensorboard delete` |
| `tensorboards.get(ref, *, workspace=None)` | `Tensorboard` | `tensorboard status` |
| `tensorboards.list(workspace, *, status=None, job=None, keyword=None, limit=20, cursor=None)` | `Page[Tensorboard]` | `tensorboard list` |
| `tensorboards.scalars(ref, *, tag='', run=None, points=None, workspace=None)` | `TensorboardScalars` | `tensorboard scalars` |
| `tensorboards.start(ref, *, workspace=None)` | `None` | `tensorboard start` |
| `tensorboards.status(refs, *, workspace=None)` | `tuple[Tensorboard, ...]` | `tensorboard status` |
| `tensorboards.stop(ref, *, workspace=None)` | `None` | `tensorboard stop` |
| `tensorboards.tags(ref, *, workspace=None)` | `TensorboardTags` | `tensorboard tags` |
| `tensorboards.url(ref, *, workspace=None)` | `str` | `tensorboard status` (app URL) |
| `tensorboards.wait(ref, *, target='RUNNING', raise_on_failure=False, timeout=60, poll_interval=3, workspace=None)` | `Tensorboard` | `tensorboard start` / `stop` (wait for target state) |

## 7. Remote Execution (`exec` / `exec_stream`) & Notebook File Transfer

### Remote Execution (`exec`)

`notebooks`, `jobs`, `hpc`, `ray`, and `servings` provide `exec(...) -> ExecResult` (plus `exec_stream(...) -> AsyncIterator[str]` on `InspireAsyncClient`):

```python
result = client.notebooks.exec(
    notebook.ref,
    command="python -u check.py",
    cwd="/inspire/ssd/project/example/public/work",
    env={"MODE": "check"},
    timeout=120,
    transport="auto",
    on_output=lambda chunk: print(chunk, end="", flush=True),
)
print(result.returncode, result.completed, result.transport)

result = client.jobs.exec(job.ref, command="hostname", instance="rank=0")
result = client.hpc.exec(hpc_job.ref, command="hostname")       # defaults to launcher
result = client.ray.exec(ray_job.ref, command="hostname")       # defaults to head
result = client.servings.exec(serving.ref, command="hostname")  # defaults to first running replica
```

- **Transports:**
  - **Notebooks:** `transport="auto"` prefers a cached, reachable SSH `rtunnel` bridge for the active account/workspace/Notebook and falls back to `jupyter` (Jupyter terminal WebSocket). Explicit `transport="ssh"` requires an existing cached bridge (`inspire notebook connection refresh <name>`).
  - **Jobs, HPC, Ray, Serving:** Execute over the platform's interactive PTY WebSocket (`transport="pty"`).
- **Output capture & streaming:**
  - `ExecResult` fields: `returncode`, `output`, `stdout`, `stderr`, `completed`, `transport`, `instance=""`, `truncated=False`, `total_output_bytes=0` (for PTY/Jupyter streams, `stdout == output` and `stderr == ""`; only SSH separates `stdout` and `stderr`).
  - `max_output_bytes` defaults to `4 * 1024 * 1024` (4 MiB; `None` disables the cap). When exceeded, middle bytes are replaced with `\n[... output truncated ...]\n` and `truncated=True`.
  - Pass `output_to="report.txt"` (or a writable text file object) with `capture=False` to stream large outputs directly to disk without holding them in memory, and iterate over saved output files in bounded chunks using `from inspire.sdk import iter_output_file`.
  - On `InspireAsyncClient`, `on_output` accepts either a synchronous function or an `async def` coroutine (`Callable[[str], None | Awaitable[None]] | None`).
  - On timeout or premature disconnect before the completion marker, `exec` returns `returncode=124` and `completed=False`.

### Notebook File Transfer (`upload` / `download`)

`notebooks.upload` and `notebooks.download` transfer files over `auto`, `jupyter` (Jupyter Contents API, single-file up to `max_bytes=16 MiB` by default, `recursive=False` only), or `ssh` (cached SSH bridge via `scp`, supporting large files and `recursive=True`):

```python
uploaded = client.notebooks.upload(
    notebook.ref, local="./config.json", remote="experiment/config.json", transport="jupyter",
)
downloaded = client.notebooks.download(
    notebook.ref, remote="experiment/result.bin", local="./results/result.bin",
    transport="jupyter", overwrite=False,
)
print(uploaded.remote_path, uploaded.bytes_transferred, downloaded.transport)
```

- Returns `TransferResult(local, remote, remote_path, bytes_transferred, files_transferred, transport)`.
- Relative `remote` paths resolve against the Notebook's Jupyter `serverRoot` across both `jupyter` and `ssh` transports, and `result.remote_path` gives the resolved container-absolute path so subsequent `notebooks.exec` calls can reference the exact file regardless of default shell working directory.

## 8. Workload Creation Specs (`*CreateSpec`, `PublicPathMountMode` & `plan()`)

Construct a `*CreateSpec` dataclass and pass it to `plan(spec)` (read-only dry-run validation) or `create(spec, *, operation_id=None)`:

```python
from inspire import JobCreateSpec, PublicPathMountMode, Quota

spec = JobCreateSpec(
    name="sdk-job",
    command="python train.py",
    workspace="my-workspace",
    project="my-project",
    group="compute-group-name",
    quota=Quota(1, 8, 32),
    image="my-image:latest",
    public_path_mount_mode=PublicPathMountMode.READ_ONLY,
)
plan = client.jobs.plan(spec)
print(plan.summary)
handle = client.jobs.create(spec, operation_id="pipeline-stage-1")
finished = client.jobs.wait(handle.ref, raise_on_failure=True)
```

| Spec Class | Required Fields | Optional Fields (with defaults) |
| --- | --- | --- |
| `JobCreateSpec` | `name`, `workspace`, `project`, `group`, `quota`, `image`, `command` | `nodes=1`, `shm_gib=None`, `priority=None`, `max_time_hours=None`, `description=None`, `framework='pytorch'`, `auto_fault_tolerance=None`, `fault_tolerance_max_retry=None`, `fault_tolerance_retry_interval_sec=None`, `datasets=[]`, `envs={}`, `keep_after_success_hours=None`, `keep_after_failure_hours=None`, `public_path_readonly=None`, `enable_notification=None`, `exclude_nodes=[]`, `specified_nodes=[]`, `public_path_mount_mode=None` |
| `NotebookCreateSpec` | `name`, `workspace`, `project`, `group`, `quota`, `image` | `shm_gib=None`, `auto_stop=False`, `auto_stop_after=None`, `datasets=[]`, `enable_notification=None`, `public_path_readonly=None`, `project_path_readonly=None`, `priority=None`, `node=None`, `public_path_mount_mode=None` |
| `HPCJobCreateSpec` | `name`, `entrypoint`, `workspace`, `project`, `group`, `quota`, `image` | `image_type='SOURCE_PRIVATE'`, `instance_count=1`, `priority=None`, `number_of_tasks=1`, `cpus_per_task=None`, `memory_per_cpu=None`, `enable_hyper_threading=False`, `max_time_hours=None`, `keep_after_finish_hours=None`, `datasets=[]`, `description=None`, `enable_notification=False`, `public_path_readonly=None`, `public_path_mount_mode=None` |
| `RayJobCreateSpec` | `name`, `command`, `workspace`, `project`, `group`, `quota`, `image` | `image_type='SOURCE_PUBLIC'`, `description=''`, `priority=None`, `shm_gib=None`, `workers=[]`, `public_path_readonly=None`, `public_path_mount_mode=None` |
| `ServingCreateSpec` | `name`, `model`, `command`, `port`, `workspace`, `project`, `group`, `quota`, `image` | `model_version=None`, `replicas=1`, `nodes_per_replica=1`, `shm_gib=None`, `priority=None`, `custom_domain=None`, `description=''`, `auto_scaling=None`, `public_path_readonly=None`, `public_path_mount_mode=None`, `port_configs=None`, `model_source=None` |
| `TensorboardCreateSpec` | `name`, `workspace`, `project`, `group` | `summary_path=None` (required non-empty at creation), `job=None`, `auto_stop_hours=None` |

- **Project public-path mount mode (`PublicPathMountMode`):** All five compute workload specs accept `public_path_mount_mode` as `PublicPathMountMode` (`READ_WRITE=0`, `READ_ONLY=1`, `NOT_MOUNTED=2`) or an integer `0|1|2`. Omitting it leaves `publicpath_mount_mode` out of the request so the platform applies its default (`0`). Deprecated `public_path_readonly` (`True` → `1`, `False` → `0`) emits `DeprecationWarning` and raises `ValidationError` if combined with a conflicting `public_path_mount_mode`. `Notebook`, `Job`, and `Serving` detail `.view` projections expose `public_path_mount_mode` whenever the platform returns mount metadata.

## 9. Write Semantics (`single_send`), Errors & Timeouts

All SDK JSON mutation calls execute inside `single_send`: each write is sent at most once and is never automatically retried or replayed across session refreshes or transport fallbacks. (If a client has not received a platform response within the last 60 seconds, it runs a read-only `GetUserDetail` preflight before sending the write.)

| Write Outcome | Raised Exception | Recommended Action |
| --- | --- | --- |
| Explicit rejection (HTTP 4xx except 401/403/429, or v2 business error such as `Conflict`) | `ValidationError` | Fix parameters or resource state |
| Permission denied (HTTP 403) | `AuthenticationError` | Check account/workspace permissions |
| Rate-limited or transient server refusal (`TransientAPIError` / HTTP 429) | `TransportError(retryable=True)` | Safe for the caller to retry explicitly |
| Ambiguous outcome after send (post-send HTTP 401/3xx/5xx, network drop, timeout, or missing ID) | `SubmissionUncertainError` (creates) / `MutationUncertainError` (mutations), `retryable=False` | Inspect the target catalog (`err.inspect`) before retrying to avoid duplicate creation |

### Structured Error Context

Every public `InspireError` subclass exposes optional scalar attributes (`None` when absent):
- `code: str | None`, `service: str | None`, `action: str | None`, `request_id: str | None` — extracted from the `/api/v2` response envelope.
- `status: int | None` — observed HTTP status code (including HTTP `200` on v2 business errors; `None` when unwrapping a standalone envelope without HTTP context).
- `retry_after: float | None` — parsed `Retry-After` delay in seconds.

Wrapping exceptions inherit missing context fields from their cause without overwriting outer fields. Request bodies, credentials, cookies, and raw HTTP objects are never attached, and server error messages pass through the shared text redactor before truncation.

```python
from inspire.sdk import InspireError, SubmissionUncertainError

try:
    handle = client.jobs.create(spec, operation_id="example-operation")
except SubmissionUncertainError as error:
    # Inspect the catalog before deciding whether to submit again.
    print(error.operation_id, error.inspect, error.request_id)
except InspireError as error:
    print(error.code, error.status, error.service, error.action, error.request_id)
```

### Retry Timing & Operation Deadlines

- Safe read operations make at most **3 attempts** on transient failures. Valid `Retry-After` headers (seconds or HTTP-date on HTTP 429/5xx) are honored in full; missing or invalid headers use exponential backoff with jitter starting at `0.5s` and capped at `8s`.
- If the next backoff wait would exceed the remaining `operation_timeout`, the SDK raises `WaitTimeoutError` immediately without sleeping. Exhausted `TransportError`, `WaitTimeoutError`, and HTTP 429 write refusals preserve `status` and `retry_after`.

### Public Exports (`inspire.sdk.__all__`)

`inspire.sdk.__all__` defines all **133** public symbols (re-exported at top-level `inspire`):
- **Entrypoints & Accounts (3):** `Accounts`, `InspireAsyncClient`, `InspireClient`
- **Typed References (19):** `APIKeyRef`, `ComputeGroupRef`, `DatasetApplicationRef`, `DatasetRef`, `DatasetTagRef`, `DatasetVersionRef`, `HPCJobRef`, `ImageRef`, `JobRef`, `ModelRef`, `NotebookRef`, `ProjectOwnerRef`, `ProjectRef`, `QuotaRef`, `RayJobRef`, `ResourceRef`, `ServingRef`, `TensorboardRef`, `WorkspaceRef`
- **Creation Specs & Enums (7):** `HPCJobCreateSpec`, `JobCreateSpec`, `NotebookCreateSpec`, `PublicPathMountMode`, `RayJobCreateSpec`, `ServingCreateSpec`, `TensorboardCreateSpec`
- **Models & Handles (82):** `APIKeyInfo`, `AccountCheck`, `AccountContext`, `AccountInfo`, `AsyncHPCJobHandle`, `AsyncImageRegisterHandle`, `AsyncImageSaveHandle`, `AsyncJobHandle`, `AsyncNotebookHandle`, `AsyncRayJobHandle`, `AsyncServingHandle`, `AsyncTensorboardHandle`, `DatasetApplication`, `DatasetDetail`, `DatasetInfo`, `DatasetMount`, `DatasetTag`, `DatasetValidation`, `DatasetVersion`, `EventResult`, `ExecResult`, `HPCJob`, `HPCJobHandle`, `HPCJobPlan`, `Image`, `ImageDetail`, `ImageRegisterHandle`, `ImageSaveHandle`, `ImageSelector`, `InitResult`, `Instance`, `Job`, `JobHandle`, `JobPlan`, `LogResult`, `MetricGroup`, `ModelDeployConfig`, `ModelInfo`, `ModelRegisterHandle`, `ModelStatus`, `ModelVersion`, `Notebook`, `NotebookHandle`, `NotebookImageSizeEstimate`, `NotebookPlan`, `NotebookResourceSnapshot`, `NotebookRun`, `Page`, `Permission`, `ProjectDetail`, `ProjectInfo`, `ProjectOwner`, `Quota`, `QuotaOption`, `RayJob`, `RayJobHandle`, `RayJobPlan`, `RayScalingEvent`, `Resource`, `ResourceAvailability`, `ResourceUsage`, `Serving`, `ServingAPIMetricSeries`, `ServingAPIMetricTimeRange`, `ServingAPIMetrics`, `ServingConfigItem`, `ServingConfigs`, `ServingHandle`, `ServingInvocationCredentials`, `ServingInvocationInfo`, `ServingPlan`, `ServingScaleHistoryEntry`, `ServingVersion`, `StatusResult`, `Tensorboard`, `TensorboardHandle`, `TensorboardScalarPoint`, `TensorboardScalarSeries`, `TensorboardScalars`, `TensorboardTags`, `TransferResult`, `WorkloadSchedulePolicy`
- **Exceptions (20):** `AmbiguousResourceError`, `AuthenticationCooldownError`, `AuthenticationError`, `ClientClosedError`, `ClientThreadError`, `ConfigurationError`, `HPCJobFailedError`, `InspireError`, `JobFailedError`, `MutationUncertainError`, `NotebookFailedError`, `RayJobFailedError`, `ResolutionIncompleteError`, `ResourceNotFoundError`, `ServingFailedError`, `SubmissionUncertainError`, `TensorboardFailedError`, `TransportError`, `ValidationError`, `WaitTimeoutError`
- **Helpers & Type Aliases (2):** `JobEvent`, `iter_output_file`

## 10. Windows Support, Private Local Files & Maintenance

- **Windows event loop:** Async SSH `exec` and SCP `upload`/`download` spawn `asyncio` subprocesses and require `ProactorEventLoop` (the Windows default). If a host framework overrides the loop with `SelectorEventLoop`, the SDK raises `ConfigurationError` with instructions to configure `WindowsProactorEventLoopPolicy`.
- **Private local file permissions:** Account configs, session caches, and exported API keys are written atomically. On POSIX, directories use `0700` and private files use `0600` (with `O_NOFOLLOW`). On Windows, the writer applies and verifies a protected, current-user-only `FullControl` ACL (`ContainerInherit,ObjectInherit`) on `~/.inspire` and account directories, and `api-key export` sets and verifies the ACL on each new target file before writing secret material.
- **CLI-only features:** Interactive `inspire init`, `update`, `uninstall`, `cache *` CLI commands, `api-key export`/`run`, JSON/TOML `<workload> batch`, interactive `shell` / `ssh` / `ssh-config` / `proxy-url`, and PNG/sparkline terminal rendering remain CLI-only.
- **Maintaining async wrappers:** After adding or modifying synchronous facade methods in `cli/inspire/sdk/`, run `uv run python scripts/generate_sdk_async.py` inside `cli/` and verify with `uv run pytest tests/test_sdk_async.py tests/test_sdk_surface_consistency.py`.
