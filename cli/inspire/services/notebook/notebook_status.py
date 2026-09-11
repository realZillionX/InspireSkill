"""Shared notebook status vocabulary and startup failure detail."""


class NotebookFailedError(Exception):
    """Raised when a notebook reaches a terminal failure state."""

    def __init__(self, notebook_id: str, status: str, detail: dict, events: str = ""):
        self.notebook_id = notebook_id
        self.status = status
        self.detail = detail
        self.events = events
        parts = [f"Notebook '{notebook_id}' reached terminal status: {status}"]
        sub = detail.get("sub_status")
        if sub:
            parts.append(f"Sub-status: {sub}")
        super().__init__(". ".join(parts))


TERMINAL_STATUSES = frozenset({"FAILED", "ERROR", "STOPPED", "DELETED"})


def normalize_status(value: str) -> str:
    return value.upper() or "UNKNOWN"
