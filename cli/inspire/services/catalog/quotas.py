"""Quota values and payload construction shared by CLI and SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from inspire.services.utils.identifiers import is_full_uuid



class QuotaParseError(ValueError):
    """Raised when a ``--quota`` argument cannot be parsed."""


class QuotaMatchError(ValueError):
    """Raised on zero or multi-match of a quota triple inside a workspace."""


class QuotaCatalogUnavailable(ValueError):
    """Raised when the quota catalog could not be read at all.

    Deliberately not a :class:`QuotaMatchError`: no match was ruled out here.
    The platform did not answer, so callers must report an API error rather
    than tell the user their ``--quota`` does not exist.
    """


@dataclass(frozen=True)
class QuotaSpec:
    """A parsed ``--quota`` triple: GPU count, CPU count, memory in GiB."""

    gpu_count: int
    cpu_count: int
    memory_gib: int

    def display(self) -> str:
        return f"{self.gpu_count},{self.cpu_count},{self.memory_gib}"


@dataclass(frozen=True)
class ResolvedQuota:
    """A matched quota row keyed to its platform handles.

    ``allowed_priority_levels`` carries the workspace's own statement about
    which task priorities this row may run at, and its three states are all
    different answers: ``()`` is "the platform declared no restriction",
    ``("low",)`` is "low priority only", and ``None`` is "the CLI could not
    read the menu" — never a licence to assume the first.
    """

    quota_id: str
    logic_compute_group_id: str
    compute_group_name: str
    gpu_count: int
    cpu_count: int
    memory_gib: int
    gpu_type: str
    raw_price: dict
    allowed_priority_levels: tuple[str, ...] | None = None


def parse_quota(text: str) -> QuotaSpec:
    if text is None:
        raise QuotaParseError("--quota is required")
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 3:
        raise QuotaParseError(
            f"--quota expects 'gpu,cpu,mem' (all integers; mem in GiB); got {text!r}"
        )
    try:
        gpu = int(parts[0])
        cpu = int(parts[1])
        mem = int(parts[2])
    except ValueError as exc:
        raise QuotaParseError(f"--quota values must be integers; got {text!r}") from exc
    if gpu < 0 or cpu <= 0 or mem <= 0:
        raise QuotaParseError(
            f"--quota requires gpu>=0, cpu>=1, mem>=1; got gpu={gpu} cpu={cpu} mem={mem}"
        )
    return QuotaSpec(gpu_count=gpu, cpu_count=cpu, memory_gib=mem)


def build_resource_spec_price(*, quota: ResolvedQuota) -> dict[str, Any]:
    """Build the ``resource_spec_price`` dict the notebook create call expects."""
    price = quota.raw_price if isinstance(quota.raw_price, dict) else {}
    cpu_info_payload = price.get("cpu_info")
    cpu_info: dict[str, Any] = cpu_info_payload if isinstance(cpu_info_payload, dict) else {}
    gpu_info_payload = price.get("gpu_info")
    gpu_info: dict[str, Any] = gpu_info_payload if isinstance(gpu_info_payload, dict) else {}
    machine_gpu_type = str(gpu_info.get("gpu_type") or price.get("gpu_type") or "").strip()
    if quota.gpu_count > 0 and not machine_gpu_type:
        raise QuotaMatchError(
            "Matched GPU quota is missing machine-readable gpu_info.gpu_type; "
            "cannot build notebook resource_spec_price safely."
        )

    payload = {
        "cpu_type": cpu_info.get("cpu_type", ""),
        "cpu_count": quota.cpu_count,
        "gpu_type": machine_gpu_type,
        "gpu_count": quota.gpu_count,
        "memory_size_gib": quota.memory_gib,
        "logic_compute_group_id": quota.logic_compute_group_id,
        "quota_id": quota.quota_id,
    }
    if quota.gpu_count <= 0:
        payload.pop("gpu_type", None)
    return payload


def validate_compute_group_name(value: str) -> str:
    """Reject platform handles while preserving a user-facing group name."""
    name = str(value or "").strip()
    if not name:
        raise QuotaMatchError("--group value cannot be empty")
    if name.casefold().startswith("lcg-") or is_full_uuid(name):
        raise QuotaMatchError("--group takes a compute group name.")
    return name
