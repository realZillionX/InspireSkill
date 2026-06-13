"""Job subcommands (excluding create/logs)."""

from __future__ import annotations

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_JOB_NOT_FOUND,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.formatters.table import display_width, render_table
from inspire.cli.utils.auth import AuthenticationError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import is_full_uuid, is_partial_id
from inspire.cli.utils.job_shell import (
    JobShellError,
    normalize_job_instances,
    open_job_shell,
    select_job_instance,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workspaces import resolve_workspace_query_scope, select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session

_STATUS_ALIAS_MAP = {
    "PENDING": {"PENDING", "job_pending", "job_creating"},
    "RUNNING": {"RUNNING", "job_running"},
    "QUEUING": {"QUEUING", "job_queuing"},
    "SUCCEEDED": {"SUCCEEDED", "job_succeeded"},
    "FAILED": {"FAILED", "job_failed"},
    "CANCELLED": {"CANCELLED", "job_cancelled", "job_stopped"},
}
_STATUS_API_ALIAS_MAP = {
    "PENDING": ("job_pending", "job_creating"),
    "RUNNING": ("job_running",),
    "QUEUING": ("job_queuing",),
    "SUCCEEDED": ("job_succeeded",),
    "FAILED": ("job_failed",),
    "CANCELLED": ("job_cancelled", "job_stopped"),
}
_JOB_ACTIVE_API_STATUSES = ("job_pending", "job_creating", "job_queuing", "job_running")
_JOB_ACTIVE_STATUSES = {
    "PENDING",
    "job_pending",
    "job_creating",
    "QUEUING",
    "job_queuing",
    "RUNNING",
    "job_running",
}
_JOB_TERMINAL_STATUSES = {
    "SUCCEEDED",
    "job_succeeded",
    "FAILED",
    "job_failed",
    "CANCELLED",
    "job_cancelled",
    "job_stopped",
}


class WebJobResolutionError(Exception):
    """Raised when a web job name cannot be resolved safely."""


class WebJobValidationError(WebJobResolutionError):
    """Raised when web job resolution input violates the CLI boundary."""


def _expand_status_aliases(statuses: list[str] | tuple[str, ...] | None) -> set[str]:
    expanded: set[str] = set()
    for value in statuses or ():
        key = str(value).upper()
        expanded.update(_STATUS_ALIAS_MAP.get(key, {str(value)}))
    return expanded


def _api_statuses_for_filter(status: Optional[str]) -> tuple[str, ...]:
    raw = str(status or "").strip()
    if not raw:
        return ()
    if raw.startswith("job_"):
        return (raw,)
    return _STATUS_API_ALIAS_MAP.get(raw.upper(), ())


def _dedupe_job_rows(rows: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        key = (
            str(row.get("job_id") or ""),
            str(row.get("workspace_name") or ""),
            str(row.get("name") or ""),
            str(row.get("created_at") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _looks_like_workspace_id(value: str) -> bool:
    return value.strip().lower().startswith("ws-")


def _looks_like_job_id(value: str) -> bool:
    return value.strip().lower().startswith("job-")


def _close_web_client() -> None:
    try:
        from inspire.platform.web.session import _close_browser_client

        _close_browser_client()
    except Exception:
        pass


def _resolve_explicit_workspace(config: Config, workspace: Optional[str], session) -> Optional[str]:  # noqa: ANN001
    if workspace is None:
        return None
    workspace = workspace.strip()
    if not workspace:
        raise ConfigError("Workspace cannot be empty")
    if workspace == "-A":
        raise ConfigError("--workspace requires a workspace name; -A is not accepted here.")
    if _looks_like_workspace_id(workspace):
        raise ConfigError(
            "--workspace takes a workspace name. "
            "See `inspire config context` for available names."
        )
    return select_workspace_id(config, explicit_workspace_name=workspace, session=session)


def _workspace_name(session, workspace_id: str) -> str:  # noqa: ANN001
    names = getattr(session, "all_workspace_names", None) or {}
    if isinstance(names, dict):
        return str(names.get(workspace_id) or "")
    return ""


def _current_user_id(session) -> str:  # noqa: ANN001
    me = browser_api_module.get_current_user(session=session)
    user_id = str(me.get("id") or me.get("user_id") or "").strip()
    if not user_id:
        raise ValueError("Cannot determine the current user from the live web session.")
    return user_id


def _list_workspace_ids(
    config: Config,
    session,  # noqa: ANN001
    *,
    workspace: Optional[str],
) -> list[str]:
    """Pick workspace_ids for a job-list call.

    Query commands require ``--workspace <name|all>`` and never inherit the
    browser session's active workspace.
    """
    workspace_ids, _ = resolve_workspace_query_scope(config, workspace=workspace, session=session)
    return workspace_ids


def _job_matches_name(job, query: Optional[str]) -> bool:  # noqa: ANN001
    if not query:
        return True
    needle = query.lower()
    haystack = " ".join(
        [
            job.name or "",
            job.command or "",
            job.project_name or "",
            job.compute_group_name or "",
            job.created_by_name or "",
        ]
    ).lower()
    return needle in haystack


def _job_info_to_row(job, *, workspace_name: str = "") -> dict:  # noqa: ANN001
    return {
        "job_id": job.job_id or "N/A",
        "name": job.name or "N/A",
        "status": job.status or "N/A",
        "created_at": job.created_at or "N/A",
        "finished_at": job.finished_at or "",
        "created_by_name": job.created_by_name or "",
        "created_by_id": job.created_by_id or "",
        "project_name": job.project_name or "",
        "project_id": job.project_id or "",
        "compute_group_name": job.compute_group_name or "",
        "gpu_type": job.gpu_type or "",
        "gpu_count": job.gpu_count,
        "instance_count": job.instance_count,
        "priority": job.priority,
        "workspace_id": job.workspace_id or "",
        "workspace_name": workspace_name,
        "command": job.command or "",
    }


def _job_list_page_size(limit: Optional[int]) -> int:
    if limit is not None and limit > 0:
        return min(limit, 100)
    return 100


def _job_list_limit_value(limit: Optional[int]) -> int | None:
    if limit is None or limit <= 0:
        return None
    return limit


def _limit_job_rows_per_workspace(rows: list[dict], limit: Optional[int]) -> list[dict]:
    limit_value = _job_list_limit_value(limit)
    if limit_value is None:
        return rows
    counts: dict[str, int] = {}
    limited: list[dict] = []
    for row in rows:
        workspace_key = str(row.get("workspace_id") or row.get("workspace_name") or "")
        current = counts.get(workspace_key, 0)
        if current >= limit_value:
            continue
        counts[workspace_key] = current + 1
        limited.append(row)
    return limited


def _scan_web_jobs_round_robin(
    *,
    session,  # noqa: ANN001
    workspace_ids: list[str],
    creator_id: Optional[str],
    api_status: Optional[str],
    allowed_statuses: set[str] | None,
    name: Optional[str],
    page_num: int,
    page_size: int,
    max_pages: int,
    limit: Optional[int],
) -> tuple[list[dict], list[dict]]:
    """Scan all candidate workspaces one page at a time."""
    rows: list[dict] = []
    limit_value = _job_list_limit_value(limit)
    workspace_states: list[dict] = [
        {
            "workspace_id": workspace_id,
            "workspace_name": _workspace_name(session, workspace_id) if workspace_id else "",
            "next_page": max(1, page_num),
            "pages": 0,
            "total": 0,
            "matched": 0,
            "done": False,
        }
        for workspace_id in workspace_ids
    ]

    while any(not state["done"] for state in workspace_states):
        active_states = [state for state in workspace_states if not state["done"]]
        if not active_states:
            break

        def fetch_page(state: dict) -> tuple[dict, list, int]:  # noqa: ANN001
            workspace_id = str(state["workspace_id"] or "")
            current_page = int(state["next_page"])
            items, total = browser_api_module.list_jobs(
                workspace_id=workspace_id or None,
                created_by=creator_id,
                status=api_status,
                keyword=name,
                page_num=current_page,
                page_size=page_size,
                session=session,
            )
            return state, items, total

        max_workers = min(len(active_states), 4)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(fetch_page, state): state for state in active_states}
            for future in as_completed(future_map):
                state, items, total = future.result()
                current_page = int(state["next_page"])
                state["pages"] += 1
                state["total"] = total

                for job in items:
                    if allowed_statuses and job.status not in allowed_statuses:
                        continue
                    if not _job_matches_name(job, name):
                        continue
                    if limit_value is not None and int(state["matched"]) >= limit_value:
                        state["done"] = True
                        break
                    rows.append(_job_info_to_row(job, workspace_name=state["workspace_name"]))
                    state["matched"] = int(state["matched"]) + 1

                if limit_value is not None and int(state["matched"]) >= limit_value:
                    state["done"] = True
                    continue
                if not items:
                    state["done"] = True
                    continue
                if total is not None and current_page * page_size >= int(total):
                    state["done"] = True
                    continue
                if int(state["pages"]) >= max_pages:
                    state["done"] = True
                    continue
                state["next_page"] = current_page + 1

    scanned = [
        {
            "workspace_id": state["workspace_id"],
            "workspace_name": state["workspace_name"],
            "total": state["total"],
            "pages": state["pages"],
        }
        for state in workspace_states
        if int(state["pages"]) > 0
    ]
    return rows, scanned


def _format_web_job_status(job_data: dict) -> str:
    if not job_data:
        return "No web job detail found."

    created_by_payload = job_data.get("created_by")
    created_by: dict = created_by_payload if isinstance(created_by_payload, dict) else {}
    framework_config = job_data.get("framework_config") or []
    first_spec = (
        framework_config[0] if framework_config and isinstance(framework_config[0], dict) else {}
    )
    price_info = first_spec.get("instance_spec_price_info") or {}
    gpu_info = price_info.get("gpu_info") or {}

    fields = [
        ("Name", job_data.get("name") or "N/A"),
        ("Status", job_data.get("status") or "N/A"),
        ("Project", job_data.get("project_name") or ""),
        ("Compute Group", job_data.get("logic_compute_group_name") or ""),
        ("Priority", job_data.get("priority_name") or job_data.get("priority") or ""),
        ("Priority Level", job_data.get("priority_level") or ""),
        ("Created By", created_by.get("name") or ""),
        ("Created", human_formatter.format_epoch(job_data.get("created_at"))),
        ("Framework", job_data.get("framework") or ""),
        ("Instances", first_spec.get("instance_count") or job_data.get("node_count") or ""),
        ("Per Instance GPU", first_spec.get("gpu_count") or ""),
        ("Per Instance CPU", first_spec.get("cpu") or price_info.get("cpu_count") or ""),
        ("Per Instance Mem", f"{first_spec.get('mem_gi')} GiB" if first_spec.get("mem_gi") else ""),
        ("Per Instance SHM", f"{first_spec.get('shm_gi')} GiB" if first_spec.get("shm_gi") else ""),
        ("GPU Type", gpu_info.get("gpu_type_display") or ""),
        ("Image", first_spec.get("image") or ""),
        ("Description", job_data.get("description") or ""),
    ]

    lines = ["Web Job Status"]
    for label, value in fields:
        if value not in (None, ""):
            lines.append(f"{label}: {scrub_raw_ids(value)}")
    command = str(job_data.get("command") or "").strip()
    if command:
        lines.append("Command:")
        lines.append(scrub_raw_ids(command))
    return "\n".join(lines)


def _format_job_instances(instances: list[dict]) -> str:
    if not instances:
        return "No job instances found."

    rendered = [
        {
            "name": scrub_raw_ids(i.get("name") or ""),
            "status": scrub_raw_ids(i.get("instance_status") or ""),
            "type": scrub_raw_ids(i.get("instance_type") or ""),
            "node": scrub_raw_ids(i.get("node") or ""),
            "created": human_formatter.format_epoch(i.get("created_at")),
        }
        for i in instances
    ]
    name_w = max(len("Instance"), *(len(str(i["name"])) for i in rendered))
    status_w = max(len("Status"), *(len(str(i["status"])) for i in rendered))
    type_w = max(len("Type"), *(len(str(i["type"])) for i in rendered))
    node_w = max(len("Node"), *(len(str(i["node"])) for i in rendered))
    header = (
        f"{'Instance':<{name_w}} {'Status':<{status_w}} "
        f"{'Type':<{type_w}} {'Node':<{node_w}} {'Created'}"
    )
    sep = "-" * len(header)
    lines = ["Job Instances", header, sep]
    for inst in rendered:
        lines.append(
            f"{inst['name']:<{name_w}} "
            f"{inst['status']:<{status_w}} "
            f"{inst['type']:<{type_w}} "
            f"{inst['node']:<{node_w}} "
            f"{inst['created']}"
        )
    lines.append(sep)
    lines.append(f"Total: {len(instances)} instance(s)")
    return "\n".join(lines)


def _resolve_web_job_id(
    *,
    config: Config,
    job: str,
    workspace: Optional[str],
    all_workspaces: bool,
    max_pages: int,
    pick: Optional[int] = None,
    allow_raw_id: bool = False,
    scan_limit: Optional[int] = None,
    workspace_must_be_single: bool = False,
) -> str:
    job = (job or "").strip()
    if not job:
        raise WebJobResolutionError("Job name cannot be empty")
    if _looks_like_job_id(job):
        if allow_raw_id:
            return job
        raise WebJobValidationError(
            "CLI commands take a job name. "
            "Use `inspire job list --workspace <name|all>` to find the name."
        )
    if not allow_raw_id and (
        is_full_uuid(job, prefix="job-") or is_partial_id(job, prefix="job-")
    ):
        raise WebJobValidationError(
            "CLI commands take a job name. "
            "Use `inspire job list --workspace <name|all>` to find the name."
        )
    if workspace_must_be_single and (workspace or "").strip().lower() == "all":
        raise ConfigError("--workspace must be a workspace name for this command.")

    limit = 0 if pick is not None else 2
    page_size = max(1, int(scan_limit)) if scan_limit is not None else 100
    scan_pages = 1 if scan_limit is not None else max_pages
    rows, _ = _list_web_jobs(
        config=config,
        workspace=workspace,
        all_workspaces=all_workspaces,
        status=None,
        name=job,
        page_num=1,
        page_size=page_size,
        max_pages=scan_pages,
        limit=limit,
    )
    exact = [row for row in rows if row.get("name") == job]
    if pick is not None:
        candidate_rows = exact if exact else rows
        if pick < 1 or pick > len(candidate_rows):
            raise WebJobResolutionError(
                f"--pick {pick} out of range; {len(candidate_rows)} web jobs match "
                f"{scrub_raw_ids(job)!r}."
            )
        return str(candidate_rows[pick - 1]["job_id"])
    if len(exact) == 1:
        return str(exact[0]["job_id"])
    if len(exact) > 1:
        candidate_names = ", ".join(scrub_raw_ids(row.get("name") or "") for row in exact[:5])
        raise WebJobResolutionError(
            f"Multiple web jobs share name {scrub_raw_ids(job)!r}; refine the name. "
            f"Candidates: {candidate_names}"
        )
    if len(rows) == 1:
        return str(rows[0]["job_id"])
    if rows:
        candidate_names = ", ".join(scrub_raw_ids(row.get("name") or "") for row in rows[:5])
        raise WebJobResolutionError(
            f"Multiple web jobs match {scrub_raw_ids(job)!r}; pass the full job name. "
            f"Candidates: {candidate_names}"
        )
    hint_workspace = scrub_raw_ids(workspace or "all")
    hint = f"inspire job list --workspace {hint_workspace} --name {scrub_raw_ids(job)}"
    raise WebJobResolutionError(
        f"No web job matching {scrub_raw_ids(job)!r} found. "
        f"Try `{hint}`."
    )


def _format_job_list(rows: list[dict]) -> str:
    """Render jobs as a compact name-first table."""
    if not rows:
        return "No jobs found."

    rendered_rows = [
        {
            **r,
            "name": scrub_raw_ids(r.get("name", "")),
            "status": scrub_raw_ids(r.get("status", "")),
            "created_at": scrub_raw_ids(human_formatter.format_epoch(r.get("created_at"))),
            "workspace_name": scrub_raw_ids(r.get("workspace_name", "")),
            "created_by_name": scrub_raw_ids(r.get("created_by_name", "")),
        }
        for r in rows
    ]

    def width(header: str, values: list[str], *, max_width: int) -> int:
        return min(max(display_width(header), *(display_width(value) for value in values), 1), max_width)

    table_rows = [
        (
            str(row["name"]),
            str(row["status"]),
            str(row["created_at"]),
            str(row.get("workspace_name") or ""),
            str(row.get("created_by_name") or ""),
        )
        for row in rendered_rows
    ]
    widths = [
        width("Name", [row[0] for row in table_rows], max_width=120),
        width("Status", [row[1] for row in table_rows], max_width=16),
        width("Created", [row[2] for row in table_rows], max_width=19),
        width("Workspace", [row[3] for row in table_rows], max_width=24),
        width("Created By", [row[4] for row in table_rows], max_width=16),
    ]

    lines = [
        "Jobs",
        *render_table(
            ("Name", "Status", "Created", "Workspace", "Created By"),
            table_rows,
            widths,
            line_char="─",
        ),
    ]
    lines.append(f"Total: {len(rows)} job(s)")
    return "\n".join(lines)


def _list_web_jobs(
    *,
    config: Config,
    workspace: Optional[str],
    all_workspaces: bool,
    status: Optional[str],
    name: Optional[str],
    page_num: int,
    page_size: int,
    max_pages: int,
    limit: Optional[int],
    api_statuses: tuple[str, ...] | None = None,
) -> tuple[list[dict], list[dict]]:
    try:
        session = get_web_session()
        creator_id = _current_user_id(session)

        allowed_statuses = _expand_status_aliases([status]) if status else None
        mapped_api_statuses = tuple(api_statuses or ()) or _api_statuses_for_filter(status)
        query_statuses: tuple[str | None, ...] = mapped_api_statuses or (None,)
        rows: list[dict] = []
        scanned: list[dict] = []
        limit_value = _job_list_limit_value(limit)
        workspace_ids = _list_workspace_ids(
            config,
            session,
            workspace=workspace,
        )

        if name and (workspace or "").strip().lower() == "all":
            for query_status in query_statuses:
                status_rows, status_scanned = _scan_web_jobs_round_robin(
                    session=session,
                    workspace_ids=workspace_ids,
                    creator_id=creator_id,
                    api_status=query_status,
                    allowed_statuses=allowed_statuses,
                    name=name,
                    page_num=page_num,
                    page_size=page_size,
                    max_pages=max_pages,
                    limit=limit,
                )
                rows.extend(status_rows)
                scanned.extend(status_scanned)
            rows = _dedupe_job_rows(rows)
            rows.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return _limit_job_rows_per_workspace(rows, limit), scanned

        for workspace_id in workspace_ids:
            pages_read_total = 0
            total_seen = 0
            workspace_label = _workspace_name(session, workspace_id) if workspace_id else ""

            for query_status in query_statuses:
                current_page = max(1, page_num)
                pages_read = 0
                status_total = 0
                status_matched = 0

                while True:
                    if limit_value is not None and status_matched >= limit_value:
                        break
                    items, total = browser_api_module.list_jobs(
                        workspace_id=workspace_id or None,
                        created_by=creator_id,
                        status=query_status,
                        keyword=name,
                        page_num=current_page,
                        page_size=page_size,
                        session=session,
                    )
                    pages_read += 1
                    pages_read_total += 1
                    status_total = max(status_total, int(total or 0))

                    for job in items:
                        if allowed_statuses and job.status not in allowed_statuses:
                            continue
                        if not _job_matches_name(job, name):
                            continue
                        if limit_value is not None and status_matched >= limit_value:
                            break
                        rows.append(_job_info_to_row(job, workspace_name=workspace_label))
                        status_matched += 1

                    if limit_value is not None and status_matched >= limit_value:
                        break
                    if not items:
                        break
                    if total is not None and current_page * page_size >= int(total):
                        break
                    if pages_read >= max_pages:
                        break
                    current_page += 1

                total_seen += status_total

            scanned.append(
                {
                    "workspace_id": workspace_id,
                    "workspace_name": workspace_label,
                    "total": total_seen,
                    "pages": pages_read_total,
                }
            )

        rows = _dedupe_job_rows(rows)
        rows.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return _limit_job_rows_per_workspace(rows, limit), scanned
    finally:
        _close_web_client()


def _watch_jobs(
    ctx: Context,
    *,
    config: Config,
    workspace: Optional[str],
    all_workspaces: bool,
    status: Optional[str],
    name: Optional[str],
    page_size: int,
    max_pages: int,
    limit: Optional[int],
    interval: int,
    active: bool,
) -> None:
    """Continuously poll live platform results and re-render the job list."""
    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

    exclude_statuses: set[str] | None = None
    if active:
        exclude_statuses = set(_JOB_TERMINAL_STATUSES)

    completed_this_session: list[dict] = []
    completed_job_ids: set[str] = set()
    last_status_by_id: dict[str, str] = {}
    terminal_statuses = set(_JOB_TERMINAL_STATUSES)

    try:
        while True:
            jobs, scanned = _list_web_jobs(
                config=config,
                workspace=workspace,
                all_workspaces=all_workspaces,
                status=status,
                name=name,
                page_num=1,
                page_size=page_size,
                max_pages=max_pages,
                limit=limit,
                api_statuses=_JOB_ACTIVE_API_STATUSES if active and not status else None,
            )
            if exclude_statuses:
                jobs = [j for j in jobs if j.get("status") not in exclude_statuses]

            for job_item in jobs:
                jid = str(job_item.get("job_id") or "")
                cur_status = str(job_item.get("status") or "")
                prior = last_status_by_id.get(jid)
                if (
                    cur_status in terminal_statuses
                    and prior not in terminal_statuses
                    and jid
                    and jid not in completed_job_ids
                ):
                    completed_this_session.append(dict(job_item))
                    completed_job_ids.add(jid)
                if jid:
                    last_status_by_id[jid] = cur_status

            if ctx.json_output:
                timestamp = datetime.now().strftime("%H:%M:%S")
                click.echo(
                    json_formatter.format_json(
                        {
                            "event": "refresh",
                            "timestamp": timestamp,
                            "source": "web",
                            "jobs": jobs,
                            "scanned": scanned,
                            "completed_this_session": completed_this_session,
                        }
                    )
                )
            else:
                os.system("clear")
                click.echo(_format_job_list(jobs))
                if completed_this_session:
                    click.echo(f"\n✅ Completed This Session ({len(completed_this_session)})")
                    click.echo("─" * 60)
                    for entry in completed_this_session:
                        s = (entry.get("status") or "").lower()
                        emoji = "✅" if "succeeded" in s else "❌"
                        click.echo(
                            f"{scrub_raw_ids(entry.get('name', 'N/A')):<32}  "
                            f"{emoji} {scrub_raw_ids(entry.get('status', 'N/A'))}"
                        )
                click.echo(f"\n(refreshing every {interval}s; Ctrl+C to stop)")

            time.sleep(interval)

    except KeyboardInterrupt:
        if not ctx.json_output:
            click.echo("\nStopped watching.")
        sys.exit(EXIT_SUCCESS)
    finally:
        api_logger.setLevel(original_level)


@click.command("list")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help=(
        "Maximum jobs per workspace to query and display. "
        "If omitted, query all pages in each workspace."
    ),
)
@click.option("--status", "-s", help="Filter by status (PENDING, RUNNING, SUCCEEDED, FAILED)")
@click.option(
    "--active",
    "-a",
    is_flag=True,
    help="Show only active jobs (exclude failed, cancelled, stopped)",
)
@click.option("--watch", "-w", is_flag=True, help="Continuously refresh job list")
@click.option(
    "--interval",
    type=click.IntRange(1),
    default=10,
    show_default=True,
    help="Refresh interval in seconds for --watch",
)
@click.option("--workspace", required=True, help="Workspace name or 'all'.")
@click.option("--keyword", default=None, help="Case-insensitive keyword filter for job name/command")
@pass_context
def list_jobs(
    ctx: Context,
    limit: Optional[int],
    status: Optional[str],
    active: bool,
    watch: bool,
    interval: int,
    workspace: Optional[str],
    keyword: Optional[str],
) -> None:
    """List training jobs from the platform.

    Requires ``--workspace <name|all>``. Use ``all`` to fan out across every
    visible workspace.

    \b
    Example:
        inspire job list --workspace 分布式训练空间
        inspire job list --workspace 分布式训练空间 --limit 20 --status RUNNING
        inspire job list --workspace 分布式训练空间 --keyword qwen35
        inspire job list --workspace all --keyword qwen35 --limit 20
        inspire job list --workspace 分布式训练空间 --active
        inspire job list --workspace 分布式训练空间 --watch --active -n 20
    """
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)

        if watch:
            _watch_jobs(
                ctx,
                config=config,
                workspace=workspace,
                all_workspaces=False,
                status=status,
                name=keyword,
                page_size=_job_list_page_size(limit),
                max_pages=50,
                limit=limit,
                interval=interval,
                active=active,
            )
            return

        rows, scanned = _list_web_jobs(
            config=config,
            workspace=workspace,
            all_workspaces=False,
            status=status,
            name=keyword,
            page_num=1,
            page_size=_job_list_page_size(limit),
            max_pages=50,
            limit=limit,
            api_statuses=_JOB_ACTIVE_API_STATUSES if active and not status else None,
        )

        if active:
            rows = [j for j in rows if j.get("status") in _JOB_ACTIVE_STATUSES]

        if ctx.json_output:
            click.echo(
                json_formatter.format_json({"source": "web", "jobs": rows, "scanned": scanned})
            )
        else:
            click.echo(_format_job_list(rows))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


@click.command("id")
@click.argument("job")
@click.option("--workspace", required=True, help="Workspace name or 'all'.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous.",
)
@pass_context
def show_id(
    ctx: Context,
    job: str,
    workspace: Optional[str],
    pick: Optional[int],
) -> None:
    """Print the platform ID for a training job name."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        job_id = _resolve_web_job_id(
            config=config,
            job=job,
            workspace=workspace,
            all_workspaces=False,
            max_pages=50,
            pick=pick,
        )
        if ctx.json_output:
            click.echo(json_formatter.format_json({"name": job, "id": job_id}, allow_ids=True))
        else:
            click.echo(job_id)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except WebJobValidationError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("status")
@click.argument("job")
@click.option("--workspace", required=True, help="Workspace name or 'all'.")
@pass_context
def status(
    ctx: Context,
    job: str,
    workspace: Optional[str],
) -> None:
    """Check the status of a training job.

    JOB is the name shown in `inspire job list`.

    \b
    Example:
        inspire job status my-training-run --workspace 分布式训练空间
    """
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        job_id = _resolve_web_job_id(
            config=config,
            job=job,
            workspace=workspace,
            all_workspaces=False,
            max_pages=50,
        )
        try:
            session = get_web_session()
            job_data = browser_api_module.get_job_detail_v2(job_id, session=session)
        finally:
            _close_web_client()

        if ctx.json_output:
            click.echo(json_formatter.format_json({"source": "web", "job": job_data}))
        else:
            click.echo(_format_web_job_status(job_data))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except WebJobValidationError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid job id" in msg:
            _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("instances")
@click.argument("job")
@click.option(
    "--workspace",
    required=True,
    help="Workspace name.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=500,
    show_default=True,
    help="Maximum instances to query and display.",
)
@pass_context
def instances(
    ctx: Context,
    job: str,
    workspace: Optional[str],
    limit: int,
) -> None:
    """List pod-level instances for a distributed-training job."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        job_id = _resolve_web_job_id(
            config=config,
            job=job,
            workspace=workspace,
            all_workspaces=False,
            max_pages=1,
            scan_limit=limit,
        )
        try:
            session = get_web_session()
            rows, total = browser_api_module.list_job_instances(
                job_id,
                limit=limit,
                session=session,
            )
        finally:
            _close_web_client()

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"source": "web", "job_id": job_id, "instances": rows, "total": total}
                )
            )
        else:
            click.echo(_format_job_instances(rows))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("stop")
