"""Image subcommands."""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import (
    forget_resource_identity,
    remember_resource_identity,
    resolve_by_name,
)
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    require_web_session,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import ConfigError
from inspire.config.workspaces import resolve_workspace_operation_scope
from inspire.platform.web import browser_api as browser_api_module


def _resolve_image_name(
    ctx: Context,
    name: str,
    *,
    pick: Optional[int] = None,
    session=None,  # noqa: ANN001
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
        for source in ("private", "public", "official"):
            try:
                imgs = browser_api_module.list_images_by_source(source=source, session=session)
            except Exception:
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
        return bucket

    return resolve_by_name(
        ctx,
        name=name,
        resource_type="image",
        list_candidates=_lister,
        json_output=ctx.json_output,
        pick_index=pick,
        session=session,
        workspace_id=str(getattr(session, "workspace_id", "") or ""),
        owner_scope="self",
        require_live=require_live,
        list_command="inspire image list",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PUBLIC_SOURCE_CHOICES = ("official", "public", "private", "all")
_ALL_SOURCE_KEYS = ("official", "public", "private")


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


def _image_detail_view(img: browser_api_module.CustomImageInfo) -> dict[str, str]:
    view = _image_summary(img)
    optional = {
        "registry": scrub_raw_ids(img.url),
        "description": scrub_raw_ids(img.description),
        "created_at": scrub_raw_ids(img.created_at),
    }
    view.update({key: value for key, value in optional.items() if value})
    return view


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
        ("Registry", "registry"),
        ("Description", "description"),
        ("Created", "created_at"),
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
@pass_context
def list_images_cmd(
    ctx: Context,
    source: str,
) -> None:
    """List available Docker images.

    \b
    Examples:
        inspire image list                     # All visible images
        inspire image list --source public     # Public images
        inspire image list --source private    # Personal-visible images
        inspire image list --source official   # Official images
        inspire --json image list              # JSON output
    """
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    images: list[browser_api_module.CustomImageInfo] = []
    warnings: list[str] = []

    try:
        if source == "all":
            for src_key in _ALL_SOURCE_KEYS:
                try:
                    items = browser_api_module.list_images_by_source(
                        source=src_key, session=session
                    )
                except Exception as e:
                    warnings.append(
                        f"{src_key} image catalog unavailable: {scrub_raw_ids(e)}"
                    )
                    continue
                images.extend(items)

            images = _dedupe_images_by_id(images)

            if not images and warnings:
                raise ValueError("; ".join(warnings))
        else:
            items = browser_api_module.list_images_by_source(source=source, session=session)
            images.extend(items)
    except Exception as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to list images: {scrub_raw_ids(e)}",
            EXIT_API_ERROR,
        )
        return

    results = [_image_summary(image) for image in images]
    if ctx.json_output:
        payload: dict[str, object] = {"images": results}
        if warnings:
            payload["warnings"] = warnings
        click.echo(json_formatter.format_json(payload))
        return

    for warning in warnings:
        click.echo(f"Warning: {warning}", err=True)

    click.echo(_format_image_list(results))


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


@click.command("detail")
@click.argument("name")
@pass_context
def image_detail(
    ctx: Context,
    name: str,
) -> None:
    """Show detailed information about an image.

    NAME is the image's ``<name>:<version>`` (or just ``<name>`` if unambiguous).

    \b
    Examples:
        inspire image detail my-image:v1
        inspire --json image detail unified-base:v2
    """
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    image_id = _resolve_image_name(ctx, name, session=session)

    try:
        image = browser_api_module.get_image_detail(image_id=image_id, session=session)
    except Exception as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to get image detail: {scrub_raw_ids(e)}",
            EXIT_API_ERROR,
        )
        return

    view = _image_detail_view(image)
    remember_resource_identity(
        session=session,
        resource_type="image",
        resource_id=image.image_id,
        name=_image_label(image),
        workspace_id=str(getattr(session, "workspace_id", "") or ""),
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
    help="Image name (lowercase, digits, dashes, dots, underscores)",
)
@click.option(
    "--version",
    "-v",
    required=True,
    help="Image version tag (e.g., v1.0)",
)
@click.option(
    "--description",
    "-d",
    default="",
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
    version: str,
    description: str,
    visibility: str,
    method: str,
    wait: bool,
) -> None:
    """Register an external Docker image on the platform.

    This is for images you built outside the platform. To save a running
    notebook as an image, use 'inspire image save' instead.

    \b
    Push workflow (default):
      1. inspire image register -n my-img -v v1.0
      2. docker tag <local-image> <registry-url>   (shown in output)
      3. docker push <registry-url>
      4. Platform detects the push and marks the image READY.

    \b
    Address workflow:
      Register an image already hosted on a public/private registry.
      inspire image register -n my-img -v v1.0 --method address

    \b
    Examples:
        inspire image register -n my-pytorch -v v1.0
        inspire image register -n my-img -v v2.0 --method address
        inspire image register -n my-img -v v1.0 --visibility public --wait
    """
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    visibility_value = (
        "VISIBILITY_PUBLIC" if visibility.lower() == "public" else "VISIBILITY_PRIVATE"
    )
    add_method_value = 2 if method.lower() == "address" else 0

    try:
        result = browser_api_module.create_image(
            name=name,
            version=version,
            description=description,
            visibility=visibility_value,
            add_method=add_method_value,
            session=session,
        )
    except Exception as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to register image: {scrub_raw_ids(e)}",
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
        workspace_id=str(getattr(session, "workspace_id", "") or ""),
        owner_scope="self",
    )

    ready = False
    if wait and image_id:
        try:
            browser_api_module.wait_for_image_ready(image_id=image_id, session=session)
            ready = True
        except (TimeoutError, ValueError) as e:
            _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
            return

    if ctx.json_output:
        payload = {
            "name": image_label,
            "status": "ready" if ready else "registered",
            "visibility": visibility.lower(),
        }
        if registry_url and method.lower() == "push":
            payload["registry"] = scrub_raw_ids(registry_url)
        click.echo(json_formatter.format_json(payload))
        return

    click.echo(f"Image {'ready' if ready else 'registered'}: {image_label}")
    if registry_url and method.lower() == "push":
        safe_registry_url = scrub_raw_ids(registry_url)
        click.echo(f"docker tag <local-image> {safe_registry_url}")
        click.echo(f"docker push {safe_registry_url}")


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


