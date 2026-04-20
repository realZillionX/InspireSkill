"""Job command helpers."""

from __future__ import annotations

import logging
import os

from inspire.platform.openapi import _validate_job_id_format
from inspire.cli.context import Context, EXIT_JOB_NOT_FOUND
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.id_resolver import (
    is_full_uuid,
    is_partial_id,
    normalize_partial,
    resolve_partial_id,
)

logger = logging.getLogger(__name__)


def resolve_job_id(ctx: Context, job_id: str) -> str:
    """Resolve a full or partial job ID to a complete ``job-<uuid>`` string.

    Resolution order:
    1. Full UUID (with or without ``job-`` prefix) -> return directly.
    2. Partial hex -> search local cache, then web API.
    3. Not hex -> error with format hint.
    """
    job_id = job_id.strip()
    if not job_id:
        exit_with_error(ctx, "InvalidJobID", "Job ID cannot be empty", EXIT_JOB_NOT_FOUND)

    # Full UUID with prefix
    if is_full_uuid(job_id, prefix="job-"):
        # Ensure the job- prefix is present
        if not job_id.lower().startswith("job-"):
            job_id = f"job-{job_id}"
        return job_id

    # Partial hex
    if is_partial_id(job_id, prefix="job-"):
        partial = normalize_partial(job_id, prefix="job-")
        matches = _search_job_cache(partial)

        if not matches:
            matches = _search_job_api(partial)

        return resolve_partial_id(
            ctx,
            partial,
            "job",
            matches,
            ctx.json_output,
        )

    # Not a hex string at all — give the original validation error
    format_error = _validate_job_id_format(job_id)
    msg = format_error or f"Invalid job ID format: {job_id}"
    exit_with_error(
        ctx,
        "InvalidJobID",
        msg,
        EXIT_JOB_NOT_FOUND,
        hint="Expected a full job ID (job-xxxxxxxx-...) or a partial hex prefix (4+ chars).",
    )
    return ""  # unreachable, exit_with_error calls sys.exit


def _search_job_cache(partial: str) -> list[tuple[str, str]]:
    """Search local job cache for IDs starting with *partial*."""
    from inspire.cli.utils.job_cache_api import JobCache
    from inspire.config import Config, ConfigError

    try:
        config = Config.from_env(require_target_dir=False)
        cache_path = config.get_expanded_cache_path()
    except (ConfigError, OSError, ValueError, TypeError) as error:
        logger.debug(
            "Falling back to INSPIRE_JOB_CACHE for partial lookup (%s): %s", partial, error
        )
        cache_path = os.getenv("INSPIRE_JOB_CACHE")

    if not cache_path:
        return []

    try:
        cache = JobCache(cache_path)
        jobs = cache.list_jobs(limit=0)
    except (OSError, ValueError, TypeError) as error:
        logger.debug("Unable to read local job cache %s: %s", cache_path, error)
        return []

    matches: list[tuple[str, str]] = []
    for job in jobs:
        jid = job.get("job_id", "")
        # Strip prefix for comparison
        uuid_part = jid[4:] if jid.lower().startswith("job-") else jid
        if uuid_part.lower().startswith(partial):
            label = job.get("name") or job.get("status") or ""
            matches.append((jid, label))
    return matches


def _search_job_api(partial: str) -> list[tuple[str, str]]:
    """Try the web API for job listing, silently return [] on failure."""
    try:
        from inspire.platform.web.browser_api.jobs import list_jobs as web_list_jobs

        items, _ = web_list_jobs(page_size=100)
        matches: list[tuple[str, str]] = []
        for job in items:
            jid = job.job_id
            uuid_part = jid[4:] if jid.lower().startswith("job-") else jid
            if uuid_part.lower().startswith(partial):
                label = job.name or job.status or ""
                matches.append((jid, label))
        return matches
    except Exception as error:  # noqa: BLE001 - graceful fallback by design for resolver UX
        logger.debug("Web job lookup fallback for partial %s failed: %s", partial, error)
        return []
