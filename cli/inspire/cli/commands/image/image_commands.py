"""Image subcommands."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import (
    is_full_uuid,
    is_partial_id,
    normalize_partial,
    resolve_partial_id,
)
from inspire.cli.utils.notebook_cli import (
    require_web_session,
    resolve_json_output,
)
from inspire.platform.web import browser_api as browser_api_module


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


def _image_to_dict(img: browser_api_module.CustomImageInfo) -> dict:
    """Convert a CustomImageInfo to a plain dict for JSON output."""
    return {
        "image_id": img.image_id,
        "url": img.url,
        "name": img.name,
        "framework": img.framework,
        "version": img.version,
        "source": img.source,
        "status": img.status,
        "description": img.description,
        "created_at": img.created_at,
    }


def _dedupe_images_by_id(images: list[dict]) -> list[dict]:
    """Deduplicate image dictionaries by image_id while preserving order."""
    deduped: list[dict] = []
    seen_ids: set[str] = set()
    for image in images:
        image_id = str(image.get("image_id", "")).strip()
        if image_id:
            if image_id in seen_ids:
                continue
            seen_ids.add(image_id)
        deduped.append(image)
    return deduped


def _resolve_image_id(
    ctx: Context,
    image_id: str,
    json_output: bool,
    session,
) -> str:
    """Resolve a full or partial image ID.

    Full UUIDs pass through; partial hex triggers a list + prefix match.
    """
    image_id = image_id.strip()

    if is_full_uuid(image_id):
        return image_id

    if not is_partial_id(image_id):
        return image_id  # not hex — let the API handle the error

    partial = normalize_partial(image_id)

    try:
        all_images: list[browser_api_module.CustomImageInfo] = []
        for src_key in _ALL_SOURCE_KEYS:
            items = browser_api_module.list_images_by_source(source=src_key, session=session)
            all_images.extend(items)
    except Exception:
        return image_id  # can't list — pass through and let the API error

    matches: list[tuple[str, str]] = []
    seen: set[str] = set()
    for img in all_images:
        iid = img.image_id
        if iid in seen:
            continue
        seen.add(iid)
        if iid.lower().startswith(partial):
            label = img.name or img.status or ""
            matches.append((iid, label))

    if not matches:
        return image_id  # no match — pass through for API error

    return resolve_partial_id(ctx, partial, "image", matches, json_output)


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
    default="official",
    show_default=True,
    help="Image source filter",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def list_images_cmd(
    ctx: Context,
    source: str,
    json_output: bool,
) -> None:
    """List available Docker images.

    \b
    Examples:
        inspire image list                     # Official images
        inspire image list --source private    # Personal-visible images
        inspire image list --source all        # All sources
        inspire image list --source all --json # JSON output
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Listing images requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    results: list[dict] = []
    warnings: list[str] = []

    try:
        if source == "all":
            for src_key in _ALL_SOURCE_KEYS:
                try:
                    items = browser_api_module.list_images_by_source(
                        source=src_key, session=session
                    )
                except Exception as e:
                    warnings.append(f"{src_key}: {e}")
                    continue
                results.extend(_image_to_dict(img) for img in items)

            results = _dedupe_images_by_id(results)

            if not results and warnings:
                raise ValueError("; ".join(warnings))
        else:
            items = browser_api_module.list_images_by_source(source=source, session=session)
            results.extend(_image_to_dict(img) for img in items)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to list images: {e}", EXIT_API_ERROR)
        return

    if json_output:
        payload = {"images": results, "total": len(results)}
        if warnings:
            payload["warnings"] = warnings
        click.echo(json_formatter.format_json(payload))
        return

    for warning in warnings:
        click.echo(f"Warning: failed to list images from {warning}", err=True)

    click.echo(human_formatter.format_image_list(results))


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


