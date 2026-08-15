"""`inspire notebook save-image` / `cancel-save-image` — snapshot a running notebook.

Saving is a notebook lifecycle event: the container is committed in place, so
the notebook is unusable for as long as the save runs and comes back afterwards
without being stopped. The custom image is the *product* of that event — manage
it afterwards with `inspire image list/detail/set-visibility/delete`.
"""

from __future__ import annotations

from typing import Optional

import click

# Shared with `inspire image set-visibility`, which applies the same mapping.
from inspire.cli.commands.image.image_commands import _parse_visibility_value

# Imported as a module, not as `from ... import _resolve_notebook_id`: the name
# is resolved per call so tests can patch it on `notebook_lookup` itself, the
# same way this file reaches the platform through `browser_api_module`. Binding
# it at import time silently breaks every test that stubs the resolver.
from inspire.cli.commands.notebook import notebook_lookup as notebook_lookup_module
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.human_formatter import format_mutation_success
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import (
    NAME_PICK_HELP,
    reject_id_at_boundary,
    remember_resource_identity,
)
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    get_base_url,
    require_web_session,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import ConfigError
from inspire.config.workspaces import resolve_workspace_operation_scope
from inspire.platform.web import browser_api as browser_api_module


_SIZE_UNITS: tuple[tuple[str, int], ...] = (
    ("TiB", 1024**4),
    ("GiB", 1024**3),
    ("MiB", 1024**2),
    ("KiB", 1024),
)


def _format_size_bytes(value: int) -> str:
    """Render the platform's snapshot estimate, which is a byte count."""
    for label, divisor in _SIZE_UNITS:
        if value >= divisor:
            return f"{value / divisor:.2f} {label}"
    return f"{value} B"


