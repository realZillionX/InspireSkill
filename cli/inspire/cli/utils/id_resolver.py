"""Shared name-resolution utilities."""

from __future__ import annotations

from contextvars import ContextVar
import logging
import re
from typing import Any, Callable, Iterable, Optional, TypeVar

from inspire.cli.context import Context, EXIT_VALIDATION_ERROR
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
    StaleResourceIndexRefresh,
    scope_for_session,
)

logger = logging.getLogger(__name__)

NAME_PICK_HELP = "Pick the Nth candidate (1-indexed) when the name is ambiguous."

_STALE_HANDLE_INVALIDATION: ContextVar[bool] = ContextVar(
    "inspire_stale_handle_invalidation",
    default=False,
)


_FULL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_HEX_RE = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)
_HEX_CHUNKS_RE = re.compile(r"^[0-9a-f]+(?:-[0-9a-f]+)*$", re.IGNORECASE)

_MIN_PARTIAL_LEN = 4
_T = TypeVar("_T")

_STALE_HANDLE_MESSAGE_RE = re.compile(
    r"(?:"
    r"\b404\b"
    r"|\bnot\s+found\b"
    r"|\bdoes\s+not\s+exist\b"
    r"|\binvalid(?:\s+[a-z0-9]+){0,4}\s+(?:resource|id|handle)\b"
    r")",
    re.IGNORECASE,
)

_AUTH_ERROR_NAMES = frozenset(
    {
        "AuthenticationError",
        "AuthError",
        "ForbiddenError",
        "PermissionError",
        "SessionExpiredError",
        "UnauthorizedError",
    }
)

_AUTH_ERROR_MARKERS = (
    "authentication",
    "unauthorized",
    "forbidden",
    "login required",
    "token expired",
    "invalid credentials",
)

_TIMEOUT_ERROR_NAMES = frozenset(
    {
        "ConnectTimeout",
        "ReadTimeout",
        "TimeoutError",
        "TimeoutExpired",
    }
)


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
    replaces its previous internal handle. ``reconcile_scope=True`` is reserved for
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
            f"CLI commands only accept {resource_type} names.",
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

    snapshot_generation: int | None = None
    snapshot_revision: int | None = None
    cache_snapshot_available = False
    if index is not None and scope is not None:
        try:
            snapshot_generation, snapshot_revision = index.snapshot_token(scope)
            cache_snapshot_available = True
        except Exception:  # noqa: BLE001 - cache revision checks are best effort
            logger.debug("Resource identity cache revision read failed", exc_info=True)

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

    if index is not None and scope is not None and cache_snapshot_available:
        records = _candidate_identities(
            candidates if reconcile_scope else matches,
            name_key=name_key,
            id_key=id_key,
        )
        try:
            if reconcile_scope:
                index.reconcile(
                    scope,
                    records,
                    ttl_seconds=cache_ttl_seconds,
                    expected_generation=snapshot_generation,
                    expected_revision=snapshot_revision,
                )
            else:
                index.replace_name(
                    scope,
                    name,
                    records,
                    ttl_seconds=cache_ttl_seconds,
                    expected_generation=snapshot_generation,
                    expected_revision=snapshot_revision,
                )
        except StaleResourceIndexRefresh:
            # A create/delete/write-through won while the live list request
            # was in flight. Prefer that newer cache identity over this older
            # snapshot instead of resurrecting a deleted handle.
            try:
                current = index.lookup(scope, name)
            except Exception:  # noqa: BLE001
                current = []
            if current:
                matches = [
                    {
                        id_key: item.resource_id,
                        name_key: item.name,
                        "owner_id": item.owner_id,
                        "status": item.status,
                        "created_at": item.created_at,
                    }
                    for item in current
                ]
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

    # --pick <N> selects the Nth candidate in the displayed order.
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
            "Where supported, pass `--pick <N>` to select one of the candidates "
            "above (1-indexed). Otherwise narrow the workspace scope or rename "
            "one of the duplicates."
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
            allow_name_fallback=not _STALE_HANDLE_INVALIDATION.get(),
        )
    except Exception:  # noqa: BLE001
        logger.debug("Resource identity cache tombstone failed", exc_info=True)


def _status_code_from_error(error: BaseException) -> int | None:
    for candidate in (error, getattr(error, "response", None)):
        if candidate is None:
            continue
        for attribute in ("status_code", "status", "http_status", "code"):
            value = getattr(candidate, attribute, None)
            if value is None:
                continue
            try:
                status = int(value)
            except (TypeError, ValueError):
                continue
            if 100 <= status <= 599:
                return status
    return None


