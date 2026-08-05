"""Output helpers for keeping platform handles out of CLI observation surfaces."""

from __future__ import annotations

import codecs
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

_STREAM_TOKEN_PARTIAL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{1,96}$"
)
_STREAM_LABEL_PARTIAL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:id|uuid|handle)\s*[:=#]?\s*[0-9a-f]*$"
)


def _partial_raw_id_suffix_start(text: str) -> int | None:
    """Return the start of a possible identifier split across stream chunks."""
    candidates: list[int] = []
    for pattern in (
        _STREAM_TOKEN_PARTIAL_RE,
        _STREAM_LABEL_PARTIAL_RE,
    ):
        match = pattern.search(text)
        if match is not None:
            candidate = match.group(0)
            if scrub_raw_ids(candidate) != candidate:
                continue
            candidates.append(match.start())
    return min(candidates) if candidates else None


class RawIdStreamScrubber:
    """Redact platform handles while preserving interactive byte streams."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""

    def feed(self, payload: bytes | str) -> bytes:
        """Return the safe portion of one stream chunk."""
        if isinstance(payload, bytes):
            text = self._decoder.decode(payload)
        else:
            text = str(payload)
        if not text:
            return b""

        combined = self._pending + text
        partial_start = _partial_raw_id_suffix_start(combined)
        if partial_start is None:
            self._pending = ""
            return scrub_raw_ids(combined).encode("utf-8")

        self._pending = combined[partial_start:]
        return scrub_raw_ids(combined[:partial_start]).encode("utf-8")

    def flush(self) -> bytes:
        """Flush decoder and any identifier suffix held for the next chunk."""
        tail = self._pending + self._decoder.decode(b"", final=True)
        self._pending = ""
        return scrub_raw_ids(tail).encode("utf-8")


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


__all__ = ["RawIdStreamScrubber", "scrub_raw_ids"]
