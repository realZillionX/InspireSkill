import re

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


def is_compact_prefixed_platform_id_body(value: str) -> bool:
    body = value.strip().lower()
    if len(body.replace("-", "")) < 3:
        return False
    return bool(_HEX_CHUNKS_RE.match(body))


def looks_like_platform_id(value: str) -> bool:
    """Heuristic for handle-shaped inputs rejected at the CLI boundary.

    Catches the common prefixes (``job-`` / ``hpc-job-`` / ``rj-`` / ``sv-``
    / ``image-`` / ``notebook-`` / ``nb-``) and bare full UUIDs.

    A bare hexadecimal string is intentionally *not* rejected.  Names are a
    valid user namespace, so values such as ``2026`` or ``cafe`` must still
    be resolvable by name.  The platform's externally copyable handles use a
    recognizable prefix or a full UUID at the CLI boundary.

    Only prefixes the platform actually mints are listed.  Everyday words
    such as ``node``/``task``/``pod``/``container``/``group`` are excluded on
    purpose: a job legitimately named ``node-001`` has to stay addressable,
    and rejecting it here would leave no way to reference it at all now that
    names are the CLI's only handle.
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
        "tb-",
        "user-",
    )
    for prefix in sorted(id_prefixes, key=len, reverse=True):
        if not v.startswith(prefix):
            continue
        body = v[len(prefix) :]
        return (
            is_full_uuid(body) or is_partial_id(body) or is_compact_prefixed_platform_id_body(body)
        )
    # Bare UUID — stripping only colons/underscores would be wrong, just match
    # exactly.  Do not treat bare partial hex as an ID: it may be a name.
    return bool(_FULL_UUID_RE.match(v))
