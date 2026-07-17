"""Batch and matrix submission helpers for workload command groups."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import click

try:  # pragma: no cover - Python 3.11 path
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils import job_submit
from inspire.cli.utils.auth import AuthenticationError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.quota_resolver import (
    QuotaMatchError,
    QuotaParseError,
    SCHEDULE_TYPE_DSW,
    SCHEDULE_TYPE_HPC,
    SCHEDULE_TYPE_TRAIN,
    build_resource_spec_price,
    parse_quota,
    resolve_quota,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workload_profiles import (
    PROFILE_FIELDS,
    apply_workload_profile,
    merge_workload_profiles,
    normalize_workload_profiles,
    profile_required_message,
)
from inspire.config.workspaces import select_workspace_id, workspace_label
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import NotebookFailedError
from inspire.platform.web.session import get_web_session


class _FormatMap(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        raise KeyError(f"Unknown template variable: {key}")


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        if path.suffix.lower() == ".json":
            data = json.load(f)
        elif path.suffix.lower() in {".toml", ".tml"}:
            data = tomllib.load(f)
        else:
            raise ConfigError("Batch config must be JSON or TOML.")
    if not isinstance(data, dict):
        raise ConfigError("Batch config must be an object at the top level.")
    return data


def _matrix_rows(matrix: Any) -> list[dict[str, Any]]:
    if matrix in (None, {}):
        return [{}]
    if not isinstance(matrix, dict):
        raise ConfigError("matrix must be an object.")
    keys = list(matrix.keys())
    values: list[list[Any]] = []
    for key in keys:
        raw = matrix[key]
        if not isinstance(raw, list) or not raw:
            raise ConfigError(f"matrix.{key} must be a non-empty array.")
        values.append(raw)
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def _render(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return value.format_map(_FormatMap(variables))
    if isinstance(value, list):
        return [_render(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, variables) for key, item in value.items()}
    return value


def _expanded_items(
    data: dict[str, Any],
    *,
    item_key: str = "jobs",
) -> list[dict[str, Any]]:
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError("defaults must be an object.")
    _ensure_no_condition_defaults(defaults, item_key=item_key)

    items = data.get(item_key)
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{item_key} must be a non-empty array.")

    expanded: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            raise ConfigError(f"Each {item_key} entry must be an object.")
        item_matrix = raw_item.get("matrix", data.get("matrix"))
        for matrix_vars in _matrix_rows(item_matrix):
            merged = {**defaults, **{k: v for k, v in raw_item.items() if k != "matrix"}}
            variables = {**merged, **matrix_vars, "index": len(expanded), "item_index": index}
            if item_key == "jobs":
                variables["job_index"] = index
            rendered = _render(merged, variables)
            rendered["_matrix"] = dict(matrix_vars)
            expanded.append(rendered)
    return expanded


def _require_str(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Batch item is missing required string field: {key}")
    return value


def _require_int(item: dict[str, Any], key: str, *, min_value: int | None = None) -> int:
    if key not in item or item[key] is None:
        raise ConfigError(f"Batch item is missing required integer field: {key}")
    value = item[key]
    if isinstance(value, bool):
        raise ConfigError(f"Batch item field {key} must be an integer.")
    try:
        number = int(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Batch item field {key} must be an integer.") from e
    if min_value is not None and number < min_value:
        raise ConfigError(f"Batch item field {key} must be >= {min_value}.")
    return number


def _require_float(item: dict[str, Any], key: str, *, min_value: float | None = None) -> float:
    if key not in item or item[key] is None:
        raise ConfigError(f"Batch item is missing required number field: {key}")
    value = item[key]
    if isinstance(value, bool):
        raise ConfigError(f"Batch item field {key} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Batch item field {key} must be a number.") from e
    if min_value is not None and number < min_value:
        raise ConfigError(f"Batch item field {key} must be >= {min_value}.")
    return number


def _require_bool(item: dict[str, Any], key: str) -> bool:
    if key not in item or item[key] is None:
        raise ConfigError(f"Batch item is missing required boolean field: {key}")
    value = item[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"Batch item field {key} must be a boolean.")


def _optional_str(item: dict[str, Any], key: str) -> str | None:
    if key not in item or item[key] is None:
        return None
    return _require_str(item, key)


def _optional_str_list(item: dict[str, Any], key: str) -> list[str]:
    if key not in item or item[key] is None:
        return []
    value = item[key]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ConfigError(f"Batch item field {key} must not contain empty node names.")
        return [stripped]
    if not isinstance(value, list):
        raise ConfigError(f"Batch item field {key} must be a string or an array of strings.")
    result: list[str] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, str) or not raw.strip():
            raise ConfigError(
                f"Batch item field {key}[{index}] must be a non-empty string."
            )
        result.append(raw.strip())
    return result


def _optional_bool(item: dict[str, Any], key: str, *, default: bool = False) -> bool:
    if key not in item or item[key] is None:
        return default
    return _require_bool(item, key)


def _optional_int(item: dict[str, Any], key: str, *, min_value: int | None = None) -> int | None:
    if key not in item or item[key] is None:
        return None
    return _require_int(item, key, min_value=min_value)


def _batch_profiles(data: dict[str, Any]) -> dict[str, dict[str, dict[str, str]]]:
    return normalize_workload_profiles(data.get("profiles", {}))


def _ensure_no_condition_defaults(defaults: dict[str, Any], *, item_key: str) -> None:
    condition_fields = set(PROFILE_FIELDS)
    disallowed = condition_fields | {"compute_group"}
    bad = sorted(key for key in defaults if key in disallowed)
    if bad:
        joined = ", ".join(bad)
        raise ConfigError(
            f"Batch defaults cannot set workload condition fields: {joined}. "
            f"Move them into a profile and set profile = \"<name>\" for {item_key} items."
        )


def _apply_item_profile(
    *,
    config: Config,
    kind: str,
    item: dict[str, Any],
    local_profiles: dict[str, dict[str, dict[str, str]]],
) -> dict[str, Any]:
    profile_name = _optional_str(item, "profile")
    profiles = merge_workload_profiles(getattr(config, "profiles", {}), local_profiles)
    fields = apply_workload_profile(
        profiles=profiles,
        kind=kind,
        profile_name=profile_name,
        values={field: item.get(field) for field in PROFILE_FIELDS},
    )
    merged = dict(item)
    for field, value in fields.items():
        if value is not None:
            merged[field] = value
    return merged


def _require_condition_str(item: dict[str, Any], key: str, *, kind: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(profile_required_message(kind, key, batch=True))
    return value


def _validate_kind_if_present(
    item: dict[str, Any],
    *,
    allowed: set[str],
    command_name: str,
) -> None:
    has_type = "type" in item and item["type"] is not None
    has_kind = "kind" in item and item["kind"] is not None
    if not has_type and not has_kind:
        return

    raw_type = item.get("type")
    raw_kind = item.get("kind")
    if has_type and (not isinstance(raw_type, str) or not raw_type.strip()):
        raise ConfigError("Batch item field type must be a non-empty string when set.")
    if has_kind and (not isinstance(raw_kind, str) or not raw_kind.strip()):
        raise ConfigError("Batch item field kind must be a non-empty string when set.")

    type_value = str(raw_type).strip().lower() if has_type else None
    kind_value = str(raw_kind).strip().lower() if has_kind else None
    if type_value and kind_value and type_value != kind_value:
        raise ConfigError("Batch item type and kind must match when both are set.")
    value = type_value or kind_value
    if value not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ConfigError(f"{command_name} batch item type must be one of: {allowed_text}.")


def _require_max_time_hours(item: dict[str, Any]) -> float:
    has_max_time = "max_time" in item and item["max_time"] is not None
    has_max_time_hours = "max_time_hours" in item and item["max_time_hours"] is not None
    if has_max_time and has_max_time_hours:
        raise ConfigError("Batch item must use only one of max_time or max_time_hours.")
    if has_max_time:
        return _require_float(item, "max_time", min_value=0.000001)
    if has_max_time_hours:
        return _require_float(item, "max_time_hours", min_value=0.000001)
    raise ConfigError("Batch item is missing required number field: max_time")


def _optional_max_time_hours(item: dict[str, Any]) -> float | None:
    has_max_time = "max_time" in item and item["max_time"] is not None
    has_max_time_hours = "max_time_hours" in item and item["max_time_hours"] is not None
    if not has_max_time and not has_max_time_hours:
        return None
    return _require_max_time_hours(item)


def _prepare_training_item(
    item: dict[str, Any],
    *,
    config: Config,
    session: Any,
) -> job_submit.JobSubmissionPlan:
    quota_spec = parse_quota(_require_condition_str(item, "quota", kind="job"))
    workspace_id = select_workspace_id(
        config,
        explicit_workspace_name=_require_condition_str(item, "workspace", kind="job"),
        session=session,
    )
    if not workspace_id:
        raise ConfigError("Batch training item requires workspace resolution.")
    resolved_quota = resolve_quota(
        spec=quota_spec,
        workspace_id=workspace_id,
        session=session,
        schedule_config_type=SCHEDULE_TYPE_TRAIN,
        group_override=_require_condition_str(item, "group", kind="job"),
    )
    selected, _ = job_submit.select_project_for_workspace(
        config,
        workspace_id=workspace_id,
        requested=_require_condition_str(item, "project", kind="job"),
    )
    fault_retry = _optional_int(item, "fault_tolerance_max_retry", min_value=0)
    return job_submit.build_training_job_plan(
        config=config,
        name=_require_str(item, "name"),
        command=_require_str(item, "command"),
        quota=resolved_quota,
        framework=_optional_str(item, "framework") or "pytorch",
        project_id=selected.project_id,
        workspace_id=workspace_id,
        image=_require_condition_str(item, "image", kind="job"),
        priority=_optional_int(item, "priority", min_value=1) or 10,
        nodes=_optional_int(item, "nodes", min_value=1) or 1,
        max_time_hours=_optional_max_time_hours(item),
        project_name=selected.name,
        auto_fault_tolerance=_optional_bool(
            item,
            "auto_fault_tolerance",
            default=config.job_auto_fault_tolerance,
        ),
        fault_tolerance_max_retry=(
            config.job_fault_tolerance_max_retry if fault_retry is None else fault_retry
        ),
        enable_notification=_optional_bool(
            item,
            "enable_notification",
            default=config.job_enable_notification,
        ),
        exclude_nodes=_optional_str_list(item, "exclude_nodes"),
        shm_size=_optional_int(item, "shm_size", min_value=1),
    )


def _prepare_hpc_item(
    item: dict[str, Any],
    *,
    config: Config,
    session: Any,
) -> dict[str, Any]:
    from inspire.cli.commands.hpc.hpc_commands import build_hpc_create_payload
    from inspire.cli.commands.hpc.hpc_commands import _looks_like_full_slurm_script
    from inspire.cli.commands.hpc.hpc_commands import _resolve_project_id

    entrypoint = _require_str(item, "entrypoint")
    if _looks_like_full_slurm_script(entrypoint):
        raise ConfigError("HPC entrypoint must be the Slurm body, not a full sbatch script.")

    quota_spec = parse_quota(_require_condition_str(item, "quota", kind="hpc"))
    workspace_id = select_workspace_id(
        config,
        explicit_workspace_name=_require_condition_str(item, "workspace", kind="hpc"),
        session=session,
    )
    if not workspace_id:
        raise ConfigError("Batch HPC item requires workspace resolution.")
    resolved_quota = resolve_quota(
        spec=quota_spec,
        workspace_id=workspace_id,
        session=session,
        schedule_config_type=SCHEDULE_TYPE_HPC,
        group_override=_require_condition_str(item, "group", kind="hpc"),
    )
    cpus_per_task = _optional_int(item, "cpus_per_task", min_value=1)
    if cpus_per_task is None:
        cpus_per_task = max(1, int(quota_spec.cpu_count))
    memory_per_cpu = _optional_int(item, "memory_per_cpu", min_value=1)
    if memory_per_cpu is None:
        memory_per_cpu = max(1, int(quota_spec.memory_gib) // max(1, int(cpus_per_task)))
    return build_hpc_create_payload(
        name=_require_str(item, "name"),
        logic_compute_group_id=resolved_quota.logic_compute_group_id,
        project_id=_resolve_project_id(config, _require_condition_str(item, "project", kind="hpc")),
        workspace_id=workspace_id,
        image=_require_condition_str(item, "image", kind="hpc"),
        image_type=_optional_str(item, "image_type") or "SOURCE_PRIVATE",
        entrypoint=entrypoint,
        quota_id=resolved_quota.quota_id,
        instance_count=_optional_int(item, "instance_count", min_value=1) or 1,
        task_priority=_optional_int(item, "priority", min_value=1) or 10,
        number_of_tasks=_optional_int(item, "number_of_tasks", min_value=1) or 1,
        cpus_per_task=int(cpus_per_task),
        memory_per_cpu=int(memory_per_cpu),
        enable_hyper_threading=_optional_bool(item, "enable_hyper_threading", default=False),
        resource_spec_price=build_resource_spec_price(quota=resolved_quota),
    )

def _project_request_value(config: Config, requested: str) -> str:
    if requested.startswith("project-"):
        raise ConfigError(
            "Batch item field project takes a project name. "
            "See `inspire config context` for available names."
        )
    for alias, project_id in (config.projects or {}).items():
        if alias.lower() == requested.lower():
            return project_id
    return requested


def _select_notebook_project(
    *,
    config: Config,
    workspace_id: str,
    requested: str,
    session: Any,
    needs_gpu_quota: bool,
):
    projects = browser_api_module.list_projects(workspace_id=workspace_id, session=session)
    if not projects:
        raise ConfigError("No projects available in this workspace.")

    congested = None
    if needs_gpu_quota:
        congested = (
            browser_api_module.check_scheduling_health(
                workspace_id=workspace_id,
                project_ids={p.project_id for p in projects},
                session=session,
            )
            or None
        )

    try:
        selected, _ = browser_api_module.select_project(
            projects,
            _project_request_value(config, requested),
            allow_requested_over_quota=False,
            needs_gpu_quota=needs_gpu_quota,
            project_order=config.project_order or None,
            congested_projects=congested,
        )
    except ValueError as e:
        raise ConfigError(str(e)) from e
    return selected


def _select_notebook_image(*, workspace_id: str, requested: str, session: Any):
    from inspire.cli.commands.notebook.notebook_create_flow import _find_image_match

    images = browser_api_module.list_images(workspace_id=workspace_id, session=session)
    selected = _find_image_match(images, requested)
    if not selected:
        for source in ("SOURCE_PUBLIC", "SOURCE_PRIVATE"):
            try:
                extra_images = browser_api_module.list_images(
                    workspace_id=workspace_id,
                    source=source,
                    session=session,
                )
            except Exception:
                continue
            images = images + extra_images
            selected = _find_image_match(images, requested)
            if selected:
                break
    if not selected:
        raise ConfigError(f"Image {requested!r} not found.")
    return selected


def _prepare_notebook_item(
    item: dict[str, Any],
    *,
    config: Config,
    session: Any,
) -> dict[str, Any]:
    from inspire.cli.commands.notebook.notebook_create_flow import format_quota_display

    quota_spec = parse_quota(_require_condition_str(item, "quota", kind="notebook"))
    workspace_name = _require_condition_str(item, "workspace", kind="notebook")
    workspace_id = select_workspace_id(
        config,
        explicit_workspace_name=workspace_name,
        session=session,
    )
    if not workspace_id:
        raise ConfigError("Batch notebook item requires workspace resolution.")

    resolved_quota = resolve_quota(
        spec=quota_spec,
        workspace_id=workspace_id,
        session=session,
        schedule_config_type=SCHEDULE_TYPE_DSW,
        group_override=_require_condition_str(item, "group", kind="notebook"),
    )
    selected_project = _select_notebook_project(
        config=config,
        workspace_id=workspace_id,
        requested=_require_condition_str(item, "project", kind="notebook"),
        session=session,
        needs_gpu_quota=resolved_quota.gpu_count > 0,
    )
    selected_image = _select_notebook_image(
        workspace_id=workspace_id,
        requested=_require_condition_str(item, "image", kind="notebook"),
        session=session,
    )
    shm_size = _optional_int(item, "shm_size", min_value=1) or 32
    task_priority = _optional_int(item, "priority", min_value=1) or 10
    resource_spec_price = build_resource_spec_price(
        quota=resolved_quota,
        shared_memory_size=shm_size,
    )

    create_kwargs = {
        "name": _require_str(item, "name"),
        "project_id": selected_project.project_id,
        "project_name": selected_project.name,
        "image_id": selected_image.image_id,
        "image_url": selected_image.url,
        "logic_compute_group_id": resolved_quota.logic_compute_group_id,
        "quota_id": resolved_quota.quota_id,
        "gpu_type": resolved_quota.gpu_type,
        "gpu_count": resolved_quota.gpu_count,
        "cpu_count": resolved_quota.cpu_count,
        "memory_size": resolved_quota.memory_gib,
        "shared_memory_size": shm_size,
        "auto_stop": _optional_bool(item, "auto_stop", default=False),
        "workspace_id": workspace_id,
        "task_priority": task_priority,
        "resource_spec_price": resource_spec_price,
    }

    post_start = _optional_str(item, "post_start")
    post_start_script_raw = _optional_str(item, "post_start_script")
    if post_start and post_start_script_raw:
        raise ConfigError("Batch notebook item must use either post_start or post_start_script.")
    post_start_script = Path(post_start_script_raw).expanduser() if post_start_script_raw else None
    if post_start_script and not post_start_script.is_file():
        raise ConfigError(f"Notebook post_start_script not found: {post_start_script}")

    return {
        "kind": "notebook",
        "name": create_kwargs["name"],
        "create_kwargs": create_kwargs,
        "workspace_name": workspace_label(session, workspace_id, workspace_name),
        "project_name": selected_project.name,
        "image_name": selected_image.name,
        "resource": format_quota_display(resolved_quota),
        "compute_group_name": resolved_quota.compute_group_name,
        "wait": _optional_bool(item, "wait", default=False),
        "post_start": post_start,
        "post_start_script": post_start_script,
        "gpu_count": resolved_quota.gpu_count,
    }


def _submit_notebook_plan(plan: dict[str, Any], *, config: Config, session: Any) -> dict[str, Any]:
    from inspire.cli.commands.notebook.notebook_create_flow import (
        _extract_notebook_id,
        _resolve_created_notebook_id,
    )
    from inspire.cli.utils.notebook_post_start import resolve_notebook_post_start_spec

    create_kwargs = dict(plan["create_kwargs"])
    result = browser_api_module.create_notebook(**create_kwargs, session=session)
    notebook_id = _extract_notebook_id(result)
    wait = bool(plan.get("wait"))
    post_start_spec = resolve_notebook_post_start_spec(
        config=config,
        post_start=plan.get("post_start"),
        post_start_script=plan.get("post_start_script"),
    )

    if post_start_spec is not None:
        wait = True
    if wait and not notebook_id:
        notebook_id = _resolve_created_notebook_id(
            name=str(plan["name"]),
            workspace_id=str(create_kwargs["workspace_id"]),
            session=session,
        )
    if wait and not notebook_id:
        raise ConfigError(
            f"Notebook {plan['name']!r} was submitted, but the platform response did not "
            "let the CLI find the created notebook by name for wait/post_start."
        )
    if wait:
        browser_api_module.wait_for_notebook_running(
            notebook_id=notebook_id,
            session=session,
            timeout=600,
        )
    if post_start_spec is not None:
        browser_api_module.run_command_in_notebook(
            notebook_id=notebook_id,
            command=post_start_spec.command,
            session=session,
            timeout=20,
            completion_marker=post_start_spec.completion_marker,
        )

    return {"kind": "notebook", "name": plan["name"], "result": result}


def _require_list(item: dict[str, Any], key: str) -> list[Any]:
    value = item.get(key)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"Batch item field {key} must be a non-empty array.")
    return value


def _ray_worker_specs(
    item: dict[str, Any],
    *,
    config: Config,
    local_profiles: dict[str, dict[str, dict[str, str]]],
) -> tuple[str, ...]:
    specs: list[str] = []
    for raw in _require_list(item, "workers"):
        if isinstance(raw, str):
            if not raw.strip():
                raise ConfigError("Batch item field workers must not contain empty strings.")
            specs.append(raw)
            continue
        if not isinstance(raw, dict):
            raise ConfigError("Batch item field workers must contain strings or objects.")
        worker = _apply_item_profile(
            config=config,
            kind="ray",
            item=dict(raw),
            local_profiles=local_profiles,
        )
        missing = {"name", "min", "max"} - set(worker.keys())
        if missing:
            raise ConfigError(f"Ray worker spec is missing keys: {sorted(missing)}.")
        for field in ("image", "group", "quota"):
            _require_condition_str(worker, field, kind="ray")
        skip = {"profile", "workspace", "project"}
        parts = [
            f"{key}={worker[key]}"
            for key in sorted(worker.keys())
            if key not in skip and worker[key] is not None
        ]
        specs.append(";".join(parts))
    return tuple(specs)


def _prepare_ray_item(
    item: dict[str, Any],
    *,
    ctx: Context,
    config: Config,
    session: Any,
    local_profiles: dict[str, dict[str, dict[str, str]]],
):
    from inspire.cli.commands.ray.ray_commands import _assemble_create_body

    removed = [
        key
        for key in ("head_image", "head_group", "head_quota", "head_image_type", "head_shm")
        if key in item
    ]
    if removed:
        joined = ", ".join(sorted(removed))
        raise ConfigError(
            f"Unsupported Ray batch fields: {joined}. "
            "Use image, group, quota, image_type, and shm_size instead."
        )

    return _assemble_create_body(
        ctx,
        config=config,
        session=session,
        name=_require_str(item, "name"),
        command=_require_str(item, "command"),
        description=_optional_str(item, "description") or "",
        project=_require_condition_str(item, "project", kind="ray"),
        workspace=_require_condition_str(item, "workspace", kind="ray"),
        priority=_optional_int(item, "priority", min_value=1) or 10,
        image=_require_condition_str(item, "image", kind="ray"),
        image_type=_optional_str(item, "image_type") or "SOURCE_PUBLIC",
        group=_require_condition_str(item, "group", kind="ray"),
        quota=_require_condition_str(item, "quota", kind="ray"),
        shm_size=_optional_int(item, "shm_size", min_value=1),
        workers=_ray_worker_specs(item, config=config, local_profiles=local_profiles),
    )


def _prepare_serving_item(
    item: dict[str, Any],
    *,
    ctx: Context,
    config: Config,
    session: Any,
) -> dict[str, Any]:
    from inspire.cli.commands.serving.serving_commands import (
        _build_resource_spec_price as _build_serving_resource_spec_price,
        _resolve_image_id as _resolve_serving_image_id,
        _resolve_model_for_create,
        _resolve_project_id as _resolve_serving_project_id,
    )
    from inspire.cli.utils.quota_resolver import (
        SCHEDULE_TYPE_SERVING,
    )

    workspace_id = select_workspace_id(
        config,
        explicit_workspace_name=_require_condition_str(item, "workspace", kind="serving"),
        session=session,
    )
    if not workspace_id:
        raise ConfigError("Batch serving item requires workspace resolution.")
    project_id = _resolve_serving_project_id(
        workspace_id=workspace_id,
        session=session,
        config=config,
        requested=_require_condition_str(item, "project", kind="serving"),
    )
    if not project_id:
        raise ConfigError("Batch serving item requires project resolution.")

    quota_spec = parse_quota(_require_condition_str(item, "quota", kind="serving"))
    resolved = resolve_quota(
        spec=quota_spec,
        workspace_id=workspace_id,
        session=session,
        schedule_config_type=SCHEDULE_TYPE_SERVING,
        group_override=_require_condition_str(item, "group", kind="serving"),
    )
    user = browser_api_module.get_current_user(session=session)
    current_user_id = str(user.get("id") or user.get("user_id") or "").strip()
    if not current_user_id:
        raise ConfigError("Cannot determine the current user from the live web session.")
    model_id, latest_version, _model_label = _resolve_model_for_create(
        name=_require_str(item, "model"),
        workspace_id=workspace_id,
        project_id=project_id,
        user_id=current_user_id,
        session=session,
        ctx=ctx,
    )
    final_model_version = _optional_int(item, "model_version", min_value=1) or latest_version
    if final_model_version is None:
        raise ConfigError("Could not infer model version. Set model_version in the batch item.")

    return {
        "name": _require_str(item, "name"),
        "workspace_id": workspace_id,
        "project_id": project_id,
        "logic_compute_group_id": resolved.logic_compute_group_id,
        "model_id": model_id,
        "model_version": final_model_version,
        "mirror_id": _resolve_serving_image_id(
            _require_condition_str(item, "image", kind="serving"),
            session=session,
            ctx=ctx,
        ),
        "command": _require_str(item, "command"),
        "port": _require_int(item, "port", min_value=1),
        "description": _optional_str(item, "description") or "",
        "replicas": _optional_int(item, "replicas", min_value=1) or 1,
        "node_num_per_replica": _optional_int(item, "nodes_per_replica", min_value=1) or 1,
        "shm_gi": _optional_int(item, "shm_gib", min_value=1),
        "task_priority": _optional_int(item, "priority", min_value=1) or 10,
        "custom_domain": _optional_str(item, "custom_domain"),
        "resource_spec_price": _build_serving_resource_spec_price(resolved),
    }


def _emit_batch_result(
    ctx: Context,
    *,
    dry_run: bool,
    outputs: list[dict[str, Any]],
    command_name: str,
) -> None:
    if ctx.json_output:
        click.echo(json_formatter.format_json({"dry_run": dry_run, "items": outputs}))
        return

    click.echo(
        human_formatter.format_success(
            f"{'Dry run' if dry_run else 'Submitted'} {len(outputs)} "
            f"{command_name} batch item(s)"
        )
    )
    for index, item in enumerate(outputs, start=1):
        name = item.get("name") or item.get("create_kwargs", {}).get("name") or "-"
        kind = item.get("kind") or command_name
        click.echo(f"{index}. {kind}: {scrub_raw_ids(str(name))}")
    if dry_run:
        click.echo("No workloads were submitted.")


def _handle_batch_exception(ctx: Context, error: Exception) -> None:
    if isinstance(error, (ConfigError, KeyError)):
        _handle_error(ctx, "ConfigError", str(error), EXIT_CONFIG_ERROR)
    if isinstance(error, click.UsageError):
        _handle_error(ctx, "ValidationError", str(error), EXIT_CONFIG_ERROR)
    if isinstance(error, (QuotaParseError, QuotaMatchError)):
        _handle_error(ctx, "ValidationError", str(error), EXIT_VALIDATION_ERROR)
    if isinstance(error, AuthenticationError):
        _handle_error(ctx, "AuthenticationError", str(error), EXIT_AUTH_ERROR)
    if isinstance(error, NotebookFailedError):
        _handle_error(ctx, "NotebookFailed", str(error), EXIT_API_ERROR)
    _handle_error(ctx, "APIError", str(error), EXIT_API_ERROR)


@click.command("batch")
@click.argument("config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--dry-run",
    is_flag=True,
    help="Expand the matrix, resolve each job, and print plans without submitting anything.",
)
@pass_context
def job_batch(ctx: Context, config_path: Path, dry_run: bool) -> None:
    """Submit a JSON/TOML matrix through `job create`.

    The config format is command-local: top-level `jobs` is required, while
    optional `defaults`, `profiles`, and `matrix` reduce repetition. Every
    expanded item must include all required `job create` fields; condition
    fields may come from `profile = "<name>"`.

    \b
    Required fields after expansion:
        name, command, quota, workspace, project, group, image
        Optional fields use create-command defaults: priority, framework,
        nodes, max_time, auto_fault_tolerance, fault_tolerance_max_retry,
        enable_notification, exclude_nodes, shm_size

    \b
    Examples:
        inspire job batch experiments.json --dry-run
        inspire job batch experiments.toml
    """
    try:
        data = _load_config(config_path)
        local_profiles = _batch_profiles(data)
        items = _expanded_items(data, item_key="jobs")
        config, _ = Config.from_files_and_env()
        session = get_web_session()

        outputs: list[dict[str, Any]] = []
        for item in items:
            _validate_kind_if_present(
                item,
                allowed={"job", "training"},
                command_name="job",
            )
            item = _apply_item_profile(
                config=config,
                kind="job",
                item=item,
                local_profiles=local_profiles,
            )
            plan = _prepare_training_item(item, config=config, session=session)
            if dry_run:
                payload = job_submit.training_plan_payload(plan)
                payload["matrix"] = item.get("_matrix", {})
                outputs.append(payload)
            else:
                data = browser_api_module.create_training_job(
                    payload=plan.create_kwargs,
                    session=session,
                )
                result = {"code": 0, "data": data}
                outputs.append(
                    {"kind": "training", "name": plan.create_kwargs["name"], "result": result}
                )
        _emit_batch_result(ctx, dry_run=dry_run, outputs=outputs, command_name="job")
    except Exception as e:
        _handle_batch_exception(ctx, e)


@click.command("batch")
@click.argument("config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--dry-run",
    is_flag=True,
    help="Expand the matrix, resolve each HPC job, and print plans without submitting anything.",
)
@pass_context
def hpc_batch(ctx: Context, config_path: Path, dry_run: bool) -> None:
    """Submit a JSON/TOML matrix through `hpc create`.

    Top-level `jobs` is required. Optional `defaults`, `profiles`, and
    `matrix` reduce repetition. Condition fields may come from
    `profile = "<name>"`.

    \b
    Required fields after expansion:
        name, entrypoint, quota, workspace, project, group, image

    \b
    Examples:
        inspire hpc batch jobs.json --dry-run
        inspire hpc batch jobs.toml
    """
    try:
        data = _load_config(config_path)
        local_profiles = _batch_profiles(data)
        items = _expanded_items(data, item_key="jobs")
        config, _ = Config.from_files_and_env()
        session = get_web_session()

        outputs: list[dict[str, Any]] = []
        for item in items:
            _validate_kind_if_present(item, allowed={"hpc"}, command_name="hpc")
            item = _apply_item_profile(
                config=config,
                kind="hpc",
                item=item,
                local_profiles=local_profiles,
            )
            create_kwargs = _prepare_hpc_item(item, config=config, session=session)
            if dry_run:
                outputs.append(
                    {
                        "dry_run": True,
                        "kind": "hpc",
                        "name": create_kwargs["job_name"],
                        "create_kwargs": create_kwargs,
                        "matrix": item.get("_matrix", {}),
                    }
                )
            else:
                data = browser_api_module.create_hpc_job(
                    payload=create_kwargs,
                    session=session,
                )
                result = {"code": 0, "data": data}
                outputs.append({"kind": "hpc", "name": create_kwargs["job_name"], "result": result})
        _emit_batch_result(ctx, dry_run=dry_run, outputs=outputs, command_name="hpc")
    except Exception as e:
        _handle_batch_exception(ctx, e)


@click.command("batch")
@click.argument("config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--dry-run",
    is_flag=True,
    help="Expand the matrix, resolve each notebook, and print plans without creating anything.",
)
@pass_context
def notebook_batch(ctx: Context, config_path: Path, dry_run: bool) -> None:
    """Create notebook instances from a JSON/TOML matrix.

    Top-level `notebooks` is required. Optional `defaults`, `profiles`, and `matrix`
    reduce repetition. Every expanded item must include the notebook create
    fields listed below; condition fields may come from `profile = "<name>"`.
    `wait`, `post_start`, and
    `post_start_script` are optional execution controls.

    \b
    Required fields after expansion:
        name, quota, workspace, project, group, image

    \b
    Examples:
        inspire notebook batch notebooks.json --dry-run
        inspire notebook batch notebooks.toml
    """
    try:
        data = _load_config(config_path)
        local_profiles = _batch_profiles(data)
        items = _expanded_items(data, item_key="notebooks")
        config, _ = Config.from_files_and_env()
        session = get_web_session()

        outputs: list[dict[str, Any]] = []
        for item in items:
            _validate_kind_if_present(
                item,
                allowed={"notebook", "dsw"},
                command_name="notebook",
            )
            item = _apply_item_profile(
                config=config,
                kind="notebook",
                item=item,
                local_profiles=local_profiles,
            )
            plan = _prepare_notebook_item(item, config=config, session=session)
            if dry_run:
                outputs.append(
                    {
                        "dry_run": True,
                        "kind": "notebook",
                        "name": plan["name"],
                        "create_kwargs": plan["create_kwargs"],
                        "workspace_name": plan["workspace_name"],
                        "project_name": plan["project_name"],
                        "image_name": plan["image_name"],
                        "resource": plan["resource"],
                        "compute_group_name": plan["compute_group_name"],
                        "matrix": item.get("_matrix", {}),
                    }
                )
            else:
                outputs.append(_submit_notebook_plan(plan, config=config, session=session))
        _emit_batch_result(ctx, dry_run=dry_run, outputs=outputs, command_name="notebook")
    except Exception as e:
        _handle_batch_exception(ctx, e)


@click.command("batch")
@click.argument("config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--dry-run",
    is_flag=True,
    help="Expand the matrix, resolve each Ray job, and print plans without submitting anything.",
)
@pass_context
def ray_batch(ctx: Context, config_path: Path, dry_run: bool) -> None:
    """Create Ray jobs from a JSON/TOML matrix.

    Top-level `jobs` is required. Each expanded item must describe the Ray
    create request with visible names. `profile = "<name>"` can fill
    workspace/project/image/group/quota. Worker objects may also set
    `profile = "<name>"` to fill image/group/quota.

    \b
    Required fields after expansion:
        name, command, workspace, project, image, group, quota, workers

    \b
    Examples:
        inspire ray batch ray-jobs.json --dry-run
        inspire ray batch ray-jobs.toml
    """
    try:
        data = _load_config(config_path)
        local_profiles = _batch_profiles(data)
        items = _expanded_items(data, item_key="jobs")
        config, _ = Config.from_files_and_env()
        session = get_web_session()

        outputs: list[dict[str, Any]] = []
        for item in items:
            _validate_kind_if_present(item, allowed={"ray"}, command_name="ray")
            item = _apply_item_profile(
                config=config,
                kind="ray",
                item=item,
                local_profiles=local_profiles,
            )
            body = _prepare_ray_item(
                item,
                ctx=ctx,
                config=config,
                session=session,
                local_profiles=local_profiles,
            )
            if dry_run:
                outputs.append(
                    {
                        "dry_run": True,
                        "kind": "ray",
                        "name": body["name"],
                        "create_body": body,
                        "matrix": item.get("_matrix", {}),
                    }
                )
            else:
                result = browser_api_module.create_ray_job(body, session=session)
                outputs.append({"kind": "ray", "name": body["name"], "result": result})
        _emit_batch_result(ctx, dry_run=dry_run, outputs=outputs, command_name="ray")
    except Exception as e:
        _handle_batch_exception(ctx, e)


@click.command("batch")
@click.argument("config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Expand the matrix, resolve each inference serving, and print plans "
        "without creating anything."
    ),
)
@pass_context
def serving_batch(ctx: Context, config_path: Path, dry_run: bool) -> None:
    """Create inference servings from a JSON/TOML matrix.

    Top-level `servings` is required. Each expanded item must include the
    serving create fields as visible names or values. Condition fields may
    come from `profile = "<name>"`.

    \b
    Required fields after expansion:
        name, model, workspace, project, group, quota, image, command, port

    \b
    Examples:
        inspire serving batch servings.json --dry-run
        inspire serving batch servings.toml
    """
    try:
        data = _load_config(config_path)
        local_profiles = _batch_profiles(data)
        items = _expanded_items(data, item_key="servings")
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()

        outputs: list[dict[str, Any]] = []
        for item in items:
            _validate_kind_if_present(
                item,
                allowed={"serving", "inference", "inference-serving"},
                command_name="serving",
            )
            item = _apply_item_profile(
                config=config,
                kind="serving",
                item=item,
                local_profiles=local_profiles,
            )
            payload = _prepare_serving_item(item, ctx=ctx, config=config, session=session)
            if dry_run:
                outputs.append(
                    {
                        "dry_run": True,
                        "kind": "serving",
                        "name": payload["name"],
                        "payload": payload,
                        "matrix": item.get("_matrix", {}),
                    }
                )
            else:
                submit_payload = dict(payload)
                workspace_id = str(submit_payload.pop("workspace_id"))
                project_id = str(submit_payload.pop("project_id"))
                result = browser_api_module.create_serving(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    session=session,
                    **submit_payload,
                )
                outputs.append({"kind": "serving", "name": payload["name"], "result": result})
        _emit_batch_result(ctx, dry_run=dry_run, outputs=outputs, command_name="serving")
    except Exception as e:
        _handle_batch_exception(ctx, e)


__all__ = ["job_batch", "hpc_batch", "notebook_batch", "ray_batch", "serving_batch"]
