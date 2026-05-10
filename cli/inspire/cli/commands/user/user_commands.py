"""`inspire user` subcommands."""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Optional

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
from inspire.cli.utils.auth import AuthenticationError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import reject_id_at_boundary
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import get_web_session

_SSH_KEY_TYPES = {
    "ssh-rsa",
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}


def _resolve_workspace_id(config: Config, workspace: Optional[str], session) -> Optional[str]:  # noqa: ANN001
    if workspace is None:
        return None
    return select_workspace_id(config, explicit_workspace_name=workspace, session=session)


def _ssh_key_id(item: dict) -> str:
    return str(item.get("ssh_id") or item.get("id") or "").strip()


def _ssh_key_name(item: dict) -> str:
    return str(item.get("name") or item.get("title") or "").strip()


def _ssh_key_fingerprint(item: dict) -> str:
    return str(item.get("fingerprint") or item.get("finger_print") or "").strip()


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


def _resolve_ssh_key_by_name(ctx: Context, name: str, *, session) -> dict:  # noqa: ANN001
    key_name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="SSH key",
        list_command="inspire user ssh-keys list",
    )
    items, _ = browser_api_module.list_user_ssh_keys(page_size=1000, session=session)
    matches = [item for item in items if _ssh_key_name(item) == key_name]
    if not matches:
        _handle_error(
            ctx,
            "ValidationError",
            f"SSH key '{scrub_raw_ids(key_name)}' was not found.",
            EXIT_VALIDATION_ERROR,
            hint="Run `inspire user ssh-keys list` to see available key names.",
        )
    if len(matches) > 1:
        _handle_error(
            ctx,
            "ValidationError",
            f"Multiple SSH keys are named '{scrub_raw_ids(key_name)}'.",
            EXIT_VALIDATION_ERROR,
            hint="Rename or delete the duplicate key from the platform user center first.",
        )
    ssh_id = _ssh_key_id(matches[0])
    if not ssh_id:
        _handle_error(
            ctx,
            "APIError",
            f"SSH key '{scrub_raw_ids(key_name)}' has no delete handle in the API response.",
            EXIT_API_ERROR,
        )
    return matches[0]


@click.command("whoami")
@pass_context
def whoami_user(ctx: Context) -> None:
    """Show the logged-in user."""
    try:
        session = get_web_session()
        info = browser_api_module.get_current_user(session=session) or {}

        if ctx.json_output:
            click.echo(json_formatter.format_json(info))
            return

        click.echo("Current User")
        click.echo(f"Name:      {info.get('name', 'N/A')}")
        click.echo(f"Login:     {(info.get('extra_info') or {}).get('login_name', 'N/A')}")
        click.echo(f"Role:      {info.get('global_role', 'N/A')}")
        if info.get("email"):
            click.echo(f"Email:     {info.get('email')}")

    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("quota")
@pass_context
def quota_user(ctx: Context) -> None:
    """Show the current user's quota.

    \b
    Note: user-level quota is admin-only on qz.sii.edu.cn. Regular users may
    see `用户不存在`; use `inspire project list` for per-project remaining
    budget and GPU caps instead.
    """
    try:
        session = get_web_session()
        data = browser_api_module.get_user_quota(session=session)
        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return
        if not data:
            click.echo("No quota data returned.")
            return
        click.echo("User Quota")
        for k, v in data.items():
            click.echo(f"  {k}: {v}")

    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        msg = str(e)
        if "用户不存在" in msg or "user does not exist" in msg.lower():
            msg = (
                f"{msg}\n\n"
                "Hint: user-level quota is admin-only on qz.sii.edu.cn; regular "
                "users may see this error. Use `inspire project list` for "
                "per-project remaining budget and GPU caps."
            )
        _handle_error(ctx, "APIError", msg, EXIT_API_ERROR)


