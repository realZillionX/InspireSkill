"""Shared training-job status vocabulary (other workload types differ)."""

_STATUS = {
    "PENDING": "PENDING",
    "CREATING": "PENDING",
    "QUEUING": "QUEUING",
    "RUNNING": "RUNNING",
    "SUCCEEDED": "SUCCEEDED",
    "FAILED": "FAILED",
    "CANCELLED": "CANCELLED",
    "STOPPED": "CANCELLED",
}
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
RAW_TERMINAL_STATUSES = frozenset(
    {
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "STOPPED",
        "job_succeeded",
        "job_failed",
        "job_cancelled",
        "job_stopped",
    }
)


def normalize_status(value: str) -> str:
    return _STATUS.get(value.upper().removeprefix("JOB_"), "UNKNOWN")


STATUS_ALIAS_MAP = {
    "PENDING": {"PENDING", "job_pending", "job_creating"},
    "RUNNING": {"RUNNING", "job_running"},
    "QUEUING": {"QUEUING", "job_queuing"},
    "SUCCEEDED": {"SUCCEEDED", "job_succeeded"},
    "FAILED": {"FAILED", "job_failed"},
    "CANCELLED": {"CANCELLED", "job_cancelled", "job_stopped"},
}
STATUS_API_ALIAS_MAP = {
    "PENDING": ("job_pending", "job_creating"),
    "RUNNING": ("job_running",),
    "QUEUING": ("job_queuing",),
    "SUCCEEDED": ("job_succeeded",),
    "FAILED": ("job_failed",),
    "CANCELLED": ("job_cancelled", "job_stopped"),
}
JOB_ACTIVE_API_STATUSES = ("job_pending", "job_creating", "job_queuing", "job_running")
JOB_ACTIVE_STATUSES = {
    "PENDING",
    "job_pending",
    "job_creating",
    "QUEUING",
    "job_queuing",
    "RUNNING",
    "job_running",
}
