# Python SDK Reference

The `inspire-skill` package includes a full-featured Python SDK alongside the `inspire` CLI:
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

`Accounts` manages `~/.inspire/accounts/<alias>/` directly without constructing a client or making network calls (`InspireClient.accounts` and `InspireAsyncClient.accounts` point to the same synchronous class):

| Method | Description |
| --- | --- |
| `Accounts.list()` | Return a sorted tuple of configured account aliases |
| `Accounts.current()` | Return the saved default account alias on disk (`None` if unset) |
| `Accounts.exists(name)` | Return whether the account alias exists |
| `Accounts.config_path(name)` | Return the `Path` to the account's `config.toml` |
| `Accounts.add(name, *, username, password, base_url=DEFAULT_BASE_URL, proxy=None, use=False, overwrite=False)` | Create an account profile; the first account becomes the default automatically, and subsequent accounts switch the default only when `use=True` (`overwrite=True` replaces the account directory and its caches) |
| `Accounts.use(name)` | Set the persistent default account on disk |
| `Accounts.rename(old, new)` | Rename an account alias and migrate its Notebook target caches |
| `Accounts.remove(name)` | **Immediately delete the account directory and its cached state without confirmation** |

```python
from inspire import Accounts, InspireClient

Accounts.add("research", username="login-name", password="password", use=False)
with InspireClient(account="research") as client:
    identity = client.login()
    result = client.init()
    print(result.config_path, result.changed, result.warnings)

# Or construct directly from credentials (updates local config without changing the default account):
with InspireClient.from_credentials("login-name", "password", account="research") as client:
    identity = client.login()
```

### Client Construction, `login()`, and `init()`

- **Constructors:**
  - `InspireClient(account=None, *, username=None, password=None, base_url=None, proxy=None, allow_browser=False, timeout=30, operation_timeout=120, catalog_ttl=60, catalog_disk_cache=False)`
  - `InspireAsyncClient(account=None, *, username=None, password=None, base_url=None, proxy=None, allow_browser=False, timeout=30, operation_timeout=120, catalog_ttl=60, catalog_disk_cache=False, concurrency=None)`
  - If `username` and `password` are omitted, the client loads the named `account` (or the current default account if `account=None`). If `username` and `password` are provided (as a pair), missing accounts are created and existing accounts update only explicitly changed `auth`/`api`/`proxy` fields (`proxy=""` clears proxy settings; `proxy=None` preserves existing proxy settings). Constructing a client never alters `~/.inspire/current`.
  - `InspireClient` pins its account at construction time; `InspireAsyncClient` performs local config loading when entering `async with` or on its first method call (accessing `.account` or `.base_url` before initialization raises `ConfigurationError`).
- **`login(*, force=False) -> AccountInfo`** (async: `await client.login(force=False)`):
  - Reuses a cached disk session if under the 1-hour `SESSION_TTL` and verifies it via `GetUserDetail`; otherwise (or when `force=True`) renews the session under the account refresh lock: re-reads disk cache → tries passwordless SSO cookie renewal → performs credential login guarded by the login circuit breaker (`AuthenticationCooldownError.retry_at`).
  - Chromium login fallback and browser-backed requests are enabled only when `allow_browser=True`. If a CAPTCHA is required and cannot be satisfied non-interactively, `AuthenticationError` is raised without submitting credentials.
- **`init(*, force=False) -> InitResult`** (async: `await client.init(force=False)`):
  - Logs in, verifies at least one accessible Workspace, and atomically writes normalized account config (`force=False` merges discovered fields while preserving custom TOML sections; `force=True` rebuilds from the template). Interactive prompts, Playwright installation, and `ssh-keygen` remain CLI-only (`inspire init`).

## 3. Concurrency, Cancellation & Lifecycle

- **Thread and task safety:**
  - Use each synchronous `InspireClient` within a single thread and process (close with `with` or `client.close()`).
  - Share a single `InspireAsyncClient` across concurrent tasks on the same event loop (`asyncio.gather(...)` overlaps native async HTTP, WebSocket, and SSH/SCP subprocess I/O; close with `async with` or `await client.close()`).
  - The `concurrency` parameter on `InspireAsyncClient` is deprecated (passing a positive integer emits `DeprecationWarning` and is a no-op; non-positive values raise `ValidationError`). Use `asyncio.Semaphore` if you need application-level concurrency throttling.
