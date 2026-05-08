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
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.job_cli import resolve_job_id
from inspire.cli.utils.job_shell import (
    JobShellError,
    normalize_job_instances,
    open_job_shell,
    select_job_instance,
)
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
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


class WebJobResolutionError(Exception):
    """Raised when a web job name/id cannot be resolved safely."""


def _expand_status_aliases(statuses: list[str] | tuple[str, ...] | None) -> set[str]:
    expanded: set[str] = set()
    for value in statuses or ():
        key = str(value).upper()
        expanded.update(_STATUS_ALIAS_MAP.get(key, {str(value)}))
    return expanded


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


def _resolve_explicit_workspace(config: Config, workspace: Optional[str]) -> Optional[str]:
    if workspace is None:
        return None
    workspace = workspace.strip()
    if not workspace:
        raise ConfigError("Workspace cannot be empty")
    if _looks_like_workspace_id(workspace):
        return select_workspace_id(config, explicit_workspace_id=workspace)
    return select_workspace_id(config, explicit_workspace_name=workspace)


def _workspace_name(session, workspace_id: str) -> str:  # noqa: ANN001
    names = getattr(session, "all_workspace_names", None) or {}
    if isinstance(names, dict):
        return str(names.get(workspace_id) or "")
    return ""


def _list_workspace_ids(
    config: Config,
    session,  # noqa: ANN001
    *,
    explicit_workspace_id: Optional[str],
    all_workspaces: bool,
) -> list[str]:
    """Pick workspace_ids for a job-list call.

    Precedence:
      1. ``--workspace`` explicit alias / name
      2. ``-A`` widens to the union of ``[workspaces]`` alias-map values and
         the SSO session's known workspaces (alias map preferred when present
         per the v4 contract; SSO list as a backstop when discover hasn't run)
      3. Default = SSO session's workspace
    """
    if explicit_workspace_id:
        return [explicit_workspace_id]

    if all_workspaces:
        seen: set[str] = set()
        ordered: list[str] = []
        current = str(getattr(session, "workspace_id", "") or "").strip()
        if current:
            ordered.append(current)
            seen.add(current)
        # Union of [workspaces] alias-map values (user-curated via
        # `inspire init --discover`) AND session.all_workspace_ids (whatever
        # SSO sees). Either source alone could miss workspaces the user
        # actually wants to scan, so we widen across both.
        alias_values = [str(v).strip() for v in (config.workspaces or {}).values() if v]
        for wid in alias_values:
            if wid and wid not in seen:
                ordered.append(wid)
                seen.add(wid)
        for wid in getattr(session, "all_workspace_ids", None) or []:
            wid_s = str(wid or "").strip()
            if wid_s and wid_s not in seen:
                ordered.append(wid_s)
                seen.add(wid_s)
        return ordered or [""]

    current = str(getattr(session, "workspace_id", "") or "").strip()
    return [current] if current else [""]


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
    limit: int,
) -> tuple[list[dict], list[dict]]:
    """Scan all candidate workspaces one page at a time."""
    rows: list[dict] = []
    workspace_states: list[dict] = [
        {
            "workspace_id": workspace_id,
            "workspace_name": _workspace_name(session, workspace_id) if workspace_id else "",
            "next_page": max(1, page_num),
            "pages": 0,
            "total": 0,
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

        limit_reached = False
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
                    rows.append(_job_info_to_row(job, workspace_name=state["workspace_name"]))

                if limit > 0 and len(rows) >= limit:
                    limit_reached = True
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
        if limit_reached:
            for remaining in workspace_states:
                remaining["done"] = True

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
            lines.append(f"{label}: {value}")
    command = str(job_data.get("command") or "").strip()
    if command:
        lines.append("Command:")
        lines.append(command)
    return "\n".join(lines)


def _format_job_instances(instances: list[dict]) -> str:
    if not instances:
        return "No job instances found."

    name_w = max(len("Instance"), *(len(str(i.get("name") or "")) for i in instances))
    status_w = max(len("Status"), *(len(str(i.get("instance_status") or "")) for i in instances))
    type_w = max(len("Type"), *(len(str(i.get("instance_type") or "")) for i in instances))
    node_w = max(len("Node"), *(len(str(i.get("node") or "")) for i in instances))
    header = (
        f"{'Instance':<{name_w}} {'Status':<{status_w}} "
        f"{'Type':<{type_w}} {'Node':<{node_w}} {'Created'}"
    )
    sep = "-" * len(header)
    lines = ["Job Instances", header, sep]
    for inst in instances:
        lines.append(
            f"{str(inst.get('name') or ''):<{name_w}} "
            f"{str(inst.get('instance_status') or ''):<{status_w}} "
            f"{str(inst.get('instance_type') or ''):<{type_w}} "
            f"{str(inst.get('node') or ''):<{node_w}} "
            f"{human_formatter.format_epoch(inst.get('created_at'))}"
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
    all_users: bool,
    created_by: Optional[str],
    max_pages: int,
    pick: Optional[int] = None,
) -> str:
    job = (job or "").strip()
    if not job:
        raise WebJobResolutionError("Job name/id cannot be empty")
    if _looks_like_job_id(job):
        return job

    limit = 0 if pick is not None else 2
    rows, _ = _list_web_jobs(
        config=config,
        workspace=workspace,
        all_workspaces=all_workspaces,
        all_users=all_users,
        created_by=created_by,
        status=None,
        name=job,
        page_num=1,
        page_size=100,
        max_pages=max_pages,
        limit=limit,
    )
    exact = [row for row in rows if row.get("name") == job]
    if pick is not None:
        candidate_rows = exact if exact else rows
        if pick < 1 or pick > len(candidate_rows):
            raise WebJobResolutionError(
                f"--pick {pick} out of range; {len(candidate_rows)} web jobs match {job!r}."
            )
        return str(candidate_rows[pick - 1]["job_id"])
    if len(exact) == 1:
        return str(exact[0]["job_id"])
    if len(exact) > 1:
        candidate_names = ", ".join(str(row.get("name") or "") for row in exact[:5])
        raise WebJobResolutionError(
            f"Multiple web jobs share name {job!r}; refine the name. Candidates: {candidate_names}"
        )
    if len(rows) == 1:
        return str(rows[0]["job_id"])
    if rows:
        candidate_names = ", ".join(str(row.get("name") or "") for row in rows[:5])
        raise WebJobResolutionError(
            f"Multiple web jobs match {job!r}; pass the full job name. Candidates: {candidate_names}"
        )
    raise WebJobResolutionError(
        f"No web job matching {job!r} found. Try `inspire job list -A --name {job}`."
    )


def _format_job_list(rows: list[dict]) -> str:
    """Render jobs as a table without raw ids.

    The v2 user boundary takes names only — surfacing ``job-<uuid>`` in
    the listing invites agents to round-trip them and then hit
    ``reject_id_at_boundary`` later. JSON output keeps every field for
    scripts.
    """
    if not rows:
        return "No jobs found."

    name_w = max(len("Name"), *(len(str(r["name"])) for r in rows))
    status_w = max(len("Status"), *(len(str(r["status"])) for r in rows))
    created_w = max(len("Created"), *(len(str(r["created_at"])) for r in rows))
    workspace_w = max(
        len("Workspace"),
        *(len(str(r.get("workspace_name") or "")) for r in rows),
    )
    user_w = max(len("Created By"), *(len(str(r.get("created_by_name") or "")) for r in rows))

    header = (
        f"{'Name':<{name_w}}  {'Status':<{status_w}}  "
        f"{'Created':<{created_w}}  {'Workspace':<{workspace_w}}  {'Created By':<{user_w}}"
    )
    sep = "-" * len(header)
    lines = ["Jobs", header, sep]
    for row in rows:
        workspace = str(row.get("workspace_name") or "")
        created_by = str(row.get("created_by_name") or "")
        lines.append(
            f"{str(row['name']):<{name_w}}  "
            f"{str(row['status']):<{status_w}}  "
            f"{str(row['created_at']):<{created_w}}  "
            f"{workspace:<{workspace_w}}  "
            f"{created_by:<{user_w}}"
        )
    lines.append(sep)
    lines.append(f"Total: {len(rows)} job(s)")
    return "\n".join(lines)


def _list_web_jobs(
    *,
    config: Config,
    workspace: Optional[str],
    all_workspaces: bool,
    all_users: bool,
    created_by: Optional[str],
    status: Optional[str],
    name: Optional[str],
    page_num: int,
    page_size: int,
    max_pages: int,
    limit: int,
) -> tuple[list[dict], list[dict]]:
    try:
        explicit_workspace_id = _resolve_explicit_workspace(config, workspace)
        session = get_web_session()

        creator_id = (created_by or "").strip() or None
        if creator_id is None and not all_users:
            me = browser_api_module.get_current_user(session=session)
            creator_id = str(me.get("id") or me.get("user_id") or "").strip() or None

        allowed_statuses = _expand_status_aliases([status]) if status else None
        api_status = status if status and status.startswith("job_") else None
        rows: list[dict] = []
        scanned: list[dict] = []
        workspace_ids = _list_workspace_ids(
            config,
            session,
            explicit_workspace_id=explicit_workspace_id,
            all_workspaces=all_workspaces,
        )

        if name and all_workspaces and not explicit_workspace_id:
            rows, scanned = _scan_web_jobs_round_robin(
                session=session,
                workspace_ids=workspace_ids,
                creator_id=creator_id,
                api_status=api_status,
                allowed_statuses=allowed_statuses,
                name=name,
                page_num=page_num,
                page_size=page_size,
                max_pages=max_pages,
                limit=limit,
            )
            rows.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            if limit > 0:
                rows = rows[:limit]
            return rows, scanned

        for workspace_id in workspace_ids:
            current_page = max(1, page_num)
            pages_read = 0
            total = 0
            workspace_label = _workspace_name(session, workspace_id) if workspace_id else ""

            while True:
                items, total = browser_api_module.list_jobs(
                    workspace_id=workspace_id or None,
                    created_by=creator_id,
                    status=api_status,
                    keyword=name,
                    page_num=current_page,
                    page_size=page_size,
                    session=session,
                )
                pages_read += 1

                for job in items:
                    if allowed_statuses and job.status not in allowed_statuses:
                        continue
                    if not _job_matches_name(job, name):
                        continue
                    rows.append(_job_info_to_row(job, workspace_name=workspace_label))

                if not name:
                    break
                if limit > 0 and len(rows) >= limit:
                    break
                if not items:
                    break
                if total is not None and current_page * page_size >= int(total):
                    break
                if pages_read >= max_pages:
                    break
                current_page += 1

            scanned.append(
                {
                    "workspace_id": workspace_id,
                    "workspace_name": workspace_label,
                    "total": total,
                    "pages": pages_read,
                }
            )

            if limit > 0 and len(rows) >= limit:
                break

        rows.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        if limit > 0:
            rows = rows[:limit]
        return rows, scanned
    finally:
        _close_web_client()


def _watch_jobs(
    ctx: Context,
    *,
    config: Config,
    workspace: Optional[str],
    all_workspaces: bool,
    all_users: bool,
    created_by: Optional[str],
    status: Optional[str],
    name: Optional[str],
    page_size: int,
    max_pages: int,
    limit: int,
    interval: int,
    active: bool,
) -> None:
    """Continuously poll the web API and re-render the job list."""
    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

    exclude_statuses: set[str] | None = None
    if active:
        exclude_statuses = {"FAILED", "job_failed", "CANCELLED", "job_cancelled", "job_stopped"}

    completed_this_session: list[dict] = []
    completed_job_ids: set[str] = set()
    last_status_by_id: dict[str, str] = {}
    terminal_statuses = {
        "SUCCEEDED",
        "job_succeeded",
        "FAILED",
        "job_failed",
        "CANCELLED",
        "job_cancelled",
        "job_stopped",
    }

    try:
        while True:
            jobs, scanned = _list_web_jobs(
                config=config,
                workspace=workspace,
                all_workspaces=all_workspaces,
                all_users=all_users,
                created_by=created_by,
                status=status,
                name=name,
                page_num=1,
                page_size=page_size,
                max_pages=max_pages,
                limit=limit,
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
                            f"{str(entry.get('name', 'N/A')):<32}  "
                            f"{emoji} {entry.get('status', 'N/A')}"
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
@click.option("--limit", "-n", type=int, default=0, help="Max jobs to show (0 = all)")
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
    type=int,
    default=10,
    show_default=True,
    help="Refresh interval in seconds for --watch",
)
@click.option("--web", is_flag=True, help="Accepted for compatibility; list always uses Web API")
@click.option("--workspace", default=None, help="Workspace alias or name")
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="Search every visible workspace (current + [workspaces] alias values)",
)
@click.option("--name", default=None, help="Case-insensitive keyword filter for job name/command")
@click.option(
    "--all-users",
    is_flag=True,
    help="Include jobs from all users (default: current user only)",
)
@click.option("--created-by", default=None, help="Filter by creator user ID")
@click.option("--page-num", type=int, default=1, show_default=True, help="Web list page number")
@click.option("--page-size", type=int, default=100, show_default=True, help="Web list page size")
@click.option(
    "--max-pages",
    type=int,
    default=50,
    show_default=True,
    help="Max web pages to scan per workspace when --name is set",
)
@pass_context
def list_jobs(
    ctx: Context,
    limit: int,
    status: Optional[str],
    active: bool,
    watch: bool,
    interval: int,
    web: bool,
    workspace: Optional[str],
    all_workspaces: bool,
    name: Optional[str],
    all_users: bool,
    created_by: Optional[str],
    page_num: int,
    page_size: int,
    max_pages: int,
) -> None:
    """List training jobs from the platform Web API.

    Default scope is the active workspace from your SSO session. Pass
    ``-A`` to fan out across every workspace alias (the union of your
    account's ``[workspaces]`` values + the SSO-visible workspaces).

    \b
    Example:
        inspire job list
        inspire job list --limit 20 --status RUNNING
        inspire job list --name qwen35
        inspire job list -A --name qwen35 --limit 20
        inspire job list --active
        inspire job list --watch --active -n 20
    """
    del web
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)

        if watch:
            _watch_jobs(
                ctx,
                config=config,
                workspace=workspace,
                all_workspaces=all_workspaces,
                all_users=all_users,
                created_by=created_by,
                status=status,
                name=name,
                page_size=page_size,
                max_pages=max_pages,
                limit=limit,
                interval=interval,
                active=active,
            )
            return

        rows, scanned = _list_web_jobs(
            config=config,
            workspace=workspace,
            all_workspaces=all_workspaces,
            all_users=all_users,
            created_by=created_by,
            status=status,
            name=name,
            page_num=page_num,
            page_size=page_size,
            max_pages=max_pages,
            limit=limit,
        )

        if active:
            exclude_statuses = {
                "FAILED",
                "job_failed",
                "CANCELLED",
                "job_cancelled",
                "job_stopped",
            }
            rows = [j for j in rows if j.get("status") not in exclude_statuses]

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


