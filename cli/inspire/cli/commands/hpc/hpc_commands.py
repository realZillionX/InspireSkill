"""HPC job commands for Inspire CLI."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence, cast

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.collection_output import (
    DEFAULT_COLLECTION_LIMIT,
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.dataset_mounts import (
    DatasetSpecError,
    dataset_mount_views,
    dataset_option,
    describe_dataset_mounts,
    parse_dataset_specs_or_usage_error,
    resolve_dataset_info,
)
from inspire.cli.utils.errors import (
    exit_with_error as _handle_error,
    require_confirmation,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.image_resolver import resolve_image_url
from inspire.cli.utils.task_priority import (
    TaskPriorityError,
    resolve_workspace_task_priority,
    task_priority_option,
)
from inspire.config import Config, ConfigError
from inspire.config.workload_profiles import apply_workload_profile, profile_required_message
from inspire.cli.utils.id_resolver import (
    NAME_PICK_HELP,
    forget_resource_identity,
    looks_like_platform_id,
    reject_id_at_boundary,
    remember_resource_identity,
    resolve_by_name,
    run_with_stale_handle_retry,
)
from inspire.cli.utils.project_resolver import (
    project_display_name,
    resolve_project_id as resolve_project_id_by_name,
)
from inspire.config.workspaces import (
    resolve_workspace_query_scope,
    select_workspace_id,
    workspace_label,
    workspace_name_map,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import DatasetMount
from inspire.platform.web.session import SessionExpiredError, get_web_session

from .public_output import (
    format_hpc_status,
    public_hpc_list_item,
    public_hpc_status,
)

_DEFAULT_INSTANCE_SCAN_LIMIT = 500


def _current_user_id(session) -> str:  # noqa: ANN001
    me = browser_api_module.get_current_user(session=session)
    user_id = str(me.get("id") or me.get("user_id") or "").strip()
    if not user_id:
        raise ValueError("Cannot determine the current user from the live web session.")
    return user_id


def _created_hpc_job_id(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("job_id", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    for key in ("job", "data", "result"):
        value = _created_hpc_job_id(payload.get(key))
        if value:
            return value
    return ""


def _resolve_hpc_name_in_workspace(
    ctx: Context,
    *,
    session,
    name: str,
    workspace: str,
    limit: int,
    pick: Optional[int] = None,
    require_live: bool = False,
) -> str:
    workspace_id = select_workspace_id(
        explicit_workspace_name=workspace,
        session=session,
    )
    if workspace_id is None:
        raise ConfigError("--workspace is required.")
    user_id = _current_user_id(session)

    def _lister():
        jobs, _ = browser_api_module.list_hpc_jobs(
            workspace_id=workspace_id,
            created_by=user_id,
            page_num=1,
            page_size=limit,
            session=session,
        )
        return [
            {
                "name": j.name,
                "id": j.job_id,
                "status": j.status,
                "workspace_id": j.workspace_id,
                "created_at": j.created_at,
            }
            for j in jobs
        ]

    return resolve_by_name(
        ctx,
        name=name,
        resource_type="hpc",
        list_candidates=_lister,
        pick_index=pick,
        session=session,
        workspace_id=workspace_id,
        owner_scope="self",
        require_live=require_live,
        list_command=f"inspire hpc list --workspace {workspace}",
    )


def _reject_hpc_name_at_boundary(ctx: Context, name: str) -> str:
    return reject_id_at_boundary(
        ctx,
        name,
        resource_type="hpc",
        list_command="inspire hpc list --workspace <workspace>",
    )


def _run_readonly_hpc_operation(
    ctx: Context,
    *,
    session,
    name: str,
    workspace: str,
    limit: int,
    pick: Optional[int] = None,
    operation,
):
    """Run a read-only HPC operation and recover one stale cache hit."""
    def _resolve(require_live: bool) -> str:
        return _resolve_hpc_name_in_workspace(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=limit,
            pick=pick,
            require_live=require_live,
        )

    def _invalidate(job_id: str) -> None:
        workspace_id = select_workspace_id(
            explicit_workspace_name=workspace,
            session=session,
        )
        if workspace_id is None:
            raise ConfigError("--workspace is required.")
        forget_resource_identity(
            session=session,
            resource_type="hpc",
            resource_id=job_id,
            workspace_id=workspace_id,
            owner_scope="self",
        )

    return run_with_stale_handle_retry(
        name=name,
        resolve_cached=lambda: _resolve(False),
        resolve_live=lambda _name: _resolve(True),
        operation=lambda job_id: operation(job_id, session),
        invalidate=_invalidate,
    )


def _resolve_project_id(
    config: Config,
    requested: Optional[str],
    *,
    workspace_id: Optional[str] = None,
    session=None,
    ctx: Context | None = None,
) -> str:
    """Resolve a visible project name against the current workspace."""
    if not requested:
        raise ConfigError("--project is required.")
    if ctx is not None:
        requested = reject_id_at_boundary(
            ctx,
            requested,
            resource_type="project",
            list_command="inspire project list",
        )
    elif looks_like_platform_id(requested):
        raise ConfigError("--project takes a project name.")
    if not workspace_id or session is None:
        raise ConfigError("A live workspace project list is required to resolve --project.")
    projects = browser_api_module.list_projects(
        workspace_id=workspace_id,
        session=session,
    )
    return resolve_project_id_by_name(config, requested, projects)


def _project_label(config: Config, requested: Optional[str]) -> str:
    if requested:
        return project_display_name(config, requested)
    return "(project name unavailable)"


def _looks_like_full_slurm_script(entrypoint: str) -> bool:
    stripped = entrypoint.lstrip()
    return stripped.startswith("#!") or "#SBATCH" in entrypoint


def _missing_srun_warning(entrypoint: str) -> str:
    """Warn about a body that will finish SUCCEEDED without a Slurm step.

    Measured: a body without ``srun`` still runs — sbatch executes it on the
    first node — so the job ends SUCCEEDED and its logs carry the output. What
    it never does is create a step, which `GetJob` reports as ``steps: 0/0``
    against ``1/1`` for the same body launched with ``srun``. On one node that
    difference is invisible in the result; on several it means every node but
    the first sat idle. This stays a warning rather than an error because the
    body did run.
    """
    if re.search(r"(^|[;&|(\s])srun([\s;&|)]|$)", entrypoint):
        return ""
    return (
        "Warning: the entrypoint never calls srun, so Slurm creates no step "
        "(hpc status will read 'Steps: 0/0') and only the first node runs the body."
    )


def _hpc_plan_payload(
    *,
    name: str,
    create_kwargs: dict[str, Any],
    project_label: str,
    workspace_label: str,
    compute_group_name: str,
    dataset_mounts: Sequence[DatasetMount] = (),
) -> dict[str, Any]:
    sbatch = create_kwargs.get("sbatch_script") or {}
    cluster = create_kwargs.get("slurm_cluster_spec") or {}
    payload: dict[str, Any] = {
        "dry_run": True,
        "name": name,
        "workspace": workspace_label,
        "project": project_label,
        "compute_group": compute_group_name,
        "image": cluster.get("image"),
        "image_type": cluster.get("image_type"),
        "nodes": cluster.get("instance_count"),
        "resource": {
            "cpu": cluster.get("cpu"),
            "memory_gib": cluster.get("mem_gi"),
        },
        "command": sbatch.get("entrypoint"),
        "number_of_tasks": sbatch.get("number_of_tasks"),
        "cpus_per_task": sbatch.get("cpus_per_task"),
        "memory_per_cpu": sbatch.get("memory_per_cpu"),
        "enable_hyper_threading": sbatch.get("enable_hyper_threading"),
        # `priority`, not `task_priority`: the latter is the *argument* name,
        # and reading it out of the payload left every `--dry-run --json` plan
        # reporting `"priority": null` while a real priority was on its way.
        "priority": create_kwargs.get("priority"),
        "enable_notification": create_kwargs.get("enable_notification"),
    }
    if dataset_mounts:
        payload["datasets"] = dataset_mount_views(dataset_mounts)
    if sbatch.get("job_max_time"):
        payload["max_time"] = sbatch.get("job_max_time")
    for key in ("description", "ttl_after_job_finish_seconds"):
        if key in create_kwargs:
            payload[key] = create_kwargs[key]
    if "is_publicpath_readonly" in create_kwargs:
        payload["public_path_readonly"] = create_kwargs["is_publicpath_readonly"]
    return payload


def _slurm_time_fields(max_time_hours: float | None) -> dict[str, Any]:
    """Build the `sbatch_script` runtime cap the console sends.

    最大运行时长 is not a top-level field: the console writes it into
    `sbatch_script` twice, once as the Slurm ``--time`` string
    ``D-HH:MM:SS`` and once as the day/hour/minute breakdown, and sends both.
    """
    if max_time_hours is None:
        return {}
    total_seconds = int(round(max_time_hours * 3600))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return {
        "job_max_time": f"{days}-{hours:02d}:{minutes:02d}:{seconds:02d}",
        "max_running_time_days": days,
        "max_running_time_hours": hours,
        "max_running_time_minutes": minutes,
    }


class SlurmLayoutError(ValueError):
    """A Slurm subdivision the platform accepts but Slurm can never run."""


@dataclass(frozen=True)
class SlurmLayout:
    """How one node-level allocation is carved up for Slurm."""

    number_of_tasks: int
    cpus_per_task: int
    memory_per_cpu: int


def resolve_slurm_layout(
    *,
    node_cpu: int,
    node_memory_gib: int,
    instance_count: int,
    number_of_tasks: int,
    cpus_per_task: int | None,
    memory_per_cpu: int | None,
) -> SlurmLayout:
    """Fill in the Slurm subdivision and refuse the ones that cannot run.

    The two layers are independent on the wire — ``slurm_cluster_spec`` buys
    nodes, ``sbatch_script`` describes how the program uses them — and
    **nothing on the platform checks one against the other**. Neither does the
    console: its `最大值` hints come from the project's per-task quota, not from
    the selected node spec, and it lets the Slurm fields be filled before a
    spec is even chosen. `CreateJobConsole` accepts every combination below and
    answers with a job id, which is why a wrong spec reads as a successful
    submit. Measured on `HPC-可上网区资源-2`, quota `0,4,16`, one node unless
    stated, each failure mode isolated from the others:

    * ``cpus_per_task=8, memory_per_cpu=1`` (8 GiB, well inside the node) —
      FAILED a minute or two after it starts running. `hpc logs` is empty,
      `hpc events` shows only the normal pod lifecycle, and no surface anywhere
      carries the sbatch rejection.
    * ``cpus_per_task=4, memory_per_cpu=64`` — same shape, same silence,
      reached through memory instead.
    * ``number_of_tasks=8, cpus_per_task=4`` — sbatch *accepts* it and the
      step queues forever. The platform reports RUNNING with `steps` stuck at
      `-/1`, so it burns the workspace's whole runtime cap having run nothing.

    A task cannot span nodes, so the binding constraint is per node: with
    ``instance_count`` nodes, Slurm packs at most ``ceil(tasks / nodes)`` tasks
    onto one node, and that node has to hold their CPU and their memory. Both
    positive controls confirm the bound is inclusive: ``number_of_tasks=4,
    cpus_per_task=1`` fills the node exactly and succeeds, and
    ``number_of_tasks=2, cpus_per_task=4`` over two nodes succeeds.

    Defaults follow the same arithmetic instead of the old "one task owns the
    whole node", which produced the hang above the moment `--number-of-tasks`
    went above 1. For a single task on a single node they are unchanged.

    Scheduling is all these checks cover. At runtime the pod's cgroup is the
    only wall, it is always the ``--quota`` memory, and it does **not** follow
    ``memory_per_cpu`` — a job that asked for 12 GiB still committed 15 GiB
    unimpeded on a 16-GiB node. Exactly filling the Slurm request is therefore
    no more dangerous than half filling it; what kills a job is the node
    figure, and `nproc` / `free` inside the container report the *host* (64
    cores, ~503 GiB), so anything that autosizes from them oversubscribes
    wildly. That belongs in the workload guide, not in a create-time check.

    Memory is always per CPU here. The console has a second input, 每节点使用内存,
    and ``sbatch_script.memory_per_node`` is a real field — the platform stores
    it, echoes it on the detail page, and round-trips it through ``GetJob`` —
    but its script generator only ever emits ``--mem-per-cpu``. Sending
    ``memory_per_node`` therefore writes a bare ``#SBATCH --mem-per-cpu=`` into
    the script and sbatch rejects the whole thing: 8 GiB, 15 GiB and 16 GiB on
    a 16-GiB node all FAILED with the same silence, while the equivalent
    ``--mem-per-cpu`` job succeeded. Sending both fields is a plain
    ``InternalError``. The field is not adopted.
    """
    node_cpu = max(1, int(node_cpu))
    node_memory_gib = max(1, int(node_memory_gib))
    instance_count = max(1, int(instance_count))
    number_of_tasks = max(1, int(number_of_tasks))
    tasks_per_node = -(-number_of_tasks // instance_count)

    if cpus_per_task is None:
        cpus_per_task = max(1, node_cpu // tasks_per_node)
    cpus_per_task = int(cpus_per_task)
    if memory_per_cpu is None:
        memory_per_cpu = max(1, node_memory_gib // (tasks_per_node * cpus_per_task))
    memory_per_cpu = int(memory_per_cpu)

    layout_text = (
        f"{number_of_tasks} task(s) x {cpus_per_task} CPU over {instance_count} node(s) "
        f"puts {tasks_per_node} task(s) on one {node_cpu}-CPU {node_memory_gib}-GiB node"
    )

    if cpus_per_task > node_cpu:
        raise SlurmLayoutError(
            f"--cpus-per-task {cpus_per_task} exceeds the {node_cpu} CPU of one node in "
            f"--quota. A task cannot span nodes, so Slurm fails the job on submit and the "
            f"platform reports FAILED with no log and no event explaining it. Lower "
            f"--cpus-per-task to at most {node_cpu}, or pick a wider --quota row."
        )
    if tasks_per_node * cpus_per_task > node_cpu:
        raise SlurmLayoutError(
            f"{layout_text}, which needs {tasks_per_node * cpus_per_task} CPU. Slurm queues "
            f"that step forever while the platform reports RUNNING, so the job burns its "
            f"whole runtime having run nothing. Lower --number-of-tasks or --cpus-per-task, "
            f"or raise --instance-count to at least "
            f"{-(-number_of_tasks * cpus_per_task // node_cpu)}."
        )

    needed_gib = tasks_per_node * cpus_per_task * memory_per_cpu
    if needed_gib > node_memory_gib:
        raise SlurmLayoutError(
            f"{layout_text}, and --memory-per-cpu {memory_per_cpu} asks for "
            f"{needed_gib} GiB there against {node_memory_gib} GiB. Slurm fails the job "
            f"on submit and the platform reports FAILED with no log and no event "
            f"explaining it. Lower --memory-per-cpu to at most "
            f"{node_memory_gib // (tasks_per_node * cpus_per_task)}, or pick a wider "
            f"--quota row."
        )

    return SlurmLayout(
        number_of_tasks=number_of_tasks,
        cpus_per_task=cpus_per_task,
        memory_per_cpu=memory_per_cpu,
    )


def build_hpc_create_payload(
    *,
    name: str,
    logic_compute_group_id: str,
    project_id: str,
    workspace_id: str,
    image: str,
    image_type: str,
    entrypoint: str,
    quota_id: str,
    instance_count: int,
    task_priority: int | None,
    number_of_tasks: int,
    cpus_per_task: int,
    memory_per_cpu: int,
    enable_hyper_threading: bool,
    resource_spec_price: dict[str, Any],
    enable_notification: bool = False,
    max_time_hours: float | None = None,
    dataset_info: list[dict[str, str]] | None = None,
    description: str | None = None,
    keep_after_finish_hours: float | None = None,
    public_path_readonly: bool | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """Build the current Web UI v2 HPC create payload.

    Optional arguments stay out of the body unless the caller sets them, so a
    payload built without them is byte-for-byte the one this command has always
    sent. ``enable_notification`` is the exception: it has always been part of
    the body, so it keeps being sent and only its value is now selectable.
    """
    payload: dict[str, Any] = {
        "job_name": name,
        "logic_compute_group_id": logic_compute_group_id,
        "project_id": project_id,
        "workspace_id": workspace_id,
        "enable_notification": bool(enable_notification),
        "sbatch_script": {
            "number_of_tasks": int(number_of_tasks),
            "cpus_per_task": int(cpus_per_task),
            # Always `memory_per_cpu`. `memory_per_node` is stored and echoed
            # by the platform but never reaches the generated script, which
            # then carries an empty `#SBATCH --mem-per-cpu=` and fails.
            "memory_per_cpu": f"{int(memory_per_cpu)}G",
            "enable_hyper_threading": bool(enable_hyper_threading),
            "entrypoint": entrypoint,
            **_slurm_time_fields(max_time_hours),
        },
        "slurm_cluster_spec": {
            "predef_quota_id": quota_id,
            "cpu": int(resource_spec_price.get("cpu_count") or 0),
            "mem_gi": int(resource_spec_price.get("memory_size_gib") or 0),
            # The platform matches on the registry URL, not the visible name;
            # sending the name is rejected with 无法找到对应镜像.
            "image": resolve_image_url(
                image, session=session, workspace_id=workspace_id
            ),
            "image_type": image_type,
            "instance_count": int(instance_count),
            "spec_price": dict(resource_spec_price),
        },
    }
    if task_priority is None:
        # Not optional in practice: a body without `priority` comes back as
        # `InternalError: internal server error`, which is on the transient
        # list, so the transport burns three retries and then reports what
        # reads like a platform outage rather than a missing field.
        raise ValueError(
            "HPC create requires a task priority; the platform answers a payload "
            "without one with an internal error."
        )
    # `priority`, not `task_priority`: v2 CreateJobConsole rejects the latter
    # with "priority must be set", which reads like the value is missing rather
    # than misnamed.
    payload["priority"] = int(task_priority)

    if dataset_info:
        payload["dataset_info"] = [dict(entry) for entry in dataset_info]
    if description is not None:
        payload["description"] = description
    if keep_after_finish_hours is not None:
        payload["ttl_after_job_finish_seconds"] = int(round(keep_after_finish_hours * 3600))
    if public_path_readonly is not None:
        payload["is_publicpath_readonly"] = bool(public_path_readonly)
    return payload


def _format_hpc_list_rows(rows: list[dict[str, str]]) -> str:
    """Format HPC job rows into a compact name-first table."""
    if not rows:
        return "No HPC jobs found."

    show_workspace = any(row.get("workspace") for row in rows)
    table_rows = [
        (
            row["name"],
            row["status"],
            *([row.get("workspace", "")] if show_workspace else []),
            row["created_at"],
        )
        for row in rows
    ]
    headers = ("Name", "Status", "Workspace", "Created") if show_workspace else (
        "Name",
        "Status",
        "Created",
    )
    widths = [
        column_width("Name", [row[0] for row in table_rows], max_width=64),
        column_width("Status", [row[1] for row in table_rows], max_width=18),
    ]
    if show_workspace:
        widths.append(
            column_width("Workspace", [row[2] for row in table_rows], max_width=32)
        )
    widths.append(
        column_width("Created", [row[-1] for row in table_rows], max_width=19)
    )
    rendered = render_table(headers, table_rows, widths, line_char="─")
    return "\n".join([rendered[1], rendered[2], *rendered[3:-1]])


def _public_hpc_instance_text(inst: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = inst.get(key)
        if value in (None, "") or isinstance(value, (dict, list, tuple, set)):
            continue
        text = scrub_raw_ids(value).strip()
        if text and "<redacted>" not in text:
            return text
    return ""


def _hpc_instance_rank(inst: dict[str, Any], position: int) -> int:
    for key in ("rank", "instance_rank", "global_rank", "index", "replica_index"):
        value = inst.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
    return position


def _hpc_instance_resource(inst: dict[str, Any]) -> str:
    direct = _public_hpc_instance_text(inst, "resource")
    if direct:
        return direct

    spec = inst
    for key in ("resource_spec", "resource_spec_price", "quota"):
        candidate = inst.get(key)
        if isinstance(candidate, dict):
            spec = candidate
            break

    values = (
        ("CPU", _public_hpc_instance_text(spec, "cpu_count", "cpu")),
        (
            "GiB",
            _public_hpc_instance_text(
                spec,
                "memory_size_gib",
                "memory_gib",
                "memory_size",
                "memory",
            ),
        ),
        ("GPU", _public_hpc_instance_text(spec, "gpu_count", "gpu")),
    )
    return ", ".join(f"{value} {unit}" for unit, value in values if value)


def _public_hpc_instances(
    instances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for position, inst in enumerate(instances):
        item: dict[str, Any] = {}
        name = _public_hpc_instance_text(
            inst,
            "name",
            "instance_name",
            "display_name",
        )
        if name and not looks_like_platform_id(name):
            item["name"] = name

        for key, candidates in (
            ("status", ("status", "instance_status", "phase", "state")),
            ("role", ("role", "component", "worker_group_name")),
            ("type", ("type", "instance_type")),
            ("node", ("node", "node_name", "host_name")),
        ):
            value = _public_hpc_instance_text(inst, *candidates)
            if value:
                item[key] = value

        resource = _hpc_instance_resource(inst)
        if resource:
            item["resource"] = resource
        item["rank"] = _hpc_instance_rank(inst, position)
        projected.append(item)
    return projected


def _format_hpc_instances(instances: list[dict[str, Any]]) -> str:
    """Format projected HPC instances as a compact table."""
    if not instances:
        return "No HPC instances found."

    columns = [("name", "Name"), ("status", "Status")]
    columns.extend(
        (key, label)
        for key, label in (
            ("role", "Role"),
            ("type", "Type"),
            ("node", "Node"),
            ("resource", "Resource"),
            ("rank", "Rank"),
        )
        if any(item.get(key) not in (None, "") for item in instances)
    )
    table_rows = [
        tuple(
            (
                item.get("name")
                or f"rank={item.get('rank')}"
                if key == "name"
                else item.get(key, "-")
            )
            for key, _ in columns
        )
        for item in instances
    ]
    widths = [
        column_width(label, [row[index] for row in table_rows], max_width=48)
        for index, (_, label) in enumerate(columns)
    ]
    rendered = render_table(
        tuple(label for _, label in columns),
        table_rows,
        widths,
    )
    return "\n".join([rendered[1], rendered[2], *rendered[3:-1]])


def _fetch_hpc_instances(
    job_id: str,
    *,
    limit: int,
    session,
    show_all: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch the bounded instance page, expanding it only for explicit ``--all``."""
    rows, total = browser_api_module.list_hpc_job_instances(
        job_id,
        limit=limit,
        session=session,
    )
    if show_all and total > len(rows):
        expanded_rows, expanded_total = browser_api_module.list_hpc_job_instances(
            job_id,
            limit=max(total, len(rows), 1),
            session=session,
        )
        rows = expanded_rows
        total = max(total, expanded_total, len(rows))
    return rows, total