@click.command("detail")
@click.argument("image_id")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def image_detail(
    ctx: Context,
    image_id: str,
    json_output: bool,
) -> None:
    """Show detailed information about an image.

    \b
    Examples:
        inspire image detail <image-id>
        inspire image detail <image-id> --json
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Image detail requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    image_id = _resolve_image_id(ctx, image_id, json_output, session)

    try:
        image = browser_api_module.get_image_detail(image_id=image_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to get image detail: {e}", EXIT_API_ERROR)
        return

    if json_output:
        click.echo(json_formatter.format_json(_image_to_dict(image)))
        return

    click.echo(human_formatter.format_image_detail(_image_to_dict(image)))


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
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
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
    json_output: bool,
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
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Registering images requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
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
        _handle_error(ctx, "APIError", f"Failed to register image: {e}", EXIT_API_ERROR)
        return

    image_data = result.get("image", {})
    image_id = image_data.get("image_id", "") or result.get("image_id", "")
    registry_url = image_data.get("address", "") or result.get("address", "")

    if wait and image_id:
        if not json_output:
            click.echo(f"Image '{image_id}' registered. Waiting for READY status...")
        try:
            browser_api_module.wait_for_image_ready(image_id=image_id, session=session)
            if not json_output:
                click.echo(f"Image '{image_id}' is now READY.")
        except (TimeoutError, ValueError) as e:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
            return

    if json_output:
        click.echo(json_formatter.format_json({"image_id": image_id, "result": result}))
        return

    click.echo(f"Image registered: {image_id or 'unknown'}")
    if registry_url and method.lower() == "push":
        click.echo("\nTo push your image:")
        click.echo(f"  docker tag <local-image> {registry_url}")
        click.echo(f"  docker push {registry_url}")
    if not wait and image_id:
        click.echo(f"\nUse 'inspire image detail {image_id}' to check status.")


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


_VISIBILITY_PUBLIC = "VISIBILITY_PUBLIC"
_VISIBILITY_PRIVATE = "VISIBILITY_PRIVATE"


def _parse_visibility_flag(public: Optional[bool]) -> Optional[str]:
    if public is None:
        return None
    return _VISIBILITY_PUBLIC if public else _VISIBILITY_PRIVATE


@click.command("save")
@click.argument("notebook_id")
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
    "--public/--private",
    "public",
    default=None,
    help=(
        "Visibility of the saved image. --public makes it visible to all users; "
        "--private keeps it visible only to you. Omit to accept platform default "
        "(currently private). Passed to /mirror/save; if that endpoint ignores "
        "the field, CLI falls back to /image/update to force the requested value."
    ),
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def save_image_cmd(
    ctx: Context,
    notebook_id: str,
    name: str,
    version: str,
    description: str,
    wait: bool,
    public: Optional[bool],
    json_output: bool,
) -> None:
    """Save a running notebook as a custom Docker image.

    \b
    Examples:
        inspire image save <notebook-id> -n my-saved-image
        inspire image save <notebook-id> -n my-img -v v2 --wait
        inspire image save <notebook-id> -n shared-base -v v1 --public
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Saving images requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    requested_visibility = _parse_visibility_flag(public)

    try:
        result = browser_api_module.save_notebook_as_image(
            notebook_id=notebook_id,
            name=name,
            version=version,
            description=description,
            visibility=requested_visibility,
            session=session,
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to save notebook as image: {e}", EXIT_API_ERROR)
        return

    image_id = result.get("image", {}).get("image_id", "") or result.get("image_id", "")

    if not image_id:
        try:
            matches = [
                img
                for img in browser_api_module.list_images_by_source(
                    source="private", session=session
                )
                if img.name == name and img.version == version
            ]
            if matches:
                matches.sort(key=lambda img: img.created_at, reverse=True)
                image_id = matches[0].image_id
        except Exception:
            pass

    visibility_applied = True
    if requested_visibility and image_id:
        try:
            browser_api_module.update_image(
                image_id=image_id,
                visibility=requested_visibility,
                session=session,
            )
        except Exception as e:
            visibility_applied = False
            if not json_output:
                click.echo(
                    f"Warning: could not force visibility={requested_visibility} via /image/update: {e}",
                    err=True,
                )

    if wait and image_id:
        if not json_output:
            click.echo(f"Image '{image_id}' is being saved. Waiting for READY status...")
        try:
            browser_api_module.wait_for_image_ready(image_id=image_id, session=session)
            if not json_output:
                click.echo(f"Image '{image_id}' is now READY.")
        except (TimeoutError, ValueError) as e:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
            return

    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "image_id": image_id,
                    "visibility_requested": requested_visibility,
                    "visibility_applied": visibility_applied,
                    "result": result,
                }
            )
        )
        return

    click.echo(f"Notebook saved as image: {image_id or 'unknown'}")
    if requested_visibility and image_id:
        label = "public" if requested_visibility == _VISIBILITY_PUBLIC else "private"
        click.echo(f"Visibility: {label}")
    if not wait and image_id:
        click.echo(f"Use 'inspire image detail {image_id}' to check build status.")


# ---------------------------------------------------------------------------
# set-visibility
# ---------------------------------------------------------------------------