- **Local I/O offload pool:** `InspireAsyncClient` offloads blocking local disk/SQLite/file-lock/certificate operations to a per-client thread pool capped at **4 workers** (network concurrency is unrestricted). One-time client initialization, direct `Accounts` calls, synchronous `on_output` callbacks, and in-memory payload parsing still run on the event loop thread.
- **Stream iteration:** `iter`, `follow_events`, `follow_logs`, and `exec_stream` are async iterators on `InspireAsyncClient` — wrap them in `contextlib.aclosing(...)` when breaking early so underlying connections close deterministically.
- **Bounded multi-ref status:** `await client.ray.status(refs)` and `await client.servings.status(refs)` cap concurrent in-flight reads at **8** per call (`jobs.status` and `hpc.status` use server-side batch endpoints in chunks of 20).
- **Cancellation:** Canceling an async task aborts in-flight network/WebSocket waits and terminates local SSH/SCP subprocesses, while waiting for any active local file/SQLite lock operation to finish cleanly. **Canceling a local Python call or `wait()` never stops a remote workload on the platform.**

## 4. Resource References, Pagination & Handles

### Pagination & Disambiguation

- Collection methods follow `list(workspace, *, ...filters, limit=20, cursor=None)` and `iter(workspace, *, ...filters, max_items=None)`.
- `Page` exposes `.items`, `.next_cursor`, and `.total`. Pass `cursor=page.next_cursor` with the same filters to fetch the next page.
- Workload listings and name scans enforce a 5,000-row ceiling (`page_num × page_size <= 5000` and max 100 pages); exceeding this raises `ResolutionIncompleteError` (narrow with `status=` / `keyword=` or pass a typed `Ref`).
- `serving create --model NAME` (and `servings.plan`/`create` by model name) scans up to 100 pages of 100 filtered models to ensure cross-page duplicates are caught; incomplete catalogs raise `ConfigError` / `ResolutionIncompleteError`.
- When a name matches multiple resources, `AmbiguousResourceError` is raised with `.candidates` containing the full resource model objects (e.g. `Job`, `Image`, `ModelInfo`). Select the desired candidate and pass `candidate.ref` to subsequent calls.
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

- **Important:** Workload `wait()` defaults to `raise_on_failure=False`, meaning it returns the finished resource object even if its status is `FAILED` or `CANCELLED`. Pass `raise_on_failure=True` to raise `JobFailedError`, `HPCJobFailedError`, `RayJobFailedError`, `NotebookFailedError`, `ServingFailedError`, or `TensorboardFailedError` (each carrying `.job`, `.notebook`, `.serving`, or `.tensorboard`).
- Use `handle.future()` when passing handles to `asyncio.wait(...)` or `asyncio.as_completed(...)` on Python 3.11+.
- To reattach a serialized `Ref` into an awaitable handle without making a network call, use `await client.<facade>.bind_ref(ref)` (or `await client.notebooks.bind_image_ref(image_ref, notebook=notebook_ref)`).

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

Both clients expose 15 resource facades with 156 sync methods and 169 async methods (the 13 additional async methods are 5 exec_stream, 7 bind_ref, and 1 bind_image_ref), plus 2 methods on cache, for 158/171 total facade methods.

| Facade | Sync Methods | Async Methods |
| --- | ---: | ---: |
| `cache` | 2 | 2 |
| `workspaces` | 2 | 2 |
| `projects` | 4 | 4 |
| `compute_groups` | 2 | 2 |
| `images` | 8 | 9 |
| `jobs` | 19 | 21 |
| `hpc` | 17 | 19 |
| `ray` | 19 | 21 |
| `servings` | 25 | 27 |
| `tensorboards` | 11 | 12 |
| `notebooks` | 23 | 26 |
| `account_info` | 4 | 4 |
| `api_keys` | 5 | 5 |
| `datasets` | 5 | 5 |
| `models` | 8 | 8 |
| `resources` | 4 | 4 |

**Calling convention across all facades:**
- Call `client.<facade>.<method>(...)` on `InspireClient`, or `await client.<facade>.<method>(...)` on `InspireAsyncClient` with identical parameters, defaults, and return models.
- Arguments after `*` are keyword-only.
- **Streaming iterator exception:** `iter`, `follow_events`, and `follow_logs` return a synchronous `Iterator` on `InspireClient` and an `AsyncIterator` on `InspireAsyncClient` (`async for item in client.<facade>.follow_events(...)` directly, without an initial `await`).
- **Async-only methods:** `jobs`, `notebooks`, `hpc`, `ray`, and `servings` additionally provide `exec_stream(...) -> AsyncIterator[str]` (same parameters as `exec`, yielding decoded output chunks via `async for`). `images`, `jobs`, `hpc`, `ray`, `servings`, `tensorboards`, and `notebooks` provide `await client.<facade>.bind_ref(ref)`, and `notebooks` also provides `await client.notebooks.bind_image_ref(image_ref, notebook=None)`.

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