def is_stale_handle_error(error: BaseException) -> bool:
    """Return whether *error* explicitly identifies a stale platform handle.

    Only explicit not-found signals are retryable. Authentication failures,
    timeouts, and server-side failures are deliberately excluded even when
    their text contains words such as ``invalid`` or ``not found``.
    """
    error_name = type(error).__name__
    if error_name in _AUTH_ERROR_NAMES or error_name in _TIMEOUT_ERROR_NAMES:
        return False

    message = re.sub(r"[-_]+", " ", str(error or "")).lower()
    if any(marker in message for marker in _AUTH_ERROR_MARKERS):
        return False
    if "timed out" in message or "timeout" in message:
        return False
    if re.search(r"\b5\d{2}\b", message):
        return False

    status = _status_code_from_error(error)
    if status is not None:
        if status in {401, 403} or status >= 500:
            return False
        if status == 404:
            return True
    return bool(_STALE_HANDLE_MESSAGE_RE.search(message))


def run_with_stale_handle_retry(
    *,
    name: str,
    resolve_cached: Callable[[], str],
    resolve_live: Callable[[str], str],
    operation: Callable[[str], _T],
    invalidate: Callable[[str], object],
) -> _T:
    """Run one handle operation and recover once from an explicit stale handle.

    ``resolve_cached`` supplies the fast cached handle. If ``operation`` fails
    with a precise 404/not-found/invalid-resource error, the previous handle is
    tombstoned through ``invalidate`` before ``resolve_live(name)`` obtains a
    fresh handle for exactly one retry. All other failures, including timeout,
    5xx, and authentication errors, propagate without repeating the operation.
    A second stale-handle failure from the live handle also propagates.
    """
    handle = resolve_cached()
    try:
        return operation(handle)
    except Exception as error:
        if not is_stale_handle_error(error):
            raise
        invalidation_token = _STALE_HANDLE_INVALIDATION.set(True)
        try:
            try:
                invalidate(handle)
            except Exception:  # noqa: BLE001 - cache invalidation is best effort
                logger.debug(
                    "Stale resource identity invalidation failed", exc_info=True
                )
        finally:
            _STALE_HANDLE_INVALIDATION.reset(invalidation_token)
        fresh_handle = resolve_live(name)
        return operation(fresh_handle)


def _looks_like_platform_id(value: str) -> bool:
    """Heuristic for handle-shaped inputs rejected at the CLI boundary.

    Catches the common prefixes (``job-`` / ``hpc-job-`` / ``rj-`` / ``sv-``
    / ``image-`` / ``notebook-`` / ``nb-``) and bare full UUIDs.

    A bare hexadecimal string is intentionally *not* rejected.  Names are a
    valid user namespace, so values such as ``2026`` or ``cafe`` must still
    be resolvable by name.  The platform's externally copyable handles use a
    recognizable prefix or a full UUID at the CLI boundary.
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
        "proj-",
        "ws-",
        "workspace-",
        "lcg-",
        "cg-",
        "group-",
        "compute-group-",
        "quota-",
        "ssh-",
        "spec-",
        "user-",
        "pod-",
        "instance-",
        "inst-",
        "node-",
        "task-",
        "container-",
    )
    for prefix in sorted(id_prefixes, key=len, reverse=True):
        if not v.startswith(prefix):
            continue
        body = v[len(prefix) :]
        return (
            is_full_uuid(body)
            or is_partial_id(body)
            or _is_compact_prefixed_platform_id_body(body)
            or (
                prefix
                in {
                    "ws-",
                    "cg-",
                    "lcg-",
                    "group-",
                    "compute-group-",
                    "workspace-",
                    "proj-",
                    "pod-",
                    "instance-",
                    "inst-",
                    "node-",
                    "task-",
                    "container-",
                }
                and bool(body)
                and bool(_HEX_CHUNKS_RE.fullmatch(body))
            )
        )
    # Bare UUID — stripping only colons/underscores would be wrong, just match
    # exactly.  Do not treat bare partial hex as an ID: it may be a name.
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
            f"CLI commands only accept {resource_type} names.",
            EXIT_VALIDATION_ERROR,
            hint=f"Find the name with `{list_command}` and pass that.",
        )
        return ""  # unreachable
    return name
