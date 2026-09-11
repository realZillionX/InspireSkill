"""Notebook discovery, submission, lifecycle and image snapshots."""

from __future__ import annotations
from .models import ImageSelector

from pathlib import Path
from inspire.services.execution.notebook_transfer import TransferResult, DEFAULT_JUPYTER_MAX_BYTES
from .resources import image_mutation
from inspire.exec_output import DEFAULT_MAX_OUTPUT_BYTES, OutputTarget
from typing import Callable
from inspire.services.execution.remote_exec import ExecResult
import builtins
import math
import time
from inspire.platform.web.flow import call, perform_sync
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any, Iterator, Sequence

from inspire.platform.web import browser_api
from inspire.platform.web.browser_api import CustomImageInfo
from inspire.services.notebook import notebooks as core
from inspire.services.notebook.notebook_output import public_notebook, public_runs
from inspire.services.notebook.notebook_status import (
    normalize_status,
    TERMINAL_STATUSES,
)
from inspire.services.catalog.quotas import parse_quota
from inspire.services.catalog.workload_quota import (
    selected_groups,
    quota_values,
    match_quota_rows,
    ensure_priority_allowed,
    allowed_priority_levels_for,
)
from .resources import Service, operation, exact, positive, platform_page
from .models import (
    Page,
    WorkspaceRef,
    ProjectRef,
    ComputeGroupRef,
    ImageRef,
    QuotaRef,
    Quota,
    QuotaOption,
    DatasetMount,
    Resource,
    EventResult,
    MetricGroup,
)
from .models_observations import NotebookRun
from .models_notebooks import (
    Notebook,
    NotebookRef,
    NotebookCreateSpec,
    NotebookPlan,
    NotebookHandle,
    ImageSaveHandle,
)
from .exceptions import (
    NotebookFailedError,
    ValidationError,
    ResolutionIncompleteError,
    ResourceNotFoundError,
    SubmissionUncertainError,
)


