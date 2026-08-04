"""Shared name-resolution utilities."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Iterable, Optional

from inspire.cli.context import Context, EXIT_VALIDATION_ERROR
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
    scope_for_session,
)

logger = logging.getLogger(__name__)


_FULL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_HEX_RE = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)
_HEX_CHUNKS_RE = re.compile(r"^[0-9a-f]+(?:-[0-9a-f]+)*$", re.IGNORECASE)

_MIN_PARTIAL_LEN = 4


def is_full_uuid(value: str, prefix: str | None = None) -> bool:
    """Return True if *value* is a full UUID, optionally with *prefix* stripped."""
    value = value.strip()
    if prefix and value.lower().startswith(prefix.lower()):
        value = value[len(prefix) :]
    return bool(_FULL_UUID_RE.match(value))


def is_partial_id(value: str, prefix: str | None = None) -> bool:
    """Return True if *value* looks like a partial platform handle."""
    value = value.strip()
    if prefix and value.lower().startswith(prefix.lower()):
        value = value[len(prefix) :]
    if len(value) < _MIN_PARTIAL_LEN:
        return False
    if is_full_uuid(value):
        return False
    return bool(_HEX_RE.match(value))


def _is_compact_prefixed_platform_id_body(value: str) -> bool:
    body = value.strip().lower()
    if len(body.replace("-", "")) < 3:
        return False
    return bool(_HEX_CHUNKS_RE.match(body))


# ---------------------------------------------------------------------------
# name-to-handle resolver (for job / hpc / ray / serving / image, etc.)
# ---------------------------------------------------------------------------


def resolve_by_name(
    ctx: Context,
    *,
    name: str,
    resource_type: str,
    list_candidates: Callable[[], Iterable[dict[str, Any]]],
    json_output: bool = False,
    name_key: str = "name",
    id_key: str = "id",
    label_fn: Optional[Callable[[dict[str, Any]], str]] = None,
    pick_index: Optional[int] = None,
    session: object | None = None,
    workspace_id: str = "",
    owner_scope: str = "",
    cache_index: ResourceIndex | None = None,
    cache_scope: ResourceScope | None = None,
    cache_ttl_seconds: int | None = None,
    require_live: bool = False,
    reconcile_scope: bool = False,
    list_command: str | None = None,
) -> str:
    """Resolve a platform name to its internal handle.

    CLI commands accept names. Platform handles (``job-…`` /
    ``hpc-job-…`` / ``rj-…`` / ``sv-…`` / ``image-…`` / raw UUIDs) are
    rejected at the user boundary.

    ``list_candidates()`` returns dicts with at least ``name_key`` and
    ``id_key``. Exact string match on ``name_key``; multiple matches abort
    with the full candidate list (we never silently send an action to the
    wrong target — two jobs with the same name would otherwise have you
    stop the wrong one).

    When ``session`` (or an explicit ``cache_scope``) is supplied, a fresh
    per-account SQLite identity-index hit avoids the live list request.
    Destructive commands pass ``require_live=True``. Successful live lookups
    reconcile the exact name so a deleted-and-recreated resource immediately
    replaces its old internal handle. ``reconcile_scope=True`` is reserved for
    callers that fetched a complete, unfiltered scope.
    """
    name = (name or "").strip()
    if not name:
        exit_with_error(
            ctx,
            "ValidationError",
            f"{resource_type} name cannot be empty",
            EXIT_VALIDATION_ERROR,
        )

    # Reject handle-looking inputs at the normal CLI boundary.
    if _looks_like_platform_id(name):
        exit_with_error(
            ctx,
            "ValidationError",
            f"CLI commands take a {resource_type} name, not a platform handle "
            "or partial handle.",
            EXIT_VALIDATION_ERROR,
            hint=f"Find the name with `{list_command or f'inspire {resource_type} list'}`.",
        )
        return ""  # unreachable

    index, scope = _resolve_cache_context(
        resource_type=resource_type,
        session=session,
        workspace_id=workspace_id,
        owner_scope=owner_scope,
        cache_index=cache_index,
        cache_scope=cache_scope,
    )
    if index is not None and scope is not None and not require_live:
        try:
            cached = index.lookup(scope, name)
        except Exception:  # noqa: BLE001 - a disposable cache must never block live lookup
            logger.debug("Resource identity cache lookup failed", exc_info=True)
            cached = []
        if cached:
            cached_matches = [
                {
                    id_key: item.resource_id,
                    name_key: item.name,
                    "owner_id": item.owner_id,
                    "status": item.status,
                    "created_at": item.created_at,
                }
                for item in cached
            ]
            return _select_name_match(
                ctx,
                name=name,
                resource_type=resource_type,
                matches=cached_matches,
                id_key=id_key,
                label_fn=label_fn,
                pick_index=pick_index,
            )

    try:
        candidates = list(list_candidates())
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:  # noqa: BLE001
        # Session / auth errors have their own code paths in the callers
        # — let them through so the CLI returns the right exit code. Only
        # wrap generic API failures with a friendly resolver context.
        cls_name = type(e).__name__
        if cls_name in {"SessionExpiredError", "AuthenticationError"}:
            raise
        if index is not None and scope is not None:
            try:
                index.record_refresh_error(scope, str(e))
            except Exception:  # noqa: BLE001
                logger.debug("Resource identity cache error write failed", exc_info=True)
        exit_with_error(
            ctx,
            "APIError",
            f"Failed to resolve {resource_type} name {name!r}: {e}",
            EXIT_VALIDATION_ERROR,
        )
        return ""  # unreachable

    matches = _dedupe_matches(
        [candidate for candidate in candidates if str(candidate.get(name_key) or "") == name],
        id_key=id_key,
    )

    if index is not None and scope is not None:
        records = _candidate_identities(
            candidates if reconcile_scope else matches,
            name_key=name_key,
            id_key=id_key,
        )
        try:
            if reconcile_scope:
                index.reconcile(scope, records, ttl_seconds=cache_ttl_seconds)
            else:
                index.replace_name(
                    scope,
                    name,
                    records,
                    ttl_seconds=cache_ttl_seconds,
                )
        except Exception:  # noqa: BLE001
            logger.debug("Resource identity cache refresh failed", exc_info=True)

    if not matches:
        exit_with_error(
            ctx,
            "NotFound",
            f"No {resource_type} with name {name!r} found.",
            EXIT_VALIDATION_ERROR,
            hint=f"List candidates with `{list_command or f'inspire {resource_type} list'}`.",
        )
        return ""  # unreachable

    return _select_name_match(
        ctx,
        name=name,
        resource_type=resource_type,
        matches=matches,
        id_key=id_key,
        label_fn=label_fn,
        pick_index=pick_index,
    )


def _resolve_cache_context(
    *,
    resource_type: str,
    session: object | None,
    workspace_id: str,
    owner_scope: str,
    cache_index: ResourceIndex | None,
    cache_scope: ResourceScope | None,
) -> tuple[ResourceIndex | None, ResourceScope | None]:
    scope = cache_scope
    if scope is None and session is not None:
        scope = scope_for_session(
            session,
            resource_type=resource_type,
            workspace_id=workspace_id,
            owner_scope=owner_scope,
        )
    if scope is None:
        return None, None
    if cache_index is not None:
        return cache_index, scope
    try:
        return ResourceIndex.for_account(), scope
    except Exception:  # noqa: BLE001
        logger.debug("Resource identity cache initialization failed", exc_info=True)
        return None, scope


def _candidate_identities(
    candidates: Iterable[dict[str, Any]],
    *,
    name_key: str,
    id_key: str,
) -> list[ResourceIdentity]:
    records: list[ResourceIdentity] = []
    for candidate in candidates:
        resource_id = str(candidate.get(id_key) or "").strip()
        candidate_name = str(candidate.get(name_key) or "").strip()
        if not resource_id or not candidate_name:
            continue
        records.append(
            ResourceIdentity(
                resource_id=resource_id,
                name=candidate_name,
                owner_id=str(
                    candidate.get("owner_id")
                    or candidate.get("created_by_id")
                    or candidate.get("user_id")
                    or ""
                ).strip(),
                status=str(candidate.get("status") or "").strip(),
                created_at=str(candidate.get("created_at") or "").strip(),
            )
        )
    return records


def _dedupe_matches(
    matches: Iterable[dict[str, Any]],
    *,
    id_key: str,
) -> list[dict[str, Any]]:
    """Dedupe one logical candidate returned through multiple source buckets."""
    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for candidate in matches:
        resource_id = str(candidate.get(id_key) or "").strip()
        if not resource_id or resource_id in seen_ids:
            continue
        seen_ids.add(resource_id)
        deduped.append(candidate)
    return deduped


def _select_name_match(
    ctx: Context,
    *,
    name: str,
    resource_type: str,
    matches: list[dict[str, Any]],
    id_key: str,
    label_fn: Optional[Callable[[dict[str, Any]], str]],
    pick_index: Optional[int],
) -> str:
    if len(matches) == 1:
        return str(matches[0].get(id_key) or "")

    # Ambiguity escape hatch for destructive cleanup: --pick <N> picks the
    # Nth candidate (1-indexed, matching the ambiguity-error list order).
    if pick_index is not None:
        if pick_index < 1 or pick_index > len(matches):
            exit_with_error(
                ctx,
                "ValidationError",
                f"--pick {pick_index} out of range; {len(matches)} {resource_type}s "
                f"share the name {name!r}.",
                EXIT_VALIDATION_ERROR,
            )
        return str(matches[pick_index - 1].get(id_key) or "")

    def _label(c: dict[str, Any]) -> str:
        if label_fn is not None:
            try:
                return scrub_raw_ids(label_fn(c))
            except (KeyError, TypeError, ValueError):
                pass
        bits = []
        status = c.get("status")
        if status:
            bits.append(str(status))
        created = c.get("created_at")
        if created:
            bits.append(f"created_at={created}")
        ws = c.get("workspace_name") or c.get("workspace")
        if ws:
            bits.append(f"workspace={ws}")
        return scrub_raw_ids("  ".join(bits)) if bits else ""

    lines = [f"  [{i}] {_label(c)}" for i, c in enumerate(matches, start=1)]
    exit_with_error(
        ctx,
        "AmbiguousName",
        f"{len(matches)} {resource_type}s share the name {name!r}:\n" + "\n".join(lines),
        EXIT_VALIDATION_ERROR,
        hint=(
            "For destructive cleanup (stop / delete) you can pass `--pick <N>` "
            "to select one of the candidates above (1-indexed). For read-only "
            "queries (status / events / instances) rename one of the duplicates."
        ),
    )
    return ""  # unreachable


def remember_resource_identity(
    *,
    session: object,
    resource_type: str,
    resource_id: str,
    name: str,
    workspace_id: str = "",
    owner_scope: str = "",
    status: str = "",
    created_at: str = "",
    cache_index: ResourceIndex | None = None,
) -> None:
    """Best-effort write-through after a successful create or live detail."""
    index, scope = _resolve_cache_context(
        resource_type=resource_type,
        session=session,
        workspace_id=workspace_id,
        owner_scope=owner_scope,
        cache_index=cache_index,
        cache_scope=None,
    )
    if index is None or scope is None:
        return
    try:
        index.upsert(
            scope,
            [
                ResourceIdentity(
                    resource_id=str(resource_id or "").strip(),
                    name=str(name or "").strip(),
                    status=str(status or "").strip(),
                    created_at=str(created_at or "").strip(),
                )
            ],
        )
    except Exception:  # noqa: BLE001
        logger.debug("Resource identity cache write-through failed", exc_info=True)


def forget_resource_identity(
    *,
    session: object,
    resource_type: str,
    workspace_id: str = "",
    owner_scope: str = "",
    resource_id: str = "",
    name: str = "",
    cache_index: ResourceIndex | None = None,
) -> None:
    """Best-effort tombstone after delete or a stale-handle API response."""
    index, scope = _resolve_cache_context(
        resource_type=resource_type,
        session=session,
        workspace_id=workspace_id,
        owner_scope=owner_scope,
        cache_index=cache_index,
        cache_scope=None,
    )
    if index is None or scope is None:
        return
    try:
        index.mark_deleted(
            scope,
            resource_id=str(resource_id or "").strip(),
            name=str(name or "").strip(),
        )
    except Exception:  # noqa: BLE001
        logger.debug("Resource identity cache tombstone failed", exc_info=True)


def _looks_like_platform_id(value: str) -> bool:
    """Heuristic for handle-shaped inputs rejected at the CLI boundary.

    Catches the common prefixes (``job-`` / ``hpc-job-`` / ``rj-`` / ``sv-``
    / ``image-`` / ``notebook-`` / ``nb-``) and bare full UUIDs.
    """
    v = value.strip().lower()
    if not v:
        return False
    id_prefixes = (
        "job-",
        "hpc-job-",
        "ray-",
        "rj-",
        "sv-",
        "serving-",
        "image-",
        "img-",
        "mirror-",
        "model-",
        "notebook-",
        "nb-",
        "project-",
        "ws-",
        "lcg-",
        "quota-",
        "ssh-",
        "spec-",
        "user-",
    )
    for prefix in sorted(id_prefixes, key=len, reverse=True):
        if not v.startswith(prefix):
            continue
        body = v[len(prefix) :]
        return (
            is_full_uuid(body)
            or is_partial_id(body)
            or _is_compact_prefixed_platform_id_body(body)
        )
    if is_partial_id(v):
        return True
    # Bare UUID — stripping only colons/underscores would be wrong, just match exactly.
    return bool(_FULL_UUID_RE.match(v))


def looks_like_platform_id(value: str) -> bool:
    """Return whether a user-supplied value has the shape of a platform handle."""
    return _looks_like_platform_id(value)


def reject_id_at_boundary(
    ctx: Context,
    value: str,
    *,
    resource_type: str,
    list_command: str,
) -> str:
    """Reject handle-shaped inputs at the user boundary, pass names through.

    Used by commands that look up a cached connection by its display name
    (``notebook shell`` / ``exec`` / ``scp`` / ``connection refresh`` /
    ``connection forget`` / ``connection status`` / ``job logs``). Names are the only normal CLI
    reference; this helper enforces that on cached-cache lookups too —
    without it, a handle-shaped argument would
    silently miss the cache key and fall through to a confusing
    "no cached connection" error.
    """
    name = (value or "").strip()
    if not name:
        exit_with_error(
            ctx,
            "ValidationError",
            f"{resource_type} name cannot be empty",
            EXIT_VALIDATION_ERROR,
        )
        return ""  # unreachable
    if _looks_like_platform_id(name):
        exit_with_error(
            ctx,
            "ValidationError",
            f"CLI commands take a {resource_type} name, not a platform handle "
            "or partial handle.",
            EXIT_VALIDATION_ERROR,
            hint=f"Find the name with `{list_command}` and pass that.",
        )
        return ""  # unreachable
    return name
