"""Serving lifecycle vocabulary, distinct from training completion."""

TERMINAL_STATUSES = frozenset({"FAILED", "ERROR", "STOPPED", "DELETED"})
SUCCESS_STATUSES = frozenset({"RUNNING"})


def normalize_status(value: str) -> str:
    return str(value or "").strip().upper() or "UNKNOWN"


def matches_status(value: str, requested: str | None) -> bool:
    return not requested or normalize_status(value) == normalize_status(requested)