def _duration(value: float, parameter: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValidationError(f"{parameter} must be finite positive seconds.")


class Notebooks(Service):
    def __init__(self, client: Any) -> None:
        super().__init__(client)
        self._contents_roots: dict[str, str] = {}

    @operation
    def exec(
        self,
        ref: str | NotebookRef,
        *,
        command: str,
        workspace: str | WorkspaceRef | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 120,
        transport: str = "auto",
        on_output: Callable[[str], None] | None = None,
        max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
        output_to: OutputTarget = None,
        capture: bool = True,
    ) -> ExecResult:
        """Execute in the same container through either transport.

        Jupyter and SSH share a container but start in different directories:
        Jupyter normally starts at its contents root, SSH at the user's home
        (often /root). Relative command paths use that cwd; relative transfer
        paths always use the Jupyter root. Use the resolved result path::

            import shlex
            result = client.notebooks.upload(ref, local="model.bin", remote="model.bin")
            client.notebooks.exec(ref, command=f"python train.py {shlex.quote(result.remote_path)}")

        train.py must itself be accessible from the command's cwd. To locate
        both files in the same known directory, set cwd explicitly::

            from pathlib import PurePosixPath
            client.notebooks.exec(ref, command="python train.py model.bin",
                                  cwd=str(PurePosixPath(result.remote_path).parent))
        """
        from inspire.services.execution import remote_exec as core
        from .remote_exec import shaped_command, authenticated_exec

        if transport not in ("auto", "jupyter", "ssh"):
            raise ValidationError("transport must be auto, jupyter, or ssh.")
        command = shaped_command(
            self,
            command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            on_output=on_output,
            max_output_bytes=max_output_bytes,
            output_to=output_to,
            capture=capture,
        )
        resolved = self._resolve(ref, workspace)
        bridge = None
        if transport != "jupyter":
            bridge = perform_sync(call(core.cached_notebook_bridge,
                notebook_id=resolved.key,
                workspace_id=resolved.workspace_id,
                account=self.client.account,
            ))
        if bridge is not None:
            return perform_sync(call(core.exec_in_notebook_ssh,
                bridge_name=bridge,
                account=self.client.account,
                command=command,
                timeout=min(timeout, self.client._transport.remaining()),
                on_output=on_output,
                max_output_bytes=max_output_bytes,
                output_to=output_to,
                capture=capture,
            ))
        if transport == "ssh":
            raise ValidationError(
                "No reachable cached SSH bridge. Run "
                f"`inspire notebook connection refresh {resolved.name}` first."
            )
        return authenticated_exec(
            self,
            core.exec_in_notebook_jupyter,
            timeout=timeout,
            notebook_id=resolved.key,
            command=command,
            on_output=on_output,
            max_output_bytes=max_output_bytes,
            output_to=output_to,
            capture=capture,
        )

    @operation
    def upload(
        self,
        ref: str | NotebookRef,
        *,
        local: str | Path,
        remote: str,
        workspace: str | WorkspaceRef | None = None,
        transport: str = "auto",
        recursive: bool = False,
        overwrite: bool = True,
        timeout: float = 120,
        max_bytes: int = DEFAULT_JUPYTER_MAX_BYTES,
    ) -> TransferResult:
        """Upload a file, or recursively transfer a directory over cached SSH.

        Small files without a bridge: Jupyter. Large files/directories: SSH.
        Auto prefers reachable cached SSH; it does not choose by file size
        or create a bridge. Jupyter holds the entire base64 JSON body
        (~4/3 file size plus copies), has
        no resume, and defaults to a 16 MiB cap; raise max_bytes deliberately.
        On both transports, relative remote paths start at the discovered
        Jupyter contents root; absolute paths name container files unchanged.
        Jupyter cannot reach paths outside its root: use transport="ssh".
        The synchronous facade caches the root per notebook for its lifetime;
        async calls use fresh facade views and rediscover it. '..' is rejected.
        The result's remote preserves the caller's request;
        remote_path is the resolved container-absolute destination (upload)
        or source (download). Exec shares the container, but Jupyter starts
        at its root and SSH at the user's home (often /root), so a relative
        command path differs from a relative transfer path. Use remote_path
        or set exec's cwd explicitly, as shown below.
        Destinations name the exact file/directory, not a containing directory.

        Bridge a transfer to exec using the resolved path::

            import shlex
            from pathlib import PurePosixPath
            result = client.notebooks.upload(ref, local="model.bin", remote="model.bin")
            client.notebooks.exec(ref, command=f"python train.py {shlex.quote(result.remote_path)}")

        train.py must be accessible from cwd. If it is beside model.bin::

            client.notebooks.exec(ref, command="python train.py model.bin",
                                  cwd=str(PurePosixPath(result.remote_path).parent))

        Downloads and SSH publication replace complete files atomically;
        recursive directory merges are incremental, not transactional. SSH
        stages a full copy in remote /tmp (and locally for downloads).
        Jupyter upload atomicity depends on the server's ContentsManager;
        a failed/uncertain PUT may have changed the remote file. Writes are
        never replayed and failures never return a success result. Existing
        destinations are checked before overwrite=False; Jupyter has no
        conditional create, so callers must exclude concurrent remote writers.
        Local no-overwrite file publication also checks atomically. Symbolic
        links are unsupported by SSH; Jupyter root/symlink confinement is
        enforced by the server. Cancellation may leave SSH staging files.
        """
        from .notebook_transfer import transfer

        _duration(timeout, "timeout")
        with self.client._transport.scope(timeout=timeout):
            return transfer(
                self, ref, local=local, remote=remote, workspace=workspace,
                transport=transport, recursive=recursive, overwrite=overwrite,
                timeout=timeout, max_bytes=max_bytes, download=False,
            )

    @operation
    def download(
        self,
        ref: str | NotebookRef,
        *,
        local: str | Path,
        remote: str,
        workspace: str | WorkspaceRef | None = None,
        transport: str = "auto",
        recursive: bool = False,
        overwrite: bool = True,
        timeout: float = 120,
        max_bytes: int = DEFAULT_JUPYTER_MAX_BYTES,
    ) -> TransferResult:
        """Download a file, or recursively transfer a directory over cached SSH.

        Small files without a bridge: Jupyter. Large files/directories: SSH.
        Auto prefers reachable cached SSH; it does not choose by file size
        or create a bridge. Jupyter holds the entire base64 JSON body
        (~4/3 file size plus copies), has
        no resume, and defaults to a 16 MiB cap; raise max_bytes deliberately.
        On both transports, relative remote paths start at the discovered
        Jupyter contents root; absolute paths name container files unchanged.
        Jupyter cannot reach paths outside its root: use transport="ssh".
        The synchronous facade caches the root per notebook for its lifetime;
        async calls use fresh facade views and rediscover it. '..' is rejected.
        The result's remote preserves the caller's request;
        remote_path is the resolved container-absolute destination (upload)
        or source (download). Exec shares the container, but Jupyter starts
        at its root and SSH at the user's home (often /root), so a relative
        command path differs from a relative transfer path. Use remote_path
        or set exec's cwd explicitly, as shown below.
        Destinations name the exact file/directory, not a containing directory.

        Bridge a transfer to exec using the resolved path::

            import shlex
            from pathlib import PurePosixPath
            result = client.notebooks.download(ref, local="model.bin", remote="model.bin")
            client.notebooks.exec(ref, command=f"python train.py {shlex.quote(result.remote_path)}")

        train.py must be accessible from cwd. If it is beside model.bin::

            client.notebooks.exec(ref, command="python train.py model.bin",
                                  cwd=str(PurePosixPath(result.remote_path).parent))

        Downloads and SSH publication replace complete files atomically;
        recursive directory merges are incremental, not transactional. SSH
        stages a full copy in remote /tmp (and locally for downloads).
        Jupyter upload atomicity depends on the server's ContentsManager;
        a failed/uncertain PUT may have changed the remote file. Writes are
        never replayed and failures never return a success result. Existing
        destinations are checked before overwrite=False; Jupyter has no
        conditional create, so callers must exclude concurrent remote writers.
        Local no-overwrite file publication also checks atomically. Symbolic
        links are unsupported by SSH; Jupyter root/symlink confinement is
        enforced by the server. Cancellation may leave SSH staging files.
        """
        from .notebook_transfer import transfer

        _duration(timeout, "timeout")
        with self.client._transport.scope(timeout=timeout):
            return transfer(
                self, ref, local=local, remote=remote, workspace=workspace,
                transport=transport, recursive=recursive, overwrite=overwrite,
                timeout=timeout, max_bytes=max_bytes, download=True,
            )

    def _notebook(self, data, workspace_id, ref=None):
        view = public_notebook(data, fallback_name=ref.name if ref else "")
        raw = str(data.get("status") or "")
        name = str(data.get("name") or (ref.name if ref else ""))
        view.update(
            name=name,
            status=normalize_status(raw),
            raw_status=raw,
            sub_status=str(data.get("sub_status") or ""),
        )
        return Notebook(
            ref=ref or self._make_ref(NotebookRef, name, core.extract_notebook_id(data), workspace_id),
            **view,
        )

    def _all(self, ws, *, keyword=None, status=None):
        user_ids = self._current_user_ids()
        rows, seen, previous = [], set(), None
        for page in range(1, 101):
            items, total = browser_api.list_notebooks(
                ws.ref.key,
                user_ids=user_ids,
                keyword=keyword or "",
                status=[status.upper()] if status else None,
                page=platform_page(page, 100),
                page_size=100,
                session=self.session,
            )
            keys = tuple(core.extract_notebook_id(x) for x in items)
            if keys and (not all(keys) or keys == previous):
                raise ResolutionIncompleteError(
                    "Platform repeated a notebook page or omitted an identity."
                )
            previous = keys
            for item, key in zip(items, keys):
                if key not in seen:
                    rows.append(self._notebook(item, ws.ref.key))
                    seen.add(key)
            if not items:
                if total is not None and len(seen) < total:
                    raise ResolutionIncompleteError("Platform omitted a notebook page.")
                return rows
            if (total is not None and page * 100 >= total) or (total is None and len(items) < 100):
                return rows
        raise ResolutionIncompleteError("Notebook scan exceeded 100 pages; narrow the query.")

    @operation
    def list(
        self,
        workspace: str | WorkspaceRef,
        *,
        status: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[Notebook]:
        ws = self.client.workspaces.get(workspace)
        user_ids = self._current_user_ids()
        return self._server_page(
            lambda page, size: browser_api.list_notebooks(
                ws.ref.key,
                user_ids=user_ids,
                keyword=keyword or "",
                status=[status.upper()] if status else None,
                page=page,
                page_size=size,
                session=self.session,
            ),
            lambda row: self._notebook(row, ws.ref.key),
            page_size=100,
            limit=limit,
            cursor=cursor,
            query=(ws.ref.key, status, keyword),
            matches=(
                lambda row: (
                    status.casefold()
                    in (
                        row.status.casefold(),
                        row.raw_status.casefold(),
                    )
                )
            )
            if status
            else None,
        )

    def iter(
        self,
        workspace: str | WorkspaceRef,
        *,
        status: str | None = None,
        keyword: str | None = None,
        max_items: int | None = None,
    ) -> Iterator[Notebook]:
        if max_items is not None:
            positive(max_items, "max_items", 100000)
        cursor = None
        seen: set[str] = set()
        while True:
            page = self.list(
                workspace, status=status, keyword=keyword, cursor=cursor,
                limit=min(100, max_items - len(seen)) if max_items is not None else 100,
            )
            for item in page.items:
                if item.ref.key not in seen:
                    seen.add(item.ref.key)
                    yield item
                    if max_items is not None and len(seen) >= max_items:
                        return
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    def _resolve(self, selector, workspace=None):
        if isinstance(selector, NotebookRef):
            self.client._validate_ref(selector, NotebookRef)
            if workspace is not None:
                self.client._validate_ref(
                    selector, NotebookRef, self.client.workspaces.get(workspace).ref.key
                )
            return selector
        if workspace is None:
            raise ValidationError("workspace is required when selecting a notebook by name.")
        ws = self.client.workspaces.get(workspace)
        return self._indexed_resolution(selector, NotebookRef, ws.ref.key, lambda: self._all(ws, keyword=selector))

    @operation
    def get(
        self,
        ref: str | NotebookRef,
        *,
        workspace: str | WorkspaceRef | None = None,
    ) -> Notebook:
        resolved = self._resolve(ref, workspace)
        data = browser_api.get_notebook_detail(notebook_id=resolved.key, session=self.session)
        if not data:
            raise ResourceNotFoundError("Notebook no longer exists or is not visible.")
        return self._notebook(data, resolved.workspace_id, resolved)

    @operation
    def status(
        self,
        refs: Sequence[str | NotebookRef],
        *,
        workspace: str | WorkspaceRef | None = None,
    ) -> tuple[Notebook, ...]:
        if isinstance(refs, str):
            raise ValidationError("refs must be a sequence, not a string.")
        return tuple(self.get(name, workspace=workspace) for name in refs)

    def _groups(self, ws):
        return self._catalog(
            "compute_groups",
            (ws.ref.key,),
            lambda: browser_api.list_notebook_compute_groups(
                workspace_id=ws.ref.key, session=self.session
            ),
        )

    def _prices(self, ws, group):
        return self._catalog(
            "prices",
            (
                ws.ref.key,
                group,
                "SCHEDULE_CONFIG_TYPE_DSW",
            ),
            lambda: browser_api.get_resource_prices(
                workspace_id=ws.ref.key,
                logic_compute_group_id=group,
                schedule_config_type="SCHEDULE_CONFIG_TYPE_DSW",
                session=self.session,
            ),
        )

    def _priority_levels(self, ws):
        from inspire.platform.web.browser_api.availability import QUOTA_PRIORITY_SPEC_FIELDS

        try:
            return self._catalog(
                "priority_levels",
                (
                    ws.ref.key,
                    QUOTA_PRIORITY_SPEC_FIELDS["notebook"],
                ),
                lambda: browser_api.get_quota_priority_levels(
                    workspace_id=ws.ref.key,
                    spec_field=QUOTA_PRIORITY_SPEC_FIELDS["notebook"],
                    session=self.session,
                ),
            )
        except Exception:
            return None

    @operation
    def quotas(
        self,
        workspace: str | WorkspaceRef,
        *,
        group: str | ComputeGroupRef | None = None,
        include_empty: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[QuotaOption]:
        ws = self.client.workspaces.get(workspace)
        if isinstance(group, ComputeGroupRef):
            self.client._validate_ref(group, ComputeGroupRef, ws.ref.key)
        from inspire.services.catalog.workload_quota import query_workspace_quotas, sort_quota_rows

        groups = self._groups(ws)
        if isinstance(group, ComputeGroupRef):
            groups = [
                g for g in groups if (g.get("logic_compute_group_id") or g.get("id")) == group.key
            ]
        views = query_workspace_quotas(
            workspace_name=ws.name,
            workload="notebook",
            group_filter=group.casefold() if isinstance(group, str) else "",
            include_empty=include_empty,
            groups=groups,
            load_prices=lambda key: self._prices(ws, key),
            load_levels=lambda: self._priority_levels(ws),
            include_identity=True,
        )
        rows = []
        sort_quota_rows(views)
        for view in views:
            triple = parse_quota(view["quota"]) if view["quota"] else None
            group_ref = self._make_ref(
                ComputeGroupRef, view["compute_group"], view["group_id"], ws.ref.key
            )
            levels = view["allowed_priority_levels"]
            rows.append(
                QuotaOption(
                    view["quota"],
                    self._make_ref(
                        QuotaRef, view["quota"], view["quota_id"] or view["group_id"], ws.ref.key
                    ),
                    Quota(triple.gpu_count, triple.cpu_count, triple.memory_gib)
                    if triple
                    else None,
                    group_ref,
                    view["gpu_type"],
                    ws.name,
                    view["priority"],
                    tuple(levels) if levels is not None else None,
                    view["points_per_hour"],
                )
            )
        return self._page(rows, limit=limit, cursor=cursor, query=(ws.ref.key, group, include_empty))

    def _plan(self, spec):
        from inspire.services.catalog.datasets import parse_dataset_specs, resolve_dataset_info
        from inspire.task_priority import resolve_task_priority

        if not isinstance(spec, NotebookCreateSpec):
            raise ValidationError("Pass a NotebookCreateSpec.")
        if spec.auto_stop_after is not None and (
            type(spec.auto_stop_after) is not int or spec.auto_stop_after < 2
        ):
            raise ValidationError("auto_stop_after must be at least 2 minutes.")
        quota_text = (
            f"{spec.quota.gpu},{spec.quota.cpu},{spec.quota.memory_gib}"
            if isinstance(spec.quota, Quota)
            else (spec.quota.name if isinstance(spec.quota, QuotaRef) else spec.quota)
        )
        _, _, _, shm = core.resolve_create_inputs(
            config=self.client._config,
            quota=quota_text,
            project=spec.project.name if isinstance(spec.project, ProjectRef) else spec.project,
            image=spec.image if isinstance(spec.image, str) else spec.image.name,
            shm_size=spec.shm_gib,
        )
        ws = self.client.workspaces.get(spec.workspace)
        groups = builtins.list(selected_groups(self._groups(ws), "notebook"))
        group_resources = [
            Resource(
                str(g.get("name") or g.get("logic_compute_group_name") or ""),
                self._make_ref(
                    ComputeGroupRef,
                    str(g.get("name") or g.get("logic_compute_group_name") or ""),
                    g.get("id") or g.get("logic_compute_group_id"),
                    ws.ref.key,
                ),
            )
            for g in groups
        ]
        group = exact(group_resources, spec.group, ComputeGroupRef, self.client, ws.ref.key)
        group_data = groups[group_resources.index(group)]
        prices = self._prices(ws, group.ref.key)
        if isinstance(spec.quota, QuotaRef):
            self.client._validate_ref(spec.quota, QuotaRef, ws.ref.key)
            prices = [
                p for p in prices if (p.get("quota_id") or p.get("spec_id")) == spec.quota.key
            ]
            if not prices:
                raise ResourceNotFoundError(
                    "No quota matches this reference in the selected group."
                )
            gpu, cpu, mem, _ = quota_values(prices[0])
            quota_text = f"{gpu},{cpu},{mem}"
        quota = match_quota_rows(
            parse_quota(quota_text), [(group_data, p) for p in prices], group_override=group.name
        )
        quota = replace(
            quota,
            allowed_priority_levels=allowed_priority_levels_for(
                self._priority_levels(ws), quota.quota_id, workload="notebook"
            ),
        )
        projects = self._catalog(
            "projects",
            (ws.ref.key,),
            lambda: browser_api.list_projects(workspace_id=ws.ref.key, session=self.session),
        )
        project_values = [
            Resource(p.name, self._make_ref(ProjectRef, p.name, p.project_id, ws.ref.key))
            for p in projects
        ]
        project_ref = exact(project_values, spec.project, ProjectRef, self.client, ws.ref.key).ref
        project, _ = core.resolve_notebook_project(
            projects=projects,
            config=self.client._config,
            project=project_ref.name,
            needs_gpu_quota=quota.gpu_count > 0,
            workspace_id=ws.ref.key,
            session=self.session,
        )
        priority = resolve_task_priority(
            spec.priority,
            fair_scheduling=self._fair_scheduling(ws),
            project_limit=project.priority_name,
        )
        ensure_priority_allowed(quota, priority, quota_command="inspire notebook quota")
        image = self.client.images.get(spec.image, workspace=ws.ref)
        mounts = parse_dataset_specs(
            [
                f"{x.dataset}:{x.version}" if isinstance(x, DatasetMount) else x
                for x in spec.datasets
            ],
            field="datasets",
        )
        dataset_info = resolve_dataset_info(mounts, workspace_id=ws.ref.key, session=self.session)
        stop_hour, stop_minute = core.split_auto_stop_after(spec.auto_stop_after)
        name = spec.name or f"notebookrun-{uuid.uuid4().hex[:8]}"
        kwargs = core.build_notebook_create_kwargs(
            name=name,
            project_id=project.project_id,
            project_name=project.name,
            image_id=image.ref.key,
            image_url=image.url,
            quota=quota,
            shm_size=shm,
            auto_stop=spec.auto_stop or spec.auto_stop_after is not None,
            workspace_id=ws.ref.key,
            task_priority=priority,
            node_id=spec.node,
            dataset_info=dataset_info or None,
            enable_notification=spec.enable_notification,
            stop_hour=stop_hour,
            stop_minute=stop_minute,
            public_path_readonly=spec.public_path_readonly,
            project_path_readonly=spec.project_path_readonly,
        )
        return NotebookPlan(
            name,
            ws,
            Resource(project_ref.name, project_ref),
            group,
            image,
            Quota(quota.gpu_count, quota.cpu_count, quota.memory_gib),
            priority,
            shm,
            kwargs["auto_stop"],
            spec.auto_stop_after,
            tuple(mounts),
            kwargs,
        )

    @operation
    def plan(self, spec: NotebookCreateSpec) -> NotebookPlan:
        return self._plan(spec)

    @operation
    def create(
        self,
        spec: NotebookCreateSpec,
        *,
        operation_id: str | None = None,
    ) -> NotebookHandle:
        plan = self._plan(spec)
        identifier = uuid.uuid4().hex if operation_id is None else operation_id
        if not isinstance(identifier, str) or not identifier:
            raise ValidationError("operation_id must be a non-empty string.")
        try:
            taken = browser_api.notebook_name_exists(
                plan.name, workspace_id=plan.workspace.ref.key, session=self.session
            )
        except Exception:
            taken = False
        if taken:
            raise ValidationError(
                f"A notebook named '{plan.name}' already exists in this workspace."
            )
        session = self.session
        with self.client._transport.single_send(identifier, create=True, inspect="notebooks"):
            result = browser_api.create_notebook(**plan.create_kwargs, session=session)
        key = core.extract_notebook_id(result) or core.resolve_created_notebook_id(
            name=plan.name,
            workspace_id=plan.workspace.ref.key,
            session=session,
            user_ids_loader=self._current_user_ids,
        )
        if not key:
            raise SubmissionUncertainError(identifier, inspect="notebooks")
        return NotebookHandle(
            plan.name,
            self._make_ref(NotebookRef, plan.name, key, plan.workspace.ref.key),
            identifier,
        )

    def wait(
        self,
        ref: str | NotebookRef,
        *,
        timeout: float = 600,
        poll_interval: float = 5,
        target: str = "RUNNING",
        raise_on_failure: bool = False,
        workspace: str | WorkspaceRef | None = None,
    ) -> Notebook:
        """Wait for RUNNING or STOPPED; target is stripped and case-insensitive."""
        _duration(timeout, "timeout")
        _duration(poll_interval, "poll_interval")
        target = normalize_status(target.strip())
        if target not in ("RUNNING", "STOPPED"):
            raise ValidationError("target must be RUNNING or STOPPED.")
        with self.client._transport.scope(timeout=timeout):
            resolved = self._resolve(ref, workspace)
            while True:
                self.client._transport.remaining()
                notebook = self.get(resolved)
                if notebook.status == target:
                    return notebook
                if notebook.status in TERMINAL_STATUSES:
                    if raise_on_failure:
                        raise NotebookFailedError(notebook)
                    return notebook
                perform_sync(call(time.sleep, min(poll_interval, self.client._transport.remaining())))

    def _mutate(self, ref, action, workspace=None):
        resolved = self._resolve(ref, workspace)
        session = self.session
        with self.client._transport.single_send():
            return action(notebook_id=resolved.key, session=session)

    @operation
    def start(self, ref: str | NotebookRef, *, workspace: str | WorkspaceRef | None = None) -> None:
        self._mutate(ref, browser_api.start_notebook, workspace)

    @operation
    def stop(self, ref: str | NotebookRef, *, workspace: str | WorkspaceRef | None = None) -> None:
        self._mutate(ref, browser_api.stop_notebook, workspace)

    @operation
    def delete(
        self, ref: str | NotebookRef, *, workspace: str | WorkspaceRef | None = None
    ) -> None:
        self._mutate(ref, browser_api.delete_notebook, workspace)

    @operation
    def events(
        self,
        ref: str | NotebookRef,
        *,
        keyword: str | None = None,
        limit: int = 100,
        workspace: str | WorkspaceRef | None = None,
    ) -> EventResult:
        from inspire.services.job.job_events import matching_events

        positive(limit)
        resolved = self._resolve(ref, workspace)
        rows = matching_events(
            browser_api.list_notebook_events(resolved.key, session=self.session),
            keyword_filter=keyword,
        )
        return EventResult(tuple(rows[-limit:]), len(rows) > limit)

    def follow_events(
        self, ref: str | NotebookRef, *, interval: float = 5, **filters: Any
    ) -> Iterator[EventResult]:
        _duration(interval, "interval")
        with self.client._transport.scope(timeout=self.client.operation_timeout):
            resolved = self._resolve(ref, filters.pop("workspace", None))
        seen = set()
        while True:
            result = self.events(resolved, **filters)
            rows = []
            for row in result.items:
                key = repr(sorted(row.items()))
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
            if rows:
                yield EventResult(tuple(rows), result.truncated)
            perform_sync(call(time.sleep, interval))

    @operation
    def lifecycle(
        self,
        ref: str | NotebookRef,
        *,
        limit: int | None = None,
        workspace: str | WorkspaceRef | None = None,
    ) -> tuple[NotebookRun, ...]:
        if limit is not None:
            positive(limit)
        resolved = self._resolve(ref, workspace)
        rows = sorted(
            browser_api.list_notebook_runs(resolved.key, session=self.session),
            key=lambda x: x.get("index", 0),
        )
        return tuple(
            NotebookRun.from_view(row)
            for row in public_runs(rows[-limit:] if limit is not None else rows)
        )

    @operation
    def metrics(
        self,
        ref: str | NotebookRef,
        *,
        metric: str = "core",
        window: str = "1h",
        start: str | datetime | None = None,
        end: str | datetime | None = None,
        interval: str | None = None,
        group: str | ComputeGroupRef | None = None,
        workspace: str | WorkspaceRef | None = None,
    ) -> tuple[MetricGroup, ...]:
        from inspire.services.metrics import resolve_metrics, parse_window, parse_absolute
        from inspire.platform.web.browser_api.metrics import INTERVAL_CHOICES, TASK_TYPE_BY_RESOURCE

        resolved = self._resolve(ref, workspace)
        interval = interval or "1m"
        if interval not in INTERVAL_CHOICES:
            raise ValidationError(
                f"Invalid interval {interval!r}; choose from: {', '.join(INTERVAL_CHOICES)}"
            )

        def timestamp(value):
            return int(value.timestamp()) if isinstance(value, datetime) else parse_absolute(value)

        end_ts = timestamp(end) if end is not None else int(time.time())
        start_ts = timestamp(start) if start is not None else end_ts - parse_window(window)
        if end_ts <= start_ts:
            raise ValidationError("end time must be after start time")
        if group is not None:
            ws = WorkspaceRef(
                "",
                resolved.account,
                resolved.base_url,
                resolved.workspace_id,
                resolved.workspace_id,
            )
            lcg = self.client.compute_groups.get(group, workspace=ws).ref.key
        else:
            lcg = core.notebook_lcg_from_detail(
                browser_api.get_notebook_detail(notebook_id=resolved.key, session=self.session)
            )
        if not lcg:
            raise ValidationError("Unable to resolve compute group; pass group.")
        return tuple(
            browser_api.get_resource_metrics_by_time(
                task_id=resolved.key,
                task_type=TASK_TYPE_BY_RESOURCE["notebook"],
                logic_compute_group_id=lcg,
                metric_types=resolve_metrics(metric),
                start_timestamp=start_ts,
                end_timestamp=end_ts,
                interval_second=INTERVAL_CHOICES[interval],
                session=self.session,
            )
        )

    @operation
    def realtime_metrics(
        self, ref: str | NotebookRef, *, workspace: str | WorkspaceRef | None = None
    ) -> tuple[browser_api.NotebookResourceSnapshot, ...]:
        resolved = self._resolve(ref, workspace)
        return tuple(
            browser_api.get_notebook_realtime_metrics(
                notebook_id=resolved.key, session=self.session
            )
        )

    @operation
    def estimate_image_size(
        self, ref: str | NotebookRef, *, workspace: str | WorkspaceRef | None = None
    ) -> browser_api.NotebookImageSizeEstimate:
        resolved = self._resolve(ref, workspace)
        return browser_api.estimate_notebook_image_size(
            notebook_id=resolved.key, session=self.session
        )

    @operation
    @image_mutation
    def save_image(
        self,
        ref: str | NotebookRef,
        *,
        name: str,
        version: str | None = None,
        description: str | None = None,
        visibility: str | None = None,
        flatten: bool = False,
        workspace: str | WorkspaceRef | None = None,
    ) -> ImageSaveHandle:
        resolved = self._resolve(ref, workspace)
        version = "v1" if version is None else version
        visibility_value = None
        if visibility is not None:
            if visibility.lower() not in ("private", "project", "public"):
                raise ValidationError("visibility must be private, project or public.")
            visibility_value = "VISIBILITY_" + visibility.upper()
        try:
            estimate = self.estimate_image_size(resolved)
        except Exception:
            estimate = None
        if estimate is not None and not estimate.notebook_running:
            raise ValidationError(
                f"Notebook {resolved.name} is not running, so there is nothing to snapshot."
            )
        session = self.session
        with self.client._transport.single_send():
            result = browser_api.save_notebook_as_image(
                notebook_id=resolved.key,
                name=name,
                version=version,
                description=description or "",
                flatten=flatten,
                session=session,
            )
        key = core.resolve_saved_image_id(
            result, name=name, version=version, workspace_id=resolved.workspace_id, session=session
        )
        image_ref = (
            self._make_ref(ImageRef, f"{name}:{version}", key, resolved.workspace_id)
            if key
            else None
        )
        warning = None
        if visibility_value and key:
            try:
                with self.client._transport.single_send():
                    browser_api.update_image(
                        image_id=key, visibility=visibility_value, session=session
                    )
            except Exception as error:
                warning = f"Visibility was not updated: {error}"
        elif visibility_value:
            warning = "Set visibility after the image appears in the catalog."
        return ImageSaveHandle(
            f"{name}:{version}",
            image_ref,
            resolved,
            flatten=flatten,
            estimated_size_bytes=estimate.size_bytes if estimate else None,
            warning=warning,
        )

    @operation
    def cancel_save_image(
        self, ref: str | NotebookRef, *, workspace: str | WorkspaceRef | None = None
    ) -> bool:
        return self._mutate(ref, browser_api.cancel_notebook_image_save, workspace)

    def wait_image_ready(
        self,
        ref: str | ImageRef | ImageSelector | ImageSaveHandle,
        *,
        timeout: float = 600,
        poll_interval: float = 5,
        workspace: str | WorkspaceRef | None = None,
    ) -> CustomImageInfo:
        """Delegate to images.wait_ready with the same refs and workspace validation.

        Both methods also accept a notebook ImageSaveHandle; its ref must exist.
        Names and ImageSelector require workspace, while bound refs may omit it.
        """
        return self.client.images.wait_ready(
            ref, timeout=timeout, poll_interval=poll_interval, workspace=workspace
        )