@click.argument("job")
@click.option("--workspace", required=True, help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous.",
)
@pass_context
def stop(ctx: Context, job: str, workspace: Optional[str], pick: Optional[int]) -> None:
    """Stop a running training job.

    \b
    Example:
        inspire job stop my-training-run --workspace 分布式训练空间
    """
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        job_id = _resolve_web_job_id(
            config=config,
            job=job,
            workspace=workspace,
            all_workspaces=False,
            max_pages=50,
            pick=pick,
            workspace_must_be_single=True,
        )
        session = get_web_session()
        browser_api_module.stop_training_job(job_id, session=session)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json({"name": job, "job_id": job_id, "status": "stopped"})
            )
        else:
            click.echo(human_formatter.format_success(f"Job stopped: {job}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid job id" in msg:
            _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("delete")
@click.argument("job")
@click.option("--workspace", required=True, help="Workspace name.")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous.",
)
@pass_context
def delete(ctx: Context, job: str, workspace: Optional[str], yes: bool, pick: Optional[int]) -> None:
    """Permanently delete a training job entry from the platform.

    \b
    The entry disappears from the platform distributed-training list.
    This cannot be undone; if the job is still running, `stop` it first.

    \b
    Example:
        inspire job delete my-training-run --workspace 分布式训练空间
    """
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        job_id = _resolve_web_job_id(
            config=config,
            job=job,
            workspace=workspace,
            all_workspaces=False,
            max_pages=50,
            pick=pick,
            workspace_must_be_single=True,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        return

    if not yes and not ctx.json_output:
        click.confirm(
            f"Permanently delete training job '{scrub_raw_ids(job)}'? This cannot be undone.",
            abort=True,
        )

    try:
        session = get_web_session()
        result = browser_api_module.delete_job(job_id=job_id, session=session)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"name": job, "job_id": job_id, "status": "deleted", "result": result}
                )
            )
        else:
            click.echo(human_formatter.format_success(f"Job deleted: {job}"))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid job id" in msg:
            _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("wait")