For external images, call `images.register`, push the image to the returned registry address, then call `images.sync(handle.ref)` and `images.wait_ready(handle.ref)`. Registration reserves a `PENDING_SYNC` slot; neither registration nor Docker push implies catalog readiness. The async client provides the same sequence with awaited methods.

### datasets

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `datasets.applications(name=None, *, to_approve=False, keyword=None, limit=20, cursor=None)` | `Page[DatasetApplication]` | `dataset applications` |
| `datasets.get(ref)` | `DatasetDetail` | `dataset show` |
| `datasets.list(keyword=None, *, tag=None, limit=20, cursor=None)` | `Page[DatasetInfo]` | `dataset list` |
| `datasets.tags()` | `tuple[DatasetTag, ...]` | `dataset tags` |
| `datasets.validate(specs, *, workspace)` | `tuple[DatasetValidation, ...]` | `dataset validate` |

### models

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `models.delete(ref, *, force=False, workspace=None, project=None)` | `None` | `model delete` |
| `models.deploy_config(ref, *, workspace=None, project=None, version=None)` | `ModelDeployConfig` | `model deploy-config` |
| `models.get(ref, *, workspace=None, project=None)` | `ModelInfo` | `model list` (name lookup) |
| `models.list(workspace, *, project=None, keyword=None, limit=20, cursor=None)` | `Page[ModelInfo]` | `model list` |
| `models.register(name, *, source_path, workspace, project, type=None, tag=None, description=None, operation_id=None)` | `ModelRegisterHandle` | `model register` |
| `models.status(refs, *, workspace=None, project=None)` | `tuple[ModelInfo, ...]` | `model list` (per-ref status) |
| `models.detail(ref, *, workspace=None, project=None)` | `ModelStatus` | `model status` |
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
| `account_info.permissions(workspace=None)` | `tuple[Permission, ...]` | `account permissions` |

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
| `jobs.status(refs, *, workspace=None)` | `tuple[Job, ...]` | `job status` |
| `jobs.stop(ref, *, workspace=None)` | `None` | `job stop` |
| `jobs.wait(ref, *, workspace=None, timeout=3600, poll_interval=10, raise_on_failure=False)` | `Job` | `job wait` |

Like `HPCJob`, `RayJob`, and `Serving`, `Job` exposes `.view` (the sanitized public projection matching `inspire --json job status`, also returned by `.to_dict()`) and `.raw` (the full platform payload). `instances()` on `jobs`, `hpc`, `ray`, and `servings` returns frozen `Instance` objects where `.label` is the human-readable instance label and `.handle` is the opaque namespaced pod identifier (hidden from `repr()`); both `exec(instance=...)` and `logs(instance=...)` accept either `.label` or `.handle`.

### notebooks

