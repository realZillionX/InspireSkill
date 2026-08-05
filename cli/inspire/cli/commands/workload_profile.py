"""Reusable workload condition profile subcommands."""

from __future__ import annotations

from typing import Any

import click

from inspire.cli.context import (
    Context,
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
from inspire.cli.utils.id_resolver import reject_id_at_boundary
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workload_profiles import (
    PROFILE_FIELDS,
    load_project_profile_data,
    normalize_workload_profiles,
)


def _field_values(profile: dict[str, str]) -> dict[str, str]:
    return {
        field: scrub_raw_ids(str(profile[field]))
        for field in PROFILE_FIELDS
        if profile.get(field)
    }


def _public_profile(profile: dict[str, Any]) -> dict[str, str]:
    return {
        field: scrub_raw_ids(str(profile[field]))
        for field in PROFILE_FIELDS
        if profile.get(field)
    }


def _profile_resource_label(kind: str) -> str:
    return f"{'HPC' if kind == 'hpc' else kind.title()} profile"


def _write_project_profiles(data: dict[str, Any], path) -> None:  # noqa: ANN001
    from inspire.cli.commands.init.toml_helpers import _toml_dumps

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_toml_dumps(data), encoding="utf-8")


def _validate_profile_fields(
    ctx: Context,
    *,
    kind: str,
    workspace: str,
    project: str,
    group: str,
    quota: str,
    image: str,
) -> dict[str, str]:
    """Reject platform handles before persisting reusable profile values."""
    return {
        "workspace": reject_id_at_boundary(
            ctx,
            workspace,
            resource_type="workspace",
            list_command="inspire config context",
        ),
        "project": reject_id_at_boundary(
            ctx,
            project,
            resource_type="project",
            list_command="inspire project list --workspace <name>",
        ),
        "group": reject_id_at_boundary(
            ctx,
            group,
            resource_type="compute group",
            list_command=f"inspire {kind} quota --workspace <name>",
        ),
        "quota": reject_id_at_boundary(
            ctx,
            quota,
            resource_type="quota",
            list_command=f"inspire {kind} quota --workspace <name>",
        ),
        "image": reject_id_at_boundary(
            ctx,
            image,
            resource_type="image",
            list_command="inspire image list --workspace <name>",
        ),
    }


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
    @click.option(
        "--limit",
        "-n",
        type=click.IntRange(1),
        default=None,
        help="Maximum profiles to display (default: 20).",
    )
    @click.option("--all", "show_all", is_flag=True, help="Show every profile.")
    @pass_context
    def list_profiles(ctx: Context, limit: int | None, show_all: bool) -> None:
        """List condition profiles for this workload."""
        try:
            effective_limit = resolve_collection_limit(
                limit=limit,
                show_all=show_all,
            )
        except ValueError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
            return

        try:
            config, _ = Config.from_files_and_env(require_credentials=False)
            profiles = getattr(config, "profiles", {}).get(kind, {})
            profile_items = [
                {
                    "name": scrub_raw_ids(str(name)),
                    **_public_profile(profile),
                }
                for name, profile in sorted(profiles.items())
                if isinstance(profile, dict)
            ]
            page = bound_collection(profile_items, limit=effective_limit)
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
                click.echo(f"No {kind} profiles found.")
                return
            for item in page.items:
                name = str(item.get("name") or "")
                fields = ", ".join(
                    f"{field}={scrub_raw_ids(str(item[field]))}"
                    for field in PROFILE_FIELDS
                    if item.get(field)
                )
                suffix = f" {fields}" if fields else ""
                click.echo(f"{scrub_raw_ids(name)}{suffix}")
            notice = truncation_notice(page)
            if notice:
                click.echo(notice)
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    @click.command("show")
    @click.argument("name", metavar="NAME")
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
            public_profile = _public_profile(profile)
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {"name": scrub_raw_ids(name), "profile": public_profile}
                    )
                )
                return
            for field in PROFILE_FIELDS:
                if value := public_profile.get(field):
                    click.echo(f"{field}={value}")
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    @click.command("set")
    @click.argument("name", metavar="NAME")
    @click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
    @click.option("--project", required=True, metavar="NAME", help="Project name.")
    @click.option(
        "--group",
        required=True,
        metavar="NAME",
        help="Full compute group name copied from the same quota row as --quota.",
    )
    @click.option(
        "--quota",
        "-q",
        required=True,
        metavar="SPEC",
        help="Resource quota as gpu,cpu,mem.",
    )
    @click.option("--image", required=True, metavar="NAME|URL", help="Image name or URL.")
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
            values = _validate_profile_fields(
                ctx,
                kind=kind,
                workspace=workspace,
                project=project,
                group=group,
                quota=quota,
                image=image,
            )
            path, data = load_project_profile_data()
            profiles_root = data.setdefault("profiles", {})
            if not isinstance(profiles_root, dict):
                raise ConfigError("[profiles] must be a TOML table.")
            kind_profiles = profiles_root.setdefault(kind, {})
            if not isinstance(kind_profiles, dict):
                raise ConfigError(f"[profiles.{kind}] must be a TOML table.")
            kind_profiles[name] = _field_values(
                values
            )
            _write_project_profiles(data, path)
            if ctx.json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "name": scrub_raw_ids(name),
                            "status": "saved",
                            "profile": kind_profiles[name],
                        }
                    )
                )
                return
            click.echo(
                format_mutation_success(
                    _profile_resource_label(kind),
                    "saved",
                    name,
                )
            )
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    @click.command("delete")
    @click.argument("name", metavar="NAME")
    @click.option(
        "--yes",
        "-y",
        is_flag=True,
        help="Skip the interactive confirmation prompt.",
    )
    @pass_context
    def delete_profile(ctx: Context, name: str, yes: bool) -> None:
        """Delete a condition profile from project config."""
        try:
            require_confirmation(
                ctx,
                yes=yes,
                prompt=f"Delete {kind} profile '{scrub_raw_ids(name)}'?",
                message=f"{kind.title()} profile deletion requires confirmation.",
            )
            path, data = load_project_profile_data()
            profiles = normalize_workload_profiles(data.get("profiles", {}))
            if name not in profiles.get(kind, {}):
                available = ", ".join(sorted(profiles.get(kind, {}))) or "(none)"
                raise ConfigError(f"Unknown {kind} profile: {name!r}. Available: {available}")
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
                click.echo(
                    json_formatter.format_json(
                        {"name": scrub_raw_ids(name), "status": "deleted"}
                    )
                )
                return
            click.echo(
                format_mutation_success(
                    _profile_resource_label(kind),
                    "deleted",
                    name,
                )
            )
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

    profile_group.add_command(list_profiles)
    profile_group.add_command(show_profile)
    profile_group.add_command(set_profile)
    profile_group.add_command(delete_profile)
    return profile_group


__all__ = ["make_profile_command"]