class HPCInstanceSelectionError(ValueError):
    """A ``--instance`` selector matched no instance in the job."""


@dataclass(frozen=True)
class HPCInstanceView:
    """One HPC instance, split into what the Agent sees and what the API needs.

    ``handle`` is the namespaced instance name the platform wants in
    ``ListSlurmdPodEvent`` and ``GetJobLog``. It is a platform handle —
    ``scrub_raw_ids`` reduces it to ``<redacted>-cluster-slurmd-0`` — so it
    never reaches output. ``label`` is the Agent-visible identity and matches
    the Role (plus Rank, when a role has replicas) column of
    ``inspire hpc instances``.
    """

    handle: str
    pod: str
    role: str
    label: str


def _hpc_instance_role(inst: dict[str, Any]) -> str:
    for key in ("role", "component", "worker_group_name"):
        value = inst.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def hpc_instance_views(instances: Sequence[dict[str, Any]]) -> list[HPCInstanceView]:
    """Project raw instance rows onto the addressable (label, handle) pairs.

    A role that appears once is its own label. A role with replicas takes the
    Rank suffix, using the same rank ``inspire hpc instances`` prints, so a
    label read off that table addresses the instance it names.
    """
    roles = [_hpc_instance_role(inst) for inst in instances]
    duplicated = {role for role in roles if role and roles.count(role) > 1}
    views: list[HPCInstanceView] = []
    for position, (inst, role) in enumerate(zip(instances, roles)):
        handle = str(inst.get("name") or "").strip()
        if not handle:
            continue
        pod = handle.rsplit("/", 1)[-1]
        rank = _hpc_instance_rank(inst, position)
        if not role:
            # Without a component the pod has no readable identity of its own;
            # its rank is all `hpc instances` shows, so address it by that.
            role = "instance"
        label = f"{role}-{rank}" if role in duplicated or role == "instance" else role
        views.append(HPCInstanceView(handle=handle, pod=pod, role=role, label=label))
    return views