| Method | Return Type | Equivalent CLI Command |
| --- | --- | --- |
| `notebooks.cancel_save_image(ref, *, workspace=None)` | `bool` | `notebook cancel-save-image` |
| `notebooks.create(spec, *, operation_id=None)` | `NotebookHandle` | `notebook create` |
| `notebooks.delete(ref, *, workspace=None)` | `None` | `notebook delete` |
| `notebooks.estimate_image_size(ref, *, workspace=None)` | `NotebookImageSizeEstimate` | `notebook save-image --dry-run` |
| `notebooks.events(ref, *, keyword=None, limit=100, workspace=None)` | `EventResult` | `notebook events` |
| `notebooks.exec(ref, *, command, workspace=None, cwd=None, env=None, timeout=120, transport='auto', on_output=None, max_output_bytes=4194304, output_to=None, capture=True)` | `ExecResult` | `notebook exec` |
| `notebooks.upload(ref, *, local, remote, workspace=None, transport='auto', recursive=False, overwrite=True, timeout=120, max_bytes=16777216)` | `TransferResult` | `notebook scp` (plus Jupyter Contents API) |
| `notebooks.download(ref, *, local, remote, workspace=None, transport='auto', recursive=False, overwrite=True, timeout=120, max_bytes=16777216)` | `TransferResult` | `notebook scp` (plus Jupyter Contents API) |
| `notebooks.follow_events(ref, *, interval=5, **filters)` | `Iterator[EventResult]` / `AsyncIterator[EventResult]` | `notebook events --follow` |
| `notebooks.get(ref, *, workspace=None)` | `Notebook` | `notebook status` |
| `notebooks.iter(workspace, *, status=None, keyword=None, max_items=None)` | `Iterator[Notebook]` / `AsyncIterator[Notebook]` | `notebook list --all` |
| `notebooks.lifecycle(ref, *, limit=None, workspace=None)` | `tuple[NotebookRun, ...]` | `notebook lifecycle` |
| `notebooks.list(workspace, *, status=None, keyword=None, limit=20, cursor=None)` | `Page[Notebook]` | `notebook list` |
| `notebooks.metrics(ref, *, metric='core', window='1h', start=None, end=None, interval=None, group=None, workspace=None)` | `tuple[MetricGroup, ...]` | `notebook metrics` |
| `notebooks.plan(spec)` | `NotebookPlan` | SDK-only for single-create planning; use `notebook batch <file> --dry-run` for batch plans |
| `notebooks.quotas(workspace, *, group=None, include_empty=False, limit=20, cursor=None)` | `Page[QuotaOption]` | `notebook quota` |
| `notebooks.realtime_metrics(ref, *, workspace=None)` | `tuple[NotebookResourceSnapshot, ...]` | `notebook metrics --now` |
| `notebooks.save_image(ref, *, name, version=None, description=None, visibility=None, flatten=False, workspace=None)` | `ImageSaveHandle` | `notebook save-image` |
| `notebooks.start(ref, *, workspace=None)` | `None` | `notebook start` |
| `notebooks.status(refs, *, workspace=None)` | `tuple[Notebook, ...]` | `notebook status` |
| `notebooks.stop(ref, *, workspace=None)` | `None` | `notebook stop` |
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
| `hpc.status(refs, *, workspace=None)` | `tuple[HPCJob, ...]` | `hpc status` |
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

`notebooks`, `jobs`, `hpc`, `ray`, and `servings` provide `exec(...) -> ExecResult` (and `exec_stream(...) -> AsyncIterator[str]` on `InspireAsyncClient`):

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
  - **Jobs, HPC, Ray, Serving:** Always execute over the platform's interactive PTY WebSocket (`transport="pty"`).
- **Output capture & streaming:**
  - `ExecResult` fields: `returncode`, `output`, `stdout`, `stderr`, `completed`, `transport`, `instance=""`, `truncated=False`, `total_output_bytes=0` (for PTY/Jupyter streams, `stdout == output` and `stderr == ""`; only SSH separates `stdout` and `stderr`).
  - `max_output_bytes` defaults to `4 * 1024 * 1024` (4 MiB; `None` disables the cap). When exceeded, middle bytes are replaced with `\n[... output truncated ...]\n` and `truncated=True`.
  - Pass `output_to="report.txt"` (or a writable text file object) with `capture=False` to stream large outputs directly to disk without holding them in memory, and iterate over saved output files in bounded chunks using `from inspire.sdk import iter_output_file`.
  - On `InspireAsyncClient`, `on_output` accepts either a synchronous function or an `async def` coroutine (`Callable[[str], None | Awaitable[None]] | None`).
  - On timeout or premature disconnect before the completion marker, `exec` returns `returncode=124` and `completed=False`.

### Notebook File Transfer (`upload` / `download`)

`notebooks.upload` and `notebooks.download` transfer files over `auto`, `jupyter` (Jupyter Contents API, single-file up to `max_bytes=16 MiB` by default, `recursive=False` only), or `ssh` (uses cached SSH bridge via `scp`, supports large files and `recursive=True`):

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

## 8. Workload Creation Specs (`*CreateSpec` & `plan()`)

Construct a `*CreateSpec` dataclass and pass it to `plan(spec)` (read-only dry-run validation) or `create(spec, *, operation_id=None)`:

```python
from inspire import JobCreateSpec, Quota

spec = JobCreateSpec(
    name="sdk-job",
    command="python train.py",
    workspace="my-workspace",
    project="my-project",
    group="compute-group-name",
    quota=Quota(1, 8, 32),
    image="my-image:latest",
)
plan = client.jobs.plan(spec)
print(plan.summary)
handle = client.jobs.create(spec, operation_id="pipeline-stage-1")
finished = client.jobs.wait(handle.ref, raise_on_failure=True)
```

