"""HPC Slurm submission and platform observations."""

from __future__ import annotations
from .models import Instance
from .instances import sdk_instances
from inspire.exec_output import DEFAULT_MAX_OUTPUT_BYTES, OutputTarget
from typing import Callable
from inspire.services.execution.remote_exec import ExecResult
from datetime import datetime
from typing import Sequence
from uuid import uuid4
from inspire.services.hpc import hpc_submission as core
from inspire.services.hpc.hpc_instances import HPCInstanceView, fetch_hpc_instances, hpc_instance_views
from inspire.services.catalog.quotas import build_resource_spec_price
from inspire.platform.web.browser_api import hpc_jobs as api
from inspire.services.hpc import hpc_status as statuses, hpc_logs as log_core
from inspire.services.hpc.hpc_output import public_hpc_status
from inspire.services.hpc.hpc_instances import select_hpc_instance_views
from inspire.platform.web.browser_api.metrics import TASK_TYPE_BY_RESOURCE

from .compute_jobs import WorkloadBinding, ComputeJobs, duration
from .resources import operation
from .models import (
    Resource,
    ComputeGroupRef,
    Quota,
    WorkspaceRef,
    DatasetMount,
    EventResult,
    LogResult,
)
from .models_compute import HPCJob, HPCJobRef, HPCJobCreateSpec, HPCJobPlan, HPCJobHandle
from .exceptions import ValidationError, HPCJobFailedError, SubmissionUncertainError