@click.command("status")
@click.argument("job")
@click.option("--web", is_flag=True, help="Query the web UI detail API instead of OpenAPI")
@click.option("--workspace", default=None, help="Workspace alias or name")
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="Resolve the job name across every visible workspace, not just the current one",
)
@click.option("--all-users", is_flag=True, help="Include jobs from all users when resolving a name")
@click.option("--created-by", default=None, help="Filter web job name resolution by creator user ID")
@click.option(
    "--max-pages",
    type=int,
    default=50,
    show_default=True,
    help="Max web pages to scan per workspace when resolving a job name",
)
@pass_context
def status(
    ctx: Context,
    job: str,
    web: bool,
    workspace: Optional[str],
    all_workspaces: bool,
    all_users: bool,
    created_by: Optional[str],
    max_pages: int,
) -> None:
    """Check the status of a training job.

    JOB is the name shown in `inspire job list`. Raw ids are accepted only
    when web detail mode is requested.

    \b
    Example:
        inspire job status my-training-run
        inspire job status --web job-...
    """
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        web_mode = web or workspace or all_users or created_by
        if web_mode:
            job_id = _resolve_web_job_id(
                config=config,
                job=job,
                workspace=workspace,
                all_workspaces=all_workspaces,
                all_users=all_users,
                created_by=created_by,
                max_pages=max_pages,
            )
            try:
                session = get_web_session()
                job_data = browser_api_module.get_job_detail(job_id, session=session)
            finally:
                _close_web_client()

            if ctx.json_output:
                click.echo(json_formatter.format_json({"source": "web", "job": job_data}))
            else:
                click.echo(_format_web_job_status(job_data))
            return

        job_id = resolve_job_id(ctx, job, all_workspaces=all_workspaces)
        api = AuthManager.get_api(config)

        result = api.get_job_detail(job_id)
        job_data = result.get("data", {})

        if ctx.json_output:
            click.echo(json_formatter.format_json(job_data))
        else:
            click.echo(human_formatter.format_job_status(job_data))

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


