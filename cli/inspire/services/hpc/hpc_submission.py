"""HPC submission payload and Slurm layout shared by CLI and SDK."""

from __future__ import annotations
from typing import Any, Sequence
from dataclasses import dataclass
from inspire.services.catalog.datasets import dataset_mount_views
from inspire.platform.web.browser_api.datasets import DatasetMount
from inspire.services.catalog.image_resolution import resolve_image_url


def created_hpc_job_id(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("job_id", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    for key in ("job", "data", "result"):
        value = created_hpc_job_id(payload.get(key))
        if value:
            return value
    return ""


def looks_like_full_slurm_script(entrypoint: str) -> bool:
    stripped = entrypoint.lstrip()
    return stripped.startswith("#!") or "#SBATCH" in entrypoint


def hpc_plan_payload(
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


def slurm_time_fields(max_time_hours: float | None) -> dict[str, Any]:
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
    image_resolver: Any = None,
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
            **slurm_time_fields(max_time_hours),
        },
        "slurm_cluster_spec": {
            "predef_quota_id": quota_id,
            "cpu": int(resource_spec_price.get("cpu_count") or 0),
            "mem_gi": int(resource_spec_price.get("memory_size_gib") or 0),
            # The platform matches on the registry URL, not the visible name;
            # sending the name is rejected with 无法找到对应镜像.
            "image": (image_resolver or resolve_image_url)(
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
