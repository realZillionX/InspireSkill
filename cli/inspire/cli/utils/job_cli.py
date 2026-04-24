"""Job command helpers."""

from __future__ import annotations

import logging
import os

from inspire.cli.context import Context, EXIT_JOB_NOT_FOUND
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.id_resolver import (
    is_full_uuid,
    is_partial_id,
    resolve_partial_id,
)

logger = logging.getLogger(__name__)


def resolve_job_id(ctx: Context, name: str, *, pick: int | None = None) -> str:
    """Resolve a training-job name to its internal ``job-<uuid>`` string.

    v2.0.0: names only. Ids (``job-…`` / raw UUID / partial hex) are
    rejected — the v2 CLI surface never accepts them, so agents that
    only ever see names don't start guessing with ``rj-`` / ``job-``
    prefixes they saw elsewhere.

    ``pick`` is the 1-indexed ambiguity escape hatch used by destructive
    cleanup commands (``stop`` / ``delete``).
    """
    name = (name or "").strip()
    if not name:
        exit_with_error(ctx, "InvalidJobName", "Job name cannot be empty", EXIT_JOB_NOT_FOUND)

    # Reject id-shaped input.
    if is_full_uuid(name, prefix="job-") or is_partial_id(name, prefix="job-"):
        exit_with_error(
            ctx,
            "ValidationError",
            f"v2 CLI takes a job name, not an id / partial-id ({name!r}).",
            EXIT_JOB_NOT_FOUND,
            hint="Use `inspire job list -A` to find the name and pass that instead.",
        )

    # Check local cache first so `inspire job list` (cache-backed) and
    # `inspire job status <name>` agree on which jobs exist — otherwise
    # agents see a name in list and 404 when they try to use it.
    matches = _search_job_cache_by_name(name)
    if not matches:
        matches = _search_job_api_by_name(name)
    # Dedupe by id — cache + API fallback can conceivably surface the same
    # job_id twice.
    seen: set[str] = set()
    matches = [
        m for m in matches
        if m[0] and m[0] not in seen and not seen.add(m[0])
    ]
    if not matches:
        exit_with_error(
            ctx,
            "JobNotFound",
            f"No job with name {name!r} found.",
            EXIT_JOB_NOT_FOUND,
            hint="Use `inspire job list` (local cache) or `inspire job list -A` (web) to find names.",
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


def _search_job_cache_by_name(name: str) -> list[tuple[str, str]]:
    """Exact-name match against the local job cache.

    Mirrors the data source `inspire job list` uses so a name shown there
    is always resolvable by `inspire job <cmd> <name>` without a web
    round-trip. The cache path comes from env/default — we don't go
    through ``Config.from_env`` here because that requires credentials,
    and resolver helpers must work in the same contexts as the commands
    they serve.
    """
    from inspire.cli.utils.job_cache_api import JobCache

    cache_path = os.path.expanduser(
        os.getenv("INSPIRE_JOB_CACHE") or "~/.inspire/jobs.json"
    )

    try:
        cache = JobCache(cache_path)
        jobs = cache.list_jobs(limit=0)
    except (OSError, ValueError, TypeError) as error:
        logger.debug("Cache-by-name read failed: %s", error)
        return []

    matches: list[tuple[str, str]] = []
    for job in jobs:
        if (job.get("name") or "") == name:
            jid = job.get("job_id", "")
            label = job.get("status") or ""
            if jid:
                matches.append((jid, label))
    return matches


def _search_job_api_by_name(name: str) -> list[tuple[str, str]]:
    """Exact-name match against the web API's job list.

    Scope: current user × session workspace, full page. This keeps
    `inspire job status <name>` from picking up a teammate's same-named
    training run (which you wouldn't have permission to operate anyway)
    or getting cut off at the default page of 50.
    """
    try:
        from inspire.platform.web.browser_api.jobs import (
            get_current_user,
            list_jobs as web_list_jobs,
        )

        me = get_current_user()
        created_by = str(me.get("id") or me.get("user_id") or "").strip() or None
        items, _ = web_list_jobs(created_by=created_by, page_size=10000)
        matches: list[tuple[str, str]] = []
        for job in items:
            if (job.name or "") == name:
                label = job.status or ""
                matches.append((job.job_id, label))
        return matches
    except Exception as error:  # noqa: BLE001
        logger.debug("Web job name lookup failed for %s: %s", name, error)
        return []
