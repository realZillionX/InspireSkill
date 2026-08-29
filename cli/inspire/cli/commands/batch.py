"""Batch and matrix submission helpers for workload command groups."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field as dataclass_field
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
from inspire.cli.formatters import json_formatter
from inspire.cli.utils import job_submit
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.dataset_mounts import (
    DatasetMount,
    DatasetSpecError,
    dataset_mount_views,
    parse_dataset_specs,
    resolve_dataset_info,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import reject_id_at_boundary
from inspire.cli.utils.image_resolver import (
    ImageCatalogCache,
    resolve_image_url,
)
from inspire.cli.utils.project_resolver import project_name_candidates, resolve_project
from inspire.cli.utils.quota_resolver import (
    QuotaMatchError,
    QuotaParseError,
    SCHEDULE_TYPE_DSW,
    SCHEDULE_TYPE_HPC,
    SCHEDULE_TYPE_TRAIN,
    build_resource_spec_price,
    load_quota_priority_levels,
    parse_quota,
    resolve_quota,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.task_priority import (
    TaskPriorityError,
    resolve_workspace_task_priority,
)
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id, workspace_label
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import NotebookFailedError
from inspire.platform.web.session import (
    SessionExpiredError,
    TransientAPIError,
    get_web_session,
)

_REFERENCE_FIELDS = {
    "workspace": ("workspace", "inspire account context"),
    "project": ("project", "inspire project list --workspace <workspace-name>"),
    "group": (
        "compute group",
        "inspire resources availability --workspace <workspace-name>",
    ),
    "image": ("image", "inspire image list --workspace <workspace-name>"),
    "model": (
        "model",
        "inspire model list --workspace <workspace-name> --project <project-name>",
    ),
}
_CONDITION_FIELDS = frozenset({"workspace", "project", "group", "quota", "image"})


@dataclass
class _BatchLiveCache:
    """Live catalogues reused only while one Batch command is expanding.

    These snapshots deliberately never leave the process. They collapse
    identical requests across matrix rows without turning mutable scheduling
    facts into persistent configuration or cross-command cache entries.
    """

    job_project_selections: job_submit.ProjectSelectionCache = dataclass_field(
        default_factory=dict
    )
    priority_levels: dict[
        tuple[str, str], dict[str, tuple[str, ...]] | None
    ] = dataclass_field(default_factory=dict)
    image_url_catalogues: ImageCatalogCache = dataclass_field(default_factory=dict)
    projects: dict[str, list[Any]] = dataclass_field(default_factory=dict)
    project_health: dict[str, set[str]] = dataclass_field(default_factory=dict)
    notebook_images: dict[tuple[str, str], list[Any]] = dataclass_field(
        default_factory=dict
    )
    dataset_info: dict[tuple[str, str, str], dict[str, str]] = dataclass_field(
        default_factory=dict
    )

_PUBLIC_FIELDS_BY_KIND = {
    "job": (
        ("framework", "framework"),
        ("nodes", "nodes"),
        ("priority", "priority"),
        ("max_time", "max_time_hours"),
        ("max_time_hours", "max_time_hours"),
        ("enable_notification", "notifications"),
        ("auto_fault_tolerance", "auto_fault_tolerance"),
        ("fault_tolerance_max_retry", "fault_tolerance_max_retry"),
        ("fault_tolerance_retry_interval", "fault_tolerance_retry_interval_seconds"),
        ("exclude_nodes", "exclude_nodes"),
        ("specified_nodes", "specified_nodes"),
        ("shm_size", "shared_memory_gib"),
        # Applied by `_prepare_training_item` and therefore part of the plan:
        # a dry run that hides them cannot be used to check a matrix.
        ("description", "description"),
        ("keep_after_success", "keep_after_success_hours"),
        ("keep_after_failure", "keep_after_failure_hours"),
        ("public_path_readonly", "public_path_readonly"),
        ("command", "command"),
    ),
    "hpc": (
        ("instance_count", "instances"),
        ("number_of_tasks", "tasks"),
        ("cpus_per_task", "cpus_per_task"),
        ("memory_per_cpu", "memory_per_cpu_gib"),
        ("priority", "priority"),
        ("entrypoint", "entrypoint"),
    ),
    "notebook": (
        ("priority", "priority"),
        ("shm_size", "shared_memory_gib"),
        ("auto_stop", "auto_stop"),
        ("auto_stop_after", "auto_stop_after_minutes"),
        ("enable_notification", "notifications"),
        ("public_path_readonly", "public_path_readonly"),
        ("project_path_readonly", "project_path_readonly"),
        ("wait", "wait"),
        ("post_start", "post_start"),
    ),
    "ray": (
        ("priority", "priority"),
        ("shm_size", "shared_memory_gib"),
        ("command", "command"),
        ("workers", "workers"),
    ),
    "serving": (
        ("model_version", "model_version"),
        ("priority", "priority"),
        ("port", "port"),
        ("replicas", "replicas"),
        ("nodes_per_replica", "nodes_per_replica"),
        ("shm_size", "shared_memory_gib"),
        ("custom_domain", "custom_domain"),
        ("command", "command"),
    ),
}


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


def _tristate_bool(item: dict[str, Any], key: str) -> bool | None:
    """Read a switch that must stay absent from the payload unless it is set.

    The create commands express these as `--flag/--no-flag` with no default, so
    an entry that never mentions the key has to produce the same request body it
    produced before the key existed. `_optional_bool` cannot say that: its
    absent case is a real ``False``.
    """
    if key not in item or item[key] is None:
        return None
    return _require_bool(item, key)


def _optional_dataset_mounts(item: dict[str, Any], key: str = "dataset") -> list[DatasetMount]:
    """Parse `dataset` entries, accepting one spec or a list of them."""
    try:
        return parse_dataset_specs(_optional_str_list(item, key))
    except DatasetSpecError as exc:
        raise ConfigError(str(exc)) from exc


def _optional_env_assignments(item: dict[str, Any], key: str = "env") -> list[dict[str, str]]:
    """Parse `env` as either a `KEY=VALUE` list or a mapping.

    TOML and JSON both express a mapping more naturally than a list of joined
    strings, so both are accepted; the command line only has the list form.
    """
    if key not in item or item[key] is None:
        return []
    value = item[key]
    if isinstance(value, dict):
        pairs = []
        for name, raw in value.items():
            if not isinstance(name, str) or not name.strip():
                raise ConfigError(f"Batch item field {key} has an empty variable name.")
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                raise ConfigError(
                    f"Batch item field {key}.{name} must be a string, integer or float."
                )
            pairs.append(f"{name}={raw}")
    else:
        pairs = _optional_str_list(item, key)
    try:
        return job_submit.parse_env_assignments(pairs)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _optional_hours(item: dict[str, Any], key: str) -> float | None:
    if key not in item or item[key] is None:
        return None
    return _require_float(item, key, min_value=0)


def _resolved_dataset_info(
    mounts: list[DatasetMount],
    *,
    workspace_id: str,
    session: Any,
    live_cache: _BatchLiveCache | None = None,
) -> list[dict[str, str]] | None:
    """Resolve batch dataset mounts, reporting a rejection as a config error.

    Resolution happens while the item is being prepared, so a bad spec stops the
    whole batch before anything is submitted rather than after the first few
    items are already running.
    """
    if not mounts:
        return None
    try:
        if live_cache is None:
            return resolve_dataset_info(
                mounts,
                workspace_id=workspace_id,
                session=session,
            )

        missing = [
            mount
            for mount in mounts
            if (workspace_id, mount.dataset, mount.version)
            not in live_cache.dataset_info
        ]
        if missing:
            resolved = resolve_dataset_info(
                missing,
                workspace_id=workspace_id,
                session=session,
            )
            for item in resolved:
                dataset = str(item.get("dataset_id") or "").strip()
                version = str(item.get("version_id") or "").strip()
                if dataset and version:
                    live_cache.dataset_info[(workspace_id, dataset, version)] = dict(
                        item
                    )

        ordered: list[dict[str, str]] = []
        for mount in mounts:
            key = (workspace_id, mount.dataset, mount.version)
            cached_item = live_cache.dataset_info.get(key)
            if cached_item is None:
                raise DatasetSpecError(
                    "The platform omitted a dataset it had just accepted: "
                    f"{mount.dataset}:{mount.version}."
                )
            ordered.append(dict(cached_item))
        return ordered
    except DatasetSpecError as exc:
        raise ConfigError(str(exc)) from exc


def _optional_int(item: dict[str, Any], key: str, *, min_value: int | None = None) -> int | None:
    if key not in item or item[key] is None:
        return None
    return _require_int(item, key, min_value=min_value)


def _ensure_no_condition_defaults(defaults: dict[str, Any], *, item_key: str) -> None:
    disallowed = _CONDITION_FIELDS | {"compute_group"}
    bad = sorted(key for key in defaults if key in disallowed)
    if bad:
        joined = ", ".join(bad)
        raise ConfigError(
            f"Batch defaults cannot set workload condition fields: {joined}. "
            f"Set them explicitly on every {item_key} item."
        )


def _reject_batch_profiles(data: dict[str, Any]) -> None:
    if "profiles" in data:
        raise ConfigError(
            "Batch profiles were removed. Put workspace, project, group, quota and image "
            "directly on every expanded item."
        )


def _validate_name_references(ctx: Context, item: dict[str, Any]) -> None:
    if "profile" in item:
        raise ConfigError(
            "Batch item profiles were removed. Set workspace, project, group, quota and "
            "image explicitly on this item."
        )
    for field, (resource_type, list_command) in _REFERENCE_FIELDS.items():
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            reject_id_at_boundary(
                ctx,
                value,
                resource_type=resource_type,
                list_command=list_command,
            )


def _validate_worker_reference_string(ctx: Context, spec: str) -> None:
    for segment in spec.split(";"):
        field, separator, value = segment.partition("=")
        if not separator:
            continue
        metadata = _REFERENCE_FIELDS.get(field.strip().lower())
        if metadata is None or not value.strip():
            continue
        resource_type, list_command = metadata
        reject_id_at_boundary(
            ctx,
            value,
            resource_type=resource_type,
            list_command=list_command,
        )


def _public_batch_plan(
    item: dict[str, Any],
    *,
    kind: str,
    name: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {"name": name}
    common_fields = (
        ("workspace", "workspace"),
        ("project", "project"),
        ("group", "compute_group"),
        ("quota", "quota"),
        ("image", "image"),
        ("model", "model"),
    )
    for source, target in (*common_fields, *_PUBLIC_FIELDS_BY_KIND[kind]):
        value = item.get(source)
        if value not in (None, "", [], {}):
            output[target] = value
    for key, value in (overrides or {}).items():
        if value in (None, "", [], {}):
            output.pop(key, None)
        else:
            output[key] = value
    return json_formatter.sanitize_json_data(output)


def _plan_mount_and_env_views(item: dict[str, Any]) -> dict[str, Any]:
    """Render the dataset and env parts of a plan the way `create` does.

    Env carries values that can be tokens, so only the names are reported —
    the same rule `inspire job create --dry-run` follows.
    """
    views: dict[str, Any] = {}
    mounts = _optional_dataset_mounts(item)
    if mounts:
        views["datasets"] = dataset_mount_views(mounts)
    envs = _optional_env_assignments(item)
    if envs:
        views["env"] = [entry["name"] for entry in envs]
    return views


def _submitted_batch_item(name: str) -> dict[str, str]:
    return {"name": scrub_raw_ids(name)}


def _require_condition_str(item: dict[str, Any], key: str, *, kind: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"Batch {kind} item is missing required condition field: {key}."
        )
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


def _batch_priority_levels(
    *,
    workspace_id: str,
    workload: str,
    session: Any,
    live_cache: _BatchLiveCache | None,
) -> dict[str, tuple[str, ...]] | None:
    if live_cache is None:
        return load_quota_priority_levels(
            workspace_id=workspace_id,
            session=session,
            workload=workload,
        )
    key = (workspace_id, workload)
    if key not in live_cache.priority_levels:
        live_cache.priority_levels[key] = load_quota_priority_levels(
            workspace_id=workspace_id,
            session=session,
            workload=workload,
        )
    return live_cache.priority_levels[key]


def _batch_projects(
    *,
    workspace_id: str,
    session: Any,
    live_cache: _BatchLiveCache | None,
) -> list[Any]:
    if live_cache is None:
        return browser_api_module.list_projects(
            workspace_id=workspace_id,
            session=session,
        )
    if workspace_id not in live_cache.projects:
        live_cache.projects[workspace_id] = browser_api_module.list_projects(
            workspace_id=workspace_id,
            session=session,
        )
    return live_cache.projects[workspace_id]


def _batch_project_health(
    *,
    workspace_id: str,
    projects: list[Any],
    session: Any,
    live_cache: _BatchLiveCache | None,
) -> set[str]:
    if live_cache is None:
        return browser_api_module.check_scheduling_health(
            workspace_id=workspace_id,
            project_ids={project.project_id for project in projects},
            session=session,
        )
    if workspace_id not in live_cache.project_health:
        live_cache.project_health[workspace_id] = (
            browser_api_module.check_scheduling_health(
                workspace_id=workspace_id,
                project_ids={project.project_id for project in projects},
                session=session,
            )
        )
    return live_cache.project_health[workspace_id]


def _prepare_training_item(
    item: dict[str, Any],
    *,
    config: Config,
    session: Any,
    specified_nodes_capabilities: dict[str, bool] | None = None,
    live_cache: _BatchLiveCache | None = None,
) -> job_submit.JobSubmissionPlan:
    quota_spec = parse_quota(_require_condition_str(item, "quota", kind="job"))
    workspace_id = select_workspace_id(
        explicit_workspace_name=_require_condition_str(item, "workspace", kind="job"),
        session=session,
    )
    if not workspace_id:
        raise ConfigError("Batch training item requires workspace resolution.")
    specified_nodes = _optional_str_list(item, "specified_nodes")
    if specified_nodes:
        capabilities_by_workspace = (
            specified_nodes_capabilities if specified_nodes_capabilities is not None else {}
        )
        if workspace_id not in capabilities_by_workspace:
            capabilities_by_workspace[workspace_id] = (
                browser_api_module.get_train_schedule_capabilities(
                    workspace_id,
                    session=session,
                ).specified_nodes
            )
        if not capabilities_by_workspace[workspace_id]:
            raise ConfigError(
                f"Workspace {item.get('workspace')!r} does not enable specified-node "
                "placement. Remove specified_nodes or choose a workspace that enables it."
            )
    def _priority_levels_loader() -> dict[str, tuple[str, ...]] | None:
        return _batch_priority_levels(
            workspace_id=workspace_id,
            workload="job",
            session=session,
            live_cache=live_cache,
        )

    resolved_quota = resolve_quota(
        spec=quota_spec,
        workspace_id=workspace_id,
        session=session,
        schedule_config_type=SCHEDULE_TYPE_TRAIN,
        group_override=_require_condition_str(item, "group", kind="job"),
        priority_levels_loader=_priority_levels_loader,
    )
    selected, _ = job_submit.select_project_for_workspace(
        config,
        workspace_id=workspace_id,
        requested=_require_condition_str(item, "project", kind="job"),
        session=session,
        selection_cache=(
            live_cache.job_project_selections if live_cache is not None else None
        ),
    )
    fault_retry = _optional_int(item, "fault_tolerance_max_retry", min_value=0)
    task_priority = resolve_workspace_task_priority(
        _optional_int(item, "priority", min_value=1),
        session=session,
        workspace_id=workspace_id,
        project_limit=selected.priority_name,
    )
    return job_submit.build_training_job_plan(
        config=config,
        name=_require_str(item, "name"),
        command=_require_str(item, "command"),
        quota=resolved_quota,
        framework=_optional_str(item, "framework") or "pytorch",
        project_id=selected.project_id,
        workspace_id=workspace_id,
        image=_require_condition_str(item, "image", kind="job"),
        priority=task_priority,
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
        specified_nodes=specified_nodes,
        shm_size=_optional_int(item, "shm_size", min_value=1),
        dataset_info=_resolved_dataset_info(
            _optional_dataset_mounts(item),
            workspace_id=workspace_id,
            session=session,
            live_cache=live_cache,
        ),
        envs=_optional_env_assignments(item) or None,
        description=_optional_str(item, "description"),
        keep_after_success_hours=_optional_hours(item, "keep_after_success"),
        keep_after_failure_hours=_optional_hours(item, "keep_after_failure"),
        public_path_readonly=_tristate_bool(item, "public_path_readonly"),
        fault_tolerance_retry_interval_sec=_optional_int(
            item, "fault_tolerance_retry_interval", min_value=1
        ),
        session=session,
        image_catalog_cache=(
            live_cache.image_url_catalogues if live_cache is not None else None
        ),
    )


def _prepare_hpc_item(
    item: dict[str, Any],
    *,
    config: Config,
    session: Any,
    live_cache: _BatchLiveCache | None = None,
) -> dict[str, Any]:
    from inspire.cli.commands.hpc.hpc_commands import (
        SlurmLayoutError,
        _looks_like_full_slurm_script,
        build_hpc_create_payload,
        resolve_slurm_layout,
    )

    entrypoint = _require_str(item, "entrypoint")
    if _looks_like_full_slurm_script(entrypoint):
        raise ConfigError("HPC entrypoint must be the Slurm body, not a full sbatch script.")

    quota_spec = parse_quota(_require_condition_str(item, "quota", kind="hpc"))
    workspace_id = select_workspace_id(
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
    instance_count = _optional_int(item, "instance_count", min_value=1) or 1
    number_of_tasks = _optional_int(item, "number_of_tasks", min_value=1) or 1
    # Same pre-flight as `hpc create`: the platform accepts a Slurm layout its
    # nodes cannot run, and answers with a job id either way.
    try:
        layout = resolve_slurm_layout(
            node_cpu=resolved_quota.cpu_count,
            node_memory_gib=resolved_quota.memory_gib,
            instance_count=instance_count,
            number_of_tasks=number_of_tasks,
            cpus_per_task=_optional_int(item, "cpus_per_task", min_value=1),
            memory_per_cpu=_optional_int(item, "memory_per_cpu", min_value=1),
        )
    except SlurmLayoutError as e:
        raise ConfigError(str(e)) from e
    selected_project = resolve_project(
        config,
        _require_condition_str(item, "project", kind="hpc"),
        _batch_projects(
            workspace_id=workspace_id,
            session=session,
            live_cache=live_cache,
        ),
    )
    project_id = str(selected_project.project_id or "").strip()
    if not project_id:
        raise ConfigError(f"Project {selected_project.name!r} has no platform record.")
    image = resolve_image_url(
        _require_condition_str(item, "image", kind="hpc"),
        session=session,
        workspace_id=workspace_id,
        catalog_cache=(
            live_cache.image_url_catalogues if live_cache is not None else None
        ),
    )
    return build_hpc_create_payload(
        name=_require_str(item, "name"),
        logic_compute_group_id=resolved_quota.logic_compute_group_id,
        project_id=project_id,
        workspace_id=workspace_id,
        image=image,
        image_type=_optional_str(item, "image_type") or "SOURCE_PRIVATE",
        entrypoint=entrypoint,
        quota_id=resolved_quota.quota_id,
        instance_count=instance_count,
        task_priority=resolve_workspace_task_priority(
            _optional_int(item, "priority", min_value=1),
            session=session,
            workspace_id=workspace_id,
            project_limit=selected_project.priority_name,
        ),
        number_of_tasks=layout.number_of_tasks,
        cpus_per_task=layout.cpus_per_task,
        memory_per_cpu=layout.memory_per_cpu,
        enable_hyper_threading=_optional_bool(item, "enable_hyper_threading", default=False),
        resource_spec_price=build_resource_spec_price(quota=resolved_quota),
        enable_notification=_optional_bool(item, "enable_notification", default=False),
        max_time_hours=_optional_max_time_hours(item),
        dataset_info=_resolved_dataset_info(
            _optional_dataset_mounts(item),
            workspace_id=workspace_id,
            session=session,
            live_cache=live_cache,
        ),
        description=_optional_str(item, "description"),
        keep_after_finish_hours=_optional_hours(item, "keep_after_finish"),
        public_path_readonly=_tristate_bool(item, "public_path_readonly"),
        session=session,
    )

def _project_request_value(config: Config, requested: str) -> str:
    try:
        return project_name_candidates(config, requested)[0]
    except ConfigError as e:
        raise ConfigError("Batch item field project takes a project name.") from e


def _select_notebook_project(
    *,
    config: Config,
    workspace_id: str,
    requested: str,
    session: Any,
    needs_gpu_quota: bool,
    live_cache: _BatchLiveCache | None = None,
):
    projects = _batch_projects(
        workspace_id=workspace_id,
        session=session,
        live_cache=live_cache,
    )
    if not projects:
        raise ConfigError("No projects available in this workspace.")

    congested = None
    if needs_gpu_quota:
        congested = _batch_project_health(
            workspace_id=workspace_id,
            projects=projects,
            session=session,
            live_cache=live_cache,
        )
        congested = congested or None

    try:
        selected, _ = browser_api_module.select_project(
            projects,
            _project_request_value(config, requested),
            needs_gpu_quota=needs_gpu_quota,
            project_order=config.project_order or None,
            congested_projects=congested,
        )
    except ValueError as e:
        raise ConfigError(str(e)) from e
    return selected


def _select_notebook_image(
    *,
    workspace_id: str,
    requested: str,
    session: Any,
    live_cache: _BatchLiveCache | None = None,
):
    from inspire.cli.commands.notebook.notebook_create_flow import _find_image_match

    def _images(source: str | None = None) -> list[Any]:
        key = (workspace_id, str(source or "official"))
        if live_cache is not None and key in live_cache.notebook_images:
            return live_cache.notebook_images[key]
        loaded = (
            browser_api_module.list_images(
                workspace_id=workspace_id,
                session=session,
            )
            if source is None
            else browser_api_module.list_images(
                workspace_id=workspace_id,
                source=source,
                session=session,
            )
        )
        if live_cache is not None:
            live_cache.notebook_images[key] = loaded
        return loaded

    images = _images()
    selected = _find_image_match(images, requested)
    if not selected:
        for source in ("SOURCE_PUBLIC", "SOURCE_PRIVATE"):
            try:
                extra_images = _images(source)
            except TransientAPIError:
                # "not found" below would be a verdict on a catalog that was
                # never listed. Say the platform did not answer instead.
                raise
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
    live_cache: _BatchLiveCache | None = None,
) -> dict[str, Any]:
    from inspire.cli.commands.notebook.notebook_create_flow import (
        _split_auto_stop_after,
        format_quota_display,
    )

    quota_spec = parse_quota(_require_condition_str(item, "quota", kind="notebook"))
    workspace_name = _require_condition_str(item, "workspace", kind="notebook")
    workspace_id = select_workspace_id(
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
        priority_levels_loader=lambda: _batch_priority_levels(
            workspace_id=workspace_id,
            workload="notebook",
            session=session,
            live_cache=live_cache,
        ),
    )
    selected_project = _select_notebook_project(
        config=config,
        workspace_id=workspace_id,
        requested=_require_condition_str(item, "project", kind="notebook"),
        session=session,
        needs_gpu_quota=resolved_quota.gpu_count > 0,
        live_cache=live_cache,
    )
    selected_image = _select_notebook_image(
        workspace_id=workspace_id,
        requested=_require_condition_str(item, "image", kind="notebook"),
        session=session,
        live_cache=live_cache,
    )
    shm_size = _optional_int(item, "shm_size", min_value=1) or 32
    task_priority = resolve_workspace_task_priority(
        _optional_int(item, "priority", min_value=1),
        session=session,
        workspace_id=workspace_id,
        project_limit=selected_project.priority_name,
    )
    resource_spec_price = build_resource_spec_price(quota=resolved_quota)

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

    dataset_info = _resolved_dataset_info(
        _optional_dataset_mounts(item),
        workspace_id=workspace_id,
        session=session,
        live_cache=live_cache,
    )
    if dataset_info is not None:
        create_kwargs["dataset_info"] = dataset_info
    auto_stop_after = _optional_int(item, "auto_stop_after", min_value=2)
    if auto_stop_after is not None:
        stop_hour, stop_minute = _split_auto_stop_after(auto_stop_after)
        create_kwargs["stop_hour"] = stop_hour
        create_kwargs["stop_minute"] = stop_minute
        # The timer only runs when auto-stop is armed, exactly as the create
        # command couples them.
        create_kwargs["auto_stop"] = True
    for key, payload_key in (
        ("enable_notification", "enable_notification"),
        ("public_path_readonly", "is_publicpath_readonly"),
        ("project_path_readonly", "is_projectuserspath_readonly"),
    ):
        value = _tristate_bool(item, key)
        if value is not None:
            create_kwargs[payload_key] = value

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

    return _submitted_batch_item(str(plan["name"]))


def _require_list(item: dict[str, Any], key: str) -> list[Any]:
    value = item.get(key)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"Batch item field {key} must be a non-empty array.")
    return value


def _ray_worker_specs(
    item: dict[str, Any],
    *,
    ctx: Context,
) -> tuple[str, ...]:
    specs: list[str] = []
    for raw in _require_list(item, "workers"):
        if isinstance(raw, str):
            if not raw.strip():
                raise ConfigError("Batch item field workers must not contain empty strings.")
            _validate_worker_reference_string(ctx, raw)
            specs.append(raw)
            continue
        if not isinstance(raw, dict):
            raise ConfigError("Batch item field workers must contain strings or objects.")
        worker = dict(raw)
        if "profile" in worker:
            raise ConfigError(
                "Ray worker profiles were removed; set image, group and quota explicitly."
            )
        _validate_name_references(ctx, worker)
        missing = {"name", "min", "max"} - set(worker.keys())
        if missing:
            raise ConfigError(f"Ray worker spec is missing keys: {sorted(missing)}.")
        for field in ("image", "group", "quota"):
            _require_condition_str(worker, field, kind="ray")
        skip = {"workspace", "project"}
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
        priority=_optional_int(item, "priority", min_value=1),
        image=_require_condition_str(item, "image", kind="ray"),
        image_type=_optional_str(item, "image_type") or "SOURCE_PUBLIC",
        group=_require_condition_str(item, "group", kind="ray"),
        quota=_require_condition_str(item, "quota", kind="ray"),
        shm_size=_optional_int(item, "shm_size", min_value=1),
        public_path_readonly=_tristate_bool(item, "public_path_readonly"),
        workers=_ray_worker_specs(
            item,
            ctx=ctx,
        ),
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
        explicit_workspace_name=_require_condition_str(item, "workspace", kind="serving"),
        session=session,
    )
    if not workspace_id:
        raise ConfigError("Batch serving item requires workspace resolution.")
    project_id = _resolve_serving_project_id(
        ctx=ctx,
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

    payload: dict[str, Any] = {
        "name": _require_str(item, "name"),
        "workspace_id": workspace_id,
        "project_id": project_id,
        "logic_compute_group_id": resolved.logic_compute_group_id,
        "model_id": model_id,
        "model_version": final_model_version,
        "mirror_id": _resolve_serving_image_id(
            _require_condition_str(item, "image", kind="serving"),
            session=session,
            workspace_id=workspace_id,
        ),
        "command": _require_str(item, "command"),
        "port": _require_int(item, "port", min_value=1),
        "description": _optional_str(item, "description") or "",
        "replicas": _optional_int(item, "replicas", min_value=1) or 1,
        "node_num_per_replica": _optional_int(item, "nodes_per_replica", min_value=1) or 1,
        "shm_gi": _optional_int(item, "shm_size", min_value=1),
        "task_priority": resolve_workspace_task_priority(
            _optional_int(item, "priority", min_value=1),
            session=session,
            workspace_id=workspace_id,
            project_id=project_id,
        ),
        "custom_domain": _optional_str(item, "custom_domain"),
        "resource_spec_price": _build_serving_resource_spec_price(resolved),
    }
    for key, kwarg in (
        ("public_path_readonly", "is_publicpath_readonly"),
        ("auto_scaling", "enable_auto_scaling"),
    ):
        value = _tristate_bool(item, key)
        if value is not None:
            payload[kwarg] = value
    return payload


def _plan_value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        return ", ".join(f"{key}={_plan_value_text(sub)}" for key, sub in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(_plan_value_text(entry) for entry in value)
    return str(value)


def _echo_batch_plan(item: dict[str, Any]) -> None:
    """Print one expanded item the way `<workload> create --dry-run` would."""
    click.echo(f"Plan: {scrub_raw_ids(str(item.get('name') or '-'))}")
    for key, value in item.items():
        if key == "name" or value in (None, "", [], {}):
            continue
        label = key.replace("_", " ").capitalize()
        click.echo(f"  {label}: {scrub_raw_ids(_plan_value_text(value))}")


def _emit_batch_result(
    ctx: Context,
    *,
    outputs: list[dict[str, Any]],
    output_limit: int | None,
    dry_run: bool = False,
) -> None:
    public_outputs = [json_formatter.sanitize_json_data(item) for item in outputs]
    page = bound_collection(public_outputs, limit=output_limit)
    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "items": page.items,
                    **page.metadata(),
                }
            )
        )
        return

    # A submit result is a list of what now exists, so names are the answer. A
    # dry run is asked for the plan, and its help says it prints one.
    if dry_run:
        for index, item in enumerate(page.items):
            if index:
                click.echo("")
            _echo_batch_plan(item)
    else:
        for item in page.items:
            name = item.get("name") or "-"
            click.echo(f"- {scrub_raw_ids(str(name))}")
    notice = truncation_notice(page, full_option="--all")
    if notice:
        click.echo(notice)


def _resolve_batch_output_limit(
    ctx: Context,
    *,
    limit: int | None,
    show_all: bool,
) -> int | None:
    """Validate batch result-output controls before any workload is submitted."""
    try:
        return resolve_collection_limit(
            limit=limit,
            show_all=show_all,
        )
    except ValueError:
        _handle_error(
            ctx,
            "ValidationError",
            "Use either --limit or --all, not both.",
            EXIT_VALIDATION_ERROR,
        )
        return None


def _handle_batch_exception(ctx: Context, error: Exception) -> None:
    if isinstance(error, TaskPriorityError):
        _handle_error(ctx, "ValidationError", str(error), EXIT_VALIDATION_ERROR)
    if isinstance(error, (ConfigError, KeyError)):
        _handle_error(ctx, "ConfigError", str(error), EXIT_CONFIG_ERROR)
    if isinstance(error, click.UsageError):
        _handle_error(ctx, "ValidationError", str(error), EXIT_CONFIG_ERROR)
    if isinstance(error, (QuotaParseError, QuotaMatchError)):
        _handle_error(ctx, "ValidationError", str(error), EXIT_VALIDATION_ERROR)
    if isinstance(error, SessionExpiredError):
        _handle_error(ctx, "AuthenticationError", str(error), EXIT_AUTH_ERROR)
    if isinstance(error, NotebookFailedError):
        _handle_error(ctx, "NotebookFailed", str(error), EXIT_API_ERROR)
    _handle_error(ctx, "APIError", str(error), EXIT_API_ERROR)


@click.command("batch")
@click.argument(
    "config_path",
    metavar="PATH",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Expand the matrix, resolve each job, and print plans without submitting anything.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum result rows to print (default: 20).",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Print every result row after processing the full batch.",
)
@pass_context
def job_batch(
    ctx: Context,
    config_path: Path,
    dry_run: bool,
    limit: int | None,
    show_all: bool,
) -> None:
    """Submit a JSON/TOML matrix through `job create`.

    The config format is command-local: top-level `jobs` is required, while
    optional `defaults` and `matrix` reduce repetition. Every expanded item
    must include its scheduling conditions explicitly.

    \b
    Required fields after expansion:
        name, command, quota, workspace, project, group, image
        Optional fields use create-command defaults: priority, framework,
        nodes, max_time, auto_fault_tolerance, fault_tolerance_max_retry,
        fault_tolerance_retry_interval, enable_notification, exclude_nodes,
        specified_nodes, shm_size, dataset, env, description, keep_after_success,
        keep_after_failure, public_path_readonly
        `dataset` takes one "<name>:<version>" or a list of them; `env` takes
        either a "KEY=VALUE" list or a table.

    \b
    Examples:
        inspire job batch experiments.json --dry-run
        inspire job batch experiments.toml
    """
    output_limit = _resolve_batch_output_limit(
        ctx,
        limit=limit,
        show_all=show_all,
    )
    try:
        data = _load_config(config_path)
        _reject_batch_profiles(data)
        items = _expanded_items(data, item_key="jobs")
        config, _ = Config.from_files_and_env()
        session = get_web_session()
        live_cache = _BatchLiveCache()
        specified_nodes_capabilities: dict[str, bool] = {}

        outputs: list[dict[str, Any]] = []
        for item in items:
            _validate_kind_if_present(
                item,
                allowed={"job", "training"},
                command_name="job",
            )
            _validate_name_references(ctx, item)
            plan = _prepare_training_item(
                item,
                config=config,
                session=session,
                specified_nodes_capabilities=specified_nodes_capabilities,
                live_cache=live_cache,
            )
            if dry_run:
                outputs.append(
                    _public_batch_plan(
                        item,
                        kind="job",
                        name=str(plan.create_kwargs["name"]),
                        overrides={
                            "workspace": workspace_label(
                                session,
                                plan.workspace_id,
                                _require_condition_str(item, "workspace", kind="job"),
                            ),
                            "project": plan.project_name,
                            "compute_group": plan.quota.compute_group_name,
                            "quota": {
                                "gpu_count": plan.quota.gpu_count,
                                "gpu_type": plan.quota.gpu_type,
                                "cpu_count": plan.quota.cpu_count,
                                "memory_gib": plan.quota.memory_gib,
                            },
                            "priority": plan.create_kwargs.get("task_priority"),
                            "nodes": plan.create_kwargs.get("instance_count"),
                            "notifications": plan.create_kwargs.get("enable_notification"),
                            "exclude_nodes": job_submit.training_plan_exclude_nodes(plan),
                            "specified_nodes": job_submit.training_plan_specified_nodes(plan),
                            "shared_memory_gib": plan.shm_size_gib,
                            **_plan_mount_and_env_views(item),
                        },
                    )
                )
            else:
                browser_api_module.create_training_job(
                    payload=plan.create_kwargs,
                    session=session,
                )
                outputs.append(
                    _submitted_batch_item(str(plan.create_kwargs["name"]))
                )
        _emit_batch_result(
            ctx,
            outputs=outputs,
            output_limit=output_limit,
            dry_run=dry_run,
        )
    except Exception as e:
        _handle_batch_exception(ctx, e)


@click.command("batch")
@click.argument(
    "config_path",
    metavar="PATH",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Expand the matrix, resolve each HPC job, and print plans without submitting anything.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum result rows to print (default: 20).",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Print every result row after processing the full batch.",
)
@pass_context
def hpc_batch(
    ctx: Context,
    config_path: Path,
    dry_run: bool,
    limit: int | None,
    show_all: bool,
) -> None:
    """Submit a JSON/TOML matrix through `hpc create`.

    Top-level `jobs` is required. Optional `defaults` and `matrix` reduce
    repetition; scheduling conditions remain explicit per item.

    \b
    Required fields after expansion:
        name, entrypoint, quota, workspace, project, group, image
        Optional fields use create-command defaults: priority, image_type,
        instance_count, number_of_tasks, cpus_per_task, memory_per_cpu,
        enable_hyper_threading, max_time, keep_after_finish, dataset,
        description, enable_notification, public_path_readonly
        `dataset` takes one "<name>:<version>" or a list of them.

    \b
    Examples:
        inspire hpc batch jobs.json --dry-run
        inspire hpc batch jobs.toml
    """
    output_limit = _resolve_batch_output_limit(
        ctx,
        limit=limit,
        show_all=show_all,
    )
    try:
        data = _load_config(config_path)
        _reject_batch_profiles(data)
        items = _expanded_items(data, item_key="jobs")
        config, _ = Config.from_files_and_env()
        session = get_web_session()
        live_cache = _BatchLiveCache()

        outputs: list[dict[str, Any]] = []
        for item in items:
            _validate_kind_if_present(item, allowed={"hpc"}, command_name="hpc")
            _validate_name_references(ctx, item)
            create_kwargs = _prepare_hpc_item(
                item,
                config=config,
                session=session,
                live_cache=live_cache,
            )
            if dry_run:
                outputs.append(
                    _public_batch_plan(
                        item,
                        kind="hpc",
                        name=str(create_kwargs["job_name"]),
                        overrides={
                            "priority": create_kwargs.get("task_priority"),
                            "instances": create_kwargs.get("instance_count"),
                        },
                    )
                )
            else:
                browser_api_module.create_hpc_job(
                    payload=create_kwargs,
                    session=session,
                )
                outputs.append(
                    _submitted_batch_item(str(create_kwargs["job_name"]))
                )
        _emit_batch_result(
            ctx,
            outputs=outputs,
            output_limit=output_limit,
            dry_run=dry_run,
        )
    except Exception as e:
        _handle_batch_exception(ctx, e)


@click.command("batch")
@click.argument(
    "config_path",
    metavar="PATH",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Expand the matrix, resolve each notebook, and print plans without creating anything.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum result rows to print (default: 20).",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Print every result row after processing the full batch.",
)
@pass_context
def notebook_batch(
    ctx: Context,
    config_path: Path,
    dry_run: bool,
    limit: int | None,
    show_all: bool,
) -> None:
    """Create notebook instances from a JSON/TOML matrix.

    Top-level `notebooks` is required. Optional `defaults` and `matrix` reduce
    repetition. Every expanded item must include the notebook create fields
    and scheduling conditions listed below.
    `wait`, `post_start`, and
    `post_start_script` are optional execution controls.

    \b
    Required fields after expansion:
        name, quota, workspace, project, group, image
        Optional fields use create-command defaults: priority, shm_size,
        auto_stop, auto_stop_after, wait, post_start, post_start_script,
        dataset, enable_notification, public_path_readonly,
        project_path_readonly
        `dataset` takes one "<name>:<version>" or a list of them;
        `auto_stop_after` is in minutes and arms auto_stop.

    \b
    Examples:
        inspire notebook batch notebooks.json --dry-run
        inspire notebook batch notebooks.toml
    """
    output_limit = _resolve_batch_output_limit(
        ctx,
        limit=limit,
        show_all=show_all,
    )
    try:
        data = _load_config(config_path)
        _reject_batch_profiles(data)
        items = _expanded_items(data, item_key="notebooks")
        config, _ = Config.from_files_and_env()
        session = get_web_session()
        live_cache = _BatchLiveCache()

        outputs: list[dict[str, Any]] = []
        for item in items:
            _validate_kind_if_present(
                item,
                allowed={"notebook", "dsw"},
                command_name="notebook",
            )
            _validate_name_references(ctx, item)
            plan = _prepare_notebook_item(
                item,
                config=config,
                session=session,
                live_cache=live_cache,
            )
            if dry_run:
                outputs.append(
                    _public_batch_plan(
                        item,
                        kind="notebook",
                        name=str(plan["name"]),
                        overrides={
                            "workspace": plan["workspace_name"],
                            "project": plan["project_name"],
                            "image": plan["image_name"],
                            "compute_group": plan["compute_group_name"],
                            "quota": plan["resource"],
                            "priority": plan["create_kwargs"].get("task_priority"),
                            "shared_memory_gib": plan["create_kwargs"].get(
                                "shared_memory_size"
                            ),
                        },
                    )
                )
            else:
                outputs.append(_submit_notebook_plan(plan, config=config, session=session))
        _emit_batch_result(
            ctx,
            outputs=outputs,
            output_limit=output_limit,
            dry_run=dry_run,
        )
    except Exception as e:
        _handle_batch_exception(ctx, e)


@click.command("batch")
@click.argument(
    "config_path",
    metavar="PATH",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Expand the matrix, resolve each Ray job, and print plans without submitting anything.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum result rows to print (default: 20).",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Print every result row after processing the full batch.",
)
@pass_context
def ray_batch(
    ctx: Context,
    config_path: Path,
    dry_run: bool,
    limit: int | None,
    show_all: bool,
) -> None:
    """Create Ray jobs from a JSON/TOML matrix.

    Top-level `jobs` is required. Each expanded item must describe the Ray
    create request with visible names. Head and worker scheduling conditions
    must be explicit on every item.

    \b
    Required fields after expansion:
        name, command, workspace, project, image, group, quota, workers
        Optional fields use create-command defaults: priority, description,
        image_type, shm_size, public_path_readonly
        Ray takes no dataset mounts: the platform rejects them, and the
        console form has no 官方数据集 section either.

    \b
    Examples:
        inspire ray batch ray-jobs.json --dry-run
        inspire ray batch ray-jobs.toml
    """
    output_limit = _resolve_batch_output_limit(
        ctx,
        limit=limit,
        show_all=show_all,
    )
    try:
        data = _load_config(config_path)
        _reject_batch_profiles(data)
        items = _expanded_items(data, item_key="jobs")
        config, _ = Config.from_files_and_env()
        session = get_web_session()

        outputs: list[dict[str, Any]] = []
        for item in items:
            _validate_kind_if_present(item, allowed={"ray"}, command_name="ray")
            _validate_name_references(ctx, item)
            body = _prepare_ray_item(
                item,
                ctx=ctx,
                config=config,
                session=session,
            )
            if dry_run:
                outputs.append(
                    _public_batch_plan(
                        item,
                        kind="ray",
                        name=str(body["name"]),
                    )
                )
            else:
                browser_api_module.create_ray_job(body, session=session)
                outputs.append(_submitted_batch_item(str(body["name"])))
        _emit_batch_result(
            ctx,
            outputs=outputs,
            output_limit=output_limit,
            dry_run=dry_run,
        )
    except Exception as e:
        _handle_batch_exception(ctx, e)


@click.command("batch")
@click.argument(
    "config_path",
    metavar="PATH",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Expand the matrix, resolve each inference serving, and print plans "
        "without creating anything."
    ),
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum result rows to print (default: 20).",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Print every result row after processing the full batch.",
)
@pass_context
def serving_batch(
    ctx: Context,
    config_path: Path,
    dry_run: bool,
    limit: int | None,
    show_all: bool,
) -> None:
    """Create inference servings from a JSON/TOML matrix.

    Top-level `servings` is required. Each expanded item must include the
    serving create fields as visible names or values, including every
    scheduling condition.

    \b
    Required fields after expansion:
        name, model, workspace, project, group, quota, image, command, port
        Optional fields use create-command defaults: priority, description,
        replicas, nodes_per_replica, shm_size, custom_domain, auto_scaling,
        public_path_readonly
        Serving takes no dataset mounts: the platform rejects them.

    \b
    Examples:
        inspire serving batch servings.json --dry-run
        inspire serving batch servings.toml
    """
    output_limit = _resolve_batch_output_limit(
        ctx,
        limit=limit,
        show_all=show_all,
    )
    try:
        data = _load_config(config_path)
        _reject_batch_profiles(data)
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
            _validate_name_references(ctx, item)
            payload = _prepare_serving_item(item, ctx=ctx, config=config, session=session)
            if dry_run:
                outputs.append(
                    _public_batch_plan(
                        item,
                        kind="serving",
                        name=str(payload["name"]),
                    )
                )
            else:
                submit_payload = dict(payload)
                workspace_id = str(submit_payload.pop("workspace_id"))
                project_id = str(submit_payload.pop("project_id"))
                browser_api_module.create_serving(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    session=session,
                    **submit_payload,
                )
                outputs.append(
                    _submitted_batch_item(str(payload["name"]))
                )
        _emit_batch_result(
            ctx,
            outputs=outputs,
            output_limit=output_limit,
            dry_run=dry_run,
        )
    except Exception as e:
        _handle_batch_exception(ctx, e)


__all__ = ["job_batch", "hpc_batch", "notebook_batch", "ray_batch", "serving_batch"]