def metric_group(detail: object) -> str | None:
    if isinstance(detail, dict):
        for key in ("logic_compute_group_id", "compute_group_id"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


class HPC(ComputeJobs[HPCJobRef, HPCJob, HPCInstanceView]):

    @operation
    def exec(
        self,
        ref: str | HPCJobRef,
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
        rows, _ = fetch_hpc_instances(resolved.key, limit=500, show_all=True, session=self.session)
        return workload_exec(
            self,
            key=resolved.key,
            workload="hpc",
            rows=rows,
            instance=instance,
            command=command,
            timeout=timeout,
            on_output=on_output,
            max_output_bytes=max_output_bytes,
            output_to=output_to,
            capture=capture,
        )

    def _fetch(self, ws, page, page_size, *, keyword=None, project=None, status=None):
        return self._binding.list_jobs(
            workspace_id=ws.ref.key, page_num=page, page_size=page_size,
            status=self._binding.normalize_status(status) if status else None,
            session=self.session,
        )

    _binding = WorkloadBinding[HPCInstanceView](
        list_page_size=50,
        list_jobs=lambda **kwargs: api.list_hpc_jobs(**kwargs),
        get_detail=lambda key, **kwargs: api.get_hpc_job_detail(key, **kwargs),
        get_details_by_ids=lambda keys, **kwargs: api.list_hpc_jobs_by_ids(keys, **kwargs),
        stop=lambda key, **kwargs: api.stop_hpc_job(key, **kwargs),
        delete=lambda key, **kwargs: api.delete_hpc_job(key, **kwargs),
        start=None,
        create=lambda payload, **kwargs: api.create_hpc_job(payload=payload, **kwargs),
        created_id=core.created_hpc_job_id,
        normalize_status=statuses.normalize_status,
        matches_status=statuses.matches_status,
        terminal_statuses=statuses.TERMINAL_STATUSES,
        success_statuses=statuses.SUCCESS_STATUSES,
        public_status=public_hpc_status,
        fetch_instances=lambda key, **kwargs: fetch_hpc_instances(key, **kwargs),
        instance_views=hpc_instance_views,
        select_instance_views=select_hpc_instance_views,
        list_logs=lambda key, **kwargs: api.list_hpc_job_logs(job_id=key, **kwargs),
        labelled_logs=log_core.labelled_logs,
        log_window=lambda rows, detail, minutes: log_core.log_time_range(rows, minutes)[:2],
        log_max_window_ms=api.HPC_LOG_MAX_WINDOW_MS,
        schedule_config_type="SCHEDULE_CONFIG_TYPE_HPC",
        task_type=TASK_TYPE_BY_RESOURCE["hpc"],
        metric_group=metric_group,
        expand_tail_fetch=True,
    )
    _kind = "hpc"
    _ref_type = HPCJobRef
    _model = HPCJob
    _failure = HPCJobFailedError

    @operation
    def plan(self, spec: HPCJobCreateSpec) -> HPCJobPlan:
        from inspire.services.catalog.datasets import parse_dataset_specs, resolve_dataset_info

        if not isinstance(spec, HPCJobCreateSpec):
            raise ValidationError("Pass a HPCJobCreateSpec.")
        if core.looks_like_full_slurm_script(spec.entrypoint):
            raise ValidationError(
                "HPC entrypoint must be the Slurm body, not a full sbatch script."
            )
        for value in (
            spec.instance_count,
            spec.number_of_tasks,
            spec.cpus_per_task,
            spec.memory_per_cpu,
        ):
            if value is not None and (type(value) is not int or value < 1):
                raise ValidationError("Slurm counts must be positive integers.")
        for hours in (spec.max_time_hours, spec.keep_after_finish_hours):
            if hours is not None:
                duration(hours, "hours", unit="hours")
        if spec.image_type not in ("SOURCE_PUBLIC", "SOURCE_PRIVATE", "SOURCE_OFFICIAL"):
            raise ValidationError(
                "image_type must be SOURCE_PUBLIC, SOURCE_PRIVATE or SOURCE_OFFICIAL."
            )
        ws = self.client.workspaces.get(spec.workspace)
        project = self._project(ws, spec.project)
        quota = self._quota(ws, spec.group, spec.quota)
        priority = self._resolve_priority(spec.priority, ws, project)
        image = self.client.images.get(spec.image, workspace=ws.ref)
        layout = core.resolve_slurm_layout(
            node_cpu=quota.cpu_count,
            node_memory_gib=quota.memory_gib,
            instance_count=spec.instance_count,
            number_of_tasks=spec.number_of_tasks,
            cpus_per_task=spec.cpus_per_task,
            memory_per_cpu=spec.memory_per_cpu,
        )
        mounts = parse_dataset_specs(
            [
                f"{x.dataset}:{x.version}" if isinstance(x, DatasetMount) else x
                for x in spec.datasets
            ],
            field="datasets",
        )
        data = resolve_dataset_info(mounts, workspace_id=ws.ref.key, session=self.session)
        body = core.build_hpc_create_payload(
            name=spec.name,
            logic_compute_group_id=quota.logic_compute_group_id,
            project_id=project.ref.key,
            workspace_id=ws.ref.key,
            image=image.url,
            image_type=spec.image_type,
            entrypoint=spec.entrypoint,
            quota_id=quota.quota_id,
            instance_count=spec.instance_count,
            task_priority=priority,
            number_of_tasks=layout.number_of_tasks,
            cpus_per_task=layout.cpus_per_task,
            memory_per_cpu=layout.memory_per_cpu,
            enable_hyper_threading=spec.enable_hyper_threading,
            resource_spec_price=build_resource_spec_price(quota=quota),
            enable_notification=spec.enable_notification,
            max_time_hours=spec.max_time_hours,
            dataset_info=data,
            description=spec.description,
            keep_after_finish_hours=spec.keep_after_finish_hours,
            public_path_readonly=spec.public_path_readonly,
            session=self.session,
        )
        payload = core.hpc_plan_payload(
            name=spec.name,
            create_kwargs=body,
            project_label=project.name,
            workspace_label=ws.name,
            compute_group_name=quota.compute_group_name,
            dataset_mounts=mounts,
        )
        return HPCJobPlan(
            name=spec.name,
            workspace=ws,
            project=project,
            group=Resource(
                quota.compute_group_name,
                self._make_ref(
                    ComputeGroupRef,
                    quota.compute_group_name,
                    quota.logic_compute_group_id,
                    ws.ref.key,
                ),
            ),
            quota=Quota(quota.gpu_count, quota.cpu_count, quota.memory_gib),
            image=image,
            priority=priority,
            create_kwargs=body,
            payload=payload,
            instance_count=spec.instance_count,
            number_of_tasks=layout.number_of_tasks,
            cpus_per_task=layout.cpus_per_task,
            memory_per_cpu=layout.memory_per_cpu,
            datasets=tuple(DatasetMount(m.dataset, m.version) for m in mounts),
        )

    @operation
    def create(self, spec: HPCJobCreateSpec, *, operation_id: str | None = None) -> HPCJobHandle:
        identifier = uuid4().hex if operation_id is None else operation_id
        if not isinstance(identifier, str) or not identifier:
            raise ValidationError("operation_id must be a non-empty string.")
        plan = self.plan(spec)
        session = self.session
        with self.client._transport.single_send(identifier, create=True, inspect="HPC jobs"):
            result = self._binding.create(plan.create_kwargs, session=session)
        key = self._binding.created_id(result)
        if not key:
            raise SubmissionUncertainError(identifier, inspect="HPC jobs")
        return HPCJobHandle(
            plan.name, self._make_ref(HPCJobRef, plan.name, key, plan.workspace.ref.key), identifier
        )

    @operation
    def instances(
        self, ref: str | HPCJobRef, *, workspace: str | WorkspaceRef | None = None
    ) -> tuple[Instance, ...]:
        resolved = self._resolve(ref, workspace)
        rows, _ = fetch_hpc_instances(resolved.key, limit=500, show_all=True, session=self.session)
        return sdk_instances("hpc", rows)

    def instance_names(
        self, ref: str | HPCJobRef, *, workspace: str | WorkspaceRef | None = None
    ) -> tuple[str, ...]:
        """Return the public labels accepted by exec and logs."""
        return tuple(row.label for row in self.instances(ref, workspace=workspace))

    @operation
    def events(
        self,
        ref: str | HPCJobRef,
        *,
        workspace: str | WorkspaceRef | None = None,
        reason: str | None = None,
        instance: str | Sequence[str] | None = None,
        workload_level: bool = False,
        limit: int = 100,
    ) -> EventResult:
        from inspire.services.hpc.hpc_events import collapse_repeated_events, labelled_events
        from inspire.services.hpc.hpc_instances import select_hpc_instance_views
        from inspire.services.job.job_events import matching_events, event_sort_key

        if workload_level and instance:
            raise ValidationError("workload_level and instance cannot be used together.")
        resolved = self._resolve(ref, workspace)
        rows = api.list_hpc_job_events(resolved.key, session=self.session) if not instance else []
        if not workload_level:
            views = select_hpc_instance_views(
                hpc_instance_views([view.raw for view in self.instances(resolved)]),
                (instance,) if isinstance(instance, str) else instance or (),
            )
            # Keep the client-owned synchronous transport in its creating thread.
            pod_events = []
            for view in views:
                pod_events.extend(
                    api.list_hpc_instance_events([view.handle], self.session, job_id=resolved.key)
                )
            rows += labelled_events(pod_events, views)
        rows = matching_events(
            collapse_repeated_events(sorted(rows, key=event_sort_key)), reason_filter=reason
        )
        selected = rows[-limit:] if limit > 0 else rows
        return EventResult(tuple(selected), len(selected) < len(rows))

    def _follow_event_batch(self, ref, **filters):
        return self.events(ref, **filters)

    @operation
    def logs(
        self,
        ref: str | HPCJobRef,
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
