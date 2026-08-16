"""Job subcommands (excluding create/logs)."""

from __future__ import annotations

import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

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
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.collection_output import (
    DEFAULT_COLLECTION_LIMIT,
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
    is_full_uuid,
    looks_like_platform_id,
    reject_id_at_boundary,
    run_with_stale_handle_retry,
)
from inspire.cli.utils.job_shell import (
    JobShellError,
    normalize_job_instances,
    open_job_shell,
    select_job_instance,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
    StaleResourceIndexRefresh,
    scope_for_session,
)
from inspire.config import Config, ConfigError
from inspire.config.workspaces import resolve_workspace_query_scope, select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session

from .public_output import (
    format_job_status,
    public_job_list_item,
    public_job_status,
)

logger = logging.getLogger(__name__)

_DEFAULT_INSTANCE_SCAN_LIMIT = 500

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
    return looks_like_platform_id(value)


def _reject_web_job_name_at_boundary(ctx: Context, job: str) -> str:
    return reject_id_at_boundary(
        ctx,
        job,
        resource_type="job",
        list_command="inspire job list --workspace <workspace>",
    )


def _job_not_found_message(job: str) -> str:
    return f"Job not found: {scrub_raw_ids(job)}"


def _close_web_client() -> None:
    try:
        from inspire.platform.web.session import _close_browser_client

        _close_browser_client()
    except Exception:
        pass


def _resolve_explicit_workspace(workspace: Optional[str], session) -> Optional[str]:  # noqa: ANN001
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
            "See `inspire account context` for available names."
        )
    return select_workspace_id(explicit_workspace_name=workspace, session=session)


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
    session,  # noqa: ANN001
    *,
    workspace: Optional[str],
) -> list[str]:
    """Pick workspace_ids for a job-list call.

    Query commands require ``--workspace <name|all>`` and never inherit the
    browser session's active workspace.
    """
    workspace_ids, _ = resolve_workspace_query_scope(
        workspace=workspace,
        session=session,
    )
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
        "cpu_count": getattr(job, "cpu_count", 0),
        "memory_gib": getattr(job, "memory_gib", 0),
        "shm_gib": getattr(job, "shm_gib", None),
        "instance_count": job.instance_count,
        "priority": job.priority,
        "workspace_id": job.workspace_id or "",
        "workspace_name": workspace_name,
        "command": job.command or "",
    }


_INSTANCE_HANDLE_RE = re.compile(
    r"^(?:pod|instance|inst)[-_](?:[0-9a-f]{4,}|[0-9a-f-]{8,})$",
    re.IGNORECASE,
)


def _looks_like_instance_handle(value: str) -> bool:
    """Return whether an instance selector looks like an internal handle."""
    value = str(value or "").strip()
    if not value:
        return False
    return (
        looks_like_platform_id(value)
        or is_full_uuid(value)
        or bool(_INSTANCE_HANDLE_RE.fullmatch(value))
    )


def _reject_job_instance_name(ctx: Context, value: str) -> str:
    """Enforce the Name-only boundary for pod/instance selectors."""
    name = reject_id_at_boundary(
        ctx,
        value,
        resource_type="job instance",
        list_command="inspire job instances <job-name> --workspace <workspace>",
    )
    if _looks_like_instance_handle(name):
        _handle_error(
            ctx,
            "ValidationError",
            "CLI commands only accept job instance names.",
            EXIT_VALIDATION_ERROR,
            hint="List instances with `inspire job instances <job-name> --workspace <workspace>`.",
        )
        return ""
    return name


def _instance_rank(item: dict, position: int) -> int:
    for key in ("rank", "instance_rank", "global_rank", "index", "replica_index"):
        value = item.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
    return position


