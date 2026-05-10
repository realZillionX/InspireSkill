"""Reusable workload condition profile subcommands."""

from __future__ import annotations

from typing import Any

import click

from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workload_profiles import (
    PROFILE_FIELDS,
    load_project_profile_data,
    normalize_workload_profiles,
)


def _field_values(profile: dict[str, str]) -> dict[str, str]:
    return {field: profile[field] for field in PROFILE_FIELDS if profile.get(field)}


def _write_project_profiles(data: dict[str, Any], path) -> None:  # noqa: ANN001
    from inspire.cli.commands.init.toml_helpers import _toml_dumps

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_toml_dumps(data), encoding="utf-8")


def make_profile_command(kind: str) -> click.Group:
    """Build ``inspire <kind> profile`` for a workload command group."""

    @click.group("profile")
    def profile_group() -> None:
        """Manage workload condition profiles.

        Profiles store only workload condition fields for reuse:
        workspace, project, group, quota, image.

        They are not account switches and are not global defaults. Create
        commands use a profile only when ``--profile`` is passed. Batch items
        may set ``profile = "<name>"``.
        """

    @click.command("list")
    @pass_context
    def list_profiles(ctx: Context) -> None:
        """List condition profiles for this workload."""
        try:
            config, _ = Config.from_files_and_env(require_credentials=False)
            profiles = getattr(config, "profiles", {}).get(kind, {})
            if ctx.json_output:
                click.echo(json_formatter.format_json({"profiles": profiles}))
                return
            if not profiles:
                click.echo(f"No {kind} profiles found.")
                return
            click.echo(f"{kind} profiles")
            for name in sorted(profiles):
                fields = ", ".join(f"{key}={scrub_raw_ids(value)}" for key, value in profiles[name].items())
                click.echo(f"  {scrub_raw_ids(name)}  {fields}")
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    @click.command("show")
    @click.argument("name")
    @pass_context
    def show_profile(ctx: Context, name: str) -> None:
        """Show a condition profile."""
        try:
            config, _ = Config.from_files_and_env(require_credentials=False)
            profiles = getattr(config, "profiles", {}).get(kind, {})
            profile = profiles.get(name)
            if profile is None:
                for alias, candidate in profiles.items():
                    if alias.lower() == name.lower():
                        name = alias
                        profile = candidate
                        break
            if profile is None:
                available = ", ".join(sorted(profiles)) or "(none)"
                raise ConfigError(f"Unknown {kind} profile: {name!r}. Available: {available}")
            if ctx.json_output:
                click.echo(json_formatter.format_json({"name": name, "profile": profile}))
                return
            click.echo(f"{kind} profile: {scrub_raw_ids(name)}")
            for field in PROFILE_FIELDS:
                click.echo(f"  {field}: {scrub_raw_ids(profile.get(field, ''))}")
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    @click.command("set")
    @click.argument("name")
    @click.option("--workspace", required=True, help="Workspace name")
    @click.option("--project", required=True, help="Project name")
    @click.option("--group", required=True, help="Compute group name")
    @click.option("--quota", "-q", required=True, help="Resource quota as gpu,cpu,mem")
    @click.option("--image", required=True, help="Image name or URL")
    @pass_context
    def set_profile(
        ctx: Context,
        name: str,
        workspace: str,
        project: str,
        group: str,
        quota: str,
        image: str,
    ) -> None:
        """Create or replace a condition profile in project config."""
        try:
            path, data = load_project_profile_data()
            profiles_root = data.setdefault("profiles", {})
            if not isinstance(profiles_root, dict):
                raise ConfigError("[profiles] must be a TOML table.")
            kind_profiles = profiles_root.setdefault(kind, {})
            if not isinstance(kind_profiles, dict):
                raise ConfigError(f"[profiles.{kind}] must be a TOML table.")
            kind_profiles[name] = _field_values(
                {
                    "workspace": workspace,
                    "project": project,
                    "group": group,
                    "quota": quota,
                    "image": image,
                }
            )
            _write_project_profiles(data, path)
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {"name": name, "path": str(path), "profile": kind_profiles[name]}
                    )
                )
                return
            click.echo(f"Saved {kind} profile: {scrub_raw_ids(name)}")
            click.echo(f"Path: {path}")
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    @click.command("delete")
    @click.argument("name")
    @click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
    @pass_context
    def delete_profile(ctx: Context, name: str, yes: bool) -> None:
        """Delete a condition profile from project config."""
        try:
            path, data = load_project_profile_data()
            profiles = normalize_workload_profiles(data.get("profiles", {}))
            if name not in profiles.get(kind, {}):
                available = ", ".join(sorted(profiles.get(kind, {}))) or "(none)"
                raise ConfigError(f"Unknown {kind} profile: {name!r}. Available: {available}")
            if not yes and not ctx.json_output:
                click.confirm(f"Delete {kind} profile '{scrub_raw_ids(name)}'?", abort=True)
            raw_profiles = data.get("profiles")
            if isinstance(raw_profiles, dict):
                raw_kind = raw_profiles.get(kind)
                if isinstance(raw_kind, dict):
                    raw_kind.pop(name, None)
                    if not raw_kind:
                        raw_profiles.pop(kind, None)
                if not raw_profiles:
                    data.pop("profiles", None)
            _write_project_profiles(data, path)
            if ctx.json_output:
                click.echo(json_formatter.format_json({"name": name, "deleted": True}))
                return
            click.echo(f"Deleted {kind} profile: {scrub_raw_ids(name)}")
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    profile_group.add_command(list_profiles)
    profile_group.add_command(show_profile)
    profile_group.add_command(set_profile)
    profile_group.add_command(delete_profile)
    return profile_group


__all__ = ["make_profile_command"]
