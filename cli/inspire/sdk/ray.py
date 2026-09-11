"""Ray cluster submission and platform observations."""

from __future__ import annotations
from .models import Instance
from .instances import sdk_instances
from inspire.exec_output import DEFAULT_MAX_OUTPUT_BYTES, OutputTarget
from typing import Callable
from inspire.services.execution.remote_exec import ExecResult
from datetime import datetime
from typing import Sequence
from uuid import uuid4
from inspire.services.ray import ray_submission as core
from inspire.services.metrics import metric_group
from inspire.services.ray.ray_instances import RayInstanceView, fetch_ray_instances, ray_instance_views
from inspire.platform.web.browser_api import ray_jobs as api
from inspire.services.ray import ray_status as statuses, ray_logs as log_core
from inspire.services.ray.ray_output import public_ray_status
from inspire.services.ray.ray_instances import select_ray_instance_views
from inspire.platform.web.browser_api.metrics import TASK_TYPE_BY_RESOURCE

from .compute_jobs import WorkloadBinding, ComputeJobs
from .resources import operation
from .models import (
    Resource,
    ComputeGroupRef,
    Quota,
    WorkspaceRef,
    EventResult,
    LogResult,
)
from .models_observations import RayScalingEvent
from .models_compute import RayJob, RayJobRef, RayJobCreateSpec, RayJobPlan, RayJobHandle
from .exceptions import ValidationError, RayJobFailedError, SubmissionUncertainError