def select_hpc_instance_views(
    views: Sequence[HPCInstanceView],
    selectors: Sequence[str],
) -> list[HPCInstanceView]:
    """Filter instances by the Role / Rank identity printed by `hpc instances`.

    An unmatched selector raises rather than narrowing the scope to nothing:
    silently returning no pods would make an empty log or event answer look
    like the platform said "there is nothing here".
    """
    if not selectors:
        return list(views)

    available = sorted({view.label for view in views} | {view.role for view in views})
    chosen: list[HPCInstanceView] = []
    for selector in selectors:
        needle = selector.strip().lower()
        matched = [
            view
            for view in views
            if needle in (view.label.lower(), view.role.lower())
        ]
        if not matched:
            raise HPCInstanceSelectionError(
                f"No HPC instance matches '{selector}'. "
                f"Available: {', '.join(available) or '(none)'}."
            )
        chosen.extend(view for view in matched if view not in chosen)
    return chosen


def _hpc_matches_list_filters(
    job: Any,
    *,
    status: Optional[str],
    keyword: Optional[str],
    workspace_name: str = "",
) -> bool:
    """Apply the public HPC list filters to readable job fields."""
    if (
        status
        and str(getattr(job, "status", "") or "").strip().casefold()
        != status.strip().casefold()
    ):
        return False
    if not keyword:
        return True

    needle = keyword.strip().casefold()
    if not needle:
        return True
    fields = (
        getattr(job, "name", ""),
        getattr(job, "status", ""),
        getattr(job, "entrypoint", ""),
        getattr(job, "project_name", ""),
        getattr(job, "compute_group_name", ""),
        getattr(job, "created_by_name", ""),
        workspace_name,
    )
    return any(needle in str(field or "").casefold() for field in fields)


