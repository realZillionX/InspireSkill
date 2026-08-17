"""`inspire dataset` subcommands — the official dataset catalogue and mounts.

Two platforms back this group. Browsing and search come from 数据广场
(:mod:`inspire.platform.web.plaza`), which is where the catalogue actually
lives; mount checking comes from the qz console's ``ValidateDataset``
(:mod:`inspire.platform.web.browser_api.datasets`), because a mount is
workspace-scoped and the catalogue knows nothing about workspaces.

A dataset's name is its catalogue code, and a version's name is its version
code. Those are the values `--dataset <name>:<version>` takes and the ones the
container path is built from; the plaza's numeric handles address nothing on
the qz side and never appear on any CLI surface.
"""

from __future__ import annotations

import sys
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
from inspire.cli.formatters.table import clip_display, column_width, render_table
from inspire.cli.utils.collection_output import (
    DEFAULT_COLLECTION_LIMIT,
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.dataset_mounts import (
    DatasetSpecError,
    parse_dataset_specs,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import reject_id_at_boundary
from inspire.cli.utils.notebook_cli import WEB_AUTH_HINT, require_web_session
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import ConfigError
from inspire.config.workspaces import resolve_workspace_operation_scope
from inspire.platform.web import plaza as plaza_module
from inspire.platform.web.browser_api.datasets import (
    container_mount_path,
    validate_dataset_mounts,
)
from inspire.platform.web.session import SessionExpiredError

LIST_COMMAND = "inspire dataset list"

# Catalogue descriptions are README-sized. The CLI is an Agent surface, so a
# detail view carries an orientation-sized summary, not the whole document.
DESCRIPTION_BUDGET = 400

# How many tag names an unknown-tag error is allowed to spend context on.
_TAG_SUGGESTION_LIMIT = 12


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarize(text: str, *, budget: int = DESCRIPTION_BUDGET) -> str:
    """Collapse a markdown description into one clipped, readable line."""
    collapsed = " ".join(str(text or "").split())
    return clip_display(scrub_raw_ids(collapsed), budget) if collapsed else ""


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _format_size(files_size_mib: int) -> str:
    """Render a version's size, which the catalogue reports in MiB."""
    size = float(files_size_mib or 0)
    if size <= 0:
        return ""
    for unit in ("MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return ""  # pragma: no cover - the loop always returns


def _dataset_row(dataset: plaza_module.DatasetSummary) -> dict[str, Any]:
    """The compact, name-only projection of one catalogue row."""
    view: dict[str, Any] = {
        "name": scrub_raw_ids(dataset.code),
        "project": scrub_raw_ids(dataset.project),
        "grade": scrub_raw_ids(dataset.grade),
        "state": scrub_raw_ids(dataset.state),
        "access": _yes_no(dataset.accessible),
        "tags": [scrub_raw_ids(tag) for tag in dataset.tags],
        "updated_at": scrub_raw_ids(dataset.updated_at),
    }
    return {key: value for key, value in view.items() if value not in ("", [], None)}


def _format_dataset_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No datasets found."
    values = [
        (
            str(row.get("name", "")),
            str(row.get("project", "")),
            str(row.get("grade", "")),
            str(row.get("state", "")),
            str(row.get("access", "")),
            ", ".join(row.get("tags", []) or []),
            str(row.get("updated_at", "")),
        )
        for row in rows
    ]
    headers = ("Name", "Project", "Grade", "State", "Access", "Tags", "Updated")
    max_widths = (36, 28, 5, 10, 6, 24, 10)
    widths = [
        column_width(header, [row[index] for row in values], max_width=max_width)
        for index, (header, max_width) in enumerate(zip(headers, max_widths))
    ]
    return "\n".join(render_table(headers, values, widths, line_char="─"))


def _dataset_detail_view(detail: plaza_module.DatasetDetail) -> dict[str, Any]:
    view: dict[str, Any] = {
        "name": scrub_raw_ids(detail.code),
        "project": scrub_raw_ids(detail.project),
        "grade": scrub_raw_ids(detail.grade),
        "state": scrub_raw_ids(detail.state),
        "access": _yes_no(detail.accessible),
        "owner": scrub_raw_ids(detail.owner),
        "maintainer": scrub_raw_ids(detail.maintainer),
        "tags": [scrub_raw_ids(tag) for tag in detail.tags],
        "data_type": scrub_raw_ids(detail.data_type),
        "source_type": scrub_raw_ids(detail.source_type),
        "license": scrub_raw_ids(detail.license_name),
        "license_url": scrub_raw_ids(detail.license_url),
        "updated_at": scrub_raw_ids(detail.updated_at),
        "description": _summarize(detail.description),
    }
    return {key: value for key, value in view.items() if value not in ("", [], None)}


def _version_views(detail: plaza_module.DatasetDetail) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for version in detail.versions:
        view: dict[str, Any] = {
            "version": scrub_raw_ids(version.code),
            "state": scrub_raw_ids(version.state),
            "size": _format_size(version.files_size_mib),
            "files": version.files_count or "",
            "formats": [scrub_raw_ids(fmt) for fmt in version.data_formats],
            "updated_at": scrub_raw_ids(version.updated_at),
            "mount": f"--dataset {detail.code}:{version.code}",
            "path": container_mount_path(detail.code, version.code),
        }
        views.append(
            {key: value for key, value in view.items() if value not in ("", [], None)}
        )
    return views


def _format_versions(versions: list[dict[str, Any]]) -> str:
    values = [
        (
            str(version.get("version", "")),
            str(version.get("state", "")),
            str(version.get("size", "")),
            str(version.get("files", "")),
            ", ".join(version.get("formats", []) or []),
            str(version.get("updated_at", "")),
        )
        for version in versions
    ]
    headers = ("Version", "State", "Size", "Files", "Formats", "Updated")
    max_widths = (24, 16, 12, 10, 18, 18)
    widths = [
        column_width(header, [row[index] for row in values], max_width=max_width)
        for index, (header, max_width) in enumerate(zip(headers, max_widths))
    ]
    return "\n".join(render_table(headers, values, widths, line_char="─"))


def _format_detail(view: dict[str, Any]) -> str:
    labels = (
        ("Name", "name"),
        ("Project", "project"),
        ("Grade", "grade"),
        ("State", "state"),
        ("Access", "access"),
        ("Owner", "owner"),
        ("Maintainer", "maintainer"),
        ("Tags", "tags"),
        ("Data type", "data_type"),
        ("Source", "source_type"),
        ("License", "license"),
        ("Updated", "updated_at"),
        ("Description", "description"),
    )
    lines: list[str] = []
    for label, key in labels:
        if key not in view:
            continue
        value = view[key]
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _tag_hint(error: plaza_module.UnknownDatasetTagError) -> str:
    """Point at real tag names without spending the whole vocabulary on it."""
    wanted = [name.casefold() for name in error.unknown]
    near = [
        name
        for name in error.available
        if any(part in name.casefold() or name.casefold() in part for part in wanted)
    ]
    candidates = (near or list(error.available))[:_TAG_SUGGESTION_LIMIT]
    listed = "、".join(candidates)
    return (
        f"{len(error.available)} tags exist; for example {listed}. "
        f"`{LIST_COMMAND}` shows each dataset's tags."
    )


def _exit_for_unknown_dataset(ctx: Context, name: str, message: str) -> None:
    hint = None
    if name.isdigit():
        # The catalogue's own numeric handles are the classic wrong answer
        # here, and they address nothing outside it.
        hint = (
            "A dataset is named by its catalogue code, such as 'pixabay-81k'. "
            f"Find it with `{LIST_COMMAND} --keyword <text>`."
        )
    _handle_error(ctx, "ValidationError", message, EXIT_VALIDATION_ERROR, hint=hint)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@click.command("list")
@click.option(
    "--keyword",
    default=None,
    metavar="KEYWORD",
    help="Server-side search over dataset name, project, and description.",
)
@click.option(
    "--tag",
    "tag_names",
    multiple=True,
    metavar="NAME",
    help="Keep datasets carrying this tag; repeatable, and a dataset matching any of them is kept.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum datasets to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every matching dataset.")
@pass_context
def list_datasets_cmd(
    ctx: Context,
    keyword: Optional[str],
    tag_names: tuple[str, ...],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List official datasets published on the data plaza.

    The catalogue is platform-wide, not workspace-scoped. Access says whether
    this account may mount the dataset at all; State says whether the data is
    ready. Use `dataset show <name>` for the versions, then `dataset validate`
    before creating a workload against them.
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    session = require_web_session(ctx, hint=WEB_AUTH_HINT)
    request_limit = (
        effective_limit if effective_limit is not None else DEFAULT_COLLECTION_LIMIT
    )

    try:
        tag_ids = plaza_module.resolve_tag_ids(tag_names, session=session)
        items, total = plaza_module.list_datasets(
            keyword=keyword,
            tag_ids=tag_ids,
            page=1,
            page_size=request_limit,
            session=session,
        )
        if show_all and total > len(items):
            items, total = plaza_module.list_datasets(
                keyword=keyword,
                tag_ids=tag_ids,
                page=1,
                page_size=total,
                session=session,
            )
    except plaza_module.UnknownDatasetTagError as e:
        _handle_error(
            ctx,
            "ValidationError",
            str(e),
            EXIT_VALIDATION_ERROR,
            hint=_tag_hint(e),
        )
        return
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception:
        _handle_error(ctx, "APIError", "Could not list datasets.", EXIT_API_ERROR)
        return

    page = bound_collection(
        [_dataset_row(item) for item in items],
        limit=effective_limit,
        total=total,
    )
    if ctx.json_output:
        click.echo(
            json_formatter.format_json({"items": page.items, **page.metadata()})
        )
        return

    click.echo(_format_dataset_rows(page.items))
    notice = truncation_notice(page)
    if notice:
        click.echo(notice)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@click.command("show")
@click.argument("name", metavar="NAME")
@pass_context
def show_dataset_cmd(ctx: Context, name: str) -> None:
    """Show one dataset and every version it can be mounted from.

    NAME is the dataset name from `inspire dataset list`. Each version line
    carries the exact `--dataset` value to pass to a create command and the
    path the data appears at inside the container.
    """
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="dataset",
        list_command=LIST_COMMAND,
    )
    session = require_web_session(ctx, hint=WEB_AUTH_HINT)

    try:
        summary = plaza_module.resolve_dataset_by_code(name, session=session)
        detail = plaza_module.get_dataset_detail(summary.dataset_id, session=session)
    except plaza_module.UnknownDatasetError as e:
        _exit_for_unknown_dataset(ctx, name, scrub_raw_ids(e))
        return
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception:
        _handle_error(ctx, "APIError", "Could not load the dataset.", EXIT_API_ERROR)
        return

    view = _dataset_detail_view(detail)
    versions = _version_views(detail)
    if ctx.json_output:
        click.echo(json_formatter.format_json({**view, "versions": versions}))
        return

    click.echo(_format_detail(view))
    click.echo("")
    if not versions:
        click.echo("This dataset has no mountable version yet.")
        return

    click.echo(_format_versions(versions))
    click.echo("")
    click.echo("Mount with:")
    for version in versions:
        click.echo(f"  {version['mount']}  ->  {version['path']}")
    if not detail.accessible:
        click.echo(
            "Access is 'no' for this account, so a mount may be refused."
        )
    # Point the example at a version that is actually carrying data.
    ready = next(
        (version for version in detail.versions if version.state == "active"),
        detail.versions[0],
    )
    click.echo(
        f"Check one first: inspire dataset validate {detail.code}:{ready.code} "
        "--workspace <workspace>"
    )


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@click.command("validate")
@click.argument("specs", metavar="NAME:VERSION...", nargs=-1, required=True)
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@pass_context
def validate_datasets_cmd(
    ctx: Context,
    specs: tuple[str, ...],
    workspace: str,
) -> None:
    """Check that dataset mounts resolve in a workspace before a create.

    Each argument is one `<name>:<version>` mount, the same value
    `--dataset` takes. The platform answers per entry, so a rejected mount
    names its own reason — unknown dataset, unknown version, or no access for
    this account in this workspace. Exits non-zero if any entry is rejected.
    """
    try:
        mounts = parse_dataset_specs(specs)
    except DatasetSpecError as e:
        _handle_error(ctx, "ValidationError", scrub_raw_ids(e), EXIT_VALIDATION_ERROR)
        return

    session = require_web_session(ctx, hint=WEB_AUTH_HINT)
    try:
        workspace_id = resolve_workspace_operation_scope(
            workspace=workspace,
            session=session,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
        return

    try:
        verdicts = validate_dataset_mounts(
            mounts,
            workspace_id=workspace_id,
            session=session,
        )
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception:
        _handle_error(
            ctx,
            "APIError",
            "Could not check the dataset mounts.",
            EXIT_API_ERROR,
        )
        return

    results = [
        {
            "name": scrub_raw_ids(verdict.dataset),
            "version": scrub_raw_ids(verdict.version),
            "mountable": verdict.ok,
            # The platform also returns its own storage location; the path that
            # matters to the caller is where the mount shows up in the container.
            "path": verdict.mount_path if verdict.ok else "",
            "reason": "" if verdict.ok else scrub_raw_ids(verdict.error),
        }
        for verdict in verdicts
    ]
    rejected = [result for result in results if not result["mountable"]]

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "workspace": scrub_raw_ids(workspace),
                    "items": [
                        {key: value for key, value in result.items() if value != ""}
                        for result in results
                    ],
                    "mountable": not rejected,
                }
            )
        )
    else:
        values = [
            (
                str(result["name"]),
                str(result["version"]),
                "ok" if result["mountable"] else "rejected",
                str(result["path"] or result["reason"]),
            )
            for result in results
        ]
        headers = ("Name", "Version", "Result", "Detail")
        max_widths = (36, 24, 8, 48)
        widths = [
            column_width(header, [row[index] for row in values], max_width=max_width)
            for index, (header, max_width) in enumerate(zip(headers, max_widths))
        ]
        click.echo("\n".join(render_table(headers, values, widths, line_char="─")))

    if rejected:
        sys.exit(EXIT_VALIDATION_ERROR)


# ---------------------------------------------------------------------------
# applications
# ---------------------------------------------------------------------------


def _application_row(
    application: plaza_module.DatasetApplication,
    *,
    incoming: bool,
) -> dict[str, Any]:
    """Project one application down to what identifies and qualifies it."""
    view: dict[str, Any] = {
        "name": scrub_raw_ids(application.dataset),
        "state": scrub_raw_ids(application.state),
        "authority": scrub_raw_ids(application.authority),
        "applied_at": scrub_raw_ids(application.applied_at),
    }
    if incoming:
        view["applicant"] = scrub_raw_ids(application.applicant)
        view["project"] = scrub_raw_ids(application.project)
    else:
        view["decided_at"] = scrub_raw_ids(application.decided_at)
        view["approver"] = scrub_raw_ids(application.approver)
    return {key: value for key, value in view.items() if value not in ("", None)}


def _application_detail_view(
    application: plaza_module.DatasetApplication,
) -> dict[str, Any]:
    view = {
        "name": scrub_raw_ids(application.dataset),
        "state": scrub_raw_ids(application.state),
        "authority": scrub_raw_ids(application.authority),
        "applicant": scrub_raw_ids(application.applicant),
        "project": scrub_raw_ids(application.project),
        "reason": _summarize(application.reason),
        "approver": scrub_raw_ids(application.approver),
        "applied_at": scrub_raw_ids(application.applied_at),
        "decided_at": scrub_raw_ids(application.decided_at),
    }
    return {key: value for key, value in view.items() if value not in ("", None)}


def _format_application_rows(rows: list[dict[str, Any]], *, incoming: bool) -> str:
    if not rows:
        return (
            "No dataset access applications are waiting for you."
            if incoming
            else "You have not applied for access to any dataset."
        )
    if incoming:
        headers = ("Name", "State", "Authority", "Applicant", "Project", "Applied")
        fields = ("name", "state", "authority", "applicant", "project", "applied_at")
        max_widths = (36, 10, 18, 16, 28, 20)
    else:
        headers = ("Name", "State", "Authority", "Applied", "Decided", "Approver")
        fields = ("name", "state", "authority", "applied_at", "decided_at", "approver")
        max_widths = (36, 10, 18, 20, 20, 16)
    values = [tuple(str(row.get(field, "")) for field in fields) for row in rows]
    widths = [
        column_width(header, [row[index] for row in values], max_width=max_width)
        for index, (header, max_width) in enumerate(zip(headers, max_widths))
    ]
    return "\n".join(render_table(headers, values, widths, line_char="─"))


def _format_application_detail(view: dict[str, Any]) -> str:
    labels = (
        ("Name", "name"),
        ("State", "state"),
        ("Authority", "authority"),
        ("Applicant", "applicant"),
        ("Project", "project"),
        ("Approver", "approver"),
        ("Applied", "applied_at"),
        ("Decided", "decided_at"),
        ("Reason", "reason"),
    )
    return "\n".join(
        f"{label}: {view[key]}" for label, key in labels if key in view
    )


@click.command("applications")
@click.argument("name", required=False, default=None)
@click.option(
    "--to-approve",
    "to_approve",
    is_flag=True,
    help="Show applications waiting for your approval instead of the ones you submitted.",
)
@click.option(
    "--keyword",
    default=None,
    metavar="KEYWORD",
    help="Server-side search over the applications listed.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum applications to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every application.")
@pass_context
def dataset_applications_cmd(
    ctx: Context,
    name: Optional[str],
    to_approve: bool,
    keyword: Optional[str],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """Show dataset access applications and where they stand.

    A dataset whose Access reads 'no' in `dataset list` cannot be mounted until
    access is granted, and applying for it is a web-only flow — this command
    reads the outcome, it does not submit, approve, or withdraw anything.

    States are `pending`, `approved`, `rejected`, and `withdrawn`. An approved application is the point at which `dataset validate <name>:<version>` is worth running again. Pass NAME, the dataset name, for the full record of every application on that one dataset.
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    if name is not None:
        name = reject_id_at_boundary(
            ctx,
            name,
            resource_type="dataset",
            list_command=LIST_COMMAND,
        )
    session = require_web_session(ctx, hint=WEB_AUTH_HINT)
    lister = (
        plaza_module.list_dataset_approvals
        if to_approve
        else plaza_module.list_dataset_applications
    )
    request_limit = (
        effective_limit if effective_limit is not None else DEFAULT_COLLECTION_LIMIT
    )

    try:
        if name is not None:
            items = plaza_module.find_dataset_applications(
                name,
                incoming=to_approve,
                session=session,
                limit=request_limit,
            )
            total = len(items)
        else:
            items, total = lister(
                keyword=keyword,
                page=1,
                page_size=request_limit,
                session=session,
            )
            if show_all and total > len(items):
                items, total = lister(
                    keyword=keyword,
                    page=1,
                    page_size=total,
                    session=session,
                )
    except plaza_module.UnknownDatasetApplicationError as e:
        _handle_error(
            ctx,
            "ValidationError",
            scrub_raw_ids(e),
            EXIT_VALIDATION_ERROR,
            hint=(
                "`inspire dataset applications` lists the ones this account can see."
            ),
        )
        return
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception:
        _handle_error(
            ctx,
            "APIError",
            "Could not list dataset access applications.",
            EXIT_API_ERROR,
        )
        return

    if name is not None:
        views = [_application_detail_view(item) for item in items]
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"name": scrub_raw_ids(name), "items": views}
                )
            )
            return
        click.echo("\n\n".join(_format_application_detail(view) for view in views))
        return

    page = bound_collection(
        [_application_row(item, incoming=to_approve) for item in items],
        limit=effective_limit,
        total=total,
    )
    if ctx.json_output:
        click.echo(
            json_formatter.format_json({"items": page.items, **page.metadata()})
        )
        return

    click.echo(_format_application_rows(page.items, incoming=to_approve))
    notice = truncation_notice(page)
    if notice:
        click.echo(notice)


@click.command("tags")
@pass_context
def list_dataset_tags_cmd(ctx: Context) -> None:
    """List the tag vocabulary `dataset list --tag` accepts.

    `--tag` matches an exact tag name, and the names are fixed Chinese terms
    that cannot be guessed reliably. This prints all of them, grouped by the
    modality they belong to.
    """
    session = require_web_session(ctx, hint=WEB_AUTH_HINT)

    try:
        tags = plaza_module.list_dataset_tags(session=session)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception:
        _handle_error(ctx, "APIError", "Could not list dataset tags.", EXIT_API_ERROR)
        return

    rows = [{"name": tag.name, "category": tag.category} for tag in tags]
    if ctx.json_output:
        click.echo(json_formatter.format_json({"items": rows, "total": len(rows)}))
        return

    if not rows:
        click.echo("No dataset tags found.")
        return

    # The vocabulary is small and fixed, so it is never truncated: a partial
    # list of accepted values would be worse than none.
    headers = ["Name", "Category"]
    widths = [
        column_width(headers[0], [row["name"] for row in rows]),
        column_width(headers[1], [row["category"] for row in rows]),
    ]
    values = [[row["name"], row["category"]] for row in rows]
    click.echo("\n".join(render_table(headers, values, widths, line_char="─")))


__all__ = [
    "dataset_applications_cmd",
    "list_dataset_tags_cmd",
    "list_datasets_cmd",
    "show_dataset_cmd",
    "validate_datasets_cmd",
]
