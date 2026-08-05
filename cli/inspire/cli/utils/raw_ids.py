"""Output helpers for keeping platform handles out of CLI observation surfaces."""

from __future__ import annotations

import re

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

# Only the prefixes the platform actually mints. Everyday words such as
# ``node``/``task``/``pod``/``container``/``group`` are not on this list on
# purpose: the body pattern below is hex-only, and hex digits are also
# letters, so ``node-001`` or ``task-abc`` would otherwise be redacted out of
# log lines and — worse — out of the Name column that this CLI now depends on
# for addressing resources at all.
_ID_PREFIX_ALTERNATION = (
    r"hpc-job|job|notebook|nb|ray|rj|sv|serving|image|img|ws|lcg|"
    r"project|user|ssh|quota|spec|model|mirror"
)

_PREFIXED_ID_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])(?P<prefix>{_ID_PREFIX_ALTERNATION})-"
    r"(?P<body>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)

_PREFIXED_COMPACT_ID_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])(?P<prefix>{_ID_PREFIX_ALTERNATION})-"
    r"(?P<body>[0-9a-f]{3,}(?:-[0-9a-f]+)*)\b",
    re.IGNORECASE,
)

_RAY_INSTANCE_HANDLE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])rj-[a-z0-9]+-[a-z0-9]+-"
    r"(?:head|w-worker|worker)-[a-z0-9]+(?:\b|$)",
    re.IGNORECASE,
)
_LABELLED_COMPACT_ID_RE = re.compile(
    r"(?P<label>\b(?:id|uuid|handle)\s*[:=#]?\s*)"
    r"(?P<value>[0-9a-f]{4,32})(?![0-9a-f])",
    re.IGNORECASE,
)

def scrub_raw_ids(value: object) -> str:
    """Replace platform-looking handles in human-visible strings.

    This helper intentionally targets UUID-shaped platform handles and common
    prefixed handles while leaving ordinary names alone.
    """

    text = "" if value is None else str(value)
    # Most specific first. In particular a bare UUID must be consumed before
    # the labelled rule below, which matches 4-32 hex digits after an ``id:``
    # label and would otherwise take only a UUID's first group and leave the
    # remaining four in the output.
    text = _PREFIXED_ID_RE.sub("<redacted>", text)
    text = _RAY_INSTANCE_HANDLE_RE.sub("<redacted>", text)
    text = _UUID_RE.sub("<redacted>", text)
    text = _LABELLED_COMPACT_ID_RE.sub(
        lambda match: f"{match.group('label')}<redacted>",
        text,
    )
    return _PREFIXED_COMPACT_ID_RE.sub("<redacted>", text)


__all__ = ["scrub_raw_ids"]