| Spec Class | Required Fields | Optional Fields (with defaults) |
| --- | --- | --- |
| `JobCreateSpec` | `name`, `workspace`, `project`, `group`, `quota`, `image`, `command` | `nodes=1`, `shm_gib=None`, `priority=None`, `max_time_hours=None`, `description=None`, `framework='pytorch'`, `auto_fault_tolerance=None`, `fault_tolerance_max_retry=None`, `fault_tolerance_retry_interval_sec=None`, `datasets=[]`, `envs={}`, `keep_after_success_hours=None`, `keep_after_failure_hours=None`, `public_path_readonly=None`, `enable_notification=None`, `exclude_nodes=[]`, `specified_nodes=[]` |
| `NotebookCreateSpec` | `name`, `workspace`, `project`, `group`, `quota`, `image` | `shm_gib=None`, `auto_stop=False`, `auto_stop_after=None`, `datasets=[]`, `enable_notification=None`, `public_path_readonly=None`, `project_path_readonly=None`, `priority=None`, `node=None` |
| `HPCJobCreateSpec` | `name`, `entrypoint`, `workspace`, `project`, `group`, `quota`, `image` | `image_type='SOURCE_PRIVATE'`, `instance_count=1`, `priority=None`, `number_of_tasks=1`, `cpus_per_task=None`, `memory_per_cpu=None`, `enable_hyper_threading=False`, `max_time_hours=None`, `keep_after_finish_hours=None`, `datasets=[]`, `description=None`, `enable_notification=False`, `public_path_readonly=None` |
| `RayJobCreateSpec` | `name`, `command`, `workspace`, `project`, `group`, `quota`, `image` | `image_type='SOURCE_PUBLIC'`, `description=''`, `priority=None`, `shm_gib=None`, `workers=[]`, `public_path_readonly=None` |
| `ServingCreateSpec` | `name`, `model`, `command`, `port`, `workspace`, `project`, `group`, `quota`, `image` | `model_version=None`, `replicas=1`, `nodes_per_replica=1`, `shm_gib=None`, `priority=None`, `custom_domain=None`, `description=''`, `auto_scaling=None`, `public_path_readonly=None` |
| `TensorboardCreateSpec` | `name`, `workspace`, `project`, `group` | `summary_path=None` (required non-empty at creation), `job=None`, `auto_stop_hours=None` |

## 9. Write Semantics (`single_send`), Errors & Timeouts

All SDK JSON mutation calls execute inside `single_send`: each write is sent at most once and is never automatically retried or replayed across session refreshes or transport fallbacks. (If a client has not received a platform response within the last 60 seconds, it runs a read-only `GetUserDetail` preflight before sending the write.)

| Write Outcome | Raised Exception | Recommended Action |
| --- | --- | --- |
| Explicit rejection (HTTP 4xx except 401/403/429, or v2 business error such as `Conflict`) | `ValidationError` | Fix parameters or resource state |
| Permission denied (HTTP 403) | `AuthenticationError` | Check account/workspace permissions |
| Rate-limited or transient server refusal (`TransientAPIError` / HTTP 429) | `TransportError(retryable=True)` | Safe for the caller to retry explicitly |
| Ambiguous outcome after send (post-send HTTP 401/3xx/5xx, network drop, timeout, or missing ID) | `SubmissionUncertainError` (creates) / `MutationUncertainError` (mutations), `retryable=False` | Inspect the target catalog (`err.inspect`) before retrying to avoid duplicate creation |

### Public Exports (`inspire.sdk.__all__`)