@click.argument("job")
@click.option(
    "--timeout",
    type=click.IntRange(1),
    default=14400,
    help="Timeout in seconds (default: 4 hours)",
)
@click.option(
    "--interval",
    type=click.IntRange(1),
    default=30,
    help="Poll interval in seconds (default: 30)",
)
@click.option("--workspace", required=True, help="Workspace name.")
@pass_context
def wait(
    ctx: Context,
    job: str,
    timeout: int,
    interval: int,
    workspace: Optional[str],
) -> None:
    """Wait for a job to complete.

    Polls the job status until it reaches a terminal state
    (SUCCEEDED, FAILED, or CANCELLED).

    \b
    Example:
        inspire job wait my-training-run --workspace 分布式训练空间 --timeout 7200
    """
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        job_id = _resolve_web_job_id(
            config=config,
            job=job,
            workspace=workspace,
            all_workspaces=False,
            max_pages=50,
        )

        terminal_statuses = {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "job_succeeded",
            "job_failed",
            "job_cancelled",
        }
        start_time = time.time()
        last_status = None

        if not ctx.json_output:
            click.echo(
                f"Waiting for job {scrub_raw_ids(job)} (timeout: {timeout}s, interval: {interval}s)"
            )

        while True:
            elapsed = time.time() - start_time

            if elapsed > timeout:
                _handle_error(ctx, "Timeout", f"Timeout after {timeout}s", EXIT_TIMEOUT)
                return

            try:
                try:
                    session = get_web_session()
                    job_data = browser_api_module.get_job_detail_v2(job_id, session=session)
                finally:
                    _close_web_client()
                current_status = job_data.get("status", "UNKNOWN")

                if current_status != last_status:
                    if ctx.json_output:
                        click.echo(
                            json_formatter.format_json(
                                {
                                    "event": "status_change",
                                    "status": current_status,
                                    "elapsed_seconds": int(elapsed),
                                }
                            )
                        )
                    else:
                        click.echo(f"\nStatus: {scrub_raw_ids(current_status)}")
                    last_status = current_status
                else:
                    if not ctx.json_output:
                        mins = int(elapsed // 60)
                        secs = int(elapsed % 60)
                        click.echo(
                            f"\r[{mins:02d}:{secs:02d}] Waiting... "
                            f"Status: {scrub_raw_ids(current_status)}",
                            nl=False,
                        )

                if current_status in terminal_statuses:
                    if ctx.json_output:
                        click.echo(json_formatter.format_json(job_data))
                    else:
                        click.echo("")
                        click.echo(human_formatter.format_job_status(job_data))

                    if current_status in {"SUCCEEDED", "job_succeeded"}:
                        sys.exit(EXIT_SUCCESS)
                    sys.exit(EXIT_GENERAL_ERROR)

            except Exception as e:
                if not ctx.json_output:
                    click.echo(f"\nWarning: Failed to get status: {scrub_raw_ids(e)}")

            time.sleep(interval)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except KeyboardInterrupt:
        if not ctx.json_output:
            click.echo("\nInterrupted")
        sys.exit(EXIT_GENERAL_ERROR)


@click.command("command")
@click.argument("job")
@click.option("--workspace", required=True, help="Workspace name or 'all'.")
@pass_context
def show_command(
    ctx: Context,
    job: str,
    workspace: Optional[str],
) -> None:
    """Show the training command used for a job."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        job_id = _resolve_web_job_id(
            config=config,
            job=job,
            workspace=workspace,
            all_workspaces=False,
            max_pages=50,
        )
        try:
            session = get_web_session()
            job_data = browser_api_module.get_job_detail_v2(job_id, session=session)
        finally:
            _close_web_client()
        source = "web"
        command_value = job_data.get("command")

        if not command_value:
            _handle_error(
                ctx,
                "CommandNotFound",
                f"No command found for job {job}",
                EXIT_API_ERROR,
            )
            return

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"job_id": job_id, "command": command_value, "source": source}
                )
            )
        else:
            click.echo(scrub_raw_ids(command_value))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid job id" in msg:
            _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("shell")
@click.argument("job")
@click.option("--rank", type=click.IntRange(0), default=None, help="Open the running instance with this rank")
@click.option("--instance", "instance_name", default=None, help="Open this exact instance name")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help="Pick the Nth matching job (1-indexed) when multiple jobs share a name",
)
@click.option("--workspace", required=True, help="Workspace name.")
@pass_context
def shell(
    ctx: Context,
    job: str,
    rank: Optional[int],
    instance_name: Optional[str],
    pick: Optional[int],
    workspace: Optional[str],
) -> None:
    """Open an interactive shell inside a running training-job instance.

    \b
    Examples:
        inspire job shell my-training-run --workspace 分布式训练空间
        inspire job shell my-training-run --workspace 分布式训练空间 --rank 0
        inspire job shell my-training-run --workspace 分布式训练空间 --instance pytorchjob-worker-0
        inspire job shell my-training-run --workspace 分布式训练空间 --pick 2
    """
    if rank is not None and instance_name is not None:
        _handle_error(
            ctx,
            "ValidationError",
            "Use only one of --rank or --instance.",
            EXIT_VALIDATION_ERROR,
        )
        return

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        job_id = _resolve_web_job_id(
            config=config,
            job=job,
            workspace=workspace,
            all_workspaces=False,
            max_pages=50,
            pick=pick,
            allow_raw_id=False,
            workspace_must_be_single=True,
        )
        session = get_web_session()
        try:
            raw_instances, _ = browser_api_module.list_job_instances(
                job_id,
                limit=200,
                session=session,
            )
        finally:
            _close_web_client()

        selected = select_job_instance(
            normalize_job_instances(raw_instances),
            instance_name=instance_name,
            rank=rank,
            prompt=not ctx.json_output,
        )

        if not ctx.json_output:
            click.echo(
                f"Opening shell: {scrub_raw_ids(job)} / {scrub_raw_ids(selected.name)}",
                err=True,
            )
            click.echo("Press Ctrl-] to disconnect.", err=True)

        code = open_job_shell(job_id=job_id, instance_name=selected.name, session=session)
        sys.exit(code)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except WebJobValidationError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except JobShellError as e:
        _handle_error(ctx, "JobShellError", str(e), EXIT_GENERAL_ERROR)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = [
    "instances",
    "list_jobs",
    "shell",
    "show_command",
    "show_id",
    "status",
    "stop",
    "delete",
    "wait",
]
