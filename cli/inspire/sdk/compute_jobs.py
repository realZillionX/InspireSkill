"""Shared SDK paging, identity, observation and quota mechanics for HPC/Ray."""

from __future__ import annotations

import builtins
import math
import time
from inspire.platform.web.flow import call, perform_sync
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable, Generic, Iterator, Protocol, Sequence, TypeVar
from inspire.platform.web import browser_api
from inspire.services.catalog.quotas import parse_quota
from inspire.services.catalog.workload_quota import selected_groups, match_quota_rows, quota_values
from .resources import Service, operation, exact, positive, platform_page
from .models import (
    ResourceRef,
    Resource,
    WorkspaceRef,
    ComputeGroupRef,
    ProjectRef,
    Quota,
    QuotaRef,
    QuotaOption,
    Page,
    EventResult,
    MetricGroup,
)
from .models_compute import WorkloadJob
from .exceptions import ValidationError, ResourceNotFoundError, ResolutionIncompleteError


R = TypeVar("R", bound=ResourceRef)
J = TypeVar("J", bound=WorkloadJob)


class InstanceView(Protocol):
    @property
    def handle(self) -> str: ...

    @property
    def label(self) -> str: ...


V = TypeVar("V", bound=InstanceView)


@dataclass(frozen=True)
class WorkloadBinding(Generic[V]):
    list_page_size: int
    list_jobs: Callable[..., tuple[Sequence[Any], int]]
    get_detail: Callable[..., dict[str, Any]]
    stop: Callable[..., object]
    delete: Callable[..., object]
    start: Callable[..., object] | None
    create: Callable[..., dict[str, Any]]
    created_id: Callable[[object], str]
    normalize_status: Callable[[str], str]
    matches_status: Callable[[str, str | None], bool]
    terminal_statuses: frozenset[str]
    success_statuses: frozenset[str]
    public_status: Callable[..., dict[str, Any]]
    fetch_instances: Callable[..., tuple[list[dict[str, Any]], int]]
    instance_views: Callable[[Sequence[dict[str, Any]]], list[V]]
    select_instance_views: Callable[[Sequence[V], Sequence[str]], list[V]]
    list_logs: Callable[..., tuple[list[dict[str, Any]], int]]
    labelled_logs: Callable[[list[dict[str, Any]], list[V]], list[dict[str, Any]]]
    log_window: Callable[
        [list[dict[str, Any]], Callable[[], dict[str, Any]], int | None], tuple[int, int]
    ]
    log_max_window_ms: int
    schedule_config_type: str
    task_type: str
    metric_group: Callable[[object], str | None]
    expand_tail_fetch: bool
    get_details_by_ids: Callable[..., dict[str, dict]] | None = None


def duration(value: float, parameter: str, *, unit: str = "seconds") -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValidationError(f"{parameter} must be finite positive {unit}.")


