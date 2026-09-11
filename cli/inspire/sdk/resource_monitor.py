"""Capacity, scheduling and live allocation reports."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from inspire.platform.web import browser_api
from inspire.platform.web.browser_api import schedule_config
from inspire.services.catalog import (
    resource_usage as usage_views,
    resource_availability as availability_views,
)
from inspire.services.job.job_events import event_sort_key, matching_events
from inspire.services.utils.collections import bound_collection
from .exceptions import ValidationError
from .models import WorkspaceRef, ComputeGroupRef, EventResult
from .models_resources import ResourceAvailability, ResourceUsage, WorkloadSchedulePolicy
from .resources import Service, operation


class Resources(Service):
    @operation
    def availability(
        self,
        workspace: str | WorkspaceRef,
        *,
        group: str | None = None,
        include_cpu: bool = False,
    ) -> tuple[ResourceAvailability, ...]:
        ws = self.client.workspaces.get(workspace)
        rows = browser_api.get_resource_inventory(
            workspace_id=ws.ref.key, session=self.session, include_cpu=include_cpu
        )
        if group:
            rows = [
                x for x in rows if group.strip().casefold() in str(x.group_name or "").casefold()
            ]
        result = []
        for row in availability_views.ordered_availability(rows):
            view = availability_views.public_availability_row(row)
            if not view["workspace"]:
                view["workspace"] = ws.name
            key = getattr(row, "group_id", "")
            ref = self._make_ref(ComputeGroupRef, row.group_name, key, ws.ref.key) if key else None
            result.append(ResourceAvailability.from_view(view, ref=ref))
        return tuple(result)

    @operation
    def policy(
        self,
        workspace: str | WorkspaceRef,
        *,
        workload: str | None = None,
    ) -> tuple[WorkloadSchedulePolicy, ...]:
        ws = self.client.workspaces.get(workspace)
        return tuple(
            x
            for x in schedule_config.get_workspace_schedule_policy(ws.ref.key, session=self.session)
            if not workload or x.workload == workload.lower()
        )

    @operation
    def usage(
        self,
        workspace: str | WorkspaceRef,
        *,
        project: str | None = None,
        user: str | None = None,
        task: str | None = None,
        group: str | None = None,
        mine: bool = False,
        details: bool = False,
        limit: int | None = None,
    ) -> ResourceUsage:
        if mine and (group or user or task or details):
            raise ValidationError(
                "mine=True reads a pre-aggregated per-project record, so it cannot be "
                "narrowed with group, user, task, or details. Use the "
                "default Project/User view or details=True with group instead."
            )
        ws = self.client.workspaces.get(workspace)
        label = usage_views.display_name(ws.name, fallback="(workspace name unavailable)")
        groups = (
            usage_views.resolve_group_ids(
                session=self.session,
                workspace_id=ws.ref.key,
                keyword=group,
                groups_loader=lambda: [row[1] for row in self.client.compute_groups._all(ws)],
            )
            if group
            else []
        )
        if group and not groups:
            raise ValidationError(
                f"No compute group in {label} matches {group!r}. "
                f"Call resources.availability({ws.name!r}) "
                "for the names this workspace has."
            )
        mode = "mine" if mine else ("task" if details else "project-user")
        if mine:
            rows = usage_views.member_rows(
                [
                    x
                    for x in browser_api.list_member_usage(ws.ref.key, session=self.session)
                    if not project or project.casefold() in x.project_name.casefold()
                ],
                workspace=label,
            )
        else:
            if groups:
                tasks = [
                    x
                    for gid, _ in groups
                    for x in browser_api.list_task_usage(
                        ws.ref.key, logic_compute_group_id=gid, session=self.session
                    )
                ]
            else:
                tasks = browser_api.list_task_usage(ws.ref.key, session=self.session)
            tasks = usage_views.filter_tasks(tasks, project=project, user=user, task=task)
            try:
                fair = self._fair_scheduling(ws)
            except Exception:
                fair = None
            rows = (
                usage_views.task_rows(tasks, workspace=label)
                if details
                else usage_views.project_user_rows(tasks, workspace=label, fair_scheduling=fair)
            )
        page = bound_collection(rows, limit=limit)
        view = {"scope": mode, "items": page.items, **page.metadata()}
        filters = {k: v for k, v in (("project", project), ("user", user), ("task", task)) if v}
        if filters:
            view["filters"] = filters
        if groups:
            view["compute_groups"] = [usage_views.display_name(name) for _, name in groups]
        return ResourceUsage.from_view(view)

    @operation
    def node_events(
        self,
        nodes: str | Sequence[str],
        *,
        since: datetime | float | None = None,
        type: str | None = None,
        reason: str | None = None,
        limit: int | None = None,
        from_component: str | None = None,
    ) -> EventResult:
        events = browser_api.list_node_events(
            [nodes] if isinstance(nodes, str) else list(nodes),
            page_size=200,
            max_pages=5,
            sort_ascending=False,
            from_component=(from_component or "").strip().lower() or None,
            session=self.session,
        )
        events = sorted(
            matching_events(events, type_filter=type, reason_filter=reason), key=event_sort_key
        )
        if since is not None:
            epoch = since.timestamp() if isinstance(since, datetime) else since
            events = [
                x
                for x in events
                if (
                    event_sort_key(x)[0] / 1000
                    if event_sort_key(x)[0] > 10**12
                    else event_sort_key(x)[0]
                )
                >= epoch
            ]
        truncated = limit is not None and len(events) > limit
        return EventResult(tuple(events[-limit:] if limit is not None else events), truncated)
