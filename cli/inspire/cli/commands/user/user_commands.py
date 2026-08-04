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
from inspire.cli.formatters.table import column_width, render_table
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

_PUBLIC_NOISE_KEYS = {
    "debug",
    "internal",
    "metadata",
    "payload",
    "progress",
    "raw",
    "request",
    "response",
    "result",
    "scanned",
    "source",
    "trace",
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


def _public_value(value: Any) -> Any:
    sanitized = json_formatter.sanitize_json_data(value)
    if isinstance(sanitized, dict):
        return {
            key: _public_value(child)
            for key, child in sanitized.items()
            if str(key).replace("-", "_").strip().lower() not in _PUBLIC_NOISE_KEYS
        }
    if isinstance(sanitized, list):
        return [_public_value(item) for item in sanitized]
    if isinstance(sanitized, tuple):
        return [_public_value(item) for item in sanitized]
    if isinstance(sanitized, str):
        return scrub_raw_ids(sanitized)
    return sanitized


def _current_user_summary(info: dict[str, Any]) -> dict[str, str]:
    extra_value = info.get("extra_info")
    extra: dict[str, Any] = extra_value if isinstance(extra_value, dict) else {}
    fields = {
        "name": info.get("name") or info.get("username"),
        "login": extra.get("login_name") or info.get("login_name"),
        "role": info.get("global_role") or info.get("role"),
        "email": info.get("email"),
    }
    return {
        key: scrub_raw_ids(str(value))
        for key, value in fields.items()
        if value not in (None, "")
    }


def _api_key_summary(item: dict[str, Any], index: int) -> dict[str, str]:
    fields = {
        "name": item.get("name") or item.get("title") or f"key-{index}",
        "created_at": item.get("create_at") or item.get("created_at"),
        "last_used_at": item.get("last_used_at") or item.get("last_used"),
        "status": item.get("status"),
    }
    return {
        key: scrub_raw_ids(str(value))
        for key, value in fields.items()
        if value not in (None, "")
    }


def _ssh_key_summary(item: dict[str, Any]) -> dict[str, str]:
    fields = {
        "name": _ssh_key_name(item) or "-",
        "fingerprint": _ssh_key_fingerprint(item),
        "created_at": item.get("created_at") or item.get("create_at"),
    }
    return {
        key: scrub_raw_ids(str(value))
        for key, value in fields.items()
        if value not in (None, "")
    }


def _flatten_public_values(value: Any, *, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        rows: list[tuple[str, str]] = []
        for key in sorted(value):
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_flatten_public_values(value[key], prefix=child_prefix))
        return rows
    if isinstance(value, list):
        if all(not isinstance(item, (dict, list, tuple)) for item in value):
            return [(prefix, ", ".join(scrub_raw_ids(item) for item in value))]
        rows = []
        for index, child in enumerate(value, start=1):
            rows.extend(_flatten_public_values(child, prefix=f"{prefix}[{index}]"))
        return rows
    if isinstance(value, tuple):
        return _flatten_public_values(list(value), prefix=prefix)
    return [(prefix or "quota", scrub_raw_ids(value))]


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
        summary = _current_user_summary(info)

        if ctx.json_output:
            click.echo(json_formatter.format_json(summary))
            return

        if not summary:
            click.echo("No user details returned.")
            return
        for label, key in (("Name", "name"), ("Login", "login"), ("Role", "role"), ("Email", "email")):
            if key in summary:
                click.echo(f"{label}: {summary[key]}")

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
    see `用户不存在`; use `<workload> quota` and live availability for ordinary
    compute decisions, and `inspire project list` only for project-level
    metadata.
    """
    try:
        session = get_web_session()
        data = browser_api_module.get_user_quota(session=session)
        public_quota = _public_value(data)
        if ctx.json_output:
            click.echo(json_formatter.format_json({"quota": public_quota}))
            return
        if not public_quota:
            click.echo("No quota data returned.")
            return
        for key, value in _flatten_public_values(public_quota):
            click.echo(f"{key}: {value}")

    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        msg = str(e)
        if "用户不存在" in msg or "user does not exist" in msg.lower():
            msg = (
                f"{msg}\n\n"
                "Hint: user-level quota is admin-only on qz.sii.edu.cn; regular "
                "users may see this error. Use `<workload> quota` and live "
                "availability for ordinary compute decisions; `inspire project "
                "list` is project-level metadata."
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
        rows = [_api_key_summary(item, index) for index, item in enumerate(items, 1)]

        if ctx.json_output:
            click.echo(json_formatter.format_json({"items": rows}))
            return

        if not rows:
            click.echo("No API keys found.")
            return

        for row in rows:
            suffix = []
            if row.get("created_at"):
                suffix.append(f"created={row['created_at']}")
            if row.get("last_used_at"):
                suffix.append(f"last_used={row['last_used_at']}")
            if row.get("status"):
                suffix.append(f"status={row['status']}")
            details = f"  {'  '.join(suffix)}" if suffix else ""
            click.echo(f"{row['name']}{details}")

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
        items, _ = browser_api_module.list_user_ssh_keys(session=session)
        rows = [_ssh_key_summary(item) for item in items]

        if ctx.json_output:
            click.echo(json_formatter.format_json({"items": rows}))
            return

        if not rows:
            click.echo("No SSH keys found.")
            return

        table_rows = [
            (
                row["name"],
                row.get("fingerprint", "-"),
                row.get("created_at", "-"),
            )
            for row in rows
        ]
        widths = [
            column_width("Name", [row[0] for row in table_rows], max_width=48),
            column_width("Fingerprint", [row[1] for row in table_rows], max_width=64),
            column_width("Created", [row[2] for row in table_rows], max_width=24),
        ]
        click.echo(
            "\n".join(
                render_table(
                    ("Name", "Fingerprint", "Created"),
                    table_rows,
                    widths,
                    line_char="─",
                )
            )
        )

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
        browser_api_module.create_user_ssh_key(
            name=key_name,
            content=content,
            session=session,
        )

        if ctx.json_output:
            click.echo(
                json_formatter.format_json({"name": key_name, "status": "created"})
            )
            return

        click.echo(f"SSH key '{scrub_raw_ids(key_name)}' has been added.")

    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@ssh_keys_user.command("delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@pass_context
def delete_ssh_key(ctx: Context, name: str, yes: bool) -> None:
    """Delete an SSH public key by name."""
    try:
        session = get_web_session()
        key = _resolve_ssh_key_by_name(ctx, name, session=session)
        key_name = _ssh_key_name(key) or name

        if not yes and not ctx.json_output:
            if not click.confirm(f"Delete SSH key '{scrub_raw_ids(key_name)}'?"):
                click.echo("Cancelled.")
                return

        browser_api_module.delete_user_ssh_key(_ssh_key_id(key), session=session)
        if ctx.json_output:
            click.echo(
                json_formatter.format_json({"name": key_name, "status": "deleted"})
            )
            return

        click.echo(f"SSH key '{scrub_raw_ids(key_name)}' has been deleted.")

    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("permissions")
@click.option("--workspace", required=True, help="Workspace name")
@pass_context
def permissions_user(
    ctx: Context,
    workspace: Optional[str],
) -> None:
    """Show granted permissions in a workspace."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        resolved_workspace = _resolve_workspace_id(config, workspace, session)
        perms = browser_api_module.get_user_permissions(
            workspace_id=resolved_workspace, session=session
        )
        permissions = sorted(set(perms))
        workspace_name = scrub_raw_ids(workspace or "")

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "workspace": workspace_name,
                        "permissions": permissions,
                    }
                )
            )
            return

        if not permissions:
            click.echo("No permissions granted in this workspace.")
            return

        click.echo(f"Workspace: {workspace_name}")
        for permission in permissions:
            click.echo(permission)

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