@click.command("set-visibility")
@click.argument("image_id")
@click.option(
    "--public/--private",
    "public",
    required=True,
    default=None,
    help="Target visibility. --public = visible to all users; --private = creator only.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def set_image_visibility_cmd(
    ctx: Context,
    image_id: str,
    public: Optional[bool],
    json_output: bool,
) -> None:
    """Flip an existing custom image's visibility (public ↔ private).

    \b
    Examples:
        inspire image set-visibility <image-id> --public
        inspire image set-visibility <image-id> --private
    """
    json_output = resolve_json_output(ctx, json_output)

    if public is None:
        _handle_error(
            ctx,
            "ValidationError",
            "One of --public / --private is required.",
            EXIT_VALIDATION_ERROR,
        )
        return

    session = require_web_session(
        ctx,
        hint=(
            "Updating image visibility requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    image_id = _resolve_image_id(ctx, image_id, json_output, session)
    visibility = _parse_visibility_flag(public)
    assert visibility is not None

    try:
        result = browser_api_module.update_image(
            image_id=image_id,
            visibility=visibility,
            session=session,
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to update image visibility: {e}", EXIT_API_ERROR)
        return

    label = "public" if visibility == _VISIBILITY_PUBLIC else "private"
    if json_output:
        click.echo(
            json_formatter.format_json(
                {"image_id": image_id, "visibility": visibility, "result": result}
            )
        )
        return

    click.echo(f"Image '{image_id}' visibility set to {label}.")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@click.command("delete")
@click.argument("image_id")
@click.option(
    "--force",
    is_flag=True,
    help="Skip confirmation prompt",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def delete_image_cmd(
    ctx: Context,
    image_id: str,
    force: bool,
    json_output: bool,
) -> None:
    """Delete a custom Docker image.

    \b
    Examples:
        inspire image delete <image-id>
        inspire image delete <image-id> --force
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=(
            "Deleting images requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    image_id = _resolve_image_id(ctx, image_id, json_output, session)

    if not force and not json_output:
        if not click.confirm(f"Delete image '{image_id}'?"):
            click.echo("Cancelled.")
            return

    try:
        result = browser_api_module.delete_image(image_id=image_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to delete image: {e}", EXIT_API_ERROR)
        return

    if json_output:
        click.echo(
            json_formatter.format_json(
                {"image_id": image_id, "status": "deleted", "result": result}
            )
        )
        return

    click.echo(f"Image '{image_id}' has been deleted.")


# ---------------------------------------------------------------------------
# set-default
# ---------------------------------------------------------------------------


@click.command("set-default")
@click.option(
    "--job",
    "job_image",
    default=None,
    help="Set default image for jobs (written to [job].image in .inspire/config.toml)",
)
@click.option(
    "--notebook",
    "notebook_image",
    default=None,
    help="Set default image for notebooks (written to [notebook].image in .inspire/config.toml)",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@pass_context
def set_default_image_cmd(
    ctx: Context,
    job_image: Optional[str],
    notebook_image: Optional[str],
    json_output: bool,
) -> None:
    """Save image preferences to .inspire/config.toml.

    \b
    Examples:
        inspire image set-default --job my-pytorch-image
        inspire image set-default --notebook my-notebook-image
        inspire image set-default --job img1 --notebook img2
    """
    json_output = resolve_json_output(ctx, json_output)

    if not job_image and not notebook_image:
        _handle_error(
            ctx,
            "ValidationError",
            "Specify at least one of --job or --notebook.",
            EXIT_VALIDATION_ERROR,
        )
        return

    from inspire.config.toml import _find_project_config

    existing_config = _find_project_config()
    config_path = existing_config if existing_config else Path(".inspire") / "config.toml"

    # Read existing config if present
    existing_data: dict = {}
    if config_path.exists():
        try:
            from inspire.config.toml import _load_toml

            existing_data = _load_toml(config_path)
        except Exception:
            existing_data = {}

    # Update the relevant sections
    updated: dict[str, str] = {}
    if job_image:
        if "job" not in existing_data:
            existing_data["job"] = {}
        existing_data["job"]["image"] = job_image
        updated["job.image"] = job_image

    if notebook_image:
        if "notebook" not in existing_data:
            existing_data["notebook"] = {}
        existing_data["notebook"]["image"] = notebook_image
        updated["notebook.image"] = notebook_image

    # Write back
    try:
        from inspire.cli.commands.init.toml_helpers import _toml_dumps

        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(_toml_dumps(existing_data), encoding="utf-8")
    except Exception as e:
        _handle_error(ctx, "ConfigError", f"Failed to write config: {e}", EXIT_CONFIG_ERROR)
        return

    if json_output:
        click.echo(
            json_formatter.format_json({"updated": updated, "config_path": str(config_path)})
        )
        return

    for key, value in updated.items():
        click.echo(f"Set {key} = {value!r} in {config_path}")


__all__ = [
    "delete_image_cmd",
    "image_detail",
    "list_images_cmd",
    "register_image_cmd",
    "save_image_cmd",
    "set_default_image_cmd",
    "set_image_visibility_cmd",
]