def _resolve_save_notebook_id(
    ctx: Context,
    *,
    notebook: str,
    workspace: str,
    pick: Optional[int],
    session,  # noqa: ANN001
) -> tuple[str, str] | None:
    """Resolve the notebook a save command targets, or report and return None.

    Both halves of the save flow address the same object the same way: a
    notebook name inside one named workspace, resolved live so a cached handle
    cannot send the request at a notebook that no longer exists.

    Returns ``(notebook_id, workspace_id)``. The workspace is not incidental:
    the saved image lands in *that* workspace's registry, so looking the image
    up afterwards has to read the same one.
    """
    try:
        workspace_id = resolve_workspace_operation_scope(
            workspace=workspace,
            session=session,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return None

    notebook_id, _ = notebook_lookup_module._resolve_notebook_id(
        ctx,
        session=session,
        base_url=get_base_url(),
        identifier=notebook,
        json_output=ctx.json_output,
        workspace_ids=[workspace_id],
        pick=pick,
        require_live=True,
    )
    return notebook_id, workspace_id


# ---------------------------------------------------------------------------
# save-image
# ---------------------------------------------------------------------------


@click.command("save-image")
@click.argument("notebook", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--name",
    "-n",
    required=True,
    metavar="NAME",
    help="Name for the saved image",
)
@click.option(
    "--version",
    "-v",
    default="v1",
    metavar="VERSION",
    show_default=True,
    help="Image version tag",
)
@click.option(
    "--description",
    "-d",
    default="",
    metavar="DESCRIPTION",
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
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the estimated snapshot size without saving anything.",
)
@pass_context
def save_image_cmd(
    ctx: Context,
    notebook: str,
    workspace: str,
    pick: Optional[int],
    name: str,
    version: str,
    description: str,
    wait: bool,
    visibility: Optional[str],
    dry_run: bool,
) -> None:
    """Save a running notebook as a custom Docker image.

    NAME is the notebook name from inspire notebook list. The notebook remains
    available after the save completes, but cannot be used while it runs, so
    the estimated snapshot size is printed first. Use --dry-run to see that
    estimate on its own, and notebook cancel-save-image to abort a save already
    running.
    """
    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list --workspace <workspace|all>",
    )
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    # Resolved through the notebook resolver, which rejects handle-shaped
    # normal CLI inputs.
    resolved = _resolve_save_notebook_id(
        ctx,
        notebook=notebook,
        workspace=workspace,
        pick=pick,
        session=session,
    )
    if not resolved:
        return
    notebook_id, workspace_id = resolved

    requested_visibility = _parse_visibility_value(visibility)
    visibility_label = visibility.lower() if visibility else ""
    image_label = scrub_raw_ids(f"{name}:{version}")
    notebook_label = scrub_raw_ids(notebook)

    # Read-only: measures the writable layer, never starts a save. A failed
    # estimate stays out of the way of the save the user actually asked for,
    # so `size_bytes is None` means "unknown", never "nothing to snapshot".
    size_bytes: Optional[int] = None
    try:
        estimate = browser_api_module.estimate_notebook_image_size(
            notebook_id=notebook_id,
            session=session,
        )
    except Exception:
        estimate = None

    if estimate is not None and not estimate.notebook_running:
        _handle_error(
            ctx,
            "ValidationError",
            f"Notebook {notebook_label} is not running, so there is nothing to snapshot.",
            EXIT_VALIDATION_ERROR,
            hint=f"Start it first: inspire notebook start {notebook} --workspace {workspace}",
        )
        return
    if estimate is not None:
        size_bytes = estimate.size_bytes

    if dry_run:
        if size_bytes is None:
            _handle_error(
                ctx,
                "APIError",
                "Could not estimate the image size.",
                EXIT_API_ERROR,
            )
            return
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "dry_run": True,
                        "notebook": notebook_label,
                        "name": image_label,
                        "estimated_size_bytes": size_bytes,
                        "estimated_size": _format_size_bytes(size_bytes),
                    }
                )
            )
            return
        click.echo(f"Save plan: {image_label}")
        click.echo(f"Notebook: {notebook_label}")
        click.echo(f"Estimated snapshot: {_format_size_bytes(size_bytes)}")
        click.echo("Nothing was saved (--dry-run).")
        return

    if size_bytes is not None and not ctx.json_output:
        click.echo(
            f"Estimated snapshot: {_format_size_bytes(size_bytes)}. "
            "The notebook cannot be used until the save finishes."
        )

    try:
        result = browser_api_module.save_notebook_as_image(
            notebook_id=notebook_id,
            name=name,
            version=version,
            description=description,
            session=session,
        )
    except Exception:
        _handle_error(
            ctx,
            "APIError",
            "Could not save notebook as an image.",
            EXIT_API_ERROR,
        )
        return

    image_id = result.get("image", {}).get("image_id", "") or result.get("image_id", "")

    if not image_id:
        try:
            want_suffix_1 = f"/{name}:{version}"
            want_name_1 = f"{name}:{version}"
            matches = []
            for img in browser_api_module.list_images_by_source(
                source="private", session=session, workspace_id=workspace_id
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

    visibility_warning: str | None = None
    if requested_visibility and image_id:
        try:
            browser_api_module.update_image(
                image_id=image_id,
                visibility=requested_visibility,
                session=session,
            )
        except Exception:
            visibility_warning = (
                "Visibility was not updated. Retry with: "
                f"inspire image set-visibility {image_label} "
                f"--visibility {visibility_label}"
            )
    elif requested_visibility and not image_id:
        visibility_warning = (
            "Set visibility after the image appears with: "
            f"inspire image set-visibility {image_label} "
            f"--visibility {visibility_label}"
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
        payload: dict[str, object] = {
            "name": image_label,
            "status": "ready" if ready else "saving",
        }
        if size_bytes is not None:
            payload["estimated_size_bytes"] = size_bytes
            payload["estimated_size"] = _format_size_bytes(size_bytes)
        if visibility_warning:
            payload["warning"] = visibility_warning
        click.echo(json_formatter.format_json(payload))
        return

    click.echo(
        format_mutation_success(
            "Image",
            "ready" if ready else "saving",
            image_label,
        )
    )
    if visibility_warning:
        click.echo(f"Warning: {visibility_warning}", err=True)


# ---------------------------------------------------------------------------
# cancel-save-image
# ---------------------------------------------------------------------------

_CANCEL_LEFTOVER_NOTE = (
    "The half-built image stays in the catalog as FAILED. "
    "Remove it with: inspire image delete <name>:<version>"
)


@click.command("cancel-save-image")
@click.argument("notebook", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def cancel_save_image_cmd(
    ctx: Context,
    notebook: str,
    workspace: str,
    pick: Optional[int],
) -> None:
    """Abort an image save that is still running and resume the notebook.

    NAME is the notebook name from inspire notebook list. A save pauses the
    notebook for as long as it takes; this hands it back. The cancel lands even
    after the platform has finished committing the layer, and the notebook
    returns to the state it was in before the save. The half-built image is
    left behind as FAILED and has to be deleted separately.
    """
    notebook = reject_id_at_boundary(
        ctx,
        notebook,
        resource_type="notebook",
        list_command="inspire notebook list --workspace <workspace|all>",
    )
    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    resolved = _resolve_save_notebook_id(
        ctx,
        notebook=notebook,
        workspace=workspace,
        pick=pick,
        session=session,
    )
    if not resolved:
        return
    notebook_id, workspace_id = resolved

    try:
        cancelled = browser_api_module.cancel_notebook_image_save(
            notebook_id=notebook_id,
            session=session,
        )
    except Exception:
        _handle_error(
            ctx,
            "APIError",
            "Could not cancel the image save.",
            EXIT_API_ERROR,
        )
        return

    notebook_label = scrub_raw_ids(notebook)

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "notebook": notebook_label,
                    "status": "cancelled" if cancelled else "not_saving",
                }
            )
        )
        return

    if not cancelled:
        click.echo(f"No image save is running for notebook {notebook_label}.")
        return

    click.echo(format_mutation_success("Image save", "cancelled", notebook_label))
    click.echo(_CANCEL_LEFTOVER_NOTE)


__all__ = [
    "cancel_save_image_cmd",
    "save_image_cmd",
]
