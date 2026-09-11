"""`inspire model` subcommands — model repository workflows."""

from __future__ import annotations

from inspire.services.catalog.models import (
    model_deploy_config_view,
    current_user_id,
    status_label,
    SERVING_PAGE_SIZE,
    serving_views,
    model_list_view,
    reported_version,
    other_versions_in_use,
    model_detail_view,
    model_version_views,
)
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
from inspire.services.utils import json_formatter
from inspire.cli.formatters.human_formatter import (
    format_epoch,
    format_mutation_success,
)
from inspire.cli.formatters.table import column_width, render_table
from inspire.services.utils.collections import (
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
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workspaces import (
    resolve_workspace_query_scope,
    select_workspace_id,
    workspace_name_map,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session
from inspire.services.catalog.model_writes import created_model_id as _created_model_id
from inspire.services.catalog.model_writes import in_use_message as _in_use_message







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


# `model-hub` reports a serving's state as an int indexing the serving status
# enum, while the `inference_serving` domain reports the same states as the
# strings below. Index 4 is pinned by measurement, not by reading the enum:
# across every model version that has servings, the count of status-4 entries
# equals the version record's own `running_infrence_serving` (11/11).
# A failed serving no longer holds the model version: it is not running, and
# starting it is not an option. Everything else is a live consumer -- `STOPPED`
# and `SLEEPING` servings can be started again, so they still break if the
# model goes away.
# One page covers every model version observed on the platform, and the Action
# rejects `page_size: -1`, so "everything" has to be a real number.




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
                "status": status_label(m.status),
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
        user_id = current_user_id(session)
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
            view = model_list_view(model, workspace=workspace_name)
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

    Includes latest version status, tags, model type, vLLM readiness,
    publication flag, owner, project, and timestamps when present.

    Read the deployment lines before deleting a model or repointing a serving:
    `Serving on Vn` names the servings that still hold that version (failed
    ones are left out -- they hold nothing), `Pending deployment` covers the
    whole model and catches a deployment that is queued but not yet running,
    and `Other versions in use` flags versions this view does not detail. None
    of the three shows up in `model versions`, whose Servings column counts
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
        user_id = current_user_id(session)
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
        view = model_detail_view(
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
        reported = reported_version(data, version_data)
        if reported is not None:
            servings, _total = browser_api_module.list_model_inference_servings(
                model_id=model_id,
                version=reported,
                page=1,
                page_size=SERVING_PAGE_SIZE,
                session=session,
                workspace_id=workspace_id,
            )
            page = bound_collection(
                serving_views(servings), limit=DEFAULT_COLLECTION_LIMIT
            )
            view["servings"] = page.items
            view.update(
                {f"servings_{key}": value for key, value in page.metadata().items()}
            )
        in_use = other_versions_in_use(version_data, reported=reported)
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

    Read this before `serving create`: the platform reports the smallest node
    shape that will hold the weights, which is exactly the floor for `--quota
    gpu,cpu,mem` and `--nodes-per-replica`. A `serving quota` triple below this
    floor is what an out-of-memory deployment looks like before it starts. vLLM
    compatibility is reported alongside because it decides whether a vLLM
    startup command is an option at all.

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
        user_id = current_user_id(session)

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

        view = model_deploy_config_view(name, resolved_version, recommended, vllm_compatible)
        nodes = view.get("min_nodes")

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

    Use this before `serving create` when you need a specific
    `--model-version`; omit the version on serving create to use the latest
    version shown by model listing.

    The Servings column counts *running* deployments on that version. A queued
    deployment counts as zero here and so does a stopped one; `model status`
    names the servings on the version it reports and flags a pending deployment
    anywhere in the model.
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
        user_id = current_user_id(session)
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
        versions = model_version_views(data, vllm_compatibility=compatibility)
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

    This cannot be undone, and it is not version-scoped: the whole entry goes,
    so every deployment still pointing at any version of it loses what it was
    serving. The registered directory on shared storage is left alone -- only
    the registry entry is removed, and `model register` can recreate it from
    the same path.

    The deployments are checked first, and a model that any serving still holds
    is refused by name. A failed serving does not count -- it holds nothing --
    but a stopped or sleeping one does, because it can be started again.
    `--force` skips the check instead of answering it.
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
        user_id = current_user_id(session)
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
            from inspire.services.catalog.model_writes import model_usage
            references, has_pending = model_usage(model_id, session=session, workspace_id=workspace_id)
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
