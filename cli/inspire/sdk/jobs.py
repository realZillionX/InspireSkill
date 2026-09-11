"""Training-job discovery, submission and observation."""

from __future__ import annotations
from .models import Instance
from .instances import sdk_instances, label_selectors
from inspire.exec_output import DEFAULT_MAX_OUTPUT_BYTES, OutputTarget
from typing import Callable
from inspire.services.execution.remote_exec import ExecResult
from typing import Iterator, Sequence, Any
import builtins
from .models import Page, WorkspaceRef, ComputeGroupRef
import math
import time
from inspire.platform.web.flow import call, perform_sync
import uuid
from datetime import datetime, timezone
from dataclasses import asdict
from .resources import Service, operation, exact, positive, platform_page
from .models import (
    Job,
    JobRef,
    JobHandle,
    JobCreateSpec,
    JobPlan,
    Quota,
    QuotaRef,
    QuotaOption,
    ProjectRef,
    LogResult,
    EventResult,
    MetricGroup,
    DatasetMount,
    Image,
    ImageRef,
)
from .exceptions import (
    ValidationError,
    ResolutionIncompleteError,
    ResourceNotFoundError,
    AmbiguousResourceError,
    SubmissionUncertainError,
    JobFailedError,
)
from inspire.services.job.job_output import public_job_status
from inspire.services.job.job_status import normalize_status, TERMINAL_STATUSES