@click.command("api-keys")
@pass_context
def api_keys_user(ctx: Context) -> None:
    """List the current user's API keys.

    Values are not returned by list — only metadata. Create/delete are not
    wrapped; use the platform user center for those.
    """
    try:
        session = get_web_session()
        items = browser_api_module.list_user_api_keys(session=session)

        if ctx.json_output:
            click.echo(json_formatter.format_json({"total": len(items), "items": items}))
            return

        if not items:
            click.echo("No API keys found.")
            return

        click.echo(f"API Keys (total={len(items)})")
        for i, item in enumerate(items, 1):
            name = scrub_raw_ids(item.get("name") or item.get("title") or f"key-{i}")
            created = item.get("create_at") or item.get("created_at") or ""
            last_used = item.get("last_used_at") or item.get("last_used") or ""
            suffix = []
            if created:
                suffix.append(f"created={created}")
            if last_used:
                suffix.append(f"last_used={last_used}")
            click.echo(f"  [{i}] {name}  " + "  ".join(suffix))

    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.group("ssh-keys")
def ssh_keys_user() -> None:
    """Manage SSH public keys in the platform user center."""


@ssh_keys_user.command("list")
@pass_context
def list_ssh_keys(ctx: Context) -> None:
    """List the current user's SSH public keys."""
    try:
        session = get_web_session()
        items, total = browser_api_module.list_user_ssh_keys(session=session)

        if ctx.json_output:
            click.echo(json_formatter.format_json({"total": total, "items": items}))
            return

        if not items:
            click.echo("No SSH keys found.")
            return

        rows = [
            {
                "name": scrub_raw_ids(_ssh_key_name(item) or "-"),
                "fingerprint": scrub_raw_ids(_ssh_key_fingerprint(item) or "-"),
                "created": scrub_raw_ids(
                    str(item.get("created_at") or item.get("create_at") or "-")
                ),
            }
            for item in items
        ]
        name_w = max(len("Name"), *(len(row["name"]) for row in rows))
        fp_w = max(len("Fingerprint"), *(len(row["fingerprint"]) for row in rows))
        click.echo(f"SSH Keys (total={total})")
        click.echo(f"{'Name':<{name_w}}  {'Fingerprint':<{fp_w}}  Created")
        click.echo(f"{'-' * name_w}  {'-' * fp_w}  -------")
        for row in rows:
            click.echo(f"{row['name']:<{name_w}}  {row['fingerprint']:<{fp_w}}  {row['created']}")

    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@ssh_keys_user.command("add")
@click.argument("name")
@click.option("--public-key", default=None, help="OpenSSH public key content")
@click.option(
    "--public-key-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    default=None,
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
        result = browser_api_module.create_user_ssh_key(
            name=key_name,
            content=content,
            session=session,
        )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"name": key_name, "status": "created", "result": result}
                )
            )
            return

        click.echo(f"SSH key '{scrub_raw_ids(key_name)}' has been added.")

    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@ssh_keys_user.command("delete")
@click.argument("name")
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@pass_context
def delete_ssh_key(ctx: Context, name: str, force: bool) -> None:
    """Delete an SSH public key by name."""
    try:
        session = get_web_session()
        key = _resolve_ssh_key_by_name(ctx, name, session=session)
        key_name = _ssh_key_name(key) or name

        if not force and not ctx.json_output:
            if not click.confirm(f"Delete SSH key '{scrub_raw_ids(key_name)}'?"):
                click.echo("Cancelled.")
                return

        result = browser_api_module.delete_user_ssh_key(_ssh_key_id(key), session=session)
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"name": key_name, "status": "deleted", "result": result}
                )
            )
            return

        click.echo(f"SSH key '{scrub_raw_ids(key_name)}' has been deleted.")

    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("permissions")
@click.option("--workspace", default=None, help="Workspace name")
@pass_context
def permissions_user(
    ctx: Context, workspace: Optional[str],) -> None:
    """Show per-workspace permission matrix (`/user/permissions/{ws}`)."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        resolved_workspace = _resolve_workspace_id(config, workspace, session)
        perms = browser_api_module.get_user_permissions(
            workspace_id=resolved_workspace, session=session
        )

        if ctx.json_output:
            click.echo(json_formatter.format_json({"workspace_id": resolved_workspace, "permissions": perms}))
            return

        if not perms:
            click.echo("No permissions granted in this workspace.")
            return

        workspace_label = workspace or "(current workspace)"
        click.echo(f"Permissions in workspace {workspace_label} ({len(perms)} granted)")
        for p in sorted(perms):
            click.echo(f"  {p}")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = [
    "whoami_user",
    "quota_user",
    "api_keys_user",
    "ssh_keys_user",
    "permissions_user",
]