@click.command("list")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME|all",
    help="Workspace name or 'all'.",
)
@click.option(
    "--status",
    "-s",
    "status_filter",
    default=None,
    metavar="STATUS",
    help="Filter by HPC job status.",
)
@click.option(
    "--keyword",
    default=None,
    metavar="KEYWORD",
    help="Case-insensitive keyword filter for job name/command and readable fields.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum HPC jobs to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every HPC job.")
@pass_context
def list_hpc(
    ctx: Context,
    workspace: Optional[str],
    status_filter: Optional[str],
    keyword: Optional[str],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List the current user's HPC jobs.

    \b
    Examples:
        inspire hpc list --workspace CPU资源空间 --status RUNNING
        inspire hpc list --workspace CPU资源空间 --keyword train
        inspire hpc list --workspace all
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    request_limit = (
        effective_limit if effective_limit is not None else DEFAULT_COLLECTION_LIMIT
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_ids, all_workspaces = resolve_workspace_query_scope(
            workspace=workspace,
            session=session,
        )
        created_by = _current_user_id(session)
        workspace_names = workspace_name_map(session)
        status_query = status_filter.strip().upper() if status_filter else None
        local_filter = bool(keyword and keyword.strip())

        jobs: list[Any] = []
        total = 0
        for workspace_id in workspace_ids:
            workspace_jobs, workspace_total = browser_api_module.list_hpc_jobs(
                workspace_id=workspace_id,
                created_by=created_by,
                status=status_query,
                page_num=1,
                page_size=request_limit,
                session=session,
            )
            if (show_all or local_filter) and workspace_total > len(workspace_jobs):
                workspace_jobs, expanded_total = browser_api_module.list_hpc_jobs(
                    workspace_id=workspace_id,
                    created_by=created_by,
                    status=status_query,
                    page_num=1,
                    page_size=max(workspace_total, len(workspace_jobs), 1),
                    session=session,
                )
                workspace_total = max(
                    workspace_total,
                    expanded_total,
                    len(workspace_jobs),
                )
            jobs.extend(workspace_jobs)
            total += max(workspace_total, len(workspace_jobs))
        if all_workspaces:
            jobs.sort(key=lambda item: str(item.created_at or ""), reverse=True)

        filtered_jobs = [
            job
            for job in jobs
            if _hpc_matches_list_filters(
                job,
                status=status_query,
                keyword=keyword,
                workspace_name=workspace_names.get(job.workspace_id, ""),
            )
        ]
        if local_filter:
            total = len(filtered_jobs)

        page = bound_collection(filtered_jobs, limit=effective_limit, total=total)
        public_items = [
            public_hpc_list_item(
                job,
                workspace=(
                    workspace_names.get(job.workspace_id)
                    or (
                        str(workspace or "")
                        if not all_workspaces
                        else "(workspace name unavailable)"
                    )
                ),
            )
            for job in page.items
        ]
        rows: list[dict[str, str]] = []
        for job in page.items:
            row = {
                "name": scrub_raw_ids(job.name or "N/A"),
                "status": scrub_raw_ids(job.status or "N/A"),
                "created_at": scrub_raw_ids(job.created_at or "N/A"),
                "entrypoint": scrub_raw_ids(job.entrypoint or ""),
                "project_name": scrub_raw_ids(job.project_name or ""),
                "compute_group_name": scrub_raw_ids(job.compute_group_name or ""),
            }
            if all_workspaces:
                row["workspace"] = scrub_raw_ids(
                    workspace_names.get(job.workspace_id)
                    or "(workspace name unavailable)"
                )
            rows.append(row)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "items": public_items,
                        **page.metadata(),
                    }
                )
            )
            return

        click.echo(_format_hpc_list_rows(rows))
        notice = truncation_notice(page, full_option="--all")
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("create")
@click.option("--name", "-n", required=True, metavar="NAME", help="HPC job name")
@click.option(
    "--entrypoint",
    "-c",
    required=True,
    help="Slurm script body (omit #SBATCH headers; use srun to launch the program)",
)
@click.option(
    "--workspace",
    metavar="NAME",
    help="Workspace name. Required unless supplied by --profile.",
)
@click.option(
    "--project",
    "-p",
    metavar="NAME",
    help="Project name. Required unless supplied by --profile.",
)
@click.option(
    "--group",
    "compute_group",
    metavar="NAME",
    help=(
        "Full compute group name copied from the same quota row as --quota. "
        "Required unless supplied by --profile "
        "(e.g. 'HPC-可上网区资源-2'; see 'inspire config context')."
    ),
)
@click.option(
    "--quota",
    "-q",
    metavar="SPEC",
    help=(
        "Node resource as 'gpu,cpu,mem' (mem in GiB). The triple chooses "
        "CPU/memory/GPU available per node. Use 'inspire hpc quota "
        "--workspace <name>' to see valid triples. Slurm options below "
        "(--cpus-per-task / --memory-per-cpu / --number-of-tasks) describe "
        "how your program uses each selected node."
    ),
)
@click.option(
    "--image",
    "-i",
    metavar="NAME|URL",
    help="Docker image URL or visible image name. Required unless supplied by --profile.",
)
@click.option(
    "--profile",
    "profile_name",
    default=None,
    metavar="NAME",
    help="HPC condition profile providing workspace/project/group/quota/image.",
)
@click.option(
    "--image-type",
    type=click.Choice(["SOURCE_PUBLIC", "SOURCE_PRIVATE", "SOURCE_OFFICIAL"]),
    default="SOURCE_PRIVATE",
    show_default=True,
    help="Image source type.",
)
@click.option(
    "--instance-count",
    type=click.IntRange(1),
    default=1,
    show_default=True,
    help="Number of selected nodes to allocate.",
)
@task_priority_option()
@click.option(
    "--number-of-tasks",
    type=click.IntRange(1),
    default=1,
    show_default=True,
    help="Slurm --ntasks value.",
)
@click.option(
    "--cpus-per-task",
    type=click.IntRange(1),
    default=None,
    help=(
        "Slurm --cpus-per-task value. Default: the --quota CPU count divided by "
        "the tasks that land on one node."
    ),
)
@click.option(
    "--memory-per-cpu",
    type=click.IntRange(1),
    default=None,
    help=(
        "Slurm --mem-per-cpu in GiB. Default: the --quota memory divided across "
        "the CPUs one node's tasks use."
    ),
)
@click.option(
    "--enable-hyper-threading/--disable-hyper-threading",
    default=False,
    show_default=True,
    help="Enable hyper-threading",
)
@click.option(
    "--max-time",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    metavar="HOURS",
    help=(
        "Max runtime in hours, sent as the Slurm '--time' cap. Omit to leave "
        "the workspace default; the workspace also enforces its own ceiling."
    ),
)
@click.option(
    "--keep-after-finish",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    metavar="HOURS",
    help=(
        "Keep the job's containers this many hours after it finishes, so they "
        "can still be inspected. Omit to let the platform release them as usual."
    ),
)
@dataset_option()
@click.option(
    "--description",
    default=None,
    metavar="TEXT",
    help="Free-text description stored with the job on the platform.",
)
@click.option(
    "--enable-notification/--no-enable-notification",
    default=False,
    show_default=True,
    help=(
        "Send Feishu notifications to the current user when this job changes "
        "state (running / succeeded / failed)."
    ),
)
@click.option(
    "--public-path-readonly/--no-public-path-readonly",
    default=None,
    help=(
        "Mount the project's public path read-only inside the containers "
        "(平台 高级设置·项目Public只读挂载). Omit to leave the platform default."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Resolve workspace, project, quota, compute group, image, and Slurm fields, "
        "then print the plan without submitting the HPC job."
    ),
)
@pass_context
def create_hpc(
    ctx: Context,
    name: str,
    entrypoint: str,
    compute_group: Optional[str],
    quota: Optional[str],
    project: Optional[str],
    workspace: Optional[str],
    profile_name: Optional[str],
    image: Optional[str],
    image_type: str,
    instance_count: int,
    priority: Optional[int],
    number_of_tasks: int,
    cpus_per_task: Optional[int],
    memory_per_cpu: Optional[int],
    enable_hyper_threading: bool,
    max_time: Optional[float],
    keep_after_finish: Optional[float],
    datasets: tuple[str, ...],
    description: Optional[str],
    enable_notification: bool,
    public_path_readonly: Optional[bool],
    dry_run: bool,
) -> None:
    """Create a CPU Slurm / HPC batch job.

    Two independent layers:
      * Node-level: --quota gpu,cpu,mem chooses the resources available per
        node; --instance-count chooses how many nodes.
      * Slurm-level: --number-of-tasks / --cpus-per-task / --memory-per-cpu
        describe how your program runs inside those nodes.

    Nothing on the platform checks the second layer against the first, and a
    mismatch is silent: too much CPU or memory per task ends as FAILED with no
    log and no event, and too many tasks for the nodes you bought sits in
    RUNNING forever having run nothing. Those combinations are refused here
    before the submit.

    ``-c/--entrypoint`` must be the Slurm script body. Do not include
    ``#SBATCH`` headers; use ``srun`` to launch the program — without it the
    job still reports SUCCEEDED, but `hpc status` shows ``Steps: 0/0`` and only
    the first node ever ran anything.

    \b
    Examples:
        inspire hpc create -n preprocess --workspace CPU资源空间 --project CI-情境智能 \
          --group HPC-可上网区资源-2 -q 0,20,256 --image hpc-base:v1 \
          -c 'srun bash -lc "python preprocess.py"'
        inspire hpc create -n probe --profile cpu-hpc -c 'srun hostname' --dry-run
        inspire hpc create -n index --profile cpu-hpc --dataset pixabay-81k:v0 \
          --max-time 4 --keep-after-finish 0.5 \
          -c 'srun bash -lc "python index.py /inspire/dataset/pixabay-81k/v0"'
    """
    dataset_mounts = parse_dataset_specs_or_usage_error(datasets)
    try:
        from inspire.cli.utils.quota_resolver import (
            QuotaMatchError,
            QuotaParseError,
            SCHEDULE_TYPE_HPC,
            build_resource_spec_price,
            parse_quota,
            resolve_quota,
        )

        config, _ = Config.from_files_and_env()

        fields = apply_workload_profile(
            profiles=getattr(config, "profiles", {}),
            kind="hpc",
            profile_name=profile_name,
            values={
                "workspace": workspace,
                "project": project,
                "group": compute_group,
                "image": image,
                "quota": quota,
            },
        )
        workspace = cast(Optional[str], fields["workspace"])
        project = cast(Optional[str], fields["project"])
        compute_group = cast(Optional[str], fields["group"])
        image = cast(Optional[str], fields["image"])
        quota = cast(Optional[str], fields["quota"])

        for field_name, value in (
            ("workspace", workspace),
            ("project", project),
            ("group", compute_group),
            ("quota", quota),
            ("image", image),
        ):
            if not value:
                _handle_error(
                    ctx,
                    "ValidationError",
                    profile_required_message("hpc", field_name),
                    EXIT_CONFIG_ERROR,
                )
                return

        workspace = cast(str, workspace)
        project = cast(str, project)
        compute_group = cast(str, compute_group)
        image = cast(str, image)
        quota = cast(str, quota)

        session = get_web_session()
        resolved_workspace_id = select_workspace_id(
            explicit_workspace_name=workspace,
            session=session,
        )
        if resolved_workspace_id is None:
            raise ConfigError(profile_required_message("hpc", "workspace"))
        resolved_project_id = _resolve_project_id(
            config,
            project,
            workspace_id=resolved_workspace_id,
            session=session,
            ctx=ctx,
        )
        final_priority = resolve_workspace_task_priority(
            priority,
            session=session,
            workspace_id=resolved_workspace_id,
            project_id=resolved_project_id,
        )
        final_image = image
        if _looks_like_full_slurm_script(entrypoint):
            _handle_error(
                ctx,
                "ValidationError",
                "HPC entrypoint must be the Slurm body, not a full sbatch script.",
                EXIT_CONFIG_ERROR,
                hint="Pass only the lines after the #SBATCH headers and launch the workload with srun.",
            )
            return

        try:
            quota_spec = parse_quota(quota)
        except QuotaParseError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
            return

        try:
            resolved_quota = resolve_quota(
                spec=quota_spec,
                workspace_id=resolved_workspace_id,
                session=session,
                schedule_config_type=SCHEDULE_TYPE_HPC,
                group_override=compute_group,
            )
        except QuotaMatchError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
            return

        quota_id = resolved_quota.quota_id
        resolved_compute_group_id = resolved_quota.logic_compute_group_id
        resource_spec_price = build_resource_spec_price(quota=resolved_quota)

        try:
            layout = resolve_slurm_layout(
                node_cpu=resolved_quota.cpu_count,
                node_memory_gib=resolved_quota.memory_gib,
                instance_count=instance_count,
                number_of_tasks=number_of_tasks,
                cpus_per_task=cpus_per_task,
                memory_per_cpu=memory_per_cpu,
            )
        except SlurmLayoutError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
            return

        # The platform resolves and checks every mount before the job is
        # submitted, exactly as the console's 校验数据 button does.
        try:
            dataset_info = resolve_dataset_info(
                dataset_mounts,
                workspace_id=resolved_workspace_id,
                session=session,
            )
        except DatasetSpecError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
            return

        create_kwargs = build_hpc_create_payload(
            name=name,
            logic_compute_group_id=resolved_compute_group_id,
            project_id=resolved_project_id,
            workspace_id=resolved_workspace_id,
            image=final_image,
            image_type=image_type,
            entrypoint=entrypoint,
            quota_id=quota_id,
            instance_count=instance_count,
            task_priority=final_priority,
            number_of_tasks=layout.number_of_tasks,
            cpus_per_task=layout.cpus_per_task,
            memory_per_cpu=layout.memory_per_cpu,
            enable_hyper_threading=enable_hyper_threading,
            resource_spec_price=resource_spec_price,
            enable_notification=enable_notification,
            max_time_hours=max_time,
            dataset_info=dataset_info,
            description=description,
            keep_after_finish_hours=keep_after_finish,
            public_path_readonly=public_path_readonly,
            session=session,
        )

        project_text = _project_label(config, project)
        workspace_text = workspace_label(session, resolved_workspace_id, workspace)

        if dry_run:
            payload = _hpc_plan_payload(
                name=name,
                create_kwargs=create_kwargs,
                project_label=project_text,
                workspace_label=workspace_text,
                compute_group_name=resolved_quota.compute_group_name,
                dataset_mounts=dataset_mounts,
            )
            if ctx.json_output:
                click.echo(json_formatter.format_json(payload))
                return
            click.echo(f"Create plan: {scrub_raw_ids(name)}")
            click.echo(f"Project: {scrub_raw_ids(project_text)}")
            click.echo(f"Workspace: {scrub_raw_ids(workspace_text)}")
            click.echo(f"Compute: {scrub_raw_ids(resolved_quota.compute_group_name)}")
            click.echo(f"Resource: {quota_spec.display()}")
            click.echo(
                f"Slurm: {layout.number_of_tasks} task(s), "
                f"{layout.cpus_per_task} CPU/task, "
                f"{layout.memory_per_cpu} GiB/CPU"
            )
            if final_priority is not None:
                click.echo(f"Priority: {final_priority}")
            if instance_count > 1:
                click.echo(f"Nodes: {instance_count}")
            for line in describe_dataset_mounts(dataset_mounts):
                click.echo(f"Dataset: {line}")
            max_time_text = (create_kwargs.get("sbatch_script") or {}).get("job_max_time")
            if max_time_text:
                click.echo(f"Max time: {max_time_text} (day-hh:mm:ss)")
            if keep_after_finish is not None:
                click.echo(f"Keep after finish: {keep_after_finish} h")
            if description is not None:
                click.echo(f"Description: {scrub_raw_ids(description)}")
            if enable_notification:
                click.echo("Notifications: enabled")
            if public_path_readonly is not None:
                click.echo(
                    "Public path: read-only" if public_path_readonly else "Public path: writable"
                )
            click.echo(f"Command: {scrub_raw_ids(entrypoint)}")
            srun_warning = _missing_srun_warning(entrypoint)
            if srun_warning:
                click.echo(srun_warning, err=True)
            return

        data = browser_api_module.create_hpc_job(
            payload=create_kwargs,
            session=session,
        )
        created_id = _created_hpc_job_id(data)
        if not created_id:
            # `CreateJobConsole` answers `{job_id, sub_code, sub_msg}`. Without
            # a job id there is nothing to report as created, and printing the
            # success line anyway is exactly the "submitted fine, ran nothing"
            # reading this command exists to avoid.
            _handle_error(
                ctx,
                "APIError",
                "HPC create returned no job id; the job was not created.",
                EXIT_API_ERROR,
                hint=str(data.get("sub_msg") or "") or None,
            )
            return
        remember_resource_identity(
            session=session,
            resource_type="hpc",
            resource_id=created_id,
            name=name,
            workspace_id=resolved_workspace_id,
            owner_scope="self",
            status=str(data.get("status") or ""),
        )

        if ctx.json_output:
            created: dict[str, Any] = {"name": name, "status": "created"}
            if dataset_mounts:
                created["datasets"] = dataset_mount_views(dataset_mounts)
            click.echo(json_formatter.format_json(created))
            return

        click.echo(human_formatter.format_mutation_success("HPC", "created", name))
        for line in describe_dataset_mounts(dataset_mounts):
            click.echo(f"Dataset: {line}")
        srun_warning = _missing_srun_warning(entrypoint)
        if srun_warning:
            click.echo(srun_warning, err=True)

    except TaskPriorityError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("status")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def status_hpc(ctx: Context, name: str, workspace: str, pick: Optional[int]) -> None:
    """Show the compact public status view for an HPC job name."""
    name = _reject_hpc_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env()
        session = get_web_session()
        data = _run_readonly_hpc_operation(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
            pick=pick,
            operation=lambda job_id, live_session: (
                browser_api_module.get_hpc_job_detail(
                    job_id,
                    session=live_session,
                )
            ),
        )

        detail = public_hpc_status(data, fallback_name=name)
        if ctx.json_output:
            # `steps` is a `done/total` counter, and the path redactor reads the
            # `-/1` form as an absolute path and returns `-<redacted>` — which
            # hides exactly the field that says whether anything ran.
            click.echo(json_formatter.format_json(detail, preserve_paths={"steps"}))
            return

        click.echo(format_hpc_status(detail))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("instances")
