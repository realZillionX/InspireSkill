"""Output helpers for keeping platform handles out of CLI observation surfaces."""

from __future__ import annotations

import re

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

_PREFIXED_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>hpc-job|job|notebook|nb|ray|rj|sv|serving|image|img|ws|workspace|"
    r"lcg|cg|group|compute-group|project|proj|user|ssh|quota|spec|model|mirror|pod|instance|inst|"
    r"node|task|container)-"
    r"(?P<body>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)

_PREFIXED_COMPACT_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>hpc-job|job|notebook|nb|ray|rj|sv|serving|image|img|ws|workspace|"
    r"lcg|cg|group|compute-group|project|proj|user|ssh|quota|spec|model|mirror|pod|instance|inst|"
    r"node|task|container)-"
    r"(?P<body>[0-9a-f]{3,}(?:-[0-9a-f]+)*)\b",
    re.IGNORECASE,
)

_PREFIXED_SHORT_NUMERIC_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>lcg|cg|ws)-"
    r"(?P<body>[0-9]+)(?![A-Za-z0-9_-])",
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
    text = _LABELLED_COMPACT_ID_RE.sub(
        lambda match: f"{match.group('label')}<redacted>",
        text,
    )
    text = _RAY_INSTANCE_HANDLE_RE.sub("<redacted>", text)
    text = _PREFIXED_ID_RE.sub("<redacted>", text)
    text = _PREFIXED_COMPACT_ID_RE.sub("<redacted>", text)
    text = _PREFIXED_SHORT_NUMERIC_ID_RE.sub("<redacted>", text)
    return _UUID_RE.sub("<redacted>", text)


__all__ = ["scrub_raw_ids"]
