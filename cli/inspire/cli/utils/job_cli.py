"""Job command helpers."""

from __future__ import annotations

import logging

from inspire.cli.context import Context, EXIT_JOB_NOT_FOUND, EXIT_VALIDATION_ERROR
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.id_resolver import (
    is_full_uuid,
    is_partial_id,
    resolve_partial_id,
)

logger = logging.getLogger(__name__)


def resolve_job_id(
    ctx: Context,
    name: str,
    *,
    pick: int | None = None,
    all_workspaces: bool = False,
) -> str:
    """Resolve a training-job name to its internal ``job-<uuid>`` string.

    Names are the CLI boundary. Platform handles (``job-…`` / raw UUID /
    partial hex) are rejected on user input.

    Default scope is the platform session's workspace. ``all_workspaces=True``
    widens the search across the workspaces visible in the live web session.
    """
    name = (name or "").strip()
    if not name:
        exit_with_error(ctx, "InvalidJobName", "Job name cannot be empty", EXIT_JOB_NOT_FOUND)

    if (
        name.lower().startswith("job-")
        or is_full_uuid(name, prefix="job-")
        or is_partial_id(name, prefix="job-")
    ):
        exit_with_error(
            ctx,
            "ValidationError",
            "CLI commands take a job name, not a platform handle or partial handle.",
            EXIT_VALIDATION_ERROR,
            hint=(
                "Use `inspire job list -A` to find the name. "
                "Use `inspire job id <name>` only for explicit platform-handle lookup."
            ),
        )

    matches = _search_web_jobs_by_name(name, all_workspaces=all_workspaces)
    if not matches:
        scope_hint = (
            "Use `inspire job list -A` to widen the search across visible workspaces."
            if not all_workspaces
            else "Even with -A no matching job was found."
        )
        exit_with_error(
            ctx,
            "JobNotFound",
            f"No job with name {name!r} found.",
            EXIT_JOB_NOT_FOUND,
            hint=scope_hint,
        )
    if len(matches) == 1:
        return matches[0][0]
    if pick is not None:
        if pick < 1 or pick > len(matches):
            exit_with_error(
                ctx,
                "ValidationError",
                f"--pick {pick} out of range; {len(matches)} jobs share name {name!r}.",
                EXIT_JOB_NOT_FOUND,
            )
        return matches[pick - 1][0]
    return resolve_partial_id(ctx, name, "job", matches, ctx.json_output)


def _search_web_jobs_by_name(
    name: str,
    *,
    all_workspaces: bool,
) -> list[tuple[str, str]]:
    """Exact-name match against the live platform job list.

    Default scope: the platform session's workspace × current user. With
    ``all_workspaces=True``: every workspace id visible in the live web session.

    Lets ``SessionExpiredError`` / ``AuthenticationError`` propagate so the
    real reason surfaces rather than a misleading "job not found".
    """
    from inspire.platform.web.browser_api.jobs import (
        get_current_user,
        list_jobs as web_list_jobs,
    )
    from inspire.platform.web.session import get_web_session

    try:
        session = get_web_session()
        me = get_current_user(session=session)
        created_by = str(me.get("id") or me.get("user_id") or "").strip() or None
        if not created_by:
            raise ValueError("Cannot determine the current user from the live web session.")
    except (KeyboardInterrupt, SystemExit):
        raise
    except ValueError:
        raise
    except Exception as error:  # noqa: BLE001
        cls_name = type(error).__name__
        if cls_name in {"SessionExpiredError", "AuthenticationError"}:
            raise
        logger.debug("Web session bootstrap failed for resolver %s: %s", name, error)
        return []

    if all_workspaces:
        seen: set[str] = set()
        workspace_ids: list[str] = []
        current = str(getattr(session, "workspace_id", "") or "").strip()
        if current:
            workspace_ids.append(current)
            seen.add(current)
        for wid in getattr(session, "all_workspace_ids", None) or []:
            wid_s = str(wid or "").strip()
            if wid_s and wid_s not in seen:
                workspace_ids.append(wid_s)
                seen.add(wid_s)
    else:
        current = str(getattr(session, "workspace_id", "") or "").strip()
        workspace_ids = [current] if current else []

    if not workspace_ids:
        return []

    matches: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for workspace_id in workspace_ids:
        try:
            items, _ = web_list_jobs(
                workspace_id=workspace_id or None,
                created_by=created_by,
                page_size=10000,
                session=session,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:  # noqa: BLE001
            cls_name = type(error).__name__
            if cls_name in {"SessionExpiredError", "AuthenticationError"}:
                raise
            logger.debug(
                "Web job lookup failed for %s in workspace %s: %s",
                name,
                workspace_id,
                error,
            )
            continue
        for job in items:
            if (job.name or "") != name:
                continue
            jid = job.job_id or ""
            if not jid or jid in seen_ids:
                continue
            seen_ids.add(jid)
            matches.append((jid, job.status or ""))
    return matches