_VISIBILITY_PUBLIC = "VISIBILITY_PUBLIC"
_VISIBILITY_PRIVATE = "VISIBILITY_PRIVATE"


def _parse_visibility_value(visibility: Optional[str]) -> Optional[str]:
    if visibility is None:
        return None
    return _VISIBILITY_PUBLIC if visibility.lower() == "public" else _VISIBILITY_PRIVATE


@click.command("save")
@click.argument("notebook")
@click.option("--workspace", required=True, help="Workspace name.")
@click.option(
    "--name",
    "-n",
    required=True,
    help="Name for the saved image",
)
@click.option(
    "--version",
    "-v",
    default="v1",
    show_default=True,
    help="Image version tag",
)
@click.option(
    "--description",
    "-d",
    default="",
    help="Image description",
)
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Wait for image to reach READY status",
)
@click.option(
    "--visibility",
    type=click.Choice(["private", "public"], case_sensitive=False),
    default=None,
    help="Image visibility. Omit to accept the platform default.",
)
@pass_context
def save_image_cmd(
    ctx: Context,
    notebook: str,
    workspace: str,
    name: str,
    version: str,
    description: str,
    wait: bool,
    visibility: Optional[str],
) -> None:
    """Save a running notebook as a custom Docker image.

    NOTEBOOK is the notebook name (from `inspire notebook list`).
    Use this after configuring and validating a notebook environment, including
    SII internal mirrors when they are reachable from the target GPU area.
    `image save` starts a medium-length image-saving process. While it is
    running, the notebook cannot be operated. Saving an image does not stop or
    delete the notebook; after saving completes, the notebook remains available
    for normal use.

    \b
    Examples:
        inspire image save my-notebook --workspace 分布式训练空间 -n my-saved-image
        inspire image save my-notebook --workspace 分布式训练空间 -n my-img -v v2 --wait
        inspire image save my-notebook --workspace 分布式训练空间 -n shared-base -v v1 --visibility public
    """
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    # Resolve the notebook name through the notebook
    # resolver, which rejects handle-shaped normal CLI inputs.
    from inspire.cli.commands.notebook.notebook_lookup import _resolve_notebook_id
    from inspire.cli.utils.notebook_cli import get_base_url, load_config

    config = load_config(ctx)
    try:
        workspace_id = resolve_workspace_operation_scope(
            config,
            workspace=workspace,
            session=session,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    base_url = get_base_url()
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=ctx.json_output,
        workspace_ids=[workspace_id],
        require_live=True,
    )

    requested_visibility = _parse_visibility_value(visibility)

    try:
        result = browser_api_module.save_notebook_as_image(
            notebook_id=notebook_id,
            name=name,
            version=version,
            description=description,
            session=session,
        )
    except Exception as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to save notebook as image: {scrub_raw_ids(e)}",
            EXIT_API_ERROR,
        )
        return

    image_id = result.get("image", {}).get("image_id", "") or result.get("image_id", "")
    image_label = scrub_raw_ids(f"{name}:{version}")

    if not image_id:
        try:
            want_suffix_1 = f"/{name}:{version}"
            want_name_1 = f"{name}:{version}"
            matches = []
            for img in browser_api_module.list_images_by_source(
                source="private", session=session
            ):
                img_name = (img.name or "").strip()
                img_url = (img.url or "").strip()
                img_version = (img.version or "").strip()
                # The API sometimes puts name as "foo" + version "v1", other
                # times name as "foo:v1"; URL always ends in "/<ns>/foo:v1".
                if (
                    (img_name == name and img_version == version)
                    or img_name == want_name_1
                    or img_url.endswith(want_suffix_1)
                ):
                    matches.append(img)
            if matches:
                matches.sort(key=lambda img: img.created_at or "", reverse=True)
                image_id = matches[0].image_id
        except Exception:
            pass

    remember_resource_identity(
        session=session,
        resource_type="image",
        resource_id=image_id,
        name=image_label,
        workspace_id=str(getattr(session, "workspace_id", "") or ""),
        owner_scope="self",
    )

    visibility_applied = requested_visibility is None
    if requested_visibility and image_id:
        try:
            browser_api_module.update_image(
                image_id=image_id,
                visibility=requested_visibility,
                session=session,
            )
            visibility_applied = True
        except Exception as e:
            visibility_applied = False
            if not ctx.json_output:
                click.echo(
                    f"Warning: could not set image visibility: {scrub_raw_ids(e)}",
                    err=True,
                )
    elif requested_visibility and not image_id:
        visibility_applied = False
        if not ctx.json_output:
            click.echo(
                "Warning: image visibility was not applied. "
                f"After the image appears, run 'inspire image set-visibility {image_label} "
                "--visibility <private|public>'.",
                err=True,
            )

    ready = False
    if wait and image_id:
        try:
            browser_api_module.wait_for_image_ready(image_id=image_id, session=session)
            ready = True
        except (TimeoutError, ValueError) as e:
            _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
            return

    if ctx.json_output:
        payload: dict[str, object] = {
            "name": image_label,
            "status": "ready" if ready else "saving",
        }
        if visibility:
            payload["visibility"] = visibility.lower()
            payload["visibility_applied"] = visibility_applied
        click.echo(json_formatter.format_json(payload))
        return

    click.echo(f"Image {'ready' if ready else 'saving'}: {image_label}")
    if requested_visibility and image_id:
        label = "public" if requested_visibility == _VISIBILITY_PUBLIC else "private"
        click.echo(f"Visibility: {label}")


