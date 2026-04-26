"""Notebook creation flow for `inspire notebook create`."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

import click

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import load_config, require_web_session, resolve_json_output
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
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import NotebookFailedError
from inspire.platform.web.session import WebSession


def format_quota_display(quota: ResolvedQuota) -> str:
    if quota.gpu_count > 0:
        label = quota.gpu_type or "GPU"
        return f"{quota.gpu_count}x{label} + {quota.cpu_count}CPU + {quota.memory_gib}GiB"
    return f"{quota.cpu_count}CPU + {quota.memory_gib}GiB"


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
) -> object | None:
    project_value = project
    if project_value and not project_value.startswith("project-"):
        for alias, project_id in (config.projects or {}).items():
            if alias.lower() == project_value.lower():
                project_value = project_id
                break

    try:
        shared_groups = getattr(config, "project_shared_path_groups", None)
        if not isinstance(shared_groups, dict) or not shared_groups:
            shared_groups = None

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
            shared_path_group_by_id=shared_groups,
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


def _find_image_match(images: list, image: str) -> object | None:
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
) -> object | None:
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
                choice = click.prompt("\nSelect image", type=int, default=default_idx)
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
    selected_project,
    selected_image,
    quota: ResolvedQuota,
    shm_size: int,
    auto_stop: bool,
    workspace_id: str,
    session: WebSession,
    json_output: bool,
    task_priority: Optional[int] = None,
) -> str | None:
    try:
        resource_spec_price = build_resource_spec_price(quota=quota)
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
        )

        notebook_id = result.get("notebook_id", "")

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
            click.echo(f"  ID: {notebook_id}")
            click.echo(f"  Name: {name}")
            click.echo(f"  Resource: {resource_display}")
            click.echo(f"  Compute group: {quota.compute_group_name}")

        return notebook_id

    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to create notebook: {e}", EXIT_API_ERROR)
        return None


def maybe_wait_for_running(
    ctx: Context,
    *,
    notebook_id: str,
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
        msg = f"Notebook failed to start: {e}"
        hint_parts = []
        if e.events:
            hint_parts.append(e.events)
        extra = e.detail.get("extra_info") or {}
        for key in ("NodeName", "HostIP"):
            if extra.get(key):
                hint_parts.append(f"{key}: {extra[key]}")
        if not hint_parts:
            hint_parts.append(
                "Check Events tab in web UI: Jobs > Interactive Modeling > notebook detail"
            )
        _handle_error(ctx, "NotebookFailed", msg, EXIT_API_ERROR, hint="\n".join(hint_parts))
        return False
    except TimeoutError as e:
        _handle_error(
            ctx,
            "Timeout",
            f"Timed out waiting for notebook to reach RUNNING: {e}",
            EXIT_API_ERROR,
        )
        return False


def maybe_run_post_start(
    ctx: Context,
    *,
    notebook_id: str,
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
        click.echo(f"Starting {post_start_spec.label}...")

    try:
        started = browser_api_module.run_command_in_notebook(
            notebook_id=notebook_id,
            command=post_start_spec.command,
            session=session,
            timeout=20,
            completion_marker=post_start_spec.completion_marker,
        )
        if not json_output and started:
            click.echo(f"{post_start_spec.label} started (log: {post_start_spec.log_path})")
            click.echo(f'  To stop: inspire notebook exec "kill $(cat {post_start_spec.pid_file})"')
        if not json_output and not started:
            click.echo(
                f"Warning: Failed to confirm {post_start_spec.label.lower()} startup; check "
                f"{post_start_spec.log_path} inside the notebook.",
                err=True,
            )
    except Exception as e:
        if not json_output:
            click.echo(f"Warning: Failed to start {post_start_spec.label.lower()}: {e}", err=True)


def _resolve_create_inputs(
    *,
    config: Config,
    quota: str | None,
    project: str | None,
    image: str | None,
    shm_size: int | None,
) -> tuple[str, str | None, str | None, int]:
    if not quota:
        quota = config.notebook_quota
    if not quota:
        raise ValueError(
            "--quota is required (pass --quota gpu,cpu,mem or set [notebook].quota in config.toml)."
        )
    if not project and not config.project_order:
        project = config.job_project_id
    if not image:
        image = config.notebook_image or config.job_image
    if shm_size is None:
        shm_size = config.shm_size if config.shm_size is not None else 32
    if shm_size < 1:
        raise ValueError("Shared memory size must be >= 1.")
    return quota, project, image, shm_size


def _resolve_task_priority(priority: Optional[int], config: Config) -> Optional[int]:
    if priority is not None:
        return priority
    return config.job_priority if hasattr(config, "job_priority") else None


def _fetch_workspace_projects(
    ctx: Context,
    *,
    workspace_id: str,
    session: WebSession,
) -> list | None:
    try:
        projects = browser_api_module.list_projects(workspace_id=workspace_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to fetch projects: {e}", EXIT_API_ERROR)
        return None

    if projects:
        return projects

    _handle_error(ctx, "ConfigError", "No projects available in this workspace", EXIT_CONFIG_ERROR)
    return None


def _cap_task_priority(
    *,
    task_priority: Optional[int],
    selected_project,
    json_output: bool,
) -> Optional[int]:
    if not selected_project.priority_name:
        return task_priority

    try:
        max_priority = int(selected_project.priority_name)
    except ValueError:
        return task_priority

    if task_priority is None or task_priority <= max_priority:
        return task_priority

    if not json_output:
        click.echo(
            f"Capping priority {task_priority} → {max_priority} "
            f"(max for project '{selected_project.name}')"
        )
    return max_priority


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
        _handle_error(ctx, "APIError", f"Failed to fetch images: {e}", EXIT_API_ERROR)
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
    generated = f"notebook-{uuid.uuid4().hex[:8]}"
    if not json_output:
        click.echo(f"Generated name: {generated}")
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
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return None

    if not resolved:
        resolved = session.workspace_id

    if not resolved:
        _handle_error(
            ctx,
            "ConfigError",
            "No workspace_id configured.",
            EXIT_CONFIG_ERROR,
            hint="Set [context].workspace in config.toml, or pass --workspace <name>.",
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
) -> None:
    del project_explicit
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Creating notebooks requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
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
        click.echo(
            f"Creating notebook with {resource_display} on {resolved_quota.compute_group_name}..."
        )

    task_priority = _resolve_task_priority(priority, config)
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

    task_priority = _cap_task_priority(
        task_priority=task_priority,
        selected_project=selected_project,
        json_output=json_output,
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
        click.echo(f"Using image: {selected_image.name}")

    name = _resolve_notebook_name(name, json_output=json_output)

    notebook_id = create_notebook_and_report(
        ctx,
        name=name,
        resource_display=resource_display,
        selected_project=selected_project,
        selected_image=selected_image,
        quota=resolved_quota,
        shm_size=shm_size,
        auto_stop=auto_stop,
        workspace_id=workspace_id,
        session=session,
        json_output=json_output,
        task_priority=task_priority,
    )
    if not notebook_id:
        return

    if not maybe_wait_for_running(
        ctx,
        notebook_id=notebook_id,
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
        session=session,
        post_start_spec=post_start_spec,
        gpu_count=resolved_quota.gpu_count,
        json_output=json_output,
    )

    if not json_output:
        click.echo(f"\nUse 'inspire notebook status {notebook_id}' to check status.")


__all__ = ["run_notebook_create", "maybe_run_post_start", "format_quota_display"]
