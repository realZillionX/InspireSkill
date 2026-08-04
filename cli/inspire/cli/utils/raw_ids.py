"""Output helpers for keeping platform handles out of CLI observation surfaces."""

from __future__ import annotations

import re

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

_PREFIXED_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>hpc-job|job|notebook|nb|ray|rj|sv|serving|image|img|ws|lcg|"
    r"project|user|ssh|quota|spec|model|mirror)-"
    r"(?P<body>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)

_PREFIXED_COMPACT_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>hpc-job|job|notebook|nb|ray|rj|sv|serving|image|img|ws|lcg|"
    r"project|user|ssh|quota|spec|model|mirror)-"
    r"(?P<body>[0-9a-f]{3,}(?:-[0-9a-f]+)*)\b",
    re.IGNORECASE,
)

def scrub_raw_ids(value: object) -> str:
    """Replace platform-looking handles in human-visible strings.

    This helper intentionally targets UUID-shaped platform handles and common
    prefixed handles while leaving ordinary names alone.
    """

    text = "" if value is None else str(value)
    text = _PREFIXED_ID_RE.sub("<redacted>", text)
    text = _PREFIXED_COMPACT_ID_RE.sub("<redacted>", text)
    return _UUID_RE.sub("<redacted>", text)


__all__ = ["scrub_raw_ids"]
