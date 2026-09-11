"""TensorBoard public lifecycle vocabulary shared by CLI and SDK."""
from inspire.services.notebook.notebook_output import sanitize_status_text

WAIT_TARGETS = frozenset({"CREATING", "RUNNING", "STOPPED", "FAILED", "ERROR", "DELETED"})
TERMINAL_STATUSES = frozenset({"FAILED", "ERROR", "DELETED"})


def normalize_status(value: object) -> str:
    return sanitize_status_text(value).upper().removeprefix("TB_STATUS_") or "UNKNOWN"
