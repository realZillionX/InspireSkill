"""Notebook creation flow for `inspire notebook create`."""

from __future__ import annotations

import uuid
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    get_base_url,
    load_config,
    require_web_session,
    resolve_json_output,
)
from inspire.cli.utils.notebook_post_start import (
    NotebookPostStartSpec,
    NO_WAIT_POST_START_WARNING,
    resolve_notebook_post_start_spec,
)
from inspire.cli.utils.quota_resolver import (
    QuotaMatchError,
    QuotaParseError,
    QuotaSpec,
    ResolvedQuota,
    SCHEDULE_TYPE_DSW,
    build_resource_spec_price,
    parse_quota,
    resolve_quota,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.task_priority import TaskPriorityError, resolve_task_priority
from inspire.config import Config, ConfigError
from inspire.config.workload_profiles import apply_workload_profile, profile_required_message
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import NotebookFailedError
from inspire.platform.web.browser_api.workspaces import (
    WorkspaceCapabilityError,
    is_fair_scheduling_workspace,
)
from inspire.platform.web.session import WebSession
from .notebook_lookup import (
    _list_notebooks_for_workspace,
    _notebook_id_from_item,
    _sort_notebook_items,
    _try_get_current_user_ids,
)


@dataclass(frozen=True)
class NotebookCreateDiagnostics:
    name: str
    workspace: str
    project: str
    image: str
    resource: str
    compute_group: str


def format_quota_display(quota: ResolvedQuota) -> str:
    if quota.gpu_count > 0:
        label = quota.gpu_type or "GPU"
        return f"{quota.gpu_count}x{label} + {quota.cpu_count}CPU + {quota.memory_gib}GiB"
    return f"{quota.cpu_count}CPU + {quota.memory_gib}GiB"


def _first_non_empty_str(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _workspace_label(
    *,
    workspace_id: str,
    session: WebSession,
    requested_workspace: str | None,
) -> str:
    if requested_workspace:
        return requested_workspace

    session_names = getattr(session, "all_workspace_names", None) or {}
    if isinstance(session_names, dict):
        name = _first_non_empty_str(session_names.get(workspace_id))
        if name:
            return name

    return "(workspace name unavailable)"


def _format_create_diagnostics(
    diagnostics: NotebookCreateDiagnostics,
    *,
    reason: str | None = None,
    events: str | None = None,
) -> str:
    lines = [
        f"Notebook: {diagnostics.name}",
        f"Workspace: {scrub_raw_ids(diagnostics.workspace)}",
        f"Project: {scrub_raw_ids(diagnostics.project)}",
        f"Compute group: {scrub_raw_ids(diagnostics.compute_group)}",
        f"Image: {diagnostics.image}",
        f"Resource: {diagnostics.resource}",
    ]
    if reason:
        lines.append(f"Reason: {scrub_raw_ids(reason)}")

    event_text = (events or "").strip()
    if event_text:
        lines.append("Platform events:")
        lines.extend(f"  {scrub_raw_ids(line)}" for line in event_text.splitlines() if line.strip())
    else:
        lines.append("Platform events: no platform events returned yet.")
    return "\n".join(lines)


def _sanitize_notebook_id(text: str, notebook_id: str) -> str:
    if not notebook_id:
        return text
    return scrub_raw_ids(text.replace(notebook_id, "<notebook-id>"))


def _event_message(event: dict) -> str:
    reason = _first_non_empty_str(event.get("reason"))
    message = _first_non_empty_str(event.get("message"), event.get("content"))
    event_type = _first_non_empty_str(event.get("type"))
    prefix = f"[{event_type}] " if event_type else ""
    label = f"{reason}: " if reason else ""
    return f"{prefix}{label}{message}".strip()


def _fetch_event_preview(notebook_id: str, session: WebSession) -> str:
    try:
        events = browser_api_module.list_notebook_events(
            notebook_id,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics must not hide the root error
        return f"failed to fetch platform events: {scrub_raw_ids(exc)}"

    lines = []
    for event in events[-10:]:
        if not isinstance(event, dict):
            continue
        message = _event_message(event)
        if message:
            lines.append(message)
    return "\n".join(lines)


def _extract_notebook_id(result: object) -> str:
    if not isinstance(result, dict):
        return ""

    for key in ("notebook_id", "id", "uuid"):
        value = _first_non_empty_str(result.get(key))
        if value:
            return value

    for key in ("notebook", "item", "instance"):
        nested = result.get(key)
        if isinstance(nested, dict):
            value = _extract_notebook_id(nested)
            if value:
                return value
    return ""


def _resolve_created_notebook_id(
    *,
    name: str,
    workspace_id: str,
    session: WebSession,
) -> str:
    try:
        user_ids = _try_get_current_user_ids(session, base_url=get_base_url())
        if not user_ids:
            return ""
        items = _list_notebooks_for_workspace(
            session,
            base_url=get_base_url(),
            workspace_id=workspace_id,
            user_ids=user_ids,
            keyword=name,
            page_size=20,
        )
    except Exception:
        return ""

    matches = [item for item in items if str(item.get("name") or "") == name]
    for item in _sort_notebook_items(matches):
        notebook_id = _notebook_id_from_item(item)
        if notebook_id:
            return notebook_id
    return ""


def resolve_notebook_project(
    ctx: Context,
    *,
    projects: list,
    config: Config,
    project: str | None,
    allow_requested_over_quota: bool,
    needs_gpu_quota: bool,
    json_output: bool,
    workspace_id: str | None = None,
    session: WebSession | None = None,
) -> Any | None:
    project_value = project
    if project_value and not project_value.startswith("project-"):
        for alias, project_id in (config.projects or {}).items():
            if alias.lower() == project_value.lower():
                project_value = project_id
                break

    try:
        congested: set[str] | None = None
        if needs_gpu_quota and workspace_id and session:
            congested = (
                browser_api_module.check_scheduling_health(
                    workspace_id=workspace_id,
                    project_ids={p.project_id for p in projects},
                    session=session,
                )
                or None
            )

        selected_project, fallback_msg = browser_api_module.select_project(
            projects,
            project_value,
            allow_requested_over_quota=allow_requested_over_quota,
            needs_gpu_quota=needs_gpu_quota,
            project_order=config.project_order or None,
            congested_projects=congested,
        )

        if not json_output:
            if fallback_msg:
                click.echo(fallback_msg)
            click.echo(
                "Using project: "
                f"{selected_project.name}{selected_project.get_quota_status(needs_gpu=needs_gpu_quota)}"
            )
    except ValueError as e:
        error_msg = str(e)
        if "not found" in error_msg:
            hint = None
            if projects:
                hint = "Available projects:\n" + "\n".join(f"  - {p.name}" for p in projects)
            _handle_error(ctx, "ValidationError", error_msg, EXIT_CONFIG_ERROR, hint=hint)
            return None
        _handle_error(ctx, "QuotaExceeded", error_msg, EXIT_CONFIG_ERROR)
        return None

    return selected_project


def _find_image_match(images: list[Any], image: str) -> Any | None:
    image_lower = image.lower()
    for img in images:
        if (
            image_lower in img.name.lower()
            or image_lower in img.url.lower()
            or img.image_id == image
        ):
            return img
    return None


def resolve_notebook_image(
    ctx: Context,
    *,
    images: list,
    image: Optional[str],
    json_output: bool,
) -> Any | None:
    selected_image = None

    if image:
        selected_image = _find_image_match(images, image)
        if not selected_image:
            hint = "Available images:\n" + "\n".join(f"  - {img.name}" for img in images[:20])
            _handle_error(
                ctx,
                "ValidationError",
                f"Image '{image}' not found",
                EXIT_CONFIG_ERROR,
                hint=hint,
            )
            return None
    else:
        if not json_output:
            click.echo("\nAvailable images:")
            for i, img in enumerate(images[:10], 1):
                click.echo(f"  [{i}] {img.name}")
            if len(images) > 10:
                click.echo(f"  ... and {len(images) - 10} more")

            default_idx = 1
            for i, img in enumerate(images, 1):
                if "pytorch" in img.name.lower():
                    default_idx = i
                    break

            try:
                choice = click.prompt(
                    "\nSelect image",
                    type=click.IntRange(1, len(images)),
                    default=default_idx,
                )
                if choice < 1 or choice > len(images):
                    _handle_error(
                        ctx,
                        "ValidationError",
                        "Invalid selection",
                        EXIT_CONFIG_ERROR,
                        hint=f"Choose between 1 and {len(images)}.",
                    )
                    return None
                selected_image = images[choice - 1]
            except click.Abort:
                _handle_error(ctx, "Aborted", "Aborted.", EXIT_CONFIG_ERROR)
                return None
        else:
            for img in images:
                if "pytorch" in img.name.lower():
                    selected_image = img
                    break
            if not selected_image:
                selected_image = images[0]

    return selected_image


def create_notebook_and_report(
    ctx: Context,
    *,
    name: str,
    resource_display: str,
    diagnostics: NotebookCreateDiagnostics,
    selected_project,
    selected_image,
    quota: ResolvedQuota,
    shm_size: int,
    auto_stop: bool,
    workspace_id: str,
    session: WebSession,
    json_output: bool,
    task_priority: Optional[int] = None,
    node_id: Optional[str] = None,
) -> str | None:
    try:
        resource_spec_price = build_resource_spec_price(
            quota=quota,
            shared_memory_size=shm_size,
        )
        result = browser_api_module.create_notebook(
            name=name,
            project_id=selected_project.project_id,
            project_name=selected_project.name,
            image_id=selected_image.image_id,
            image_url=selected_image.url,
            logic_compute_group_id=quota.logic_compute_group_id,
            quota_id=quota.quota_id,
            gpu_type=quota.gpu_type,
            gpu_count=quota.gpu_count,
            cpu_count=quota.cpu_count,
            memory_size=quota.memory_gib,
            shared_memory_size=shm_size,
            auto_stop=auto_stop,
            workspace_id=workspace_id,
            session=session,
            task_priority=task_priority,
            resource_spec_price=resource_spec_price,
            node_id=node_id,
        )

        notebook_id = _extract_notebook_id(result)
        if not notebook_id:
            notebook_id = _resolve_created_notebook_id(
                name=name,
                workspace_id=workspace_id,
                session=session,
            )
        if not notebook_id:
            _handle_error(
                ctx,
                "APIError",
                f"Notebook '{name}' was submitted, but the CLI could not find the created notebook by name.",
                EXIT_API_ERROR,
                hint=_format_create_diagnostics(
                    diagnostics,
                    reason="Create API response could not be matched to the new notebook by name.",
                ),
            )
            return None

        if json_output:
            workspace_name = (getattr(session, "all_workspace_names", None) or {}).get(
                workspace_id, ""
            )
            click.echo(
                json_formatter.format_json(
                    {
                        "notebook_id": notebook_id,
                        "name": name,
                        "resource": resource_display,
                        "quota_id": quota.quota_id,
                        "project": selected_project.name,
                        "image": selected_image.name,
                        "logic_compute_group_id": quota.logic_compute_group_id,
                        "compute_group_name": quota.compute_group_name,
                        "workspace_id": workspace_id,
                        "workspace_name": workspace_name,
                    }
                )
            )
        else:
            click.echo("\nNotebook created successfully!")
            click.echo(f"  Name: {name}")
            click.echo(f"  Workspace: {diagnostics.workspace}")
            click.echo(f"  Project: {selected_project.name}")
            click.echo(f"  Resource: {resource_display}")
            click.echo(f"  Compute group: {quota.compute_group_name}")
            click.echo(f"  Image: {selected_image.name}")
            quoted_name = shlex.quote(name)
            quoted_workspace = shlex.quote(str(diagnostics.workspace or workspace_id or ""))
            click.echo("  Next:")
            click.echo(f"    inspire notebook events {quoted_name} --workspace {quoted_workspace}")
            click.echo(f"    inspire notebook ssh {quoted_name} --workspace {quoted_workspace}")
            click.echo(f"    inspire notebook exec {quoted_name} \"pwd\"")
            click.echo(f"    inspire notebook delete {quoted_name} --workspace {quoted_workspace} --yes")

        return notebook_id

    except Exception as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to create notebook '{name}': {e}",
            EXIT_API_ERROR,
            hint=_format_create_diagnostics(diagnostics, reason=str(e)),
        )
        return None


def maybe_wait_for_running(
    ctx: Context,
    *,
    notebook_id: str,
    diagnostics: NotebookCreateDiagnostics,
    session: WebSession,
    wait: bool,
    needs_post_start: bool,
    json_output: bool,
    timeout: int = 600,
) -> bool:
    if not (wait or needs_post_start):
        return True

    if needs_post_start and not wait and not json_output:
        click.echo(NO_WAIT_POST_START_WARNING, err=True)

    if not json_output:
        click.echo("Waiting for notebook to reach RUNNING status...")

    try:
        browser_api_module.wait_for_notebook_running(
            notebook_id=notebook_id,
            session=session,
            timeout=timeout,
        )
        if not json_output:
            click.echo("Notebook is now RUNNING.")
        return True
    except NotebookFailedError as e:
        detail = e.detail or {}
        reason_parts = [f"terminal status: {e.status}"]
        sub_status = _first_non_empty_str(detail.get("sub_status"))
        if sub_status:
            reason_parts.append(f"sub-status: {sub_status}")
        hint_parts = []
        events = _sanitize_notebook_id(e.events or "", notebook_id)
        if events:
            hint_parts.append(events)
        else:
            fetched_events = _sanitize_notebook_id(
                _fetch_event_preview(notebook_id, session),
                notebook_id,
            )
            if fetched_events:
                hint_parts.append(fetched_events)
        extra = detail.get("extra_info") or {}
        for key in ("NodeName", "HostIP"):
            if extra.get(key):
                hint_parts.append(f"{key}: {extra[key]}")
        events_hint = "\n".join(hint_parts)
        _handle_error(
            ctx,
            "NotebookFailed",
            f"Notebook '{diagnostics.name}' failed to start.",
            EXIT_API_ERROR,
            hint=_format_create_diagnostics(
                diagnostics,
                reason="; ".join(reason_parts),
                events=events_hint,
            ),
        )
        return False
    except TimeoutError as e:
        events_hint = _sanitize_notebook_id(
            _fetch_event_preview(notebook_id, session),
            notebook_id,
        )
        reason = _sanitize_notebook_id(str(e), notebook_id)
        _handle_error(
            ctx,
            "Timeout",
            f"Timed out waiting for notebook '{diagnostics.name}' to reach RUNNING.",
            EXIT_API_ERROR,
            hint=_format_create_diagnostics(
                diagnostics,
                reason=reason,
                events=events_hint,
            ),
        )
        return False


def maybe_run_post_start(
    ctx: Context,
    *,
    notebook_id: str,
    diagnostics: NotebookCreateDiagnostics | None = None,
    session: WebSession,
    post_start_spec: NotebookPostStartSpec | None,
    gpu_count: int,
    json_output: bool,
) -> None:
    if post_start_spec is None:
        return
    if post_start_spec.requires_gpu and gpu_count <= 0:
        return

    if not json_output:
        click.echo(f"Starting {scrub_raw_ids(post_start_spec.label)}...")

    try:
        started = browser_api_module.run_command_in_notebook(
            notebook_id=notebook_id,
            command=post_start_spec.command,
            session=session,
            timeout=20,
            completion_marker=post_start_spec.completion_marker,
        )
        if not json_output and started:
            click.echo(
                f"{scrub_raw_ids(post_start_spec.label)} started "
                f"(log: {scrub_raw_ids(post_start_spec.log_path)})"
            )
            if diagnostics is not None:
                quoted_name = shlex.quote(diagnostics.name)
                click.echo(
                    "  To stop: inspire notebook exec "
                    f'{quoted_name} "kill $(cat {scrub_raw_ids(post_start_spec.pid_file)})"'
                )
            else:
                click.echo(
                    '  To stop: inspire notebook exec '
                    f'"kill $(cat {scrub_raw_ids(post_start_spec.pid_file)})"'
                )
        if not json_output and not started:
            click.echo(
                f"Warning: Failed to confirm {post_start_spec.label.lower()} startup; check "
                f"{scrub_raw_ids(post_start_spec.log_path)} inside the notebook.",
                err=True,
            )
    except Exception as e:
        if not json_output:
            click.echo(
                "Warning: Failed to start "
                f"{scrub_raw_ids(post_start_spec.label.lower())}: {scrub_raw_ids(e)}",
                err=True,
            )
            if diagnostics is not None:
                click.echo(
                    _format_create_diagnostics(
                        diagnostics,
                        reason=f"post-start failed: {e}",
                        events=_sanitize_notebook_id(
                            _fetch_event_preview(notebook_id, session),
                            notebook_id,
                        ),
                    ),
                    err=True,
                )


def _resolve_create_inputs(
    *,
    config: Config,
    quota: str | None,
    project: str | None,
    image: str | None,
    shm_size: int | None,
) -> tuple[str, str | None, str | None, int]:
    if not quota:
        raise ValueError(profile_required_message("notebook", "quota"))
    if not project:
        raise ValueError(profile_required_message("notebook", "project"))
    if not image:
        raise ValueError(profile_required_message("notebook", "image"))
    if shm_size is None:
        shm_size = config.shm_size if config.shm_size is not None else 32
    if shm_size < 1:
        raise ValueError("Shared memory size must be >= 1.")
    return quota, project, image, shm_size


def _fetch_workspace_projects(
    ctx: Context,
    *,
    workspace_id: str,
    session: WebSession,
) -> list[Any] | None:
    try:
        projects = browser_api_module.list_projects(workspace_id=workspace_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch projects: {e}", EXIT_API_ERROR)
        return None

    if projects:
        return projects

    _handle_error(ctx, "ConfigError", "No projects available in this workspace", EXIT_CONFIG_ERROR)
    return None


def _fetch_notebook_images(
    ctx: Context,
    *,
    workspace_id: str,
    session: WebSession,
    image: Optional[str],
    json_output: bool,
) -> list | None:
    try:
        images = browser_api_module.list_images(workspace_id=workspace_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch images: {scrub_raw_ids(e)}", EXIT_API_ERROR)
        return None

    if image and not _find_image_match(images, image):
        for source in ("SOURCE_PUBLIC", "SOURCE_PRIVATE"):
            try:
                extra_images = browser_api_module.list_images(
                    workspace_id=workspace_id, source=source, session=session
                )
                if extra_images:
                    if not json_output:
                        click.echo(f"Searching {source.lower().replace('source_', '')} images...")
                    images = images + extra_images
                    if _find_image_match(images, image):
                        break
            except Exception:
                pass

    if images:
        return images

    _handle_error(ctx, "ConfigError", "No images available", EXIT_CONFIG_ERROR)
    return None


def _resolve_notebook_name(name: Optional[str], *, json_output: bool) -> str:
    if name:
        return name
    generated = f"notebookrun-{uuid.uuid4().hex[:8]}"
    if not json_output:
        click.echo(f"Generated name: {scrub_raw_ids(generated)}")
    return generated


def _resolve_workspace_id(
    ctx: Context,
    *,
    config: Config,
    session: WebSession,
    workspace: Optional[str],
    workspace_id: Optional[str],
) -> Optional[str]:
    if workspace_id:
        return workspace_id
    try:
        resolved = select_workspace_id(
            config,
            explicit_workspace_name=workspace,
            session=session,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return None

    if not resolved:
        from inspire.config.workspaces import workspace_required_hint

        _handle_error(
            ctx,
            "ConfigError",
            "No workspace selected.",
            EXIT_CONFIG_ERROR,
            hint=workspace_required_hint(config),
        )
        return None

    return resolved


def _resolve_quota(
    ctx: Context,
    *,
    spec: QuotaSpec,
    workspace_id: str,
    session: WebSession,
    group_override: Optional[str],
) -> Optional[ResolvedQuota]:
    try:
        return resolve_quota(
            spec=spec,
            workspace_id=workspace_id,
            session=session,
            schedule_config_type=SCHEDULE_TYPE_DSW,
            group_override=group_override,
        )
    except QuotaMatchError as err:
        _handle_error(ctx, "ValidationError", str(err), EXIT_CONFIG_ERROR)
        return None


def run_notebook_create(
    ctx: Context,
    *,
    name: Optional[str],
    workspace: Optional[str],
    workspace_id: Optional[str],
    quota: str | None,
    project: Optional[str],
    image: Optional[str],
    shm_size: Optional[int],
    auto_stop: bool,
    wait: bool,
    post_start: str | None,
    post_start_script: Path | None,
    json_output: bool,
    priority: Optional[int] = None,
    project_explicit: bool = False,
    group: Optional[str] = None,
    node: Optional[str] = None,
    profile_name: str | None = None,
) -> None:
    del project_explicit
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )
    config = load_config(ctx)

    fields = apply_workload_profile(
        profiles=getattr(config, "profiles", {}),
        kind="notebook",
        profile_name=profile_name,
        values={
            "workspace": workspace,
            "project": project,
            "group": group,
            "image": image,
            "quota": quota,
        },
    )
    workspace = fields["workspace"]
    project = fields["project"]
    group = fields["group"]
    image = fields["image"]
    quota = fields["quota"]

    try:
        post_start_spec = resolve_notebook_post_start_spec(
            config=config,
            post_start=post_start,
            post_start_script=post_start_script,
        )
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
        return

    try:
        quota, project, image, shm_size = _resolve_create_inputs(
            config=config,
            quota=quota,
            project=project,
            image=image,
            shm_size=shm_size,
        )
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
        return

    if not group:
        _handle_error(
            ctx,
            "ValidationError",
            profile_required_message("notebook", "group"),
            EXIT_CONFIG_ERROR,
        )
        return
    if not workspace and not workspace_id:
        _handle_error(
            ctx,
            "ValidationError",
            profile_required_message("notebook", "workspace"),
            EXIT_CONFIG_ERROR,
        )
        return

    try:
        quota_spec = parse_quota(quota)
    except QuotaParseError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
        return

    workspace_id = _resolve_workspace_id(
        ctx,
        config=config,
        session=session,
        workspace=workspace,
        workspace_id=workspace_id,
    )
    if not workspace_id:
        return

    resolved_quota = _resolve_quota(
        ctx,
        spec=quota_spec,
        workspace_id=workspace_id,
        session=session,
        group_override=group,
    )
    if resolved_quota is None:
        return

    resource_display = format_quota_display(resolved_quota)
    if not json_output:
        node_note = f", pinned to node {scrub_raw_ids(node)}" if node else ""
        click.echo(
            f"Creating notebook with {scrub_raw_ids(resource_display)} on "
            f"{scrub_raw_ids(resolved_quota.compute_group_name)}{node_note}..."
        )

    try:
        fair_scheduling = is_fair_scheduling_workspace(session, workspace_id)
        uncapped_priority = resolve_task_priority(
            priority,
            fair_scheduling=fair_scheduling,
        )
    except WorkspaceCapabilityError as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return
    except TaskPriorityError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return
    projects = _fetch_workspace_projects(ctx, workspace_id=workspace_id, session=session)
    if projects is None:
        return

    selected_project = resolve_notebook_project(
        ctx,
        projects=projects,
        config=config,
        project=project,
        allow_requested_over_quota=False,
        needs_gpu_quota=(resolved_quota.gpu_count > 0),
        json_output=json_output,
        workspace_id=workspace_id,
        session=session,
    )
    if not selected_project:
        return

    task_priority = resolve_task_priority(
        priority,
        fair_scheduling=fair_scheduling,
        project_limit=selected_project.priority_name,
    )
    if task_priority != uncapped_priority and not json_output:
        click.echo(
            f"Capping priority {uncapped_priority} -> {task_priority} "
            f"(max for project '{scrub_raw_ids(selected_project.name)}')"
        )

    images = _fetch_notebook_images(
        ctx,
        workspace_id=workspace_id,
        session=session,
        image=image,
        json_output=json_output,
    )
    if images is None:
        return

    selected_image = resolve_notebook_image(
        ctx,
        images=images,
        image=image,
        json_output=json_output,
    )
    if not selected_image:
        return

    if not json_output:
        click.echo(f"Using image: {scrub_raw_ids(selected_image.name)}")

    name = _resolve_notebook_name(name, json_output=json_output)
    workspace_label = _workspace_label(
        workspace_id=workspace_id,
        session=session,
        requested_workspace=workspace,
    )
    diagnostics = NotebookCreateDiagnostics(
        name=name,
        workspace=workspace_label,
        project=selected_project.name,
        image=selected_image.name,
        resource=resource_display,
        compute_group=resolved_quota.compute_group_name,
    )

    notebook_id = create_notebook_and_report(
        ctx,
        name=name,
        resource_display=resource_display,
        diagnostics=diagnostics,
        selected_project=selected_project,
        selected_image=selected_image,
        quota=resolved_quota,
        shm_size=shm_size,
        auto_stop=auto_stop,
        workspace_id=workspace_id,
        session=session,
        json_output=json_output,
        task_priority=task_priority,
        node_id=node,
    )
    if not notebook_id:
        return

    if not maybe_wait_for_running(
        ctx,
        notebook_id=notebook_id,
        diagnostics=diagnostics,
        session=session,
        wait=wait,
        needs_post_start=(post_start_spec is not None),
        json_output=json_output,
        timeout=600,
    ):
        return

    maybe_run_post_start(
        ctx,
        notebook_id=notebook_id,
        diagnostics=diagnostics,
        session=session,
        post_start_spec=post_start_spec,
        gpu_count=resolved_quota.gpu_count,
        json_output=json_output,
    )

    if not json_output:
        click.echo(
            "\nUse "
            f"`inspire notebook status {scrub_raw_ids(name)} --workspace {scrub_raw_ids(workspace_label)}` "
            "to check status."
        )


__all__ = ["run_notebook_create", "maybe_run_post_start", "format_quota_display"]