@click.command("instances")
@click.argument("job")
@click.option("--web", is_flag=True, help="Accepted for consistency; this command uses Web API")
@click.option("--workspace", default=None, help="Workspace alias or name")
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="Search every visible workspace when resolving a job name",
)
@click.option("--all-users", is_flag=True, help="Include jobs from all users when resolving a name")
@click.option("--created-by", default=None, help="Filter name resolution by creator user ID")
@click.option("--page-num", type=int, default=1, show_default=True, help="Instance page number")
@click.option("--page-size", type=int, default=200, show_default=True, help="Instance page size")
@click.option(
    "--max-pages",
    type=int,
    default=50,
    show_default=True,
    help="Max web pages to scan per workspace when resolving a job name",
)
@pass_context
def instances(
    ctx: Context,
    job: str,
    web: bool,
    workspace: Optional[str],
    all_workspaces: bool,
    all_users: bool,
    created_by: Optional[str],
    page_num: int,
    page_size: int,
    max_pages: int,
) -> None:
    """List pod-level instances for a distributed-training job."""
    del web
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        job_id = _resolve_web_job_id(
            config=config,
            job=job,
            workspace=workspace,
            all_workspaces=all_workspaces,
            all_users=all_users,
            created_by=created_by,
            max_pages=max_pages,
        )
        try:
            session = get_web_session()
            rows, total = browser_api_module.list_job_instances(
                job_id,
                page_num=page_num,
                page_size=page_size,
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
@click.option(
    "--pick",
    type=int,
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous.",
)
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="Resolve the job name across every visible workspace, not just the current one",
)
@pass_context
def stop(ctx: Context, job: str, pick: Optional[int], all_workspaces: bool) -> None:
    """Stop a running training job.

    \b
    Example:
        inspire job stop my-training-run
    """
    job_id = resolve_job_id(ctx, job, pick=pick, all_workspaces=all_workspaces)

    try:
        config, _ = Config.from_files_and_env()
        api = AuthManager.get_api(config)

        api.stop_training_job(job_id)

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
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--pick",
    type=int,
    default=None,
    help="Pick the Nth candidate (1-indexed) when the name is ambiguous.",
)
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="Resolve the job name across every visible workspace, not just the current one",
)
@pass_context
def delete(ctx: Context, job: str, yes: bool, pick: Optional[int], all_workspaces: bool) -> None:
    """Permanently delete a training job entry from the platform (Browser API).

    \b
    The entry disappears from the distributed-training list in the web UI.
    This cannot be undone; if the job is still running, `stop` it first.

    \b
    Example:
        inspire job delete my-training-run
    """
    job_id = resolve_job_id(ctx, job, pick=pick, all_workspaces=all_workspaces)

    if not yes and not ctx.json_output:
        click.confirm(
            f"Permanently delete training job '{job}'? This cannot be undone.",
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
@click.option("--timeout", type=int, default=14400, help="Timeout in seconds (default: 4 hours)")
@click.option("--interval", type=int, default=30, help="Poll interval in seconds (default: 30)")
@click.option("--web", is_flag=True, help="Poll the web UI detail API instead of OpenAPI")
@click.option("--workspace", default=None, help="Workspace alias or name")
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="Resolve the job name across every visible workspace, not just the current one",
)
@click.option("--all-users", is_flag=True, help="Include jobs from all users in web mode")
@click.option("--created-by", default=None, help="Filter web job name resolution by creator user ID")
@click.option(
    "--max-pages",
    type=int,
    default=50,
    show_default=True,
    help="Max web pages to scan per workspace when resolving a job name",
)
@pass_context
def wait(
    ctx: Context,
    job: str,
    timeout: int,
    interval: int,
    web: bool,
    workspace: Optional[str],
    all_workspaces: bool,
    all_users: bool,
    created_by: Optional[str],
    max_pages: int,
) -> None:
    """Wait for a job to complete.

    Polls the job status until it reaches a terminal state
    (SUCCEEDED, FAILED, or CANCELLED).

    \b
    Example:
        inspire job wait my-training-run --timeout 7200
        inspire job wait --web job-... --timeout 60
    """
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        web_mode = web or workspace or all_users or created_by
        if web_mode:
            job_id = _resolve_web_job_id(
                config=config,
                job=job,
                workspace=workspace,
                all_workspaces=all_workspaces,
                all_users=all_users,
                created_by=created_by,
                max_pages=max_pages,
            )
            api = None
        else:
            job_id = resolve_job_id(ctx, job, all_workspaces=all_workspaces)
            api = AuthManager.get_api(config)

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
            click.echo(f"Waiting for job {job} (timeout: {timeout}s, interval: {interval}s)")

        while True:
            elapsed = time.time() - start_time

            if elapsed > timeout:
                _handle_error(ctx, "Timeout", f"Timeout after {timeout}s", EXIT_TIMEOUT)
                return

            try:
                if web_mode:
                    try:
                        session = get_web_session()
                        job_data = browser_api_module.get_job_detail(job_id, session=session)
                    finally:
                        _close_web_client()
                else:
                    assert api is not None
                    result = api.get_job_detail(job_id)
                    job_data = result.get("data", {})
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
                        click.echo(f"\nStatus: {current_status}")
                    last_status = current_status
                else:
                    if not ctx.json_output:
                        mins = int(elapsed // 60)
                        secs = int(elapsed % 60)
                        click.echo(
                            f"\r[{mins:02d}:{secs:02d}] Waiting... Status: {current_status}",
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
                    click.echo(f"\nWarning: Failed to get status: {e}")

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
@click.option("--web", is_flag=True, help="Read command from the web UI detail API")
@click.option("--workspace", default=None, help="Workspace alias or name")
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="Resolve the job name across every visible workspace, not just the current one",
)
@click.option("--all-users", is_flag=True, help="Include jobs from all users in web mode")
@click.option("--created-by", default=None, help="Filter web job name resolution by creator user ID")
@click.option(
    "--max-pages",
    type=int,
    default=50,
    show_default=True,
    help="Max web pages to scan per workspace when resolving a job name",
)
@pass_context
def show_command(
    ctx: Context,
    job: str,
    web: bool,
    workspace: Optional[str],
    all_workspaces: bool,
    all_users: bool,
    created_by: Optional[str],
    max_pages: int,
) -> None:
    """Show the training command used for a job."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        web_mode = web or workspace or all_users or created_by
        if web_mode:
            job_id = _resolve_web_job_id(
                config=config,
                job=job,
                workspace=workspace,
                all_workspaces=all_workspaces,
                all_users=all_users,
                created_by=created_by,
                max_pages=max_pages,
            )
            try:
                session = get_web_session()
                job_data = browser_api_module.get_job_detail(job_id, session=session)
            finally:
                _close_web_client()
            source = "web"
        else:
            job_id = resolve_job_id(ctx, job, all_workspaces=all_workspaces)
            api = AuthManager.get_api(config)
            result = api.get_job_detail(job_id)
            job_data = result.get("data", {})
            source = "api"
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
            click.echo(command_value)

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
@click.option("--rank", type=int, default=None, help="Open the running instance with this rank")
@click.option("--instance", "instance_name", default=None, help="Open this exact instance name")
@click.option(
    "--pick",
    type=int,
    default=None,
    help="Pick the Nth matching job (1-indexed) when multiple jobs share a name",
)
@click.option("--workspace", default=None, help="Workspace alias or name")
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="Resolve the job name across every visible workspace, not just the current one",
)
@click.option("--all-users", is_flag=True, help="Include jobs from all users when resolving a name")
@click.option("--created-by", default=None, help="Filter job name resolution by creator user ID")
@click.option(
    "--max-pages",
    type=int,
    default=50,
    show_default=True,
    help="Max web pages to scan per workspace when resolving a job name",
)
@pass_context
def shell(
    ctx: Context,
    job: str,
    rank: Optional[int],
    instance_name: Optional[str],
    pick: Optional[int],
    workspace: Optional[str],
    all_workspaces: bool,
    all_users: bool,
    created_by: Optional[str],
    max_pages: int,
) -> None:
    """Open an interactive shell inside a running training-job instance.

    \b
    Examples:
        inspire job shell my-training-run
        inspire job shell my-training-run --rank 0
        inspire job shell my-training-run --instance pytorchjob-worker-0
        inspire job shell my-training-run --pick 2
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
            all_workspaces=all_workspaces,
            all_users=all_users,
            created_by=created_by,
            max_pages=max_pages,
            pick=pick,
        )
        session = get_web_session()
        try:
            raw_instances, _ = browser_api_module.list_job_instances(
                job_id,
                page_num=1,
                page_size=200,
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
            click.echo(f"Opening shell: {job} / {selected.name}", err=True)
            click.echo("Press Ctrl-] to disconnect.", err=True)

        code = open_job_shell(job_id=job_id, instance_name=selected.name, session=session)
        sys.exit(code)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
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
    "status",
    "stop",
    "delete",
    "wait",
]
