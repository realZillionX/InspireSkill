"""RAY status vocabulary; STOPPED retains its platform meaning."""

TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "STOPPED", "CANCELLED", "DELETED", "ERROR"})
SUCCESS_STATUSES = frozenset({"SUCCEEDED"})


def normalize_status(value: str) -> str:
    return str(value or "").strip().upper() or "UNKNOWN"


def matches_status(value: str, requested: str | None) -> bool:
    return not requested or normalize_status(value) == normalize_status(requested)
