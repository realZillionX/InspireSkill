"""Shared name-resolution utilities."""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Optional

import click

from inspire.cli.context import Context, EXIT_VALIDATION_ERROR
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.raw_ids import scrub_raw_ids


_FULL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_HEX_RE = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)

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


def normalize_partial(value: str, prefix: str | None = None) -> str:
    """Strip known *prefix* and return the lowercase hex portion."""
    value = value.strip()
    if prefix and value.lower().startswith(prefix.lower()):
        value = value[len(prefix) :]
    return value.lower()


def resolve_partial_id(
    ctx: Context,
    partial: str,
    resource_type: str,
    matches: list[tuple[str, str]],
    json_output: bool,
) -> str:
    """Disambiguate duplicate name matches.

    *matches* is a list of ``(platform_handle, display_label)`` tuples.

    Returns the resolved platform handle, or calls ``exit_with_error`` on failure.
    """
    if not matches:
        exit_with_error(
            ctx,
            "NotFound",
            f"No {resource_type} matching '{partial}'.",
            EXIT_VALIDATION_ERROR,
            hint=f"Run 'inspire {resource_type} list' to see available names.",
        )

    if len(matches) == 1:
        return matches[0][0]

    # Multiple matches are name collisions in the current lookup scope. Do
    # not invite users to type handles to disambiguate normal CLI commands.
    if json_output:
        exit_with_error(
            ctx,
            "AmbiguousName",
            f"{len(matches)} {resource_type}s share the name '{partial}'.",
            EXIT_VALIDATION_ERROR,
            hint=(
                "Rename one of the duplicates, or pass `--pick <N>` (1-indexed) "
                "when the command supports it."
            ),
        )

    click.echo(f"{len(matches)} {resource_type}s share the name '{partial}':")
    for idx, (full_id, label) in enumerate(matches, start=1):
        del full_id
        display = scrub_raw_ids(label) or "(no extra context)"
        click.echo(f"  [{idx}] {display}")

    choice = click.prompt(
        f"Select {resource_type}",
        type=click.IntRange(1, len(matches)),
        default=1,
        show_default=True,
    )
    return matches[choice - 1][0]


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
            hint=(
                f"Find the name with `inspire {resource_type} list`. "
                "Use the resource's dedicated `id` command only for explicit "
                "platform-handle lookup."
            ),
        )
        return ""  # unreachable

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
        exit_with_error(
            ctx,
            "APIError",
            f"Failed to resolve {resource_type} name {name!r}: {e}",
            EXIT_VALIDATION_ERROR,
        )
        return ""  # unreachable

    matches = [c for c in candidates if str(c.get(name_key) or "") == name]

    # Dedupe by id_key — image resolver in particular iterates multiple
    # source buckets (private / public / official) and the same image can
    # show up in two of them; without dedupe we'd raise a false-ambiguous
    # for a single unique target.
    _seen_ids: set[str] = set()
    _deduped: list[dict[str, Any]] = []
    for c in matches:
        cid = str(c.get(id_key) or "")
        if not cid or cid in _seen_ids:
            continue
        _seen_ids.add(cid)
        _deduped.append(c)
    matches = _deduped

    if not matches:
        exit_with_error(
            ctx,
            "NotFound",
            f"No {resource_type} with name {name!r} found.",
            EXIT_VALIDATION_ERROR,
            hint=f"List candidates with `inspire {resource_type} list` (or `-A`).",
        )
        return ""  # unreachable

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
            return label_fn(c)
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
    if any(v.startswith(p) for p in id_prefixes):
        return True
    if is_partial_id(v):
        return True
    # Bare UUID — stripping only colons/underscores would be wrong, just match exactly.
    return bool(_FULL_UUID_RE.match(v))


def reject_id_at_boundary(
    ctx: Context,
    value: str,
    *,
    resource_type: str,
    list_command: str,
) -> str:
    """Reject handle-shaped inputs at the user boundary, pass names through.

    Used by commands that look up a cached connection by its display name
    (``notebook shell`` / ``exec`` / ``scp`` / ``refresh`` / ``forget`` /
    ``connections test`` / ``job logs``). Names are the only normal CLI
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
