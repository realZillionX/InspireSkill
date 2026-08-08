"""`inspire user` subcommands."""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
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
from inspire.cli.formatters.human_formatter import format_mutation_success
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
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workspaces import (
    resolve_workspace_query_scope,
    workspace_name_map,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session

_SSH_KEY_TYPES = {
    "ssh-rsa",
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}


def _ssh_key_id(item: dict) -> str:
    return str(item.get("ssh_id") or item.get("id") or "").strip()


def _ssh_key_name(item: dict) -> str:
    return str(item.get("name") or item.get("title") or "").strip()


def _current_user_summary(info: dict[str, Any]) -> dict[str, str]:
    fields = {
        "name": info.get("name") or info.get("display_name"),
        "role": info.get("global_role") or info.get("role"),
        "email": info.get("email"),
    }
    return {
        key: scrub_raw_ids(str(value))
        for key, value in fields.items()
        if value not in (None, "")
    }


def _api_key_summary(item: dict[str, Any], index: int) -> dict[str, str]:
    name = item.get("name") or item.get("title") or f"key-{index}"
    return {"name": scrub_raw_ids(str(name))}


def _ssh_key_summary(item: dict[str, Any]) -> dict[str, str]:
    return {"name": scrub_raw_ids(_ssh_key_name(item) or "-")}


def _list_ssh_keys_for_output(
    *,
    session,
    limit: int | None,
) -> tuple[list[dict[str, Any]], int]:  # noqa: ANN001
    if limit is not None:
        return browser_api_module.list_user_ssh_keys(
            page=1,
            page_size=limit,
            session=session,
        )

    page = 1
    page_size = 1000
    collected: list[dict[str, Any]] = []
    known_total = 0
    while True:
        items, total = browser_api_module.list_user_ssh_keys(
            page=page,
            page_size=page_size,
            session=session,
        )
        collected.extend(items)
        known_total = max(known_total, total, len(collected))
        if not items or len(collected) >= known_total:
            return collected, known_total
        page += 1


def _read_public_key(
    ctx: Context,
    *,
    public_key: Optional[str],
    public_key_file: Optional[Path],
) -> str:
    if bool(public_key) == bool(public_key_file):
        _handle_error(
            ctx,
            "ValidationError",
            "Pass exactly one of --public-key or --public-key-file.",
            EXIT_VALIDATION_ERROR,
        )
    if public_key_file is not None:
        try:
            public_key = public_key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            _handle_error(
                ctx,
                "ValidationError",
                f"Failed to read SSH public key file: {exc}",
                EXIT_VALIDATION_ERROR,
            )
    return _validate_public_key(ctx, public_key or "")


def _validate_public_key(ctx: Context, value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        _handle_error(
            ctx,
            "ValidationError",
            "SSH public key must contain exactly one non-empty line.",
            EXIT_VALIDATION_ERROR,
        )
    parts = lines[0].split()
    if len(parts) < 2:
        _handle_error(
            ctx,
            "ValidationError",
            "SSH public key must use OpenSSH public key format.",
            EXIT_VALIDATION_ERROR,
        )
    key_type = parts[0]
    if key_type not in _SSH_KEY_TYPES:
        _handle_error(
            ctx,
            "ValidationError",
            f"Unsupported SSH public key type: {key_type}",
            EXIT_VALIDATION_ERROR,
        )
    try:
        base64.b64decode(parts[1].encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error):
        _handle_error(
            ctx,
            "ValidationError",
            "SSH public key payload is not valid base64.",
            EXIT_VALIDATION_ERROR,
        )
    return lines[0]


def _resolve_ssh_key_by_name(
    ctx: Context,
    name: str,
    *,
    session,
    pick: int | None = None,
    require_live: bool = False,
) -> dict:  # noqa: ANN001
    key_name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="SSH key",
        list_command="inspire user ssh-keys list",
    )

    def _lister() -> list[dict[str, Any]]:
        items, _ = browser_api_module.list_user_ssh_keys(
            page_size=1000,
            session=session,
        )
        return [
            {
                "name": _ssh_key_name(item),
                "id": _ssh_key_id(item),
                "created_at": item.get("created_at") or item.get("create_at") or "",
            }
            for item in items
            if _ssh_key_name(item) and _ssh_key_id(item)
        ]

    ssh_id = resolve_by_name(
        ctx,
        name=key_name,
        resource_type="ssh-key",
        list_candidates=_lister,
        session=session,
        owner_scope="self",
        pick_index=pick,
        require_live=require_live,
        list_command="inspire user ssh-keys list",
    )
    return {"name": key_name, "ssh_id": ssh_id}


@click.command("whoami")
@pass_context
def whoami_user(ctx: Context) -> None:
    """Show the logged-in user."""
    try:
        session = get_web_session()
        info = browser_api_module.get_current_user(session=session) or {}
        summary = _current_user_summary(info)

        if ctx.json_output:
            click.echo(json_formatter.format_json(summary))
            return

        if not summary:
            click.echo("No user details returned.")
            return
        for label, key in (("Name", "name"), ("Role", "role"), ("Email", "email")):
            if key in summary:
                click.echo(f"{label}: {summary[key]}")

    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("api-keys")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum key names to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every API key name.")
@pass_context
def api_keys_user(ctx: Context, limit: int | None, show_all: bool) -> None:
    """List the current user's API keys.

    Values are not returned by list — only names. Create/delete are not
    wrapped; use the platform user center for those.
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    try:
        session = get_web_session()
        items = browser_api_module.list_user_api_keys(session=session)
        rows = [_api_key_summary(item, index) for index, item in enumerate(items, 1)]
        page = bound_collection(rows, limit=effective_limit)

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

        if not page.items:
            click.echo("No API keys found.")
            return

        for row in page.items:
            click.echo(row["name"])
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.group("ssh-keys")
def ssh_keys_user() -> None:
    """Manage SSH public keys in the platform user center."""


@ssh_keys_user.command("list")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum key names to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every SSH key name.")
@pass_context
def list_ssh_keys(ctx: Context, limit: int | None, show_all: bool) -> None:
    """List the current user's SSH public keys."""
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    try:
        session = get_web_session()
        items, total = _list_ssh_keys_for_output(
            session=session,
            limit=effective_limit,
        )
        rows = [_ssh_key_summary(item) for item in items]
        page = bound_collection(rows, limit=effective_limit, total=total)

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

        if not page.items:
            click.echo("No SSH keys found.")
            return

        for row in page.items:
            click.echo(row["name"])
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@ssh_keys_user.command("add")
@click.argument("name", metavar="NAME")
@click.option(
    "--public-key",
    default=None,
    metavar="KEY",
    help="OpenSSH public key content",
)
@click.option(
    "--public-key-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    default=None,
    metavar="PATH",
    help="Path to an OpenSSH .pub file",
)
@pass_context
def add_ssh_key(
    ctx: Context,
    name: str,
    public_key: Optional[str],
    public_key_file: Optional[Path],
) -> None:
    """Add an SSH public key by name."""
    try:
        key_name = reject_id_at_boundary(
            ctx,
            name,
            resource_type="SSH key",
            list_command="inspire user ssh-keys list",
        )
        content = _read_public_key(
            ctx,
            public_key=public_key,
            public_key_file=public_key_file,
        )
        session = get_web_session()
        existing, _ = browser_api_module.list_user_ssh_keys(page_size=1000, session=session)
        if any(_ssh_key_name(item) == key_name for item in existing):
            _handle_error(
                ctx,
                "ValidationError",
                f"SSH key '{scrub_raw_ids(key_name)}' already exists.",
                EXIT_VALIDATION_ERROR,
            )
        created = browser_api_module.create_user_ssh_key(
            name=key_name,
            content=content,
            session=session,
        )
        created_id = _ssh_key_id(created if isinstance(created, dict) else {})
        if created_id:
            remember_resource_identity(
                session=session,
                resource_type="ssh-key",
                resource_id=created_id,
                name=key_name,
                owner_scope="self",
            )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"name": scrub_raw_ids(key_name), "status": "created"}
                )
            )
            return

        click.echo(format_mutation_success("SSH key", "created", key_name))

    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@ssh_keys_user.command("delete")