class Ray(ComputeJobs[RayJobRef, RayJob, RayInstanceView]):

    @operation
    def exec(
        self,
        ref: str | RayJobRef,
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
        rows, _ = fetch_ray_instances(resolved.key, limit=500, show_all=True, session=self.session)
        return workload_exec(
            self,
            key=resolved.key,
            workload="ray",
            rows=rows,
            instance=instance,
            command=command,
            timeout=timeout,
            on_output=on_output,
            max_output_bytes=max_output_bytes,
            output_to=output_to,
            capture=capture,
        )

    _binding = WorkloadBinding[RayInstanceView](
        list_page_size=20,
        list_jobs=lambda **kwargs: api.list_ray_jobs(**kwargs),
        get_detail=lambda key, **kwargs: api.get_ray_job_detail(key, **kwargs),
        stop=lambda key, **kwargs: api.stop_ray_job(key, **kwargs),
        delete=lambda key, **kwargs: api.delete_ray_job(key, **kwargs),
        start=lambda key, **kwargs: api.start_ray_job(key, **kwargs),
        create=lambda payload, **kwargs: api.create_ray_job(payload, **kwargs),
        created_id=core.created_ray_job_id,
        normalize_status=statuses.normalize_status,
        matches_status=statuses.matches_status,
        terminal_statuses=statuses.TERMINAL_STATUSES,
        success_statuses=statuses.SUCCESS_STATUSES,
        public_status=public_ray_status,
        fetch_instances=lambda key, **kwargs: fetch_ray_instances(key, **kwargs),
        instance_views=ray_instance_views,
        select_instance_views=select_ray_instance_views,
        list_logs=lambda key, **kwargs: api.list_ray_job_logs(**kwargs),
        labelled_logs=log_core.labelled_logs,
        log_window=lambda rows, detail, minutes: log_core.clamped_window(detail(), minutes)[:2],
        log_max_window_ms=api.RAY_LOG_MAX_WINDOW_MS,
        schedule_config_type="SCHEDULE_CONFIG_TYPE_RAY_JOB",
        task_type=TASK_TYPE_BY_RESOURCE["ray"],
        metric_group=metric_group,
        expand_tail_fetch=False,
    )
    _kind = "ray"
    _ref_type = RayJobRef
    _model = RayJob
    _failure = RayJobFailedError

    @operation
    def plan(self, spec: RayJobCreateSpec) -> RayJobPlan:
        if not isinstance(spec, RayJobCreateSpec):
            raise ValidationError("Pass a RayJobCreateSpec.")
        if spec.shm_gib is not None and (type(spec.shm_gib) is not int or spec.shm_gib < 1):
            raise ValidationError("shm_gib must be a positive integer.")
        ws = self.client.workspaces.get(spec.workspace)
        project = self._project(ws, spec.project)
        head = self._quota(ws, spec.group, spec.quota)
        image = self.client.images.get(spec.image, workspace=ws.ref)
        image_text = spec.image if isinstance(spec.image, str) else image.name
        quota_text = f"{head.gpu_count},{head.cpu_count},{head.memory_gib}"

        def resolve_image(raw):
            if image is not None and raw == image_text:
                return image.ref.key
            return self.client.images.get(raw, workspace=ws.ref).ref.key

        body = core.assemble_create_body(
            workspace_id=ws.ref.key,
            project_id=project.ref.key,
            head_quota=head,
            resolve_quota=lambda triple, group: self._quota(ws, group, triple),
            resolve_image=resolve_image,
            resolve_priority=lambda requested: self._resolve_priority(requested, ws, project),
            name=spec.name,
            command=spec.command,
            description=spec.description,
            project=project.name,
            workspace=ws.name,
            priority=spec.priority,
            image=image_text,
            image_type=spec.image_type,
            group=head.compute_group_name,
            quota=quota_text,
            shm_size=spec.shm_gib,
            workers=tuple(spec.workers),
            public_path_readonly=spec.public_path_readonly,
        )
        payload = core.ray_plan_payload(
            body=body,
            workspace=ws.name,
            project=project.name,
            group=head.compute_group_name,
            quota=quota_text,
            image=image_text,
            workers=tuple(spec.workers),
            description=spec.description,
            shm_size=spec.shm_gib,
            public_path_readonly=spec.public_path_readonly,
        )
        return RayJobPlan(
            name=spec.name,
            workspace=ws,
            project=project,
            group=Resource(
                head.compute_group_name,
                self._make_ref(
                    ComputeGroupRef,
                    head.compute_group_name,
                    head.logic_compute_group_id,
                    ws.ref.key,
                ),
            ),
            quota=Quota(head.gpu_count, head.cpu_count, head.memory_gib),
            image=image,
            priority=body["task_priority"],
            create_kwargs=body,
            payload=payload,
            workers=tuple(core.parse_worker_spec(raw) for raw in spec.workers),
            shm_gib=spec.shm_gib,
        )

    @operation
    def create(self, spec: RayJobCreateSpec, *, operation_id: str | None = None) -> RayJobHandle:
        identifier = uuid4().hex if operation_id is None else operation_id
        if not isinstance(identifier, str) or not identifier:
            raise ValidationError("operation_id must be a non-empty string.")
        plan = self.plan(spec)
        session = self.session
        with self.client._transport.single_send(identifier, create=True, inspect="Ray jobs"):
            result = self._binding.create(plan.create_kwargs, session=session)
        key = self._binding.created_id(result)
        if not key:
            raise SubmissionUncertainError(identifier, inspect="Ray jobs")
        return RayJobHandle(
            plan.name, self._make_ref(RayJobRef, plan.name, key, plan.workspace.ref.key), identifier
        )

    @operation
    def start(self, ref: str | RayJobRef, *, workspace: str | WorkspaceRef | None = None) -> None:
        assert self._binding.start is not None
        self._mutate(ref, self._binding.start, workspace)

    @operation
    def instances(
        self, ref: str | RayJobRef, *, workspace: str | WorkspaceRef | None = None
    ) -> tuple[Instance, ...]:
        resolved = self._resolve(ref, workspace)
        rows, _ = fetch_ray_instances(resolved.key, limit=500, show_all=True, session=self.session)
        return sdk_instances("ray", rows)

    def instance_names(
        self, ref: str | RayJobRef, *, workspace: str | WorkspaceRef | None = None
    ) -> tuple[str, ...]:
        """Return the public labels accepted by exec and logs."""
        return tuple(row.label for row in self.instances(ref, workspace=workspace))

    @operation
    def events(
        self,
        ref: str | RayJobRef,
        *,
        workspace: str | WorkspaceRef | None = None,
        type: str | None = None,
        reason: str | None = None,
        instance: str | Sequence[str] | None = None,
        workload_level: bool = False,
        limit: int = 100,
    ) -> EventResult:
        from inspire.services.ray.ray_events import fetch_recent_ray_events
        from inspire.services.job.job_events import matching_events

        if workload_level and instance:
            raise ValidationError("workload_level and instance cannot be used together.")
        resolved = self._resolve(ref, workspace)
        rows = fetch_recent_ray_events(
            resolved.key,
            session=self.session,
            selectors=(instance,) if isinstance(instance, str) else instance or (),
            workload_level=workload_level,
        )
        rows = matching_events(rows, type_filter=type, reason_filter=reason)
        selected = rows[-limit:] if limit > 0 else rows
        return EventResult(tuple(selected), len(selected) < len(rows))

    def _follow_event_batch(self, ref, **filters):
        return self.events(ref, **filters)

    @operation
    def logs(
        self,
        ref: str | RayJobRef,
        *,
        workspace: str | WorkspaceRef | None = None,
        instance: str | Sequence[str] | None = None,
        window: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        tail: int | None = None,
        head: int | None = None,
        limit: int | None = None,
    ) -> LogResult:
        """Read logs by instance label or handle, singly or as a sequence.

        None and "all" select every instance. Print labels, never handles."""
        return self._logs(
            ref,
            workspace=workspace,
            instance=instance,
            window=window,
            start=start,
            end=end,
            tail=tail,
            head=head,
            limit=limit,
        )

    @operation
    def scaling(
        self,
        ref: str | RayJobRef,
        *,
        group: str | None = None,
        limit: int | None = None,
        workspace: str | WorkspaceRef | None = None,
    ) -> tuple[RayScalingEvent, ...]:
        from inspire.services.ray.ray_scaling import public_ray_scaling_events, event_time

        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValidationError("limit must be a positive integer.")
        resolved = self._resolve(ref, workspace)
        group = (group or "").strip()
        rows, _ = api.list_ray_job_scaling_histories(
            resolved.key, worker_group_name=group, page_num=1, page_size=-1, session=self.session
        )
        rows = sorted(rows, key=event_time)
        return tuple(
            RayScalingEvent.from_view(row)
            for row in public_ray_scaling_events(rows[-limit:] if limit else rows, group=group)
        )