`inspire.sdk.__all__` exports **131 symbols** (all re-exported at top-level `inspire`):
- **Entrypoints & Accounts (3):** `Accounts`, `InspireAsyncClient`, `InspireClient`
- **Typed References (19):** `APIKeyRef`, `ComputeGroupRef`, `DatasetApplicationRef`, `DatasetRef`, `DatasetTagRef`, `DatasetVersionRef`, `HPCJobRef`, `ImageRef`, `JobRef`, `ModelRef`, `NotebookRef`, `ProjectOwnerRef`, `ProjectRef`, `QuotaRef`, `RayJobRef`, `ResourceRef`, `ServingRef`, `TensorboardRef`, `WorkspaceRef`
- **Creation Specs (6):** `HPCJobCreateSpec`, `JobCreateSpec`, `NotebookCreateSpec`, `RayJobCreateSpec`, `ServingCreateSpec`, `TensorboardCreateSpec`
- **Models & Handles (81):** `AsyncJobHandle`, `AsyncHPCJobHandle`, `AsyncRayJobHandle`, `AsyncServingHandle`, `AsyncTensorboardHandle`, `AsyncNotebookHandle`, `AsyncImageSaveHandle`, `AsyncImageRegisterHandle`, `APIKeyInfo`, `AccountCheck`, `AccountContext`, `AccountInfo`, `DatasetApplication`, `DatasetDetail`, `DatasetInfo`, `DatasetMount`, `DatasetTag`, `DatasetValidation`, `DatasetVersion`, `EventResult`, `ExecResult`, `TransferResult`, `HPCJob`, `HPCJobHandle`, `HPCJobPlan`, `Image`, `ImageDetail`, `ImageRegisterHandle`, `ImageSaveHandle`, `ImageSelector`, `InitResult`, `Job`, `JobHandle`, `Instance`, `JobPlan`, `LogResult`, `MetricGroup`, `ModelDeployConfig`, `ModelInfo`, `ModelRegisterHandle`, `ModelStatus`, `ModelVersion`, `Notebook`, `NotebookHandle`, `NotebookImageSizeEstimate`, `NotebookPlan`, `NotebookResourceSnapshot`, `NotebookRun`, `Page`, `Permission`, `ProjectDetail`, `ProjectInfo`, `ProjectOwner`, `Quota`, `QuotaOption`, `RayJob`, `RayJobHandle`, `RayJobPlan`, `RayScalingEvent`, `Resource`, `ResourceAvailability`, `ResourceUsage`, `Serving`, `ServingAPIMetricSeries`, `ServingAPIMetricTimeRange`, `ServingAPIMetrics`, `ServingConfigItem`, `ServingConfigs`, `ServingHandle`, `ServingInvocationCredentials`, `ServingInvocationInfo`, `ServingPlan`, `ServingScaleHistoryEntry`, `ServingVersion`, `Tensorboard`, `TensorboardHandle`, `TensorboardScalarPoint`, `TensorboardScalarSeries`, `TensorboardScalars`, `TensorboardTags`, `WorkloadSchedulePolicy`
- **Exceptions (20):** `AmbiguousResourceError`, `AuthenticationCooldownError`, `AuthenticationError`, `ClientClosedError`, `ClientThreadError`, `ConfigurationError`, `HPCJobFailedError`, `InspireError`, `JobFailedError`, `MutationUncertainError`, `NotebookFailedError`, `RayJobFailedError`, `ResolutionIncompleteError`, `ResourceNotFoundError`, `ServingFailedError`, `SubmissionUncertainError`, `TensorboardFailedError`, `TransportError`, `ValidationError`, `WaitTimeoutError`
- **Helpers & Type Aliases (2):** `JobEvent`, `iter_output_file`

## 10. Windows Support, Private Local Files & Maintenance

- **Windows event loop:** Async SSH `exec` and SCP `upload`/`download` spawn `asyncio` subprocesses and require `ProactorEventLoop` (the default on Windows). If a host framework overrides the loop with `SelectorEventLoop`, the SDK raises `ConfigurationError` with instructions to enable `WindowsProactorEventLoopPolicy`.
- **Private local file permissions:** Account configs, session caches, and exported API keys are written atomically. On POSIX, directories use `0700` and private files use `0600` (with `O_NOFOLLOW`). On Windows (where `chmod(0600)` does not restrict access and `%USERPROFILE%` provides the base profile boundary), the writer uses `PowerShell` once per directory per process to apply and verify a protected, current-user-only `FullControl` ACL (`ContainerInherit,ObjectInherit`) on `~/.inspire` and account directories; `api-key export` always sets and verifies the ACL on each new target file before writing secret material.
- **CLI-only features:** Interactive `inspire init`, `update`, `uninstall`, `cache *` CLI commands, `api-key export`/`run`, JSON/TOML `<workload> batch`, interactive `shell` / `ssh` / `ssh-config` / `proxy-url`, and PNG/sparkline terminal rendering remain CLI-only.
- **Maintaining async wrappers:** After adding or modifying synchronous facade methods in `cli/inspire/sdk/`, run `uv run python scripts/generate_sdk_async.py` inside `cli/` and verify with `uv run pytest tests/test_sdk_async.py tests/test_sdk_surface_consistency.py`.