@click.argument("name", metavar="NAME")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@pass_context
def delete_ssh_key(ctx: Context, name: str, pick: int | None, yes: bool) -> None:
    """Delete an SSH public key by name."""
    try:
        require_confirmation(
            ctx,
            yes=yes,
            prompt=f"Delete SSH key '{scrub_raw_ids(name)}'?",
            message="SSH key deletion requires confirmation.",
        )
        session = get_web_session()
        key = _resolve_ssh_key_by_name(ctx, name, session=session, pick=pick)
        key_name = _ssh_key_name(key) or name
        key_id = _ssh_key_id(key)

        def _delete(resolved_id: str) -> str:
            browser_api_module.delete_user_ssh_key(resolved_id, session=session)
            return resolved_id

        key_id = run_with_stale_handle_retry(
            name=key_name,
            resolve_cached=lambda: key_id,
            resolve_live=lambda live_name: _ssh_key_id(
                _resolve_ssh_key_by_name(
                    ctx,
                    live_name,
                    session=session,
                    pick=pick,
                    require_live=True,
                )
            ),
            operation=_delete,
            invalidate=lambda stale_id: forget_resource_identity(
                session=session,
                resource_type="ssh-key",
                resource_id=stale_id,
                owner_scope="self",
            ),
        )
        forget_resource_identity(
            session=session,
            resource_type="ssh-key",
            resource_id=key_id,
            name=key_name,
            owner_scope="self",
        )
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"name": scrub_raw_ids(key_name), "status": "deleted"}
                )
            )
            return

        click.echo(format_mutation_success("SSH key", "deleted", key_name))

    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("permissions")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME|all",
    help="Workspace name or 'all'.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum permission names to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every permission name.")