def _public_instance_text(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value in (None, "") or isinstance(value, (dict, list, tuple, set)):
            continue
        text = scrub_raw_ids(value).strip()
        if text and "<redacted>" not in text:
            return text
    return ""


def _instance_resource(item: dict) -> str:
    direct = _public_instance_text(item, "resource")
    if direct:
        return direct

    spec: dict = item
    for key in ("resource_spec", "resource_spec_price", "quota"):
        candidate = item.get(key)
        if isinstance(candidate, dict):
            spec = candidate
            break

    values = (
        ("CPU", _public_instance_text(spec, "cpu_count", "cpu")),
        (
            "GiB",
            _public_instance_text(
                spec,
                "memory_size_gib",
                "memory_gib",
                "memory_size",
                "memory",
            ),
        ),
        ("GPU", _public_instance_text(spec, "gpu_count", "gpu")),
    )
    return ", ".join(f"{value} {unit}" for unit, value in values if value)


def _public_job_instances(instances: list[dict]) -> list[dict]:
    """Project job instances onto the stable public CLI schema."""
    public_instances: list[dict] = []
    for position, item in enumerate(instances):
        public: dict[str, Any] = {}
        name = _public_instance_text(item, "name", "instance_name", "display_name")
        if name and not _looks_like_instance_handle(name):
            public["name"] = name

        for key, candidates in (
            ("status", ("status", "instance_status", "phase", "state")),
            ("role", ("role", "component", "worker_group_name")),
            ("type", ("type", "instance_type")),
            ("node", ("node", "node_name", "host_name")),
        ):
            value = _public_instance_text(item, *candidates)
            if value:
                public[key] = value

        resource = _instance_resource(item)
        if resource:
            public["resource"] = resource
        public["rank"] = _instance_rank(item, position)
        public_instances.append(public)
    return public_instances


def _fetch_job_instances(
    job_id: str,
    *,
    limit: int,
    session,
    show_all: bool,
) -> tuple[list[dict], int]:
    """Fetch the bounded instance page, expanding it only for explicit ``--all``."""
    rows, total = browser_api_module.list_job_instances(
        job_id,
        limit=limit,
        session=session,
    )
    if show_all and total > len(rows):
        expanded_rows, expanded_total = browser_api_module.list_job_instances(
            job_id,
            limit=max(total, len(rows), 1),
            session=session,
        )
        rows = expanded_rows
        total = max(total, expanded_total, len(rows))
    return rows, total


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


def _format_job_instances(instances: list[dict]) -> str:
    if not instances:
        return "No job instances found."

    columns = [("name", "Name"), ("status", "Status")]
    columns.extend(
        (key, label)
        for key, label in (
            ("role", "Role"),
            ("type", "Type"),
            ("node", "Node"),
            ("resource", "Resource"),
            ("rank", "Rank"),
        )
        if any(item.get(key) not in (None, "") for item in instances)
    )
    rows = [
        tuple(
            (
                item.get("name")
                or f"rank={item.get('rank')}"
                if key == "name"
                else item.get(key, "-")
            )
            for key, _ in columns
        )
        for item in instances
    ]
    widths = [
        column_width(label, [row[index] for row in rows], max_width=48)
        for index, (_, label) in enumerate(columns)
    ]
    rendered = render_table(
        tuple(label for _, label in columns),
        rows,
        widths,
    )
    return "\n".join([rendered[1], rendered[2], *rendered[3:-1]])


def _resolve_web_job_id(
    *,
    job: str,
    workspace: Optional[str],
    all_workspaces: bool,
    max_pages: int,
    pick: Optional[int] = None,
    scan_limit: Optional[int] = None,
    workspace_must_be_single: bool = False,
    require_live: bool = False,
) -> str:
    job = (job or "").strip()
    if not job:
        raise WebJobResolutionError("Job name cannot be empty")
    if _looks_like_job_id(job):
        raise WebJobValidationError(
            "CLI commands take a job name. "
            "Use `inspire job list --workspace <name|all>` to find the name."
        )
    if workspace_must_be_single and (workspace or "").strip().lower() == "all":
        raise ConfigError("--workspace must be a workspace name for this command.")

    session = None
    workspace_ids: list[str] = []
    if (workspace or "").strip() or not all_workspaces:
        session = get_web_session()
        workspace_ids = _list_workspace_ids(
            session,
            workspace=workspace,
        )
    cache_scopes: dict[str, ResourceScope] = {}
    if session is not None:
        for workspace_id in workspace_ids:
            scope = scope_for_session(
                session,
                resource_type="job",
                workspace_id=workspace_id,
                owner_scope="self",
            )
            if scope is not None:
                cache_scopes[workspace_id] = scope
    try:
        cache_index = ResourceIndex.for_account() if cache_scopes else None
    except Exception:
        cache_index = None

    snapshot_tokens: dict[str, tuple[int, int]] = {}
    if cache_index is not None:
        for workspace_id, scope in cache_scopes.items():
            try:
                snapshot_tokens[workspace_id] = cache_index.snapshot_token(scope)
            except Exception:
                continue

    if cache_index is not None and cache_scopes and not require_live:
        cached: list[tuple[str, ResourceIdentity]] = []
        try:
            for workspace_id, scope in cache_scopes.items():
                cached.extend(
                    (workspace_id, item)
                    for item in cache_index.lookup(scope, job)
                )
        except Exception:
            cached = []
        if cached:
            assert session is not None
            cached.sort(
                key=lambda item: (
                    item[1].created_at,
                    item[1].observed_at,
                    item[1].resource_id,
                ),
                reverse=True,
            )
            if pick is not None:
                if pick < 1 or pick > len(cached):
                    raise WebJobResolutionError(
                        f"--pick {pick} out of range; {len(cached)} web jobs share "
                        f"name {scrub_raw_ids(job)!r}."
                    )
                return cached[pick - 1][1].resource_id
            if len(cached) == 1:
                return cached[0][1].resource_id
            candidates = []
            for workspace_id, item in cached[:10]:
                bits = [
                    _workspace_name(session, workspace_id),
                    item.status,
                    item.created_at,
                ]
                label = " / ".join(
                    scrub_raw_ids(bit) for bit in bits if str(bit or "").strip()
                )
                candidates.append(label or scrub_raw_ids(item.name))
            raise WebJobResolutionError(
                f"Multiple web jobs share name {scrub_raw_ids(job)!r}: "
                + ", ".join(candidates)
            )

    limit = 0 if pick is not None else 2
    page_size = max(1, int(scan_limit)) if scan_limit is not None else 100
    scan_pages = 1 if scan_limit is not None else max_pages
    rows, _ = _list_web_jobs(
        workspace=workspace,
        status=None,
        name=job,
        page_num=1,
        page_size=page_size,
        max_pages=scan_pages,
        limit=limit,
    )
    exact = [row for row in rows if row.get("name") == job]
    stale_workspaces: set[str] = set()
    if cache_index is not None and cache_scopes:
        for workspace_id, scope in cache_scopes.items():
            token = snapshot_tokens.get(workspace_id)
            if token is None:
                continue
            try:
                cache_index.replace_name(
                    scope,
                    job,
                    [
                        ResourceIdentity(
                            resource_id=str(row.get("job_id") or ""),
                            name=job,
                            owner_id=str(row.get("created_by_id") or ""),
                            status=str(row.get("status") or ""),
                            created_at=str(row.get("created_at") or ""),
                        )
                        for row in exact
                        if str(row.get("workspace_id") or "") == workspace_id
                    ],
                    expected_generation=token[0],
                    expected_revision=token[1],
                )
            except StaleResourceIndexRefresh:
                try:
                    if cache_index.generation() != token[0]:
                        continue
                except Exception:
                    pass
                stale_workspaces.add(workspace_id)
            except Exception:
                continue

    if stale_workspaces:
        assert session is not None
        # A create/delete/write-through won while the live list was in flight.
        # Never let that older response resurrect the deleted handle.
        exact = [
            row
            for row in exact
            if str(row.get("workspace_id") or "") not in stale_workspaces
        ]
        current_rows: list[dict] = []
        for workspace_id in stale_workspaces:
            scope = cache_scopes.get(workspace_id)
            if scope is None or cache_index is None:
                continue
            try:
                current_rows.extend(
                    {
                        "job_id": item.resource_id,
                        "name": item.name,
                        "workspace_id": workspace_id,
                        "workspace_name": _workspace_name(session, workspace_id),
                        "status": item.status,
                        "created_at": item.created_at,
                        "created_by_id": item.owner_id,
                    }
                    for item in cache_index.lookup(
                        scope,
                        job,
                        fresh_only=False,
                    )
                )
            except Exception:
                continue
        exact = _dedupe_job_rows([*exact, *current_rows])

    candidate_rows = (
        [
            row
            for row in rows
            if str(row.get("workspace_id") or "") not in stale_workspaces
        ]
        if stale_workspaces
        else rows
    )
    if pick is not None:
        candidate_rows = exact if exact else candidate_rows
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
    if len(candidate_rows) == 1:
        return str(candidate_rows[0]["job_id"])
    if candidate_rows:
        candidate_names = ", ".join(
            scrub_raw_ids(row.get("name") or "") for row in candidate_rows[:5]
        )
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


def _run_readonly_web_job_operation(
    *,
    job: str,
    workspace: Optional[str],
    all_workspaces: bool = False,
    max_pages: int = 50,
    scan_limit: Optional[int] = None,
    pick: Optional[int] = None,
    workspace_must_be_single: bool = False,
    session_factory=None,
    resolver=None,
    operation,
):
    """Run a read-only job operation and recover one stale cache hit."""
    active_session = None

    def _session():
        nonlocal active_session
        if active_session is None:
            active_session = (session_factory or get_web_session)()
        return active_session

    def _resolve(require_live: bool) -> str:
        resolve_job = resolver or _resolve_web_job_id
        return resolve_job(
            job=job,
            workspace=workspace,
            all_workspaces=all_workspaces,
            max_pages=max_pages,
            scan_limit=scan_limit,
            pick=pick,
            workspace_must_be_single=workspace_must_be_single,
            require_live=require_live,
        )

    def _invalidate(job_id: str) -> None:
        session = _session()
        workspace_id = ""
        workspace_names = getattr(session, "all_workspace_names", None)
        if (
            workspace
            and workspace.strip().casefold() != "all"
            and isinstance(workspace_names, dict)
        ):
            requested_workspace = workspace.strip().casefold()
            for candidate_id, candidate_name in workspace_names.items():
                if str(candidate_name or "").strip().casefold() == requested_workspace:
                    workspace_id = str(candidate_id or "").strip()
                    break
        forget_resource_identity(
            session=session,
            resource_type="job",
            resource_id=job_id,
            workspace_id=workspace_id,
            owner_scope="self",
        )

    return run_with_stale_handle_retry(
        name=job,
        resolve_cached=lambda: _resolve(False),
        resolve_live=lambda _name: _resolve(True),
        operation=lambda job_id: operation(job_id, _session()),
        invalidate=_invalidate,
    )


def _format_job_list(rows: list[dict]) -> str:
    """Render jobs as a compact name-first table."""
    if not rows:
        return "No jobs found."

    def positive_int(value: object) -> int:
        try:
            return max(0, int(float(str(value))))
        except (TypeError, ValueError):
            return 0

    def resource_text(row: dict) -> str:
        parts: list[str] = []
        gpu_count = positive_int(row.get("gpu_count"))
        gpu_type = str(row.get("gpu_type") or "GPU").replace("NVIDIA ", "")
        if gpu_count > 0:
            parts.append(f"{gpu_count}x{gpu_type}")
        cpu_count = positive_int(row.get("cpu_count"))
        if cpu_count > 0:
            parts.append(f"{cpu_count}C")
        memory_gib = positive_int(row.get("memory_gib"))
        if memory_gib > 0:
            parts.append(f"{memory_gib}G")
        shm_gib = positive_int(row.get("shm_gib"))
        if shm_gib > 0:
            parts.append(f"shm{shm_gib}G")
        return " ".join(parts) or "-"

    rendered_rows = [
        {
            **r,
            "name": scrub_raw_ids(r.get("name", "")),
            "status": scrub_raw_ids(r.get("status", "")),
            "resource": scrub_raw_ids(resource_text(r)),
            "created_at": scrub_raw_ids(human_formatter.format_epoch(r.get("created_at"))),
            "workspace_name": scrub_raw_ids(r.get("workspace_name", "")),
            "created_by_name": scrub_raw_ids(r.get("created_by_name", "")),
        }
        for r in rows
    ]

    table_rows = [
        (
            str(row["name"]),
            str(row["status"]),
            str(row["resource"]),
            str(row["created_at"]),
            str(row.get("workspace_name") or ""),
            str(row.get("created_by_name") or ""),
        )
        for row in rendered_rows
    ]
    widths = [
        column_width("Name", [row[0] for row in table_rows], max_width=120),
        column_width("Status", [row[1] for row in table_rows], max_width=16),
        column_width("Resource", [row[2] for row in table_rows], max_width=32),
        column_width("Created", [row[3] for row in table_rows], max_width=19),
        column_width("Workspace", [row[4] for row in table_rows], max_width=24),
        column_width("Created By", [row[5] for row in table_rows], max_width=16),
    ]

    rendered = render_table(
        ("Name", "Status", "Resource", "Created", "Workspace", "Created By"),
        table_rows,
        widths,
        line_char="─",
    )
    return "\n".join([rendered[1], rendered[2], *rendered[3:-1]])


def _list_web_jobs(
    *,
    workspace: Optional[str],
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
    *,
    workspace: Optional[str],
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

    try:
        while True:
            jobs, scanned = _list_web_jobs(
                workspace=workspace,
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
            page = bound_collection(
                jobs,
                limit=limit,
                total=sum(int(item.get("total") or 0) for item in scanned),
            )
            jobs = page.items

            click.clear()
            click.echo(_format_job_list(jobs))
            notice = truncation_notice(page, full_option="--limit N")
            if notice:
                click.echo(f"\n{notice}")

            time.sleep(interval)

    except KeyboardInterrupt:
        sys.exit(EXIT_SUCCESS)
    finally:
        api_logger.setLevel(original_level)


@click.command("list")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME|all",
    help="Workspace name or 'all'.",
)
@click.option(
    "--status",
    "-s",
    metavar="STATUS",
    help="Filter by status (PENDING, RUNNING, SUCCEEDED, FAILED)",
)
@click.option(
    "--keyword",
    default=None,
    metavar="KEYWORD",
    help="Case-insensitive keyword filter for job name/command",
)
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
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum jobs to display across requested workspaces (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every matching job.")
@pass_context
def list_jobs(
    ctx: Context,
    workspace: Optional[str],
    status: Optional[str],
    keyword: Optional[str],
    active: bool,
    watch: bool,
    interval: int,
    limit: Optional[int],
    show_all: bool,
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
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    if watch and ctx.json_output:
        _handle_error(
            ctx,
            "UsageError",
            "--json --watch is not supported. Drop --json to watch, "
            "or drop --watch for a one-shot JSON result.",
            EXIT_VALIDATION_ERROR,
        )
        return
    if watch and show_all:
        _handle_error(
            ctx,
            "ValidationError",
            "--watch --all is not supported. Use --limit with --watch.",
            EXIT_VALIDATION_ERROR,
        )
        return

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)

        if watch:
            _watch_jobs(
                workspace=workspace,
                status=status,
                name=keyword,
                page_size=_job_list_page_size(effective_limit),
                max_pages=50,
                limit=effective_limit,
                interval=interval,
                active=active,
            )
            return

        rows, scanned = _list_web_jobs(
            workspace=workspace,
            status=status,
            name=keyword,
            page_num=1,
            page_size=_job_list_page_size(effective_limit),
            max_pages=50,
            limit=effective_limit,
            api_statuses=_JOB_ACTIVE_API_STATUSES if active and not status else None,
        )

        if active:
            rows = [j for j in rows if j.get("status") in _JOB_ACTIVE_STATUSES]

        page = bound_collection(
            rows,
            limit=effective_limit,
            total=sum(int(item.get("total") or 0) for item in scanned),
        )
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "items": [
                            public_job_list_item(item)
                            for item in page.items
                        ],
                        **page.metadata(),
                    }
                )
            )
        else:
            click.echo(_format_job_list(page.items))
            notice = truncation_notice(page)
            if notice:
                click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


