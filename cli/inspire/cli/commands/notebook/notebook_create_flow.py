"""Notebook creation flow for `inspire notebook create`."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
)
from inspire.cli.formatters import human_formatter
from inspire.services.utils import json_formatter
from inspire.cli.utils.dataset_mounts import (
    DatasetSpecError,
    dataset_mount_views,
    describe_dataset_mounts,
    resolve_dataset_info,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import reject_id_at_boundary, remember_resource_identity
from inspire.cli.utils.project_resolver import resolve_project
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
    ensure_priority_allowed,
    parse_quota,
    resolve_quota,
)
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.task_priority import TaskPriorityError, resolve_task_priority
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import DatasetMount, NotebookFailedError
from inspire.platform.web.browser_api.workspaces import (
    WorkspaceCapabilityError,
    is_fair_scheduling_workspace,
)
from inspire.platform.web.session import TransientAPIError, WebSession
from .notebook_lookup import (
    _list_notebooks_for_workspace,
    _try_get_current_user_ids,
)
from inspire.services.notebook.notebook_output import public_operation

from inspire.services.notebook import notebooks as notebook_services
from inspire.services.notebook.notebooks import (
    build_notebook_create_kwargs,
    first_non_empty_str as first_non_empty_str,
    extract_notebook_id as extract_notebook_id,
    resolve_create_inputs as resolve_create_inputs,
    split_auto_stop_after as split_auto_stop_after,
    format_quota_display as format_quota_display,
    find_image_match as find_image_match,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotebookCreateDiagnostics:
    name: str
    workspace: str
    project: str
    image: str
    resource: str
    compute_group: str


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
        name = first_non_empty_str(session_names.get(workspace_id))
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
    sanitized = scrub_raw_ids(text.replace(notebook_id, ""))
    return " ".join(sanitized.split())


def _event_message(event: dict) -> str:
    reason = first_non_empty_str(event.get("reason"))
    message = first_non_empty_str(event.get("message"), event.get("content"))
    event_type = first_non_empty_str(event.get("type"))
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


def resolve_created_notebook_id(
    *,
    name: str,
    workspace_id: str,
    session: WebSession,
) -> str:
    return notebook_services.resolve_created_notebook_id(
        name=name, workspace_id=workspace_id, session=session,
        user_ids_loader=lambda: _try_get_current_user_ids(session, base_url=get_base_url()),
        list_loader=_list_notebooks_for_workspace,
    )


# Preserve the CLI patch point used by creation tests.
_resolve_created_notebook_id = resolve_created_notebook_id


def _reject_taken_notebook_name(
    ctx: Context,
    *,
    name: str,
    workspace_id: str,
    session: WebSession,
) -> None:
    """Stop before adding a second notebook under a name already in use.

    The platform does not enforce the answer — ``CreateNotebook`` accepts a
    duplicate name and hands back a second notebook carrying it — but the CLI
    addresses notebooks by name, so the duplicate turns every later
    ``inspire notebook <verb> <name>`` into an ambiguity that needs ``--pick``
    to resolve. Catching it here costs one request and leaves the workspace
    addressable.

    A pre-check that could not reach the platform is not evidence the name is
    free, so it steps aside rather than blocking the create the caller asked
    for.
    """
    try:
        taken = browser_api_module.notebook_name_exists(
            name,
            workspace_id=workspace_id,
            session=session,
        )
    except Exception:  # noqa: BLE001 - an advisory check must not block the create
        logger.debug("Notebook name pre-check failed for %r", name, exc_info=True)
        return

    if not taken:
        return

    _handle_error(
        ctx,
        "ValidationError",
        f"A notebook named '{name}' already exists in this workspace.",
        EXIT_VALIDATION_ERROR,
        hint=(
            "Choose a different --name. The platform would accept the duplicate, "
            "but both notebooks would then answer to the same name and every "
            "later inspire notebook command would need --pick."
        ),
    )


def resolve_notebook_project(
    ctx: Context,
    *,
    projects: list,
    config: Config,
    project: str | None,
    needs_gpu_quota: bool,
    json_output: bool,
    workspace_id: str | None = None,
    session: WebSession | None = None,
) -> Any | None:
    project_value = (
        reject_id_at_boundary(
            ctx,
            project,
            resource_type="project",
            list_command="inspire project list",
        )
        if project
        else project
    )
    if project_value:
        try:
            project_value = resolve_project(
                config,
                project_value,
                projects,
            ).name
        except ConfigError as e:
            # An unknown --project is ordinary user input, not a crash. `job
            # create` and `hpc create` already answer it with one line; this
            # path used to let the ConfigError reach the top and print a
            # traceback above the same message.
            _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
            return None

    try:
        selected_project, selection_message = notebook_services.resolve_notebook_project(
            projects=projects, config=config, project=project_value,
            needs_gpu_quota=needs_gpu_quota, workspace_id=workspace_id, session=session,
            api=browser_api_module,
        )

        if not json_output:
            if selection_message:
                click.echo(selection_message)
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
        _handle_error(ctx, "ValidationError", error_msg, EXIT_CONFIG_ERROR)
        return None

    return selected_project


def resolve_notebook_image(
    ctx: Context,
    *,
    images: list,
    image: Optional[str],
    json_output: bool,
) -> Any | None:
    selected_image = None

    if image:
        selected_image = (
            notebook_services.resolve_notebook_image(images, image)
            if find_image_match(images, image) else None
        )
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
    dataset_mounts: Sequence[DatasetMount] = (),
    dataset_info: Optional[list[dict[str, str]]] = None,
    enable_notification: Optional[bool] = None,
    stop_hour: Optional[int] = None,
    stop_minute: Optional[int] = None,
    public_path_readonly: Optional[bool] = None,
    project_path_readonly: Optional[bool] = None,
) -> str | None:
    try:
        result = browser_api_module.create_notebook(
            **build_notebook_create_kwargs(
                name=name, project_id=selected_project.project_id, project_name=selected_project.name,
                image_id=selected_image.image_id, image_url=selected_image.url,
                quota=quota, shm_size=shm_size, auto_stop=auto_stop, workspace_id=workspace_id,
                task_priority=task_priority, node_id=node_id, dataset_info=dataset_info,
                enable_notification=enable_notification, stop_hour=stop_hour, stop_minute=stop_minute,
                public_path_readonly=public_path_readonly, project_path_readonly=project_path_readonly,
            ),
            session=session,
        )

        notebook_id = extract_notebook_id(result)
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
        remember_resource_identity(
            session=session,
            resource_type="notebook",
            resource_id=notebook_id,
            name=name,
            workspace_id=workspace_id,
            owner_scope="self",
        )

        if json_output:
            extra: dict[str, Any] = {}
            if dataset_mounts:
                extra["datasets"] = dataset_mount_views(dataset_mounts)
            click.echo(json_formatter.format_json(public_operation(name, "created", **extra)))
        else:
            click.echo(human_formatter.format_mutation_success("Notebook", "created", name))
            for line in describe_dataset_mounts(dataset_mounts):
                click.echo(f"Dataset: {line}")

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
        sub_status = first_non_empty_str(detail.get("sub_status"))
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

    try:
        started = browser_api_module.run_command_in_notebook(
            notebook_id=notebook_id,
            command=post_start_spec.command,
            session=session,
            timeout=20,
            completion_marker=post_start_spec.completion_marker,
        )
        if not json_output and started:
            if diagnostics is not None:
                click.echo(
                    human_formatter.format_mutation_success(
                        "Notebook post-start",
                        "started",
                        diagnostics.name,
                    )
                )
            else:
                click.echo(human_formatter.format_success("Notebook post-start started"))
        if not json_output and not started:
            subject = (
                f" for '{scrub_raw_ids(diagnostics.name)}'"
                if diagnostics is not None
                else ""
            )
            click.echo(
                f"Warning: Could not confirm notebook post-start{subject}.",
                err=True,
            )
    except Exception:
        if not json_output:
            subject = (
                f" for '{scrub_raw_ids(diagnostics.name)}'"
                if diagnostics is not None
                else ""
            )
            click.echo(
                f"Warning: Notebook post-start failed{subject}.",
                err=True,
            )


def _fetch_workspace_projects(
    ctx: Context,
    *,
    workspace_id: str,
    session: WebSession,
) -> list[Any] | None:
    try:
        projects = browser_api_module.list_projects(workspace_id=workspace_id, session=session)
    except Exception:
        logger.debug("Failed to fetch notebook projects", exc_info=True)
        _handle_error(ctx, "APIError", "Could not load notebook projects.", EXIT_API_ERROR)
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
    except Exception:
        logger.debug("Failed to fetch notebook images", exc_info=True)
        _handle_error(ctx, "APIError", "Could not load notebook images.", EXIT_API_ERROR)
        return None

    if image and not find_image_match(images, image):
        for source in ("SOURCE_PUBLIC", "SOURCE_PRIVATE"):
            try:
                extra_images = browser_api_module.list_images(
                    workspace_id=workspace_id, source=source, session=session
                )
                if extra_images:
                    if ctx.debug and not json_output:
                        click.echo(f"Searching {source.lower().replace('source_', '')} images...")
                    images = images + extra_images
                    if find_image_match(images, image):
                        break
            except TransientAPIError:
                # An unsearched source must not become "no such image": the
                # caller matches `image` against whatever this returns.
                logger.debug("Notebook image source unavailable", exc_info=True)
                _handle_error(
                    ctx,
                    "APIError",
                    "Could not load notebook images: the platform is rate "
                    "limiting or unavailable. Retry in a moment.",
                    EXIT_API_ERROR,
                )
                return None
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
    dataset_mounts: Sequence[DatasetMount] = (),
    enable_notification: Optional[bool] = None,
    auto_stop_after: Optional[int] = None,
    public_path_readonly: Optional[bool] = None,
    project_path_readonly: Optional[bool] = None,
) -> None:
    del project_explicit
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )
    config = load_config(ctx)

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
        quota, project, image, shm_size = resolve_create_inputs(
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
            "--group is required.",
            EXIT_CONFIG_ERROR,
        )
        return
    if not workspace and not workspace_id:
        _handle_error(
            ctx,
            "ValidationError",
            "--workspace is required.",
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
    if ctx.debug and not json_output:
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
    if task_priority != uncapped_priority and ctx.debug and not json_output:
        click.echo(
            f"Capping priority {uncapped_priority} -> {task_priority} "
            f"(max for project '{scrub_raw_ids(selected_project.name)}')"
        )

    try:
        ensure_priority_allowed(
            resolved_quota, task_priority, quota_command="inspire notebook quota"
        )
    except QuotaMatchError as err:
        _handle_error(ctx, "ValidationError", str(err), EXIT_VALIDATION_ERROR)
        return

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

    if ctx.debug and not json_output:
        click.echo(f"Using image: {scrub_raw_ids(selected_image.name)}")

    name = _resolve_notebook_name(name, json_output=json_output)
    _reject_taken_notebook_name(
        ctx,
        name=name,
        workspace_id=workspace_id,
        session=session,
    )
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

    # Resolving the mounts is the platform's own 校验数据 round trip: it fills
    # in the storage path each entry needs and turns a typo into an error here
    # rather than into a notebook that never starts.
    try:
        dataset_info = resolve_dataset_info(
            dataset_mounts,
            workspace_id=workspace_id,
            session=session,
        )
    except DatasetSpecError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "APIError", f"Could not check the dataset mounts: {e}", EXIT_API_ERROR)
        return

    stop_hour, stop_minute = split_auto_stop_after(auto_stop_after)

    notebook_id = create_notebook_and_report(
        ctx,
        name=name,
        diagnostics=diagnostics,
        selected_project=selected_project,
        selected_image=selected_image,
        quota=resolved_quota,
        shm_size=shm_size,
        auto_stop=auto_stop or auto_stop_after is not None,
        workspace_id=workspace_id,
        session=session,
        json_output=json_output,
        task_priority=task_priority,
        node_id=node,
        dataset_mounts=dataset_mounts,
        dataset_info=dataset_info or None,
        enable_notification=enable_notification,
        stop_hour=stop_hour,
        stop_minute=stop_minute,
        public_path_readonly=public_path_readonly,
        project_path_readonly=project_path_readonly,
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
        notebook_id=notebook_id,
        diagnostics=diagnostics,
        session=session,
        post_start_spec=post_start_spec,
        gpu_count=resolved_quota.gpu_count,
        json_output=json_output,
    )


__all__ = ["run_notebook_create", "maybe_run_post_start", "format_quota_display"]
