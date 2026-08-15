"""Image subcommands."""

from __future__ import annotations

from typing import Any, Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.human_formatter import format_mutation_success
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.collection_output import (
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
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    require_web_session,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import ConfigError
from inspire.config.workspaces import resolve_workspace_operation_scope
from inspire.platform.web import browser_api as browser_api_module


_IMAGE_LIST_COMMAND = "inspire image list --workspace <workspace-name>"

_WORKSPACE_HELP = "Workspace name. Images live in this workspace's image registry."


def _resolve_registry_scope(
    ctx: Context,
    *,
    workspace: str,
    session: Any,
) -> str | None:
    """Resolve ``--workspace`` to the image registry a command addresses.

    Images are stored per workspace: every ``ListImages`` / ``CreateImage``
    request carries ``registry_hint: {workspace_id}``. The session's active
    workspace is *not* a safe default — this account's is an empty registry
    while its 67 custom images live in another — so every image command takes
    the workspace explicitly and threads this id down to the platform call.

    Returns the workspace id, or ``None`` once the failure has been reported.
    """
    try:
        workspace_id = resolve_workspace_operation_scope(
            workspace=workspace,
            session=session,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return None
    return workspace_id


def _resolve_image_name(
    ctx: Context,
    name: str,
    *,
    pick: Optional[int] = None,
    session=None,  # noqa: ANN001
    workspace_id: str,
    require_live: bool = False,
) -> str:
    """Resolve a custom-image name (``<name>:<version>`` or bare ``<name>``) to image_id.

    Custom images are identified by ``name:version`` on the platform; a plain
    name without ``:`` matches any version but can be ambiguous and will
    fall through to the shared ambiguity UI.
    """
    if session is None:
        session = require_web_session(ctx, hint=WEB_AUTH_HINT)

    def _lister():
        bucket = []
        failed_sources: list[str] = []
        for source in ("private", "public", "official"):
            try:
                imgs = browser_api_module.list_images_by_source(
                    source=source, session=session, workspace_id=workspace_id
                )
            except Exception:
                failed_sources.append(source)
                continue
            for i in imgs:
                full = f"{i.name}" if ":" in (i.name or "") else f"{i.name}:{i.version}" if i.version else i.name
                bucket.append(
                    {
                        "name": full,
                        "id": i.image_id,
                        "status": i.status,
                    }
                )
        if failed_sources and not any(
            candidate["name"] == name for candidate in bucket
        ):
            if len(failed_sources) == len(_ALL_SOURCE_KEYS):
                raise RuntimeError("Image catalog is unavailable.")
            raise RuntimeError("Image catalog lookup is incomplete.")
        return bucket

    return resolve_by_name(
        ctx,
        name=name,
        resource_type="image",
        list_candidates=_lister,
        pick_index=pick,
        session=session,
        workspace_id=workspace_id,
        owner_scope="self",
        require_live=require_live,
        list_command=_IMAGE_LIST_COMMAND,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PUBLIC_SOURCE_CHOICES = ("official", "public", "private", "all")
_ALL_SOURCE_KEYS = ("official", "public", "private")

_VISIBILITY_PUBLIC = "VISIBILITY_PUBLIC"
_VISIBILITY_PRIVATE = "VISIBILITY_PRIVATE"


def _parse_visibility_value(visibility: Optional[str]) -> Optional[str]:
    if visibility is None:
        return None
    return _VISIBILITY_PUBLIC if visibility.lower() == "public" else _VISIBILITY_PRIVATE


def _parse_source_value(_ctx: click.Context, _param: click.Parameter, value: str) -> str:
    normalized = value.strip().lower()
    if normalized in _PUBLIC_SOURCE_CHOICES:
        return normalized
    allowed = ", ".join(_PUBLIC_SOURCE_CHOICES)
    raise click.BadParameter(f"invalid source '{value}'. Choose one of: {allowed}")


def _image_label(img: browser_api_module.CustomImageInfo) -> str:
    name = str(img.name or "").strip()
    version = str(img.version or "").strip()
    if version and ":" not in name:
        return f"{name}:{version}"
    return name


def _image_visibility(source: str) -> str:
    return {
        "SOURCE_OFFICIAL": "official",
        "SOURCE_PUBLIC": "public",
        "SOURCE_PRIVATE": "private",
    }.get(str(source or "").strip(), "")


def _image_summary(img: browser_api_module.CustomImageInfo) -> dict[str, str]:
    """Return the compact, name-only image representation exposed by the CLI."""
    return {
        "name": scrub_raw_ids(_image_label(img)),
        "status": scrub_raw_ids(img.status),
        "framework": scrub_raw_ids(img.framework),
        "visibility": _image_visibility(img.source),
    }


def _format_image_list(images: list[dict[str, str]]) -> str:
    if not images:
        return "No images found."
    rows = [
        (
            image.get("name", ""),
            image.get("status", ""),
            image.get("framework", ""),
            image.get("visibility", ""),
        )
        for image in images
    ]
    widths = [
        column_width("Name", [row[0] for row in rows], max_width=64),
        column_width("Status", [row[1] for row in rows], max_width=18),
        column_width("Framework", [row[2] for row in rows], max_width=24),
        column_width("Visibility", [row[3] for row in rows], max_width=12),
    ]
    return "\n".join(
        render_table(
            ("Name", "Status", "Framework", "Visibility"),
            rows,
            widths,
            line_char="─",
        )
    )


def _format_image_detail(image: dict[str, str]) -> str:
    labels = (
        ("Name", "name"),
        ("Status", "status"),
        ("Framework", "framework"),
        ("Visibility", "visibility"),
    )
    return "\n".join(
        f"{label}: {image[key]}" for label, key in labels if image.get(key)
    )


def _dedupe_images_by_id(
    images: list[browser_api_module.CustomImageInfo],
) -> list[browser_api_module.CustomImageInfo]:
    """Deduplicate internal image records while preserving platform order."""
    deduped: list[browser_api_module.CustomImageInfo] = []
    seen_ids: set[str] = set()
    for image in images:
        image_id = str(image.image_id or "").strip()
        if image_id:
            if image_id in seen_ids:
                continue
            seen_ids.add(image_id)
        deduped.append(image)
    return deduped


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@click.command("list")
@click.option("--workspace", required=True, metavar="NAME", help=_WORKSPACE_HELP)
@click.option(
    "--source",
    "-s",
    type=str,
    callback=_parse_source_value,
    metavar="[official|public|private|all]",
    default="all",
    show_default=True,
    help="Image source filter",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum images to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every matching image.")
@pass_context
def list_images_cmd(
    ctx: Context,
    workspace: str,
    source: str,
    limit: int | None,
    show_all: bool,
) -> None:
    """List the Docker images visible in one workspace's registry."""
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    scope = _resolve_registry_scope(ctx, workspace=workspace, session=session)
    if scope is None:
        return
    workspace_id = scope

    images: list[browser_api_module.CustomImageInfo] = []
    warnings: list[str] = []

    try:
        if source == "all":
            for src_key in _ALL_SOURCE_KEYS:
                try:
                    items = browser_api_module.list_images_by_source(
                        source=src_key, session=session, workspace_id=workspace_id
                    )
                except Exception:
                    warnings.append(f"{src_key} image catalog unavailable.")
                    continue
                images.extend(items)

            images = _dedupe_images_by_id(images)

            if not images and warnings:
                _handle_error(
                    ctx,
                    "APIError",
                    "Image catalog is unavailable.",
                    EXIT_API_ERROR,
                )
                return
        else:
            items = browser_api_module.list_images_by_source(
                source=source, session=session, workspace_id=workspace_id
            )
            images.extend(items)
    except Exception:
        _handle_error(
            ctx,
            "APIError",
            "Could not list images.",
            EXIT_API_ERROR,
        )
        return

    results = [_image_summary(image) for image in images]
    page = bound_collection(results, limit=effective_limit)
    if ctx.json_output:
        payload: dict[str, object] = {
            "items": page.items,
            **page.metadata(),
        }
        if warnings:
            payload["warnings"] = warnings
        click.echo(json_formatter.format_json(payload))
        return

    for warning in warnings:
        click.echo(f"Warning: {warning}", err=True)

    click.echo(_format_image_list(page.items))
    notice = truncation_notice(page)
    if notice:
        click.echo(notice)


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


@click.command("detail")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help=_WORKSPACE_HELP)
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def image_detail(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
) -> None:
    """Show an image's status, framework, and visibility.

    NAME is the image name with an optional version tag, such as name:v1.
    """
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="image",
        list_command=_IMAGE_LIST_COMMAND,
    )
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    scope = _resolve_registry_scope(ctx, workspace=workspace, session=session)
    if scope is None:
        return
    workspace_id = scope

    try:
        image = run_with_stale_handle_retry(
            name=name,
            resolve_cached=lambda: _resolve_image_name(
                ctx,
                name,
                pick=pick,
                session=session,
                workspace_id=workspace_id,
            ),
            resolve_live=lambda live_name: _resolve_image_name(
                ctx,
                live_name,
                pick=pick,
                session=session,
                workspace_id=workspace_id,
                require_live=True,
            ),
            operation=lambda image_id: browser_api_module.get_image_detail(
                image_id=image_id,
                session=session,
            ),
            invalidate=lambda image_id: forget_resource_identity(
                session=session,
                resource_type="image",
                resource_id=image_id,
                workspace_id=workspace_id,
                owner_scope="self",
            ),
        )
    except Exception:
        _handle_error(
            ctx,
            "APIError",
            "Could not load image details.",
            EXIT_API_ERROR,
        )
        return

    view = _image_summary(image)
    remember_resource_identity(
        session=session,
        resource_type="image",
        resource_id=image.image_id,
        name=_image_label(image),
        workspace_id=workspace_id,
        owner_scope="self",
        status=image.status,
        created_at=image.created_at,
    )
    if ctx.json_output:
        click.echo(json_formatter.format_json(view))
        return

    click.echo(_format_image_detail(view))


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


@click.command("register")
@click.option(
    "--name",
    "-n",
    required=True,
    metavar="NAME",
    help="Image name (lowercase, digits, dashes, dots, underscores)",
)
@click.option("--workspace", required=True, metavar="NAME", help=_WORKSPACE_HELP)
@click.option(
    "--version",
    "-v",
    required=True,
    metavar="VERSION",
    help="Image version tag (e.g., v1.0)",
)
@click.option(
    "--description",
    "-d",
    default="",
    metavar="DESCRIPTION",
    help="Image description",
)
@click.option(
    "--visibility",
    type=click.Choice(["private", "public"], case_sensitive=False),
    default="private",
    show_default=True,
    help="Image visibility",
)
@click.option(
    "--method",
    type=click.Choice(["push", "address"], case_sensitive=False),
    default="push",
    show_default=True,
    help="'push': create a slot then docker-push your image; "
    "'address': register an image already hosted elsewhere",
)
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Wait for image to reach READY status",
)
@pass_context
def register_image_cmd(
    ctx: Context,
    name: str,
    workspace: str,
    version: str,
    description: str,
    visibility: str,
    method: str,
    wait: bool,
) -> None:
    """Register an external Docker image on the platform.

    Push mode prints the registry-specific docker tag and docker push commands.
    Address mode registers an image already hosted in a registry. Use notebook
    save-image for a running notebook.
    """
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    scope = _resolve_registry_scope(ctx, workspace=workspace, session=session)
    if scope is None:
        return
    workspace_id = scope

    visibility_value = _parse_visibility_value(visibility)
    assert visibility_value is not None
    add_method_value = 2 if method.lower() == "address" else 0

    try:
        result = browser_api_module.create_image(
            name=name,
            version=version,
            workspace_id=workspace_id,
            description=description,
            visibility=visibility_value,
            add_method=add_method_value,
            session=session,
        )
    except Exception:
        _handle_error(
            ctx,
            "APIError",
            "Could not register image.",
            EXIT_API_ERROR,
        )
        return

    image_data = result.get("image", {})
    image_id = image_data.get("image_id", "") or result.get("image_id", "")
    registry_url = image_data.get("address", "") or result.get("address", "")
    image_label = scrub_raw_ids(f"{name}:{version}")

    remember_resource_identity(
        session=session,
        resource_type="image",
        resource_id=image_id,
        name=image_label,
        workspace_id=workspace_id,
        owner_scope="self",
    )

    ready = False
    if wait and image_id:
        try:
            browser_api_module.wait_for_image_ready(image_id=image_id, session=session)
            ready = True
        except (TimeoutError, ValueError):
            _handle_error(
                ctx,
                "APIError",
                "Image did not become ready.",
                EXIT_API_ERROR,
            )
            return

    if ctx.json_output:
        payload = {
            "name": image_label,
            "status": "ready" if ready else "registered",
        }
        if registry_url and method.lower() == "push":
            payload["registry"] = scrub_raw_ids(registry_url)
        click.echo(json_formatter.format_json(payload))
        return

    click.echo(
        format_mutation_success(
            "Image",
            "ready" if ready else "registered",
            image_label,
        )
    )
    if registry_url and method.lower() == "push":
        safe_registry_url = scrub_raw_ids(registry_url)
        click.echo(f"docker tag <local-image> {safe_registry_url}")
        click.echo(f"docker push {safe_registry_url}")


# ---------------------------------------------------------------------------
# set-visibility
# ---------------------------------------------------------------------------


@click.command("set-visibility")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help=_WORKSPACE_HELP)
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--visibility",
    type=click.Choice(["private", "public"], case_sensitive=False),
    required=True,
    default=None,
    help="Target visibility.",
)
@pass_context
def set_image_visibility_cmd(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
    visibility: str,
) -> None:
    """Set a custom image's visibility."""
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="image",
        list_command=_IMAGE_LIST_COMMAND,
    )
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    scope = _resolve_registry_scope(ctx, workspace=workspace, session=session)
    if scope is None:
        return
    workspace_id = scope

    image_id = _resolve_image_name(
        ctx,
        name,
        pick=pick,
        session=session,
                workspace_id=workspace_id,
        require_live=True,
    )
    visibility_value = _parse_visibility_value(visibility)
    assert visibility_value is not None

    try:
        browser_api_module.update_image(
            image_id=image_id,
            visibility=visibility_value,
            session=session,
        )
    except Exception:
        _handle_error(
            ctx,
            "APIError",
            "Could not update image visibility.",
            EXIT_API_ERROR,
        )
        return

    remember_resource_identity(
        session=session,
        resource_type="image",
        resource_id=image_id,
        name=name,
        workspace_id=workspace_id,
        owner_scope="self",
    )
    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "name": scrub_raw_ids(name),
                    "status": "updated",
                }
            )
        )
        return

    click.echo(format_mutation_success("Image", "updated", name))


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@click.command("delete")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help=_WORKSPACE_HELP)
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
def delete_image_cmd(
    ctx: Context,
    name: str,
    workspace: str,
    yes: bool,
    pick: Optional[int],
) -> None:
    """Delete a custom Docker image by name and version."""
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="image",
        list_command=_IMAGE_LIST_COMMAND,
    )
    require_confirmation(
        ctx,
        yes=yes,
        prompt=f"Delete image '{scrub_raw_ids(name)}'?",
        message="Image deletion requires confirmation.",
    )
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    scope = _resolve_registry_scope(ctx, workspace=workspace, session=session)
    if scope is None:
        return
    workspace_id = scope

    image_id = _resolve_image_name(
        ctx,
        name,
        pick=pick,
        session=session,
                workspace_id=workspace_id,
        require_live=True,
    )

    try:
        browser_api_module.delete_image(image_id=image_id, session=session)
    except Exception:
        _handle_error(
            ctx,
            "APIError",
            "Could not delete image.",
            EXIT_API_ERROR,
        )
        return

    forget_resource_identity(
        session=session,
        resource_type="image",
        resource_id=image_id,
        name=name,
        workspace_id=workspace_id,
        owner_scope="self",
    )

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {"name": scrub_raw_ids(name), "status": "deleted"}
            )
        )
        return

    click.echo(format_mutation_success("Image", "deleted", name))


__all__ = [
    "delete_image_cmd",
    "image_detail",
    "list_images_cmd",
    "register_image_cmd",
    "set_image_visibility_cmd",
]