@click.command("status")
@click.argument("job", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def status(
    ctx: Context,
    job: str,
    workspace: Optional[str],
    pick: Optional[int],
) -> None:
    """Check the status of a training job.

    NAME is shown in `inspire job list`.

    \b
    Example:
        inspire job status my-training-run --workspace 分布式训练空间
    """
    job = _reject_web_job_name_at_boundary(ctx, job)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        try:
            job_data = _run_readonly_web_job_operation(
                job=job,
                workspace=workspace,
                pick=pick,
                workspace_must_be_single=True,
                operation=lambda job_id, session: (
                    browser_api_module.get_job_detail_v2(job_id, session=session)
                ),
            )
        finally:
            _close_web_client()

        detail = public_job_status(job_data, fallback_name=job)
        if ctx.json_output:
            click.echo(json_formatter.format_json(detail))
        else:
            click.echo(format_job_status(detail))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except WebJobValidationError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid job id" in msg:
            logger.debug("Job status used a stale internal handle", exc_info=True)
            _handle_error(
                ctx,
                "JobNotFound",
                _job_not_found_message(job),
                EXIT_JOB_NOT_FOUND,
            )
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("instances")
@click.argument("job", metavar="NAME")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME",
    help="Workspace name.",
)
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum instances to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show the complete instance list.")
@pass_context
def instances(
    ctx: Context,
    job: str,
    workspace: Optional[str],
    pick: Optional[int],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List pod-level instances for a distributed-training job."""
    job = _reject_web_job_name_at_boundary(ctx, job)
    try:
        output_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    request_limit = (
        output_limit if output_limit is not None else DEFAULT_COLLECTION_LIMIT
    )
    resolution_limit = (
        limit if limit is not None else _DEFAULT_INSTANCE_SCAN_LIMIT
    )

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        try:
            rows, total = _run_readonly_web_job_operation(
                job=job,
                workspace=workspace,
                scan_limit=resolution_limit,
                pick=pick,
                workspace_must_be_single=True,
                operation=lambda job_id, session: (
                    _fetch_job_instances(
                        job_id,
                        session=session,
                        limit=request_limit,
                        show_all=show_all,
                    )
                ),
            )
        finally:
            _close_web_client()

        page = bound_collection(rows, limit=output_limit, total=total)
        public_items = _public_job_instances(page.items)

        if ctx.json_output:
            payload: dict[str, Any] = {
                "name": scrub_raw_ids(job),
                "items": public_items,
                **page.metadata(),
            }
            click.echo(json_formatter.format_json(payload))
        else:
            click.echo(_format_job_instances(public_items))
            notice = truncation_notice(page)
            if notice:
                click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("stop")
@click.argument("job", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def stop(ctx: Context, job: str, workspace: Optional[str], pick: Optional[int]) -> None:
    """Stop a running training job.

    \b
    Example:
        inspire job stop my-training-run --workspace 分布式训练空间
    """
    job = _reject_web_job_name_at_boundary(ctx, job)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        job_id = _resolve_web_job_id(
            job=job,
            workspace=workspace,
            all_workspaces=False,
            max_pages=50,
            pick=pick,
            workspace_must_be_single=True,
            require_live=True,
        )
        session = get_web_session()
        browser_api_module.stop_training_job(job_id, session=session)

        if ctx.json_output:
            click.echo(json_formatter.format_json({"name": job, "status": "stopped"}))
        else:
            click.echo(human_formatter.format_mutation_success("Job", "stopped", job))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid job id" in msg:
            logger.debug("Job stop used a stale internal handle", exc_info=True)
            _handle_error(
                ctx,
                "JobNotFound",
                _job_not_found_message(job),
                EXIT_JOB_NOT_FOUND,
            )
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("delete")
@click.argument("job", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
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
    help=NAME_PICK_HELP,
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
    job = _reject_web_job_name_at_boundary(ctx, job)
    require_confirmation(
        ctx,
        yes=yes,
        prompt=(
            f"Permanently delete training job '{scrub_raw_ids(job)}'? "
            "This cannot be undone."
        ),
        message="Training job deletion requires confirmation.",
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        job_id = _resolve_web_job_id(
            job=job,
            workspace=workspace,
            all_workspaces=False,
            max_pages=50,
            pick=pick,
            workspace_must_be_single=True,
            require_live=True,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        return

    try:
        session = get_web_session()
        browser_api_module.delete_job(job_id=job_id, session=session)
        workspace_id = _resolve_explicit_workspace(workspace, session)
        if workspace_id:
            try:
                index = ResourceIndex.for_account()
                scope = scope_for_session(
                    session,
                    resource_type="job",
                    workspace_id=workspace_id,
                    owner_scope="self",
                )
                if index is not None and scope is not None:
                    index.mark_deleted(scope, resource_id=job_id, name=job)
            except Exception:
                pass

        if ctx.json_output:
            click.echo(json_formatter.format_json({"name": job, "status": "deleted"}))
        else:
            click.echo(human_formatter.format_mutation_success("Job", "deleted", job))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid job id" in msg:
            logger.debug("Job delete used a stale internal handle", exc_info=True)
            _handle_error(
                ctx,
                "JobNotFound",
                _job_not_found_message(job),
                EXIT_JOB_NOT_FOUND,
            )
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("wait")
@click.argument("job", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
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
@pass_context
def wait(
    ctx: Context,
    job: str,
    timeout: int,
    interval: int,
    workspace: Optional[str],
    pick: Optional[int],
) -> None:
    """Wait for a job to complete.

    Polls the job status until it reaches a terminal state
    (SUCCEEDED, FAILED, or CANCELLED).

    \b
    Example:
        inspire job wait my-training-run --workspace 分布式训练空间 --timeout 7200
    """
    job = _reject_web_job_name_at_boundary(ctx, job)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        try:
            job_id, initial_job_data = _run_readonly_web_job_operation(
                job=job,
                workspace=workspace,
                pick=pick,
                workspace_must_be_single=True,
                operation=lambda resolved_id, session: (
                    resolved_id,
                    browser_api_module.get_job_detail_v2(
                        resolved_id,
                        session=session,
                    ),
                ),
            )
        finally:
            _close_web_client()

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
        pending_job_data: dict | None = initial_job_data

        while True:
            elapsed = time.time() - start_time

            if elapsed > timeout:
                _handle_error(ctx, "Timeout", f"Timeout after {timeout}s", EXIT_TIMEOUT)
                return

            try:
                if pending_job_data is not None:
                    job_data = pending_job_data
                    pending_job_data = None
                else:
                    try:
                        job_id, job_data = _run_readonly_web_job_operation(
                            job=job,
                            workspace=workspace,
                            pick=pick,
                            workspace_must_be_single=True,
                            operation=lambda resolved_id, session: (
                                resolved_id,
                                browser_api_module.get_job_detail_v2(
                                    resolved_id,
                                    session=session,
                                ),
                            ),
                        )
                    finally:
                        _close_web_client()
                current_status = job_data.get("status", "UNKNOWN")

                if current_status != last_status:
                    if not ctx.json_output:
                        click.echo(f"Status: {scrub_raw_ids(current_status)}")
                    last_status = current_status

                if current_status in terminal_statuses:
                    detail = public_job_status(job_data, fallback_name=job)
                    if ctx.json_output:
                        click.echo(json_formatter.format_json(detail))
                    else:
                        click.echo(human_formatter.format_job_status(detail))

                    if current_status in {"SUCCEEDED", "job_succeeded"}:
                        sys.exit(EXIT_SUCCESS)
                    sys.exit(EXIT_GENERAL_ERROR)

            except Exception as e:
                logger.debug("Job wait status refresh failed: %s", e, exc_info=True)

            time.sleep(interval)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except KeyboardInterrupt:
        if not ctx.json_output:
            click.echo("\nInterrupted")
        sys.exit(EXIT_GENERAL_ERROR)


@click.command("command")
@click.argument("job", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def show_command(
    ctx: Context,
    job: str,
    workspace: Optional[str],
    pick: Optional[int],
) -> None:
    """Show the training command used for a job."""
    job = _reject_web_job_name_at_boundary(ctx, job)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        try:
            job_data = _run_readonly_web_job_operation(
                job=job,
                workspace=workspace,
                pick=pick,
                workspace_must_be_single=True,
                operation=lambda resolved_id, session: (
                    browser_api_module.get_job_detail_v2(
                        resolved_id,
                        session=session,
                    )
                ),
            )
        finally:
            _close_web_client()
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
            click.echo(json_formatter.format_json({"command": scrub_raw_ids(command_value)}))
        else:
            click.echo(scrub_raw_ids(command_value))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "invalid job id" in msg:
            logger.debug("Job command lookup used a stale internal handle", exc_info=True)
            _handle_error(
                ctx,
                "JobNotFound",
                _job_not_found_message(job),
                EXIT_JOB_NOT_FOUND,
            )
        else:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


@click.command("shell")
@click.argument("job", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option("--rank", type=click.IntRange(0), default=None, help="Open the running instance with this rank")
@click.option(
    "--instance",
    "instance_name",
    default=None,
    metavar="NAME",
    help="Open this exact instance name.",
)
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

    Needs a terminal: this attaches your stdin to a remote PTY. Leave with
    `exit`, or press Ctrl+] to drop the session without ending the shell.

    \b
    Examples:
        inspire job shell my-training-run --workspace 分布式训练空间
        inspire job shell my-training-run --workspace 分布式训练空间 --rank 0
        inspire job shell my-training-run --workspace 分布式训练空间 --instance pytorchjob-worker-0
        inspire job shell my-training-run --workspace 分布式训练空间 --pick 2
    """
    job = _reject_web_job_name_at_boundary(ctx, job)
    if rank is not None and instance_name is not None:
        _handle_error(
            ctx,
            "ValidationError",
            "Use only one of --rank or --instance.",
            EXIT_VALIDATION_ERROR,
        )
        return
    if instance_name is not None:
        instance_name = _reject_job_instance_name(ctx, instance_name)

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        try:
            job_id, session, raw_instances = _run_readonly_web_job_operation(
                job=job,
                workspace=workspace,
                pick=pick,
                workspace_must_be_single=True,
                operation=lambda resolved_id, live_session: (
                    resolved_id,
                    live_session,
                    browser_api_module.list_job_instances(
                        resolved_id,
                        limit=200,
                        session=live_session,
                    )[0],
                ),
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
