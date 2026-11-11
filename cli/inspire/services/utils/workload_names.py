"""Creation name rules verified against the console workload forms."""

import re


class WorkloadNameError(ValueError):
    """A workload name violates its console creation contract."""


_RULES = {
    "notebook": (32, r"[a-z][a-z0-9-]*"),
    "ray": (32, r"[a-z][a-z0-9-]*"),
    "serving": (32, r"[a-z][a-z0-9.-]*"),
    "job": (255, r"[a-zA-Z0-9\u4e00-\u9fa5._-]+"),
    "hpc": (255, r"[a-zA-Z0-9\u4e00-\u9fa5._-]+"),
    "tensorboard": (255, r"[a-zA-Z0-9\u4e00-\u9fa5._-]+"),
}


def validate_workload_name(name: str, kind: str) -> None:
    """Validate without normalizing or silently truncating the submitted name."""
    maximum, pattern = _RULES[kind]
    if not name or len(name) > maximum:
        raise WorkloadNameError(
            f"{kind} name must contain 1–{maximum} characters (limit {maximum})."
        )
    if re.fullmatch(pattern, name) is None:
        raise WorkloadNameError(f"{kind} name must match {pattern} (limit {maximum} characters).")
