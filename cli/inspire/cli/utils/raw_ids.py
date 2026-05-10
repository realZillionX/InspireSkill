"""Output helpers for keeping platform handles out of CLI observation surfaces."""

from __future__ import annotations

import re
from typing import Match

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

_PREFIXED_ID_BROAD_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>hpc-job|job|notebook|nb|ray|rj|sv|serving|image|img|ws|lcg|"
    r"project|user|ssh|quota|spec|model|mirror)-"
    r"(?P<body>(?=[A-Za-z0-9-]*-)(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{7,})\b",
    re.IGNORECASE,
)

_PREFIX_LABELS = {
    "hpc-job": "hpc-job",
    "job": "job",
    "notebook": "notebook",
    "nb": "notebook",
    "ray": "ray-job",
    "rj": "ray-job",
    "sv": "serving",
    "serving": "serving",
    "image": "image",
    "img": "image",
    "ws": "workspace",
    "lcg": "compute-group",
    "project": "project",
    "user": "user",
    "ssh": "ssh-key",
    "quota": "quota",
    "spec": "spec",
    "model": "model",
    "mirror": "image",
}


def _replace_prefixed_id(match: Match[str]) -> str:
    prefix = match.group("prefix").lower()
    label = _PREFIX_LABELS.get(prefix, "raw")
    return f"<{label}-id>"


def scrub_raw_ids(value: object) -> str:
    """Replace platform-looking handles in human-visible strings.

    This helper intentionally targets UUID-shaped platform handles and common
    prefixed handles while leaving ordinary names alone.
    """

    text = "" if value is None else str(value)
    text = _PREFIXED_ID_RE.sub(_replace_prefixed_id, text)
    text = _PREFIXED_ID_BROAD_RE.sub(_replace_prefixed_id, text)
    return _UUID_RE.sub("<raw-id>", text)


__all__ = ["scrub_raw_ids"]
