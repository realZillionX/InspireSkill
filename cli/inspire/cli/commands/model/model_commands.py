"""`inspire model` subcommands — model repository workflows."""

from __future__ import annotations

from typing import Any, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.human_formatter import (
    format_epoch,
    format_mutation_success,
)
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.collection_output import (
    DEFAULT_COLLECTION_LIMIT,
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import (
    exit_with_error as _handle_error,
    require_confirmation,
)
from inspire.cli.utils.id_resolver import (
    NAME_PICK_HELP,
    forget_resource_identity,
    reject_id_at_boundary,
    remember_resource_identity,
    resolve_by_name,
    run_with_stale_handle_retry,
)
from inspire.cli.utils.project_resolver import resolve_project_id as resolve_project_id_by_name
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workspaces import (
    resolve_workspace_query_scope,
    select_workspace_id,
    workspace_name_map,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session


def _resolve_workspace_id(workspace: Optional[str], *, session=None) -> Optional[str]:
    if workspace is None:
        return None
    return select_workspace_id(explicit_workspace_name=workspace, session=session)


def _resolve_project_id(
    config: Config,
    requested: Optional[str],
    *,
    workspace_id: Optional[str],
    session,
) -> Optional[str]:
    if not requested:
        return None
    projects = browser_api_module.list_projects(
        workspace_id=workspace_id, session=session
    )
    return resolve_project_id_by_name(
        config,
        requested,
        projects,
    )


def _current_user_id(session) -> str:  # noqa: ANN001
    user = browser_api_module.get_current_user(session=session)
    user_id = str(user.get("id") or user.get("user_id") or "").strip()
    if not user_id:
        raise ConfigError("Cannot determine the current user from the live web session.")
    return user_id


def _status_label(value: Any) -> str:
    mapping = {
        "0": "PENDING",
        "1": "CREATING",
        "2": "SUCCESS",
        "3": "FAILED",
    }
    if value is None or value == "":
        raw = ""
    else:
        raw = str(value).strip()
    return mapping.get(raw, raw or "-")


# `model-hub` reports a serving's state as an int indexing the serving status
# enum, while the `inference_serving` domain reports the same states as the
# strings below. Index 4 is pinned by measurement, not by reading the enum:
# across every model version that has servings, the count of status-4 entries
# equals the version record's own `running_infrence_serving` (11/11).
_SERVING_STATUS_LABELS = (
    "PENDING",
    "PRE_DEPLOYING",
    "DEPLOYING",
    "FAILED",
    "RUNNING",
    "SLEEPING",
    "STOPPING",
    "STOPPED",
    "QUOTA_PENDING",
)
# A failed serving no longer holds the model version: it is not running, and
# starting it is not an option. Everything else is a live consumer -- `STOPPED`
# and `SLEEPING` servings can be started again, so they still break if the
# model goes away.
_RELEASED_SERVING_STATUSES = frozenset({"FAILED"})
# One page covers every model version observed on the platform, and the Action
# rejects `page_size: -1`, so "everything" has to be a real number.
_SERVING_PAGE_SIZE = 100


def _serving_status_label(value: Any) -> str:
    if isinstance(value, bool) or value is None or value == "":
        return ""
    try:
        index = int(str(value).strip())
    except (TypeError, ValueError):
        return scrub_raw_ids(value).strip()
    if 0 <= index < len(_SERVING_STATUS_LABELS):
        return _SERVING_STATUS_LABELS[index]
    return str(index)


def _serving_views(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Project related servings down to what identifies and qualifies them.

    The platform hands back `serving_id` and `user_avatar` alongside the name;
    neither reaches public output. The item's own `version` is dropped too --
    it is the serving's revision, not the model version that was asked about,
    and printing it beside a model version would read as the same number.
    """
    views: list[dict[str, str]] = []
    for item in items:
        name = scrub_raw_ids(item.get("name") or "").strip()
        if not name:
            continue
        status = _serving_status_label(item.get("status"))
        if status in _RELEASED_SERVING_STATUSES:
            continue
        view = {"name": name}
        if status:
            view["status"] = status
        views.append(view)
    return views


def _format_size_gi(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number <= 0:
        return "-"
    if number >= 1024:
        return f"{number / 1024:.2f} TiB"
    return f"{number:.2f} GiB"


def _version_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return f"V{text[1:]}" if text[:1].casefold() == "v" else f"V{text}"


def _created_model_id(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("model_id", "id"):
        candidate = str(value.get(key) or "").strip()
        if candidate:
            return candidate
    for key in ("model", "data", "result"):
        candidate = _created_model_id(value.get(key))
        if candidate:
            return candidate
    return ""


def _string_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [
            scrub_raw_ids(item)
            for item in value
            if str(item or "").strip()
        ]
    text = scrub_raw_ids(value).strip()
    return [text] if text else []


_IDENTITY_NAME_KEYS = (
    "created_by_name",
    "creator_name",
    "owner_name",
)
_IDENTITY_OBJECT_KEYS = (
    "created_by",
    "creator",
    "owner",
    "user",
)


def _explicit_identity_name(*payloads: Any) -> str:
    """Return only an explicitly projected display name from API payloads.

    The model API also exposes login-oriented scalar fields such as
    ``user_name``/``username``/``login_name``.  Those are identifiers, not
    display-name projections, so they must never be used as CLI owner text.
    Likewise, scalar ``owner``/``creator``/``created_by`` values are ignored.
    """
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in _IDENTITY_NAME_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return scrub_raw_ids(value).strip()
        for key in _IDENTITY_OBJECT_KEYS:
            identity = payload.get(key)
            if not isinstance(identity, dict):
                continue
            for name_key in ("name", "display_name"):
                value = identity.get(name_key)
                if isinstance(value, str) and value.strip():
                    return scrub_raw_ids(value).strip()
    return ""


def _format_model_rows(rows: list[dict[str, str]]) -> str:
    """Render a compact model-registry list."""
    if not rows:
        return "No models found."
    include_workspace = any(row.get("workspace") for row in rows)
    fields = ["name", "version", "status", "project"]
    headers = ["Name", "Version", "Status", "Project"]
    max_widths = [48, 12, 16, 36]
    if include_workspace:
        fields.append("workspace")
        headers.append("Workspace")
        max_widths.append(32)
    fields.append("updated_at")
    headers.append("Updated")
    max_widths.append(20)

    values = [tuple(row.get(field, "-") for field in fields) for row in rows]
    widths = [
        column_width(header, [row[index] for row in values], max_width=max_width)
        for index, (header, max_width) in enumerate(zip(headers, max_widths))
    ]
    return "\n".join(
        render_table(
            tuple(headers),
            values,
            widths,
            line_char="─",
        )
    )


def _model_list_view(
    model: browser_api_module.ModelInfo,
    *,
    workspace: str,
) -> dict[str, str]:
    raw = model.raw if isinstance(model.raw, dict) else {}
    model_payload = raw.get("model")
    inner = model_payload if isinstance(model_payload, dict) else {}
    created_by = _explicit_identity_name(raw, inner)
    view = {
        "name": scrub_raw_ids(model.name),
        "status": scrub_raw_ids(_status_label(model.status)),
        "project": scrub_raw_ids(model.project_name),
        "workspace": scrub_raw_ids(workspace),
        "version": scrub_raw_ids(_version_label(model.latest_version)),
        "updated_at": scrub_raw_ids(
            format_epoch(model.updated_at) if model.updated_at else ""
        ),
    }
    if created_by:
        view["created_by"] = created_by
    return {key: value for key, value in view.items() if value and value != "-"}


def _version_inner(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    model_payload = item.get("model")
    return model_payload if isinstance(model_payload, dict) else item


def _version_items(data: Any) -> list[dict[str, Any]]:
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _latest_version(data: Any) -> dict[str, Any]:
    def _key(item: dict[str, Any]) -> int:
        inner = _version_inner(item)
        try:
            return int(inner.get("version") or inner.get("model_version") or 0)
        except (TypeError, ValueError):
            return 0

    latest = max(_version_items(data), key=_key, default={})
    return _version_inner(latest)


def _version_number(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if text[:1].casefold() == "v":
        text = text[1:]
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _reported_version(data: dict[str, Any], version_data: dict[str, Any]) -> Optional[int]:
    """The version number `model status` reports on, as an int."""
    model_payload = data.get("model")
    inner: dict[str, Any] = model_payload if isinstance(model_payload, dict) else data
    latest = _latest_version(version_data)
    return _version_number(latest.get("version") or inner.get("version"))


def _running_serving_count(item: dict[str, Any]) -> int:
    try:
        return int(str(item.get("running_infrence_serving") or 0))
    except (TypeError, ValueError):
        return 0


def _other_versions_in_use(
    version_data: dict[str, Any], *, reported: Optional[int]
) -> list[str]:
    """Versions other than the reported one that still carry running servings.

    Free -- the count is already on each version record `model status` fetched.
    It matters because the serving list below only covers one version: deleting
    the model takes every version's deployments with it.
    """
    labels: list[str] = []
    for item in _version_items(version_data):
        inner = _version_inner(item)
        version = _version_number(inner.get("version") or inner.get("model_version"))
        if version is None or version == reported:
            continue
        if _running_serving_count(item) <= 0:
            continue
        labels.append(_version_label(version))
    return labels


def _model_references(
    model_id: str,
    version_data: dict[str, Any],
    *,
    session,  # noqa: ANN001
    workspace_id: Optional[str],
) -> list[str]:
    """Name every deployment that would break if this model went away.

    Deletion is not version-scoped, so this asks per version instead of only
    about the one `model status` reports on. The `running_infrence_serving`
    count already on each version record is not enough on its own either: it
    counts running deployments, while a stopped or sleeping serving can be
    started again and therefore still holds the version. Failed servings are
    dropped by `_serving_views` -- they hold nothing.
    """
    references: list[str] = []
    for item in _version_items(version_data):
        inner = _version_inner(item)
        version = _version_number(inner.get("version") or inner.get("model_version"))
        if version is None:
            continue
        servings, _total = browser_api_module.list_model_inference_servings(
            model_id=model_id,
            version=version,
            page=1,
            page_size=_SERVING_PAGE_SIZE,
            session=session,
            workspace_id=workspace_id,
        )
        label = _version_label(version)
        for serving in _serving_views(servings):
            status = serving.get("status")
            suffix = f" ({status})" if status else ""
            references.append(f"{label} {serving['name']}{suffix}")
    return references


def _in_use_message(name: str, references: list[str], *, pending: bool) -> str:
    """One line naming what still holds the model, within the output budget."""
    page = bound_collection(references, limit=DEFAULT_COLLECTION_LIMIT)
    parts = list(page.items)
    if page.truncated:
        parts.append(f"and {page.total - page.shown} more")
    if pending:
        parts.append("a deployment is queued on this model")
    return f"Model {scrub_raw_ids(name)} is still in use: {'; '.join(parts)}."


def _model_detail_view(
    name: str,
    data: dict[str, Any],
    version_data: dict[str, Any],
    *,
    vllm_compatibility: Optional[dict[int, bool]] = None,
) -> dict[str, Any]:
    model_payload = data.get("model")
    inner: dict[str, Any] = model_payload if isinstance(model_payload, dict) else data
    latest = _latest_version(version_data)
    version = latest.get("version") or inner.get("version")
    view: dict[str, Any] = {
        "name": scrub_raw_ids(inner.get("name") or name),
        "status": scrub_raw_ids(
            _status_label(latest.get("status", inner.get("status")))
        ),
        "version": _version_label(version),
        "description": scrub_raw_ids(inner.get("description") or ""),
        "type": _string_values(inner.get("model_type")),
        "tags": _string_values(inner.get("tags")),
        "published": bool(inner.get("has_published")),
        "project": scrub_raw_ids(data.get("project_name") or ""),
        "owner": _explicit_identity_name(data, inner),
        "created_at": (
            format_epoch(inner.get("created_at")) if inner.get("created_at") else ""
        ),
        "updated_at": (
            format_epoch(inner.get("updated_at")) if inner.get("updated_at") else ""
        ),
    }
    compatibility = vllm_compatibility or {}
    version_number = _version_number(version)
    if version_number is not None and version_number in compatibility:
        view["vllm_ready"] = compatibility[version_number]
    return {
        key: value
        for key, value in view.items()
        if value not in ("", None, []) or key in {"vllm_ready", "published"}
    }


def _model_version_views(
    data: dict[str, Any],
    *,
    vllm_compatibility: Optional[dict[int, bool]] = None,
) -> list[dict[str, Any]]:
    compatibility = vllm_compatibility or {}
    views: list[dict[str, Any]] = []
    for item in _version_items(data):
        inner = _version_inner(item)
        version = inner.get("version") or inner.get("model_version")
        view: dict[str, Any] = {
            "version": _version_label(version),
            "status": scrub_raw_ids(_status_label(inner.get("status") or item.get("status"))),
            "size": _format_size_gi(
                inner.get("model_size_gi")
                or inner.get("model_size_gb")
                or inner.get("size")
            ),
        }
        version_number = _version_number(version)
        if version_number is not None and version_number in compatibility:
            view["vllm_ready"] = compatibility[version_number]
        running = item.get("running_infrence_serving")
        if running not in (None, ""):
            view["running_servings"] = running
        views.append(
            {
                key: value
                for key, value in view.items()
                if value not in ("", None, "-") or key == "vllm_ready"
            }
        )
    return views


def _format_model_detail(view: dict[str, Any]) -> str:
    labels = (
        ("Name", "name"),
        ("Status", "status"),
        ("Version", "version"),
        ("Description", "description"),
        ("Type", "type"),
        ("Tags", "tags"),
        ("vLLM-ready", "vllm_ready"),
        ("Published", "published"),
        ("Project", "project"),
        ("Owner", "owner"),
        ("Created", "created_at"),
        ("Updated", "updated_at"),
        ("Pending deployment", "pending_serving"),
        ("Other versions in use", "other_versions_in_use"),
    )
    lines: list[str] = []
    for label, key in labels:
        if key not in view:
            continue
        value = view[key]
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        elif isinstance(value, bool):
            value = "yes" if value else "no"
        lines.append(f"{label}: {value}")
    if "servings" in view:
        servings = view["servings"]
        version = view.get("version") or "this version"
        if not servings:
            lines.append(f"Servings on {version}: none")
        for serving in servings:
            status = serving.get("status")
            suffix = f" ({status})" if status else ""
            lines.append(f"Serving on {version}: {serving['name']}{suffix}")
    return "\n".join(lines)


def _format_model_versions(versions: list[dict[str, Any]]) -> str:
    if not versions:
        return ""
    rows = [
        (
            str(version.get("version") or ""),
            str(version.get("status") or ""),
            str(version.get("size") or ""),
            ("yes" if version["vllm_ready"] else "no")
            if "vllm_ready" in version
            else "-",
            str(version.get("running_servings") or ""),
        )
        for version in versions
    ]
    widths = [
        column_width("Version", [row[0] for row in rows], max_width=12),
        column_width("Status", [row[1] for row in rows], max_width=16),
        column_width("Size", [row[2] for row in rows], max_width=14),
        column_width("vLLM", [row[3] for row in rows], max_width=6),
        column_width("Servings", [row[4] for row in rows], max_width=10),
    ]
    return "\n".join(
        render_table(
            ("Version", "Status", "Size", "vLLM", "Servings"),
            rows,
            widths,
            line_char="─",
        )
    )


def _resolve_model_name(
    ctx: Context,
    name: str,
    *,
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    pick: Optional[int] = None,
    session=None,  # noqa: ANN001
    require_live: bool = False,
) -> str:
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="model",
        list_command="inspire model list --workspace <workspace>",
    )
    live_session = session or get_web_session()

    def _lister():
        items, _ = browser_api_module.list_models(
            workspace_id=workspace_id,
            page=1,
            page_size=100,
            keyword=name,
            project_ids=[project_id] if project_id else None,
            user_id=user_id,
            session=live_session,
        )
        return [
            {
                "name": m.name,
                "id": m.model_id,
                "status": _status_label(m.status),
                "project": m.project_name,
                "created_at": format_epoch(m.created_at) if m.created_at else "",
            }
            for m in items
        ]

    return resolve_by_name(
        ctx,
        name=name,
        resource_type="model",
        list_candidates=_lister,
        pick_index=pick,
        session=live_session,
        workspace_id=str(workspace_id or ""),
        owner_scope="self",
        require_live=require_live,
        list_command="inspire model list --workspace <workspace>",
    )


@click.command("list")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME|all",
    help="Workspace name or 'all'.",
)
@click.option("--project", default=None, metavar="NAME", help="Project name filter")
@click.option(
    "--keyword",
    default=None,
    metavar="KEYWORD",
    help="Server-side model name/description search",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum models to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every model.")
@pass_context
def list_model(
    ctx: Context,
    workspace: Optional[str],
    project: Optional[str],
    keyword: Optional[str],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List registered models owned by the current user.

    Use filters to narrow by workspace, project, or keyword. After finding a
    candidate model, use `model status` for metadata and `model versions` to
    choose the version for serving or reproducibility.
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
        workspace_names = workspace_name_map(session)
        user_id = _current_user_id(session)
        items: list[tuple[browser_api_module.ModelInfo, str, str]] = []
        total = 0
        matched_project_scope = project is None
        for workspace_id in workspace_ids:
            try:
                project_id = _resolve_project_id(
                    config,
                    project,
                    workspace_id=workspace_id,
                    session=session,
                )
            except ConfigError as e:
                if all_workspaces and str(e).startswith("Unknown project name "):
                    continue
                raise
            matched_project_scope = True
            workspace_items, workspace_total = browser_api_module.list_models(
                workspace_id=workspace_id,
                page=1,
                page_size=request_limit,
                keyword=keyword,
                project_ids=[project_id] if project_id else None,
                user_id=user_id,
                session=session,
            )
            if show_all and workspace_total > len(workspace_items):
                workspace_items, expanded_total = browser_api_module.list_models(
                    workspace_id=workspace_id,
                    page=1,
                    page_size=max(workspace_total, len(workspace_items), 1),
                    keyword=keyword,
                    project_ids=[project_id] if project_id else None,
                    user_id=user_id,
                    session=session,
                )
                workspace_total = max(
                    workspace_total,
                    expanded_total,
                    len(workspace_items),
                )
            workspace_name = workspace_names.get(workspace_id) or ("(workspace name unavailable)")
            items.extend((model, workspace_name, workspace_id) for model in workspace_items)
            total += max(workspace_total, len(workspace_items))
        if not matched_project_scope:
            raise ConfigError(f"Unknown project name {project!r} in the requested workspaces.")
        if all_workspaces:
            items.sort(
                key=lambda item: str(item[0].updated_at or item[0].created_at or ""),
                reverse=True,
            )

        views: list[dict[str, str]] = []
        for model, workspace_name, _workspace_id in items:
            view = _model_list_view(model, workspace=workspace_name)
            views.append(view)
        page = bound_collection(views, limit=effective_limit, total=total)
        for model, _workspace_name, workspace_id in items:
            remember_resource_identity(
                session=session,
                resource_type="model",
                resource_id=model.model_id,
                name=model.name,
                workspace_id=workspace_id,
                owner_scope="self",
                status=model.status,
                created_at=model.created_at,
            )
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

        rows = [
            {
                "name": view.get("name", "-"),
                "version": view.get("version", "-"),
                "status": view.get("status", "-"),
                "project": view.get("project", "-"),
                "updated_at": view.get("updated_at", "-"),
            }
            for view in page.items
        ]
        if all_workspaces:
            for row, view in zip(rows, page.items):
                row["workspace"] = view.get("workspace", "-")
        click.echo(_format_model_rows(rows))
        notice = truncation_notice(page, full_option="--all")
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)


@click.command("status")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option("--project", default=None, metavar="NAME", help="Project name filter")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def status_model(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    project: Optional[str],
    pick: Optional[int],
) -> None:
    """Show detail of one registered model by name.

    \b
    Includes latest version status, tags, model type, vLLM readiness,
    publication flag, owner, project, and timestamps when present.

    \b
    Read the deployment lines before deleting a model or repointing a serving:
    `Serving on Vn` names the servings that still hold that version (failed ones
    are left out -- they hold nothing), `Pending deployment` covers the whole
    model and catches a deployment that is queued but not yet running, and
    `Other versions in use` flags versions this view does not detail. None of
    the three shows up in `model versions`, whose Servings column counts
    running deployments on each version and nothing else.
    """
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="model",
        list_command="inspire model list --workspace <workspace>",
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace, session=session)
        project_id = _resolve_project_id(
            config, project, workspace_id=workspace_id, session=session
        )
        user_id = _current_user_id(session)
        model_id, data, version_data = run_with_stale_handle_retry(
            name=name,
            resolve_cached=lambda: _resolve_model_name(
                ctx,
                name,
                workspace_id=workspace_id,
                project_id=project_id,
                user_id=user_id,
                pick=pick,
                session=session,
            ),
            resolve_live=lambda live_name: _resolve_model_name(
                ctx,
                live_name,
                workspace_id=workspace_id,
                project_id=project_id,
                user_id=user_id,
                pick=pick,
                session=session,
                require_live=True,
            ),
            operation=lambda resolved_model_id: (
                resolved_model_id,
                browser_api_module.get_model_detail(
                    model_id=resolved_model_id,
                    session=session,
                    workspace_id=workspace_id,
                ),
                browser_api_module.list_model_version_records(
                    model_id=resolved_model_id,
                    session=session,
                    workspace_id=workspace_id,
                ),
            ),
            invalidate=lambda resolved_model_id: forget_resource_identity(
                session=session,
                resource_type="model",
                resource_id=resolved_model_id,
                name=name,
                workspace_id=str(workspace_id or ""),
                owner_scope="self",
            ),
        )
        remember_resource_identity(
            session=session,
            resource_type="model",
            resource_id=model_id,
            name=name,
            workspace_id=str(workspace_id or ""),
            owner_scope="self",
        )

        compatibility = browser_api_module.get_model_vllm_compatibility(
            model_id,
            session=session,
            workspace_id=workspace_id,
        )
        view = _model_detail_view(
            name,
            data,
            version_data,
            vllm_compatibility=compatibility,
        )
        # Whole-model question, so no version goes in: the platform reads a
        # missing version as "any". It is the only signal that catches a
        # deployment queued behind a busy quota.
        pending = browser_api_module.check_model_inference_serving_pending(
            model_id=model_id,
            session=session,
            workspace_id=workspace_id,
        )
        view["pending_serving"] = pending.get("has_pending_serving") is True
        reported_version = _reported_version(data, version_data)
        if reported_version is not None:
            servings, _total = browser_api_module.list_model_inference_servings(
                model_id=model_id,
                version=reported_version,
                page=1,
                page_size=_SERVING_PAGE_SIZE,
                session=session,
                workspace_id=workspace_id,
            )
            page = bound_collection(
                _serving_views(servings), limit=DEFAULT_COLLECTION_LIMIT
            )
            view["servings"] = page.items
            view.update(
                {f"servings_{key}": value for key, value in page.metadata().items()}
            )
        in_use = _other_versions_in_use(version_data, reported=reported_version)
        if in_use:
            view["other_versions_in_use"] = in_use

        if ctx.json_output:
            click.echo(json_formatter.format_json(view))
            return

        click.echo(_format_model_detail(view))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)


@click.command("deploy-config")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option("--project", default=None, metavar="NAME", help="Project name filter")
@click.option(
    "--version",
    "version",
    type=click.IntRange(1),
    default=None,
    help="Model version (default: the latest version from the model list).",
)
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def deploy_config_model(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    project: Optional[str],
    version: Optional[int],
    pick: Optional[int],
) -> None:
    """Show the minimum resources a model version needs to be deployed.

    \b
    Read this before `serving create`: the platform reports the smallest node
    shape that will hold the weights, which is exactly the floor for
    `--quota gpu,cpu,mem` and `--nodes-per-replica`. A `serving quota` triple
    below this floor is what an out-of-memory deployment looks like before it
    starts. vLLM compatibility is reported alongside because it decides
    whether a vLLM startup command is an option at all.

    \b
    Examples:
        inspire model deploy-config qwen-demo --workspace 分布式训练空间
        inspire --json model deploy-config qwen-demo --workspace 分布式训练空间 --version 2
    """
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="model",
        list_command="inspire model list --workspace <workspace>",
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace, session=session)
        project_id = _resolve_project_id(
            config, project, workspace_id=workspace_id, session=session
        )
        user_id = _current_user_id(session)

        items, _total = browser_api_module.list_models(
            workspace_id=workspace_id,
            page=1,
            page_size=100,
            keyword=name,
            project_ids=[project_id] if project_id else None,
            user_id=user_id,
            session=session,
        )
        model_id = _resolve_model_name(
            ctx,
            name,
            workspace_id=workspace_id,
            project_id=project_id,
            user_id=user_id,
            pick=pick,
            session=session,
            require_live=True,
        )
        resolved_version = version
        if resolved_version is None:
            for item in items:
                if item.model_id == model_id and item.latest_version:
                    try:
                        resolved_version = int(item.latest_version)
                    except ValueError:
                        resolved_version = None
                    break
        if resolved_version is None:
            raise ConfigError(
                "Could not infer the model version. Pass --version explicitly."
            )

        recommended = browser_api_module.get_model_recommended_config(
            model_id,
            version=resolved_version,
            session=session,
            workspace_id=workspace_id,
        )
        vllm_compatible = browser_api_module.check_model_vllm_compatible(
            model_id,
            version=resolved_version,
            session=session,
            workspace_id=workspace_id,
        )

        def _count(key: str) -> Optional[int]:
            raw = recommended.get(key)
            if raw in (None, ""):
                return None
            try:
                return int(float(str(raw)))
            except (TypeError, ValueError):
                return None

        nodes = _count("min_node_count")
        gpu = _count("min_gpu_count_per_node")
        cpu = _count("min_cpu_count_per_node")
        memory = _count("min_memory_size_gib_per_node")
        view: dict[str, Any] = {
            "model": scrub_raw_ids(name),
            "version": resolved_version,
            "vllm_compatible": vllm_compatible,
        }
        if nodes is not None:
            view["min_nodes"] = nodes
        if gpu is not None:
            view["min_gpu_per_node"] = gpu
        if cpu is not None:
            view["min_cpu_per_node"] = cpu
        if memory is not None:
            view["min_memory_gib_per_node"] = memory
        if None not in (gpu, cpu, memory):
            view["min_quota"] = f"{gpu},{cpu},{memory}"

        if ctx.json_output:
            click.echo(json_formatter.format_json(view))
            return

        lines = [
            f"Model: {view['model']} v{resolved_version}",
            f"vLLM compatible: {'yes' if vllm_compatible else 'no'}",
        ]
        if "min_quota" in view:
            lines.append(f"Minimum --quota: {view['min_quota']} (gpu,cpu,mem)")
        if nodes is not None:
            lines.append(f"Minimum --nodes-per-replica: {nodes}")
        click.echo("\n".join(lines))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)


@click.command("versions")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option("--project", default=None, metavar="NAME", help="Project name filter")
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
    help="Maximum versions to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every model version.")
@pass_context
def versions_model(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    project: Optional[str],
    pick: Optional[int],
    limit: int | None,
    show_all: bool,
) -> None:
    """List all versions of one registered model by name.

    \b
    Use this before `serving create` when you need a specific
    `--model-version`; omit the version on serving create to use the latest
    version shown by model listing.

    \b
    The Servings column counts *running* deployments on that version. A queued
    deployment counts as zero here and so does a stopped one; `model status`
    names the servings on the version it reports and flags a pending
    deployment anywhere in the model.
    """
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="model",
        list_command="inspire model list --workspace <workspace>",
    )
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace, session=session)
        project_id = _resolve_project_id(
            config, project, workspace_id=workspace_id, session=session
        )
        user_id = _current_user_id(session)
        model_id, data = run_with_stale_handle_retry(
            name=name,
            resolve_cached=lambda: _resolve_model_name(
                ctx,
                name,
                workspace_id=workspace_id,
                project_id=project_id,
                user_id=user_id,
                pick=pick,
                session=session,
            ),
            resolve_live=lambda live_name: _resolve_model_name(
                ctx,
                live_name,
                workspace_id=workspace_id,
                project_id=project_id,
                user_id=user_id,
                pick=pick,
                session=session,
                require_live=True,
            ),
            operation=lambda resolved_model_id: (
                resolved_model_id,
                browser_api_module.list_model_version_records(
                    model_id=resolved_model_id,
                    session=session,
                    workspace_id=workspace_id,
                ),
            ),
            invalidate=lambda resolved_model_id: forget_resource_identity(
                session=session,
                resource_type="model",
                resource_id=resolved_model_id,
                name=name,
                workspace_id=str(workspace_id or ""),
                owner_scope="self",
            ),
        )
        remember_resource_identity(
            session=session,
            resource_type="model",
            resource_id=model_id,
            name=name,
            workspace_id=str(workspace_id or ""),
            owner_scope="self",
        )

        compatibility = browser_api_module.get_model_vllm_compatibility(
            model_id,
            session=session,
            workspace_id=workspace_id,
        )
        versions = _model_version_views(data, vllm_compatibility=compatibility)
        page = bound_collection(versions, limit=effective_limit)
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "name": scrub_raw_ids(name),
                        "items": page.items,
                        **page.metadata(),
                    }
                )
            )
            return

        if not page.items:
            click.echo(f"No versions for model {scrub_raw_ids(name)}.")
            return

        click.echo(_format_model_versions(page.items))
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)


@click.command("register")
@click.option("--name", "-n", required=True, metavar="NAME", help="Model name")
@click.option(
    "--source-path",
    required=True,
    metavar="PATH",
    help="Platform-visible model directory on shared storage",
)
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--project",
    "-p",
    required=True,
    metavar="NAME",
    help="Project name.",
)
@click.option(
    "--type",
    "model_type",
    multiple=True,
    metavar="TYPE",
    help="Model type segment; pass twice for category + task",
)
@click.option("--tag", "tags", multiple=True, metavar="TAG", help="Custom model tag")
@click.option(
    "--description",
    default="",
    metavar="DESCRIPTION",
    help="Model description",
)
@pass_context
def register_model(
    ctx: Context,
    name: str,
    source_path: str,
    workspace: str,
    project: Optional[str],
    model_type: tuple[str, ...],
    tags: tuple[str, ...],
    description: str,
) -> None:
    """Register a platform-visible model directory in the model repository.

    This creates the model entry from an existing shared-storage directory.
    It does not upload local files; copy or generate model files on the
    platform first, then pass that remote directory as `--source-path`.
    """
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace, session=session)
        if not workspace_id:
            raise ConfigError("Missing workspace.")
        requested_project = project
        project_id: Optional[str]
        project_id = _resolve_project_id(
            config,
            requested_project,
            workspace_id=workspace_id,
            session=session,
        )
        if not project_id:
            raise ConfigError("--project is required.")

        result = browser_api_module.create_model(
            name=name,
            project_id=project_id,
            workspace_id=workspace_id,
            model_source_path=source_path,
            model_type=model_type,
            tags=tags,
            description=description,
            model_source_type=1,
            session=session,
        )
        model_id = _created_model_id(result)
        remember_resource_identity(
            session=session,
            resource_type="model",
            resource_id=model_id,
            name=name,
            workspace_id=str(workspace_id or ""),
            owner_scope="self",
        )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "name": scrub_raw_ids(name),
                        "status": "registered",
                        "project": scrub_raw_ids(project or ""),
                        "workspace": scrub_raw_ids(workspace),
                    }
                )
            )
            return

        click.echo(format_mutation_success("Model", "registered", name))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)


@click.command("delete")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option("--project", default=None, metavar="NAME", help="Project name filter")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Delete without checking whether deployments still reference the model.",
)
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def delete_model_cmd(
    ctx: Context,
    name: str,
    workspace: Optional[str],
    project: Optional[str],
    yes: bool,
    force: bool,
    pick: Optional[int],
) -> None:
    """Delete a registered model and every version it holds.

    \b
    This cannot be undone, and it is not version-scoped: the whole entry goes,
    so every deployment still pointing at any version of it loses what it was
    serving. The registered directory on shared storage is left alone -- only
    the registry entry is removed, and `model register` can recreate it from
    the same path.

    \b
    The deployments are checked first, and a model that any serving still
    holds is refused by name. A failed serving does not count -- it holds
    nothing -- but a stopped or sleeping one does, because it can be started
    again. `--force` skips the check instead of answering it.
    """
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="model",
        list_command="inspire model list --workspace <workspace>",
    )
    require_confirmation(
        ctx,
        yes=yes,
        prompt=f"Delete model '{scrub_raw_ids(name)}' and all of its versions?",
        message="Model deletion requires confirmation.",
    )

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_id(workspace, session=session)
        project_id = _resolve_project_id(
            config, project, workspace_id=workspace_id, session=session
        )
        user_id = _current_user_id(session)
        model_id = _resolve_model_name(
            ctx,
            name,
            workspace_id=workspace_id,
            project_id=project_id,
            user_id=user_id,
            pick=pick,
            session=session,
            require_live=True,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
        return
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
        return

    if not force:
        try:
            version_data = browser_api_module.list_model_version_records(
                model_id=model_id,
                session=session,
                workspace_id=workspace_id,
            )
            references = _model_references(
                model_id,
                version_data,
                session=session,
                workspace_id=workspace_id,
            )
            # No version goes in: the platform reads a missing version as
            # "any", which is the only signal that catches a deployment queued
            # behind a busy quota rather than already running.
            pending = browser_api_module.check_model_inference_serving_pending(
                model_id=model_id,
                session=session,
                workspace_id=workspace_id,
            )
        except SessionExpiredError as e:
            _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
            return
        except Exception:
            # A failed probe is not an empty answer. Refusing here keeps the
            # command from deleting a model whose deployments it never saw.
            _handle_error(
                ctx,
                "APIError",
                "Could not check which deployments still use this model.",
                EXIT_API_ERROR,
                hint="Retry, or pass --force to delete without the check.",
            )
            return

        has_pending = pending.get("has_pending_serving") is True
        if references or has_pending:
            _handle_error(
                ctx,
                "ValidationError",
                _in_use_message(name, references, pending=has_pending),
                EXIT_VALIDATION_ERROR,
                hint=(
                    "Delete those servings first, or pass --force to delete the "
                    "model anyway and leave them pointing at nothing."
                ),
            )
            return

    try:
        browser_api_module.delete_model(
            model_id,
            session=session,
            workspace_id=workspace_id,
        )
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception:
        _handle_error(ctx, "APIError", "Could not delete model.", EXIT_API_ERROR)
        return

    forget_resource_identity(
        session=session,
        resource_type="model",
        resource_id=model_id,
        name=name,
        workspace_id=str(workspace_id or ""),
        owner_scope="self",
    )

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {"name": scrub_raw_ids(name), "status": "deleted"}
            )
        )
        return

    click.echo(format_mutation_success("Model", "deleted", name))


__all__ = [
    "delete_model_cmd",
    "deploy_config_model",
    "list_model",
    "register_model",
    "status_model",
    "versions_model",
]