class Jobs(Service):

    @operation
    def exec(
        self,
        ref: str | JobRef,
        *,
        command: str,
        workspace: str | WorkspaceRef | None = None,
        instance: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 120,
        on_output: Callable[[str], None] | None = None,
        max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
        output_to: OutputTarget = None,
        capture: bool = True,
    ) -> ExecResult:
        """Execute on one running instance; instance matches label or handle."""
        from .remote_exec import shaped_command, workload_exec

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
        from inspire.services.job.job_events import list_all_job_instances

        rows = list_all_job_instances(resolved.key, session=self.session)
        return workload_exec(
            self,
            key=resolved.key,
            workload="job",
            rows=rows,
            instance=instance,
            command=command,
            timeout=timeout,
            on_output=on_output,
            max_output_bytes=max_output_bytes,
            output_to=output_to,
            capture=capture,
        )

    def _all(self, ws, *, keyword=None):
        from inspire.platform.web.browser_api.jobs import list_jobs

        rows, seen, previous = [], set(), None
        for page in range(1, 101):
            items, total = list_jobs(
                workspace_id=ws.ref.key,
                keyword=keyword,
                page_num=platform_page(page, 100),
                page_size=100,
                session=self.session,
            )
            keys = tuple(x.job_id for x in items)
            if keys and keys == previous:
                raise ResolutionIncompleteError("Platform repeated a job page.")
            previous = keys
            for item in items:
                if item.job_id not in seen:
                    rows.append(self._job(asdict(item), ws.ref.key))
                    seen.add(item.job_id)
            if not items:
                if (page - 1) * 100 < total:
                    raise ResolutionIncompleteError("Platform omitted a job page.")
                return rows
            if page * 100 >= total:
                return rows
        raise ResolutionIncompleteError("Job scan exceeded 100 pages; narrow the query.")

    def _job(self, data, workspace_id, ref=None):
        data = dict(data)
        nested_raw = data.pop("raw", None) or {}
        data = dict(nested_raw, **data)
        name = str(data.get("name") or (ref.name if ref else ""))
        key = data.get("job_id") or (ref.key if ref else "")
        raw = str(data.get("status") or "")
        return Job(
            name,
            ref or self._make_ref(JobRef, name, key, workspace_id),
            normalize_status(raw),
            raw,
            str(data.get("project_name") or ""),
            str(data.get("created_at") or ""),
            str(data.get("finished_at") or ""),
            dict(data),
            public_job_status(data, fallback_name=name),
        )

    @operation
    def list(
        self,
        workspace: str | WorkspaceRef,
        *,
        status: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[Job]:
        positive(limit)
        ws = self.client.workspaces.get(workspace)
        from inspire.platform.web.browser_api.jobs import list_jobs

        offset, fingerprint = self._cursor_offset(cursor, (ws.ref.key, status, keyword))
        rows: builtins.list[Job] = []
        seen = set()
        previous = None
        for _ in range(100):
            page_num, skip = divmod(offset, 100)
            items, total = list_jobs(
                workspace_id=ws.ref.key,
                keyword=keyword,
                page_num=platform_page(page_num + 1, 100),
                page_size=100,
                session=self.session,
            )
            keys = tuple(item.job_id for item in items)
            if keys and keys == previous:
                raise ResolutionIncompleteError("Platform repeated a job page.")
            previous = keys
            if len(items) <= skip and offset < total:
                raise ResolutionIncompleteError("Platform omitted a job page.")
            for item in items[skip:]:
                job = self._job(asdict(item), ws.ref.key)
                if (
                    status is None
                    or status.casefold() in (job.status.casefold(), job.raw_status.casefold())
                ) and item.job_id not in seen:
                    if len(rows) == limit:
                        return Page(tuple(rows), self._encode_cursor(offset, fingerprint), None)
                    rows.append(job)
                    seen.add(item.job_id)
                offset += 1
            if offset >= total:
                return Page(tuple(rows), None, total if status is None else None)
            offset = (page_num + 1) * 100
        raise ResolutionIncompleteError("Job scan exceeded 100 pages; narrow the query.")

    def iter(
        self,
        workspace: str | WorkspaceRef,
        *,
        status: str | None = None,
        keyword: str | None = None,
        max_items: int | None = None,
    ) -> Iterator[Job]:
        if max_items is not None:
            positive(max_items, "max_items", 100000)
        cursor, seen, count = None, set(), 0
        while True:
            page = self.list(
                workspace=workspace,
                status=status,
                keyword=keyword,
                limit=min(100, max_items - count) if max_items else 100,
                cursor=cursor,
            )
            for item in page.items:
                if item.ref.key not in seen:
                    seen.add(item.ref.key)
                    count += 1
                    yield item
                    if max_items is not None and count >= max_items:
                        return
            if not page.next_cursor:
                return
            cursor = page.next_cursor

    def _resolve(self, selector, workspace=None):
        if isinstance(selector, JobRef):
            self.client._validate_ref(selector, JobRef)
            if workspace is not None:
                ws = self.client.workspaces.get(workspace)
                self.client._validate_ref(selector, JobRef, ws.ref.key)
            return selector
        if workspace is None:
            raise ValidationError("workspace is required when selecting a job by name.")
        ws = self.client.workspaces.get(workspace)
        return self._indexed_resolution(selector, JobRef, ws.ref.key, lambda: self._all(ws, keyword=selector))

    @operation
    def get(self, ref: str | JobRef, *, workspace: str | WorkspaceRef | None = None) -> Job:
        from inspire.platform.web.browser_api.jobs import get_job_detail_v2

        resolved = self._resolve(ref, workspace)
        data = get_job_detail_v2(resolved.key, session=self.session)
        if not data:
            raise ResourceNotFoundError("Job no longer exists or is not visible.")
        if data.get("workspace_id") and data["workspace_id"] != resolved.workspace_id:
            raise ValidationError("Job reference workspace does not match platform detail.")
        return self._job(data, resolved.workspace_id, resolved)

    def _quota_rows(self, ws, group):
        from inspire.platform.web.browser_api.notebooks import get_resource_prices
        from inspire.services.catalog.quotas import ResolvedQuota

        rows = self._catalog(
            "prices",
            (
                ws.ref.key,
                group.ref.key,
                "SCHEDULE_CONFIG_TYPE_TRAIN",
            ),
            lambda: get_resource_prices(
                workspace_id=ws.ref.key,
                logic_compute_group_id=group.ref.key,
                schedule_config_type="SCHEDULE_CONFIG_TYPE_TRAIN",
                session=self.session,
            ),
        )
        result = []
        for row in rows:
            key = str(row.get("quota_id") or row.get("spec_id") or "")
            if not key:
                raise ResolutionIncompleteError("Quota catalog omitted a tier identity.")
            from inspire.services.catalog.workload_quota import quota_values

            gpu_count, cpu_count, memory_gib, gpu = quota_values(row)
            quota = Quota(gpu_count, cpu_count, memory_gib)
            name = f"{quota.gpu},{quota.cpu},{quota.memory_gib}"
            public = QuotaOption(
                name, self._make_ref(QuotaRef, name, key, ws.ref.key), quota, group.ref, gpu
            )
            resolved = ResolvedQuota(
                key, group.ref.key, group.name, quota.gpu, quota.cpu, quota.memory_gib, gpu, row
            )
            result.append((public, resolved))
        return result

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
        from dataclasses import replace
        from inspire.services.catalog.workload_quota import query_workspace_quotas

        ws = self.client.workspaces.get(workspace)
        groups = self.client.compute_groups._all(ws)
        if isinstance(group, ComputeGroupRef):
            self.client._validate_ref(group, ComputeGroupRef, ws.ref.key)
            groups = [(item, data) for item, data in groups if item.ref.key == group.key]
        by_group = {item.ref.key: item for item, _ in groups}
        options, levels = {}, {}

        def prices(key):
            rows = []
            for option, resolved in self._quota_rows(ws, by_group[key]):
                options[(key, option.ref.key)] = option
                if resolved.allowed_priority_levels is not None:
                    levels[option.ref.key] = resolved.allowed_priority_levels
                rows.append(
                    dict(
                        resolved.raw_price,
                        quota_id=option.ref.key,
                        gpu_count=resolved.gpu_count,
                        cpu_count=resolved.cpu_count,
                        memory_size_gib=resolved.memory_gib,
                    )
                )
            return rows

        views = query_workspace_quotas(
            workspace_name=ws.name,
            workload="job",
            group_filter=group.casefold() if isinstance(group, str) else "",
            include_empty=include_empty,
            groups=[
                dict(data, id=item.ref.key, logic_compute_group_id=item.ref.key, name=item.name)
                for item, data in groups
            ],
            load_prices=prices,
            load_levels=lambda: levels,
            include_identity=True,
        )
        rows = []
        for view in views:
            key = view["group_id"]
            option = options.get((key, view["quota_id"]))
            if option is None:
                option = QuotaOption(
                    "", self._make_ref(QuotaRef, "", key, ws.ref.key), None, by_group[key].ref, ""
                )
            allowed = view["allowed_priority_levels"]
            rows.append(
                replace(
                    option,
                    workspace=ws.name,
                    priority=view["priority"],
                    allowed_priority_levels=tuple(allowed) if allowed is not None else None,
                    points_per_hour=view["points_per_hour"],
                )
            )
        return self._page(rows, limit=limit, cursor=cursor, query=(ws.ref.key, group, include_empty))

    def _plan(self, spec):
        from inspire.platform.web.browser_api.availability import get_quota_priority_levels
        from inspire.services.job.job_submission import build_training_job_plan
        from inspire.task_priority import resolve_task_priority

        if not isinstance(spec, JobCreateSpec):
            raise ValidationError("Pass a JobCreateSpec.")
        for name in ("name", "command"):
            if not isinstance(getattr(spec, name), str) or not getattr(spec, name).strip():
                raise ValidationError(f"{name} must be non-empty.")
        positive(spec.nodes, "nodes", 10000)
        if spec.shm_gib is not None:
            positive(spec.shm_gib, "shm_gib", 10000000)
        if spec.max_time_hours is not None and (
            isinstance(spec.max_time_hours, bool)
            or not isinstance(spec.max_time_hours, (int, float))
            or not math.isfinite(spec.max_time_hours)
            or spec.max_time_hours <= 0
        ):
            raise ValidationError("max_time_hours must be finite and positive.")
        if not isinstance(spec.quota, (str, Quota, QuotaRef)):
            raise ValidationError("quota must be str, Quota or QuotaRef.")
        requested_quota = spec.quota
        if isinstance(requested_quota, str):
            from inspire.services.catalog.quotas import parse_quota

            parsed = parse_quota(requested_quota)
            requested_quota = Quota(parsed.gpu_count, parsed.cpu_count, parsed.memory_gib)
        ws = self.client.workspaces.get(spec.workspace)
        project_rows = self.client.projects._all(ws)
        project = exact(
            [x[0] for x in project_rows], spec.project, ProjectRef, self.client, ws.ref.key
        )
        project_data = next(x[1] for x in project_rows if x[0].ref.key == project.ref.key)
        from inspire.services.catalog.compute_groups import group_supports_workload

        group_rows = self.client.compute_groups._all(ws)
        group = exact(
            [x[0] for x in group_rows], spec.group, ComputeGroupRef, self.client, ws.ref.key
        )
        group_data = next(x[1] for x in group_rows if x[0].ref.key == group.ref.key)
        if not group_supports_workload(group_data, "job"):
            raise ValidationError("Selected compute group does not support training jobs.")
        if isinstance(spec.image, str) and "/" in spec.image:
            image = Image(
                spec.image,
                self._make_ref(ImageRef, spec.image, spec.image, ws.ref.key),
                "url",
                spec.image,
            )
        else:
            image = self.client.images.get(spec.image, workspace=ws.ref)
        options = self._quota_rows(ws, group)
        if isinstance(spec.quota, QuotaRef):
            self.client._validate_ref(spec.quota, QuotaRef, ws.ref.key)
            matches = [x for x in options if x[0].ref.key == spec.quota.key]
        else:
            matches = [x for x in options if x[0].quota == requested_quota]
        if not matches:
            raise ResourceNotFoundError("No exact quota tier matches in the selected group.")
        if len(matches) > 1:
            raise AmbiguousResourceError("Multiple quota tiers match.", [x[0] for x in matches])
        public_quota, quota = matches[0]
        priority = resolve_task_priority(
            spec.priority,
            fair_scheduling=self._fair_scheduling(ws),
            project_limit=project_data.priority_name,
        )
        levels = self._catalog(
            "priority_levels",
            (
                ws.ref.key,
                "predef_train_spec",
            ),
            lambda: get_quota_priority_levels(
                ws.ref.key, spec_field="predef_train_spec", session=self.session
            ),
        ).get(quota.quota_id)
        if levels and all(x in ("low", "high") for x in levels):
            if ("low" if priority <= 1 else "high") not in levels:
                raise ValidationError("Requested priority is incompatible with this quota tier.")
        from inspire.services.catalog.datasets import parse_dataset_specs, resolve_dataset_info

        mounts = parse_dataset_specs(
            [
                f"{item.dataset}:{item.version}" if isinstance(item, DatasetMount) else item
                for item in spec.datasets
            ],
            field="datasets",
        )
        dataset_info = resolve_dataset_info(mounts, workspace_id=ws.ref.key, session=self.session)
        config = self.client._config
        plan = build_training_job_plan(
            config=self.client._config,
            name=spec.name,
            command=spec.command,
            quota=quota,
            framework=spec.framework,
            project_id=project.ref.key,
            workspace_id=ws.ref.key,
            image=image.url,
            priority=priority,
            nodes=spec.nodes,
            max_time_hours=spec.max_time_hours,
            shm_size=spec.shm_gib,
            shm_size_hint="shm_gib",
            fault_tolerance_dependency_message=(
                "fault_tolerance_retry_interval_sec only applies with auto_fault_tolerance."
            ),
            description=spec.description,
            project_name=project.name,
            auto_fault_tolerance=(
                spec.auto_fault_tolerance
                if spec.auto_fault_tolerance is not None
                else config.job_auto_fault_tolerance
            ),
            fault_tolerance_max_retry=(
                spec.fault_tolerance_max_retry
                if spec.fault_tolerance_max_retry is not None
                else config.job_fault_tolerance_max_retry
            ),
            fault_tolerance_retry_interval_sec=spec.fault_tolerance_retry_interval_sec,
            dataset_info=dataset_info,
            envs=[{"name": k, "value": v} for k, v in spec.envs.items()],
            keep_after_success_hours=spec.keep_after_success_hours,
            keep_after_failure_hours=spec.keep_after_failure_hours,
            public_path_readonly=spec.public_path_readonly,
            enable_notification=(
                spec.enable_notification
                if spec.enable_notification is not None
                else config.job_enable_notification
            ),
            exclude_nodes=spec.exclude_nodes,
            specified_nodes=spec.specified_nodes,
        )
        return JobPlan(
            spec.name,
            ws,
            project,
            group,
            image,
            public_quota.quota,
            priority,
            spec.nodes,
            tuple(mounts),
            len(spec.envs),
            spec.description,
            plan.max_time_ms,
            plan.shm_size_gib,
            dict(plan.create_kwargs),
        ), plan

    @operation
    def plan(self, spec: JobCreateSpec) -> JobPlan:
        return self._plan(spec)[0]

    @operation
    def create(self, spec: JobCreateSpec, *, operation_id: str | None = None) -> JobHandle:
        from inspire.platform.web.browser_api.jobs import create_training_job

        identifier = uuid.uuid4().hex if operation_id is None else operation_id
        if not isinstance(identifier, str) or not identifier:
            raise ValidationError("operation_id must be a non-empty string.")
        public, plan = self._plan(spec)
        session = self.session
        with self.client._transport.single_send(identifier, create=True, inspect="jobs"):
            data = create_training_job(payload=plan.create_kwargs, session=session)
        key = data.get("job_id") or data.get("id")
        if not isinstance(key, str) or not key:
            raise SubmissionUncertainError(identifier, inspect="jobs")
        return JobHandle(
            spec.name, self._make_ref(JobRef, spec.name, key, public.workspace.ref.key), identifier
        )

    def wait(
        self,
        ref: str | JobRef,
        *,
        workspace: str | WorkspaceRef | None = None,
        timeout: float = 3600,
        poll_interval: float = 10,
        raise_on_failure: bool = False,
    ) -> Job:
        for parameter, value in (("timeout", timeout), ("poll_interval", poll_interval)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not (math.isfinite(value) and value > 0)
            ):
                raise ValidationError(f"{parameter} must be finite positive seconds.")
        with self.client._transport.scope(timeout=timeout):
            resolved = self._resolve(ref, workspace)
            while True:
                self.client._transport.remaining()
                job = self.get(resolved)
                if job.status in TERMINAL_STATUSES:
                    if raise_on_failure and job.status != "SUCCEEDED":
                        raise JobFailedError(job)
                    return job
                perform_sync(call(time.sleep, min(poll_interval, self.client._transport.remaining())))

    @operation
    def stop(self, ref: str | JobRef, *, workspace: str | WorkspaceRef | None = None) -> None:
        from inspire.platform.web.browser_api.jobs import stop_training_job

        resolved = self._resolve(ref, workspace)
        session = self.session
        with self.client._transport.single_send():
            stop_training_job(resolved.key, session=session)

    @operation
    def delete(
        self, ref: str | JobRef, *, workspace: str | WorkspaceRef | None = None
    ) -> None:
        from inspire.platform.web.browser_api.jobs import delete_job

        resolved = self._resolve(ref, workspace)
        session = self.session
        with self.client._transport.single_send():
            delete_job(resolved.key, session=session)

    @operation
    def status(
        self,
        refs: Sequence[str | JobRef],
        *,
        workspace: str | WorkspaceRef | None = None,
    ) -> tuple[Job, ...]:
        if isinstance(refs, str):
            raise ValidationError("refs must be a sequence, not a string.")
        from inspire.platform.web.browser_api.jobs import list_jobs_by_ids

        resolved = [self._resolve(ref, workspace) for ref in refs]
        groups: dict[str, builtins.list[str]] = {}
        for ref in resolved:
            groups.setdefault(ref.workspace_id, []).append(ref.key)
        records = {
            ws: list_jobs_by_ids(keys, workspace_id=ws, session=self.session)
            for ws, keys in groups.items()
        }
        jobs = []
        for ref in resolved:
            data = records[ref.workspace_id].get(ref.key)
            if not data:
                raise ResourceNotFoundError("Job no longer exists or is not visible.")
            if data.get("workspace_id") and data["workspace_id"] != ref.workspace_id:
                raise ValidationError("Job reference workspace does not match platform detail.")
            jobs.append(self._job(data, ref.workspace_id, ref))
        return tuple(jobs)

    @operation
    def command(
        self, ref: str | JobRef, *, workspace: str | WorkspaceRef | None = None
    ) -> str:
        from inspire.platform.web.browser_api.jobs import get_job_detail_v2

        resolved = self._resolve(ref, workspace)
        return str(get_job_detail_v2(resolved.key, session=self.session).get("command") or "")

    @operation
    def instances(
        self, ref: str | JobRef, *, workspace: str | WorkspaceRef | None = None
    ) -> tuple[Instance, ...]:
        from inspire.services.job.job_events import list_all_job_instances

        resolved = self._resolve(ref, workspace)
        return sdk_instances("job", list_all_job_instances(resolved.key, session=self.session))

    def instance_names(
        self, ref: str | JobRef, *, workspace: str | WorkspaceRef | None = None
    ) -> tuple[str, ...]:
        return tuple(row.label for row in self.instances(ref, workspace=workspace))

    @operation
    def events(
        self,
        ref: str | JobRef,
        *,
        workspace: str | WorkspaceRef | None = None,
        type: str | None = None,
        reason: str | None = None,
        instance: str | Sequence[str] | None = None,
        workload_level: bool = False,
        limit: int = 100,
    ) -> EventResult:
        from inspire.services.job.job_events import collect_job_events, matching_events

        resolved = self._resolve(ref, workspace)
        selectors = (instance,) if isinstance(instance, str) else instance or ()
        rows = collect_job_events(
            resolved.key, session=self.session, instance=selectors, workload_level=workload_level,
            conflict_message="workload_level and instance cannot be used together.",
        )
        rows = matching_events(rows, type_filter=type, reason_filter=reason)
        selected = rows[-limit:] if limit > 0 else rows
        return EventResult(tuple(selected), len(selected) < len(rows))

    def follow_events(
        self, ref: str | JobRef, *, interval: float = 5, **filters: Any
    ) -> Iterator[EventResult]:
        if not math.isfinite(interval) or interval <= 0:
            raise ValidationError("interval must be finite positive seconds")
        with self.client._transport.scope(timeout=self.client.operation_timeout):
            ref = self._resolve(ref, filters.pop("workspace", None))
        seen = set()
        while True:
            result = self.events(ref, **filters)
            rows = []
            for row in result.items:
                key = repr(sorted(row.items()))
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
            if rows:
                yield EventResult(tuple(rows), result.truncated)
            if self.get(ref).status in TERMINAL_STATUSES:
                return
            perform_sync(call(time.sleep, interval))

    @operation
    def metrics(
        self,
        ref: str | JobRef,
        *,
        workspace: str | WorkspaceRef | None = None,
        metric: str = "core",
        window: str = "1h",
        start: str | datetime | None = None,
        end: str | datetime | None = None,
        interval: str = "1m",
        group: str | ComputeGroupRef | None = None,
    ) -> tuple[MetricGroup, ...]:
        from inspire.services.metrics import resolve_metrics, parse_window, parse_absolute
        from inspire.platform.web.browser_api.metrics import (
            get_resource_metrics_by_time,
            INTERVAL_CHOICES,
            TASK_TYPE_BY_RESOURCE,
        )
        from inspire.platform.web.browser_api.jobs import get_job_detail_v2

        if interval not in INTERVAL_CHOICES:
            raise ValidationError(
                f"Invalid interval {interval!r}; choose from: {', '.join(INTERVAL_CHOICES)}"
            )
        resolved = self._resolve(ref, workspace)

        def timestamp(value):
            return int(value.timestamp()) if isinstance(value, datetime) else parse_absolute(value)

        end_ts = timestamp(end) if end is not None else int(time.time())
        start_ts = timestamp(start) if start is not None else end_ts - parse_window(window)
        if end_ts <= start_ts:
            raise ValidationError("end time must be after start time")
        if group is not None:
            ws = WorkspaceRef("", resolved.account, resolved.base_url, resolved.workspace_id, resolved.workspace_id)
            lcg = self.client.compute_groups.get(group, workspace=ws).resolved.key
        else:
            lcg = get_job_detail_v2(resolved.key, session=self.session).get("logic_compute_group_id")
        if not lcg:
            raise ValidationError("Unable to resolve compute group; pass group.")
        return tuple(
            get_resource_metrics_by_time(
                task_id=resolved.key,
                task_type=TASK_TYPE_BY_RESOURCE["job"],
                logic_compute_group_id=lcg,
                metric_types=resolve_metrics(metric),
                start_timestamp=start_ts,
                end_timestamp=end_ts,
                interval_second=INTERVAL_CHOICES[interval],
                session=self.session,
            )
        )

    @operation
    def logs(
        self,
        ref: str | JobRef,
        *,
        workspace: str | WorkspaceRef | None = None,
        instance: str | Sequence[str] | None = None,
        window: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        tail: int | None = None,
        head: int | None = None,
        limit: int = 100,
        max_chars: int | None = None,
    ) -> LogResult:
        """Read logs by instance label or handle, singly or as a sequence.

        None and "all" select every instance. Print labels, never handles."""
        from inspire.services.job.job_logs import (
            window_to_minutes,
            web_log_time_range,
            fetch_job_logs,
            select_job_logs,
            format_log_line,
        )

        if tail is not None and head is not None:
            raise ValidationError("tail and head cannot be combined")
        positive(limit)
        for value, label in ((tail, "tail"), (head, "head"), (max_chars, "max_chars")):
            if value is not None:
                positive(value, label, 10000000)
        job = self.get(ref, workspace=workspace)
        from inspire.services.job.job_events import job_instance_views, select_job_instance_views

        views = self.instances(job.ref)
        selectors = label_selectors(views, instance)
        service_views = job_instance_views([view.raw for view in views])
        pods = tuple(view.handle for view in select_job_instance_views(service_views, selectors))
        if start is not None or end is not None:
            if start is None or end is None:
                raise ValidationError("Both start and end are required.")
            start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        else:
            start_ms, end_ms = web_log_time_range(
                asdict(job), window_to_minutes(window) if window else None
            )
        rows, total = (
            fetch_job_logs(
                job_id=job.ref.key,
                pod_names=builtins.list(pods),
                start_ms=start_ms,
                end_ms=end_ms,
                limit=max(limit, tail or 0, head or 0),
                session=self.session,
            )
            if pods
            else ([], 0)
        )
        selection = select_job_logs(
            rows, total=total, tail=tail, head=head, record_limit=limit, all_output=False
        )
        text = "\n".join(format_log_line(row) for row in selection.logs)
        truncated = selection.truncated or (max_chars is not None and len(text) > max_chars)
        if max_chars is not None:
            text = text[:max_chars] if head is not None else text[-max_chars:]
        return LogResult(
            text,
            tuple(pods),
            datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat(),
            datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat(),
            truncated,
            total,
            tuple(selection.logs),
        )

    def follow_logs(
        self, ref: str | JobRef, *, interval: float = 2, **filters: Any
    ) -> Iterator[LogResult]:
        from inspire.services.job.job_logs import web_log_identity, format_log_line

        if not math.isfinite(interval) or interval <= 0:
            raise ValidationError("interval must be finite positive seconds")
        with self.client._transport.scope(timeout=self.client.operation_timeout):
            ref = self._resolve(ref, filters.pop("workspace", None))
        seen: set[tuple[int, str, str, int]] = set()
        draining = False
        while True:
            result = self.logs(ref, **filters)
            rows = tuple(row for row in result.items if web_log_identity(row) not in seen)
            seen.update(web_log_identity(row) for row in result.items)
            if rows:
                yield LogResult(
                    "\n".join(format_log_line(row) for row in rows),
                    result.instances,
                    result.start,
                    result.end,
                    result.truncated,
                    result.total,
                    rows,
                )
            if draining:
                return
            draining = self.get(ref).status in TERMINAL_STATUSES
            perform_sync(call(time.sleep, interval))