class ComputeJobs(Service, Generic[R, J, V]):
    _kind: str
    _ref_type: type[R]
    _model: Any
    _failure: Any

    _binding: WorkloadBinding[V]

    def _detail(self, key):
        return self._binding.get_detail(key, session=self.session)

    def _job(self, data, ws, ref=None):
        name = str(data.get("name") or data.get("job_name") or (ref.name if ref else ""))
        key = str(data.get("job_id") or data.get("ray_job_id") or data.get("id") or "")
        raw = str(data.get("status") or "")
        view = self._binding.public_status(data, fallback_name=name)
        return self._model(
            name,
            ref or self._make_ref(self._ref_type, name, key, ws),
            self._binding.normalize_status(raw),
            raw,
            str(data.get("project_name") or ""),
            str(data.get("created_at") or ""),
            str(data.get("finished_at") or ""),
            dict(data),
            view,
        )

    def _list_job(self, row, ws):
        data = asdict(row)
        raw = data.pop("raw", None) or {}
        return self._job(dict(raw, **data), ws.ref.key)

    def _fetch(self, ws, page, page_size, *, keyword=None, project=None, status=None):
        return self._binding.list_jobs(
            workspace_id=ws.ref.key, page_num=page, page_size=page_size,
            session=self.session,
        )

    def _all(self, ws, *, keyword):
        # Only exact candidates are retained; the scan must prove uniqueness.
        rows, seen, previous = [], set(), None
        size = self._binding.list_page_size
        for page in range(1, 101):
            items, total = self._fetch(ws, platform_page(page, size), size, keyword=keyword)
            jobs = [self._list_job(row, ws) for row in items]
            keys = tuple(job.ref.key for job in jobs)
            if keys and keys == previous:
                raise ResolutionIncompleteError("Platform repeated a job page.")
            previous = keys
            for job in jobs:
                if job.name.casefold() == keyword.strip().casefold() and job.ref.key not in seen:
                    rows.append(job)
                    seen.add(job.ref.key)
            if len(rows) > 1:
                exact(rows, keyword, self._ref_type, self.client, ws.ref.key)
            if (page - 1) * size + len(items) >= total:
                return rows
            if len(items) < size:
                raise ResolutionIncompleteError("Platform omitted a job page.")
        raise ResolutionIncompleteError("Job name scan exceeded 100 pages; narrow the query.")

    def _list(self, ws, *, status, keyword, limit, cursor, project=None, local_keyword=True):
        needle = keyword.strip().casefold() if keyword and local_keyword else ""

        def matches(row):
            return self._binding.matches_status(row.raw_status, status) and (
                not needle or any(
                    needle in str(value or "").casefold()
                    for value in (
                        row.name, row.raw_status, row.project, ws.name,
                        row.raw.get("entrypoint"), row.raw.get("compute_group_name"),
                        row.raw.get("created_by_name"),
                    )
                )
            )

        return self._server_page(
            lambda page, size: self._fetch(ws, page, size, keyword=keyword, project=project, status=status),
            lambda row: self._list_job(row, ws),
            page_size=self._binding.list_page_size, limit=limit, cursor=cursor,
            query=(ws.ref.key, project, status, keyword),
            matches=matches if status is not None or needle else None,
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
    ) -> Page[J]:
        ws = self.client.workspaces.get(workspace)
        return self._list(ws, status=status, keyword=keyword, limit=limit, cursor=cursor)

    def iter(
        self,
        workspace: str | WorkspaceRef,
        *,
        status: str | None = None,
        keyword: str | None = None,
        max_items: int | None = None,
    ) -> Iterator[J]:
        if max_items is not None:
            positive(max_items, "max_items", 100000)
        cursor = None
        seen: set[str] = set()
        while True:
            page = self.list(
                workspace, status=status, keyword=keyword, cursor=cursor,
                limit=min(self._binding.list_page_size, max_items - len(seen))
                if max_items is not None else self._binding.list_page_size,
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
        if isinstance(selector, self._ref_type):
            self.client._validate_ref(selector, self._ref_type)
            if workspace is not None:
                self.client._validate_ref(
                    selector, self._ref_type, self.client.workspaces.get(workspace).ref.key
                )
            return selector
        if workspace is None:
            raise ValidationError("workspace is required when selecting a job by name.")
        if not isinstance(selector, str) or not selector.strip():
            raise ValidationError("Use a non-empty name or the matching resource reference.")
        ws = self.client.workspaces.get(workspace)
        return self._indexed_resolution(selector, self._ref_type, ws.ref.key, lambda: self._all(ws, keyword=selector))

    @operation
    def get(self, ref: str | R, *, workspace: str | WorkspaceRef | None = None) -> J:
        resolved = self._resolve(ref, workspace)
        data = self._detail(resolved.key)
        if not data:
            raise ResourceNotFoundError("Job no longer exists or is not visible.")
        if (
            resolved.workspace_id
            and data.get("workspace_id")
            and data["workspace_id"] != resolved.workspace_id
        ):
            raise ValidationError("Job reference workspace does not match platform detail.")
        return self._job(data, resolved.workspace_id, resolved)

    @operation
    def status(
        self,
        refs: Sequence[str | R],
        *,
        workspace: str | WorkspaceRef | None = None,
    ) -> tuple[J, ...]:
        if isinstance(refs, str):
            raise ValidationError("refs must be a sequence, not a string.")
        get_details = self._binding.get_details_by_ids
        if get_details is None:
            return tuple(self.get(ref, workspace=workspace) for ref in refs)
        resolved = [self._resolve(ref, workspace) for ref in refs]
        groups: dict[str, builtins.list[str]] = {}
        for ref in resolved:
            groups.setdefault(ref.workspace_id, []).append(ref.key)
        records = {
            ws: get_details(keys, workspace_id=ws, session=self.session)
            for ws, keys in groups.items()
        }
        jobs = []
        for ref in resolved:
            data = records[ref.workspace_id].get(ref.key)
            if not data:
                raise ResourceNotFoundError("Job no longer exists or is not visible.")
            if (
                ref.workspace_id
                and data.get("workspace_id")
                and data["workspace_id"] != ref.workspace_id
            ):
                raise ValidationError("Job reference workspace does not match platform detail.")
            jobs.append(self._job(data, ref.workspace_id, ref))
        return tuple(jobs)

    def wait(
        self,
        ref: str | R,
        *,
        timeout: float = 3600,
        poll_interval: float = 10,
        raise_on_failure: bool = False,
        workspace: str | WorkspaceRef | None = None,
    ) -> J:
        duration(timeout, "timeout")
        duration(poll_interval, "poll_interval")
        with self.client._transport.scope(timeout=timeout):
            resolved = self._resolve(ref, workspace)
            while True:
                self.client._transport.remaining()
                job = self.get(resolved)
                if job.status in self._binding.terminal_statuses:
                    if raise_on_failure and job.status not in self._binding.success_statuses:
                        raise self._failure(job)
                    return job
                perform_sync(call(time.sleep, min(poll_interval, self.client._transport.remaining())))

    def _mutate(self, ref, action: Callable[..., object], workspace):
        resolved = self._resolve(ref, workspace)
        session = self.session
        with self.client._transport.single_send():
            action(resolved.key, session=session)

    @operation
    def stop(self, ref: str | R, *, workspace: str | WorkspaceRef | None = None) -> None:
        self._mutate(ref, self._binding.stop, workspace)

    @operation
    def delete(self, ref: str | R, *, workspace: str | WorkspaceRef | None = None) -> None:
        self._mutate(ref, self._binding.delete, workspace)

    def follow_events(
        self, ref: str | R, *, interval: float = 5, **filters: Any
    ) -> Iterator[EventResult]:
        duration(interval, "interval")
        with self.client._transport.scope(timeout=self.client.operation_timeout):
            resolved = self._resolve(ref, filters.pop("workspace", None))
        seen = set()
        while True:
            result = self._follow_event_batch(resolved, **filters)
            rows = []
            for row in result.items:
                key = repr(sorted(row.items()))
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
            if rows:
                yield EventResult(tuple(rows), result.truncated)
            perform_sync(call(time.sleep, interval))

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
                self._binding.schedule_config_type,
            ),
            lambda: browser_api.get_resource_prices(
                workspace_id=ws.ref.key,
                logic_compute_group_id=group,
                schedule_config_type=self._binding.schedule_config_type,
                session=self.session,
            ),
        )

    def _priority_levels(self, ws):
        return None  # Neither workload publishes a per-quota priority menu.

    def _quota(self, ws, group, quota):
        groups = builtins.list(selected_groups(self._groups(ws), self._kind))
        values = [
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
        chosen = exact(values, group, ComputeGroupRef, self.client, ws.ref.key)
        data = groups[values.index(chosen)]
        prices = self._prices(ws, chosen.ref.key)
        if isinstance(quota, QuotaRef):
            self.client._validate_ref(quota, QuotaRef, ws.ref.key)
            prices = [p for p in prices if (p.get("quota_id") or p.get("spec_id")) == quota.key]
            if not prices:
                raise ResourceNotFoundError(
                    "No quota matches this reference in the selected group."
                )
            gpu, cpu, mem, _ = quota_values(prices[0])
            text = f"{gpu},{cpu},{mem}"
        else:
            text = (
                f"{quota.gpu},{quota.cpu},{quota.memory_gib}" if isinstance(quota, Quota) else quota
            )
        return match_quota_rows(
            parse_quota(text), [(data, p) for p in prices], group_override=chosen.name
        )

    def _project(self, ws, selector):
        rows = self._catalog(
            "projects",
            (ws.ref.key,),
            lambda: browser_api.list_projects(workspace_id=ws.ref.key, session=self.session),
        )
        values = [
            Resource(p.name, self._make_ref(ProjectRef, p.name, p.project_id, ws.ref.key))
            for p in rows
        ]
        return exact(values, selector, ProjectRef, self.client, ws.ref.key)

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
            workload=self._kind,
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

    @operation
    def metrics(
        self,
        ref: str | R,
        *,
        workspace: str | WorkspaceRef | None = None,
        metric: str = "core",
        window: str = "1h",
        start: str | datetime | None = None,
        end: str | datetime | None = None,
        interval: str | None = None,
        group: str | ComputeGroupRef | None = None,
    ) -> tuple[MetricGroup, ...]:
        from inspire.services.metrics import resolve_metrics, parse_window, parse_absolute
        from inspire.platform.web.browser_api.metrics import (
            get_resource_metrics_by_time,
            INTERVAL_CHOICES,
        )

        interval = interval or "1m"

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
            lcg = self._binding.metric_group(self._detail(resolved.key))
        if not lcg:
            raise ValidationError("Unable to resolve compute group; pass group.")
        return tuple(
            get_resource_metrics_by_time(
                task_id=resolved.key,
                task_type=self._binding.task_type,
                logic_compute_group_id=lcg,
                metric_types=resolve_metrics(metric),
                start_timestamp=start_ts,
                end_timestamp=end_ts,
                interval_second=INTERVAL_CHOICES[interval],
                session=self.session,
            )
        )

    def _follow_event_batch(self, ref, **filters):
        raise NotImplementedError

    def _logs(
        self,
        ref,
        *,
        workspace=None,
        instance=None,
        window=None,
        start=None,
        end=None,
        tail=None,
        head=None,
        limit=None,
    ):
        from datetime import timezone
        from .models import LogResult
        from inspire.services.job.job_logs import window_to_minutes, select_job_logs, format_log_line

        if tail is not None and head is not None:
            raise ValidationError("tail and head cannot be used together.")
        for value in (tail, head, limit):
            if value is not None and (type(value) is not int or value < 1):
                raise ValidationError("Log record counts must be positive integers.")
        resolved = self._resolve(ref, workspace)
        rows, _ = self._binding.fetch_instances(
            resolved.key, limit=500, show_all=True, session=self.session
        )
        from .instances import label_selectors

        available = self._binding.instance_views(rows)
        selectors = label_selectors(available, instance)
        views = self._binding.select_instance_views(available, selectors)
        if not views:
            raise ResourceNotFoundError(f"No instances found for {self._kind} job {resolved.name}")
        if start is not None or end is not None:
            if start is None or end is None:
                raise ValidationError("Both start and end are required.")
            start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
            if end_ms <= start_ms:
                raise ValidationError("end time must be after start time")
            cap = self._binding.log_max_window_ms
            start_ms = max(start_ms, end_ms - cap)
        else:
            minutes = window_to_minutes(window) if window else None
            start_ms, end_ms = self._binding.log_window(
                rows, lambda: self._detail(resolved.key), minutes
            )
        record_limit = limit or 100
        fetch_size = max(record_limit, tail or 0, head or 0)
        kwargs = dict(
            pod_names=[v.handle for v in views],
            start_timestamp_ms=start_ms,
            end_timestamp_ms=end_ms,
            session=self.session,
        )
        logs, total = self._binding.list_logs(resolved.key, **kwargs, page_size=fetch_size)
        if self._binding.expand_tail_fetch and head is None and total > len(logs):
            logs, total = self._binding.list_logs(resolved.key, **kwargs, page_size=total)
        selection = select_job_logs(
            self._binding.labelled_logs(logs, views),
            total=total,
            tail=tail,
            head=head,
            record_limit=record_limit,
            all_output=False,
        )
        return LogResult(
            "\n".join(format_log_line(row) for row in selection.logs),
            tuple(v.label for v in views),
            datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat(),
            datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat(),
            selection.truncated,
            selection.total,
            tuple(selection.logs),
        )