@pass_context
def permissions_user(
    ctx: Context,
    workspace: Optional[str],
    limit: int | None,
    show_all: bool,
) -> None:
    """Show granted permissions by workspace."""
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_ids, all_workspaces = resolve_workspace_query_scope(
            workspace=workspace,
            session=session,
        )
        permissions: list[str | dict[str, str]]
        if all_workspaces:
            workspace_names = workspace_name_map(session)
            permissions = []
            for workspace_id in workspace_ids:
                workspace_name = scrub_raw_ids(
                    workspace_names.get(workspace_id) or "(workspace name unavailable)"
                )
                permissions.extend(
                    {
                        "workspace": workspace_name,
                        "permission": scrub_raw_ids(permission),
                    }
                    for permission in sorted(
                        set(
                            browser_api_module.get_user_permissions(
                                workspace_id=workspace_id,
                                session=session,
                            )
                        )
                    )
                )
        else:
            permissions = [
                scrub_raw_ids(permission)
                for permission in sorted(
                    set(
                        browser_api_module.get_user_permissions(
                            workspace_id=workspace_ids[0],
                            session=session,
                        )
                    )
                )
            ]
        page = bound_collection(permissions, limit=effective_limit)

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

        if not page.items:
            click.echo(
                "No permissions granted in the requested workspaces."
                if all_workspaces
                else "No permissions granted in this workspace."
            )
            return

        for permission in page.items:
            if isinstance(permission, dict):
                click.echo(f"{permission['workspace']}: {permission['permission']}")
            else:
                click.echo(permission)
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = [
    "whoami_user",
    "api_keys_user",
    "ssh_keys_user",
    "permissions_user",
]
