"""Account API key lifecycle with explicit secret export and child-process injection."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click

from inspire.cli.context import (
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    Context,
    pass_context,
)
from inspire.services.utils import json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.services.utils.collections import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import exit_with_error, require_confirmation
from inspire.cli.utils.id_resolver import NAME_PICK_HELP
from inspire.config import ConfigError
from inspire.platform.web.browser_api import api_keys
from inspire.platform.web.session import SessionExpiredError, WebSession, get_web_session

from .key_export import export_private_key, render_key, windows_acl_tool


@contextmanager
def _errors(ctx: Context) -> Iterator[None]:
    try:
        yield
    except ConfigError:
        exit_with_error(
            ctx, "ConfigError", "Check the selected account configuration.", EXIT_CONFIG_ERROR
        )
    except SessionExpiredError:
        exit_with_error(
            ctx,
            "AuthenticationError",
            "API key operation requires a valid account session.",
            EXIT_AUTH_ERROR,
        )
    except OSError:
        exit_with_error(
            ctx,
            "ExportError",
            "Could not export the key. Check the destination directory and permissions; existing files are never overwritten.",
            EXIT_API_ERROR,
        )
    except ValueError as e:
        exit_with_error(ctx, "APIError", str(e), EXIT_API_ERROR)


def _name(_ctx: click.Context, _param: click.Parameter, value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", value):
        raise click.BadParameter("Use 1-256 letters, digits, underscores, hyphens or dots.")
    return value


def _env_name(_ctx: click.Context, _param: click.Parameter, value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise click.BadParameter("Use a valid environment variable name.")
    return value


def _emit(ctx: Context, result: dict[str, str]) -> None:
    if ctx.json_output:
        click.echo(json_formatter.format_json(result))
    else:
        for key, value in result.items():
            click.echo(f"{key.replace('_', ' ').title()}: {json_formatter.sanitize_text(value)}")


def _resolve_key(name: str, pick: int | None, session: WebSession) -> api_keys.APIKeyInfo:
    matches = [key for key in api_keys.list_api_keys(session=session) if key.name == name]
    if not matches:
        raise ValueError("API key name not found. See 'inspire account api-key list'.")
    if pick is not None:
        if pick > len(matches):
            raise ValueError("--pick is outside the matching API key list.")
        return matches[pick - 1]
    if len(matches) > 1:
        candidates = "; ".join(f"{i}: created {key.created_at}" for i, key in enumerate(matches, 1))
        raise ValueError(f"API key name is ambiguous; select --pick N. {candidates}")
    return matches[0]


@click.group("api-key")
def api_key() -> None:
    """Manage platform inference API keys for the selected account.

    Keys are separate from serving creation. List never displays key values.
    Export to a private raw/.env/shell file, explicitly capture stdout in
    your shell, or use run to inject INF_API_KEY into a child process.
    Use serving api for endpoint and request examples.
    """


@api_key.command("list")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum keys to show (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show all key names.")
@pass_context
def list_keys(ctx: Context, limit: int | None, show_all: bool) -> None:
    """List key names and creation times, never secret values."""
    try:
        effective = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        exit_with_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return
    with _errors(ctx):
        page = bound_collection(api_keys.list_api_keys(), limit=effective)
        rows = [{"name": k.name, "created_at": k.created_at} for k in page.items]
        if ctx.json_output:
            click.echo(json_formatter.format_json({"items": rows, **page.metadata()}))
        elif not rows:
            click.echo("No API keys found.")
        else:
            table_rows = [
                (
                    json_formatter.sanitize_text(k["name"]),
                    json_formatter.sanitize_text(k["created_at"]),
                )
                for k in rows
            ]
            headers = ("Name", "Created (epoch ms)")
            click.echo(
                "\n".join(
                    render_table(
                        headers,
                        table_rows,
                        [
                            column_width(h, [r[i] for r in table_rows], max_width=60)
                            for i, h in enumerate(headers)
                        ],
                    )
                )
            )
            notice = truncation_notice(page)
            if notice:
                click.echo(notice)


@api_key.command("create")
@click.option("--name", required=True, callback=_name, help="New API key name.")
@pass_context
def create_key(ctx: Context, name: str) -> None:
    """Create a named key; retrieve its secret separately with export."""
    with _errors(ctx):
        session = get_web_session()
        if any(k.name == name for k in api_keys.list_api_keys(session=session)):
            raise ValueError("An API key with this name already exists. Choose a new name.")
        api_keys.create_api_key(name, session=session)
        result = {"name": name, "status": "created"}
        try:
            if not any(k.name == name for k in api_keys.list_api_keys(session=session)):
                result["status"] = "creation accepted; confirmation pending"
        except (ValueError, SessionExpiredError):
            result["status"] = "creation accepted; confirmation pending"
        _emit(ctx, result)


@api_key.command("export")
@click.argument("name", callback=_name)
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    help="New private file; existing files and symlinks are never overwritten.",
)
@click.option(
    "--stdout",
    "to_stdout",
    is_flag=True,
    help="Explicitly emit the secret to stdout for shell capture. Incompatible with --json.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["raw", "dotenv", "sh", "powershell"]),
    default="raw",
    show_default=True,
    help="Plain key, .env assignment, or shell assignment.",
)
@click.option(
    "--env-name",
    default="INF_API_KEY",
    callback=_env_name,
    show_default=True,
    help="Environment variable used by assignment formats.",
)
@pass_context
def export_key(
    ctx: Context,
    name: str,
    pick: int | None,
    output: Path | None,
    to_stdout: bool,
    output_format: str,
    env_name: str,
) -> None:
    """Export a key to a private file or explicitly to stdout.

    Choose exactly one of --output and --stdout. File exports use POSIX 0600
    or a verified current-user-only Windows ACL. --format dotenv writes a
    standard NAME=value line, suitable for a new .env file. Existing files
    are never merged or overwritten. --stdout exposes plaintext: capture it
    in your shell; ordinary JSON output never includes the key.

    A child CLI cannot modify its parent shell's environment. Use api-key
    run to pass the key directly to a child without printing it.

    \b
    Bash/Zsh:
        INF_API_KEY="$(inspire account api-key export NAME --stdout)" && export INF_API_KEY

    \b
    PowerShell:
        $key = inspire account api-key export NAME --stdout
        if ($LASTEXITCODE -eq 0) { $env:INF_API_KEY = $key }
        Remove-Variable key
    """
    if (output is not None) == to_stdout:
        exit_with_error(
            ctx,
            "ValidationError",
            "Choose exactly one of --output or --stdout.",
            EXIT_VALIDATION_ERROR,
        )
        return
    if to_stdout and ctx.json_output:
        exit_with_error(
            ctx,
            "ValidationError",
            "--stdout cannot be combined with --json.",
            EXIT_VALIDATION_ERROR,
        )
        return
    with _errors(ctx):
        if output is not None:
            if os.path.lexists(output):
                raise ValueError("Export destination already exists; choose a new file.")
            if sys.platform == "win32":
                windows_acl_tool()
        session = get_web_session()
        key = _resolve_key(name, pick, session)
        value = api_keys.get_api_key_plaintext(key.key_id, session=session)
        content = render_key(value, output_format, env_name)
        if to_stdout:
            click.echo(content, nl=False)
        else:
            assert output is not None
            export_private_key(content, output)
            _emit(
                ctx,
                {
                    "name": name,
                    "status": "exported",
                    "format": output_format,
                    "permissions": "current-user-only ACL" if sys.platform == "win32" else "0600",
                },
            )


@api_key.command("run", context_settings={"ignore_unknown_options": True})
@click.argument("name", callback=_name)
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@click.option("--env-name", default="INF_API_KEY", callback=_env_name, show_default=True)
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=True)
@pass_context
def run_with_key(
    ctx: Context, name: str, pick: int | None, env_name: str, command: tuple[str, ...]
) -> None:
    """Run COMMAND with the key in its environment, without CLI secret output.

    Example: inspire account api-key run NAME -- python client.py
    Works on Windows and POSIX. The parent shell is unchanged. Child output
    is inherited, so the child must not print its environment or credentials.
    The child exit status is propagated; --json is not supported.
    """
    if ctx.json_output:
        exit_with_error(
            ctx,
            "ValidationError",
            "api-key run cannot be combined with --json.",
            EXIT_VALIDATION_ERROR,
        )
        return
    with _errors(ctx):
        session = get_web_session()
        key = _resolve_key(name, pick, session)
        env = os.environ.copy()
        env[env_name] = api_keys.get_api_key_plaintext(key.key_id, session=session)
        try:
            returncode = subprocess.call(command, env=env)
        except OSError:
            raise ValueError("Could not start the child command.") from None
    raise SystemExit(returncode if returncode >= 0 else 128 - returncode)


@api_key.command("delete")
@click.argument("name", callback=_name)
@click.option("--pick", type=click.IntRange(1), default=None, help=NAME_PICK_HELP)
@click.option("--yes", "-y", is_flag=True, help="Confirm permanent deletion without prompting.")
@pass_context
def delete_key(ctx: Context, name: str, pick: int | None, yes: bool) -> None:
    """Permanently revoke a key. Clients using it will lose access."""
    require_confirmation(
        ctx,
        yes=yes,
        prompt=f"Permanently delete API key '{name}'?",
        message="API key deletion requires confirmation.",
    )
    with _errors(ctx):
        session = get_web_session()
        key = _resolve_key(name, pick, session)
        api_keys.delete_api_key(key.key_id, session=session)
        result = {"name": name, "status": "deleted"}
        try:
            if any(k.key_id == key.key_id for k in api_keys.list_api_keys(session=session)):
                result["status"] = "deletion accepted; confirmation pending"
        except (ValueError, SessionExpiredError):
            result["status"] = "deletion accepted; confirmation pending"
        _emit(ctx, result)
