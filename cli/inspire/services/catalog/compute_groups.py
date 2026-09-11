"""Compute group capability checks shared by CLI and SDK."""

import json

GROUP_JOB_TYPES_BY_WORKLOAD: dict[str, frozenset[str]] = {
    "notebook": frozenset({"interactive_modeling"}),
    "job": frozenset({"distributed_training"}),
    "hpc": frozenset({"hpc_job"}),
    "ray": frozenset({"ray_job"}),
    "serving": frozenset({"inference_serving_customize", "inference_serving_exclusive"}),
    "tensorboard": frozenset({"tensorboard"}),
}


def _declared_job_types(group: dict) -> list[str]:
    """Read `support_job_type_list`, which arrives JSON-encoded as a string.

    The platform sends `'["interactive_modeling","hpc_job"]'`, not a real
    array, so an isinstance check against list silently reads every group as
    undeclared. Both shapes are accepted here in case that ever changes.
    """
    declared = group.get("support_job_type_list")
    if isinstance(declared, str):
        try:
            declared = json.loads(declared)
        except (TypeError, ValueError):
            return []
    if not isinstance(declared, list):
        return []
    return [str(entry).strip() for entry in declared if str(entry).strip()]


def group_supports_workload(group: dict, workload: str) -> bool:
    """Whether a compute group advertises support for *workload*.

    A group that does not declare `support_job_type_list` is kept: the absence
    is our ignorance, not the platform's refusal, and hiding a usable group is
    the worse failure of the two — it reads as "this workspace cannot run this",
    which no error message would ever correct.
    """
    wanted = GROUP_JOB_TYPES_BY_WORKLOAD.get(workload)
    if not wanted:
        return True
    declared = _declared_job_types(group)
    if not declared:
        return True
    return any(entry in wanted for entry in declared)