@click.argument("name", metavar="NAME")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME",
    help="Workspace name.",
)
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum instances to display (default: 20).",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Show the complete instance list.",
)
@pass_context
def instances_hpc(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List pod/component instances for an HPC job."""
    try:
        output_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    request_limit = (
        output_limit if output_limit is not None else DEFAULT_COLLECTION_LIMIT
    )
    resolution_limit = (
        limit if limit is not None else _DEFAULT_INSTANCE_SCAN_LIMIT
    )

    name = _reject_hpc_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        rows, total = _run_readonly_hpc_operation(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=resolution_limit,
            pick=pick,
            operation=lambda job_id, live_session: _fetch_hpc_instances(
                job_id,
                limit=request_limit,
                session=live_session,
                show_all=show_all,
            ),
        )
        page = bound_collection(rows, limit=output_limit, total=total)
        public_items = _public_hpc_instances(page.items)

        if ctx.json_output:
            payload: dict[str, Any] = {
                "name": scrub_raw_ids(name),
                "items": public_items,
                **page.metadata(),
            }
            click.echo(json_formatter.format_json(payload))
            return

        click.echo(_format_hpc_instances(public_items))
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("stop")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def stop_hpc(ctx: Context, name: str, workspace: str, pick: Optional[int]) -> None:
    """Stop an HPC job (pass the job name)."""
    name = _reject_hpc_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env()
        session = get_web_session()
        job_id = _resolve_hpc_name_in_workspace(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
            pick=pick,
            require_live=True,
        )
        browser_api_module.stop_hpc_job(job_id, session=session)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"name": name, "status": "stopped"}
                )
            )
            return
        click.echo(human_formatter.format_mutation_success("HPC", "stopped", name))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("delete")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def delete_hpc(ctx: Context, name: str, workspace: str, yes: bool, pick: Optional[int]) -> None:
    """Permanently delete an HPC job entry (pass the job name).

    \b
    The entry disappears from the platform HPC list. This cannot be
    undone; if the job is still running, `stop` it first.

    \b
    Example:
        inspire hpc delete my-hpc-run --workspace CPU资源空间
    """
    name = _reject_hpc_name_at_boundary(ctx, name)
    require_confirmation(
        ctx,
        yes=yes,
        prompt=(
            f"Permanently delete HPC job '{scrub_raw_ids(name)}'? "
            "This cannot be undone."
        ),
        message="HPC job deletion requires confirmation.",
    )

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        job_id = _resolve_hpc_name_in_workspace(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=10000,
            pick=pick,
            require_live=True,
        )
        browser_api_module.delete_hpc_job(job_id=job_id, session=session)
        workspace_id = select_workspace_id(
            explicit_workspace_name=workspace,
            session=session,
        )
        if workspace_id:
            forget_resource_identity(
                session=session,
                resource_type="hpc",
                resource_id=job_id,
                name=name,
                workspace_id=workspace_id,
                owner_scope="self",
            )

        if ctx.json_output:
            click.echo(json_formatter.format_json({"name": name, "status": "deleted"}))
            return
        click.echo(human_formatter.format_mutation_success("HPC", "deleted", name))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = [
    "HPCInstanceSelectionError",
    "HPCInstanceView",
    "list_hpc",
    "create_hpc",
    "hpc_instance_views",
    "select_hpc_instance_views",
    "status_hpc",
    "instances_hpc",
    "stop_hpc",
    "delete_hpc",
]