# ---------------------------------------------------------------------------
# set-visibility
# ---------------------------------------------------------------------------


@click.command("set-visibility")
@click.argument("name")
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
    visibility: str,
) -> None:
    """Flip an existing custom image's visibility (public ↔ private).

    \b
    Examples:
        inspire image set-visibility my-image:v1 --visibility public
        inspire image set-visibility my-image:v1 --visibility private
    """
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    image_id = _resolve_image_name(
        ctx,
        name,
        session=session,
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
    except Exception as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to update image visibility: {scrub_raw_ids(e)}",
            EXIT_API_ERROR,
        )
        return

    label = "public" if visibility_value == _VISIBILITY_PUBLIC else "private"
    remember_resource_identity(
        session=session,
        resource_type="image",
        resource_id=image_id,
        name=name,
        workspace_id=str(getattr(session, "workspace_id", "") or ""),
        owner_scope="self",
    )
    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {"name": scrub_raw_ids(name), "visibility": label}
            )
        )
        return

    click.echo(f"Image '{scrub_raw_ids(name)}' visibility set to {label}.")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@click.command("delete")
@click.argument("name")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt.",
)
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous.",
)
@pass_context
def delete_image_cmd(
    ctx: Context,
    name: str,
    yes: bool,
    pick: Optional[int],
) -> None:
    """Delete a custom Docker image (pass ``<name>:<version>``).

    \b
    Examples:
        inspire image delete my-image:v1
        inspire image delete my-image:v1 --yes
    """
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    image_id = _resolve_image_name(
        ctx,
        name,
        pick=pick,
        session=session,
        require_live=True,
    )

    if not yes and not ctx.json_output:
        if not click.confirm(f"Delete image '{scrub_raw_ids(name)}'?"):
            click.echo("Cancelled.")
            return

    try:
        browser_api_module.delete_image(image_id=image_id, session=session)
    except Exception as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to delete image: {scrub_raw_ids(e)}",
            EXIT_API_ERROR,
        )
        return

    forget_resource_identity(
        session=session,
        resource_type="image",
        resource_id=image_id,
        name=name,
        workspace_id=str(getattr(session, "workspace_id", "") or ""),
        owner_scope="self",
    )

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {"name": scrub_raw_ids(name), "status": "deleted"}
            )
        )
        return

    click.echo(f"Image '{scrub_raw_ids(name)}' has been deleted.")


__all__ = [
    "delete_image_cmd",
    "image_detail",
    "list_images_cmd",
    "register_image_cmd",
    "save_image_cmd",
    "set_image_visibility_cmd",
]
